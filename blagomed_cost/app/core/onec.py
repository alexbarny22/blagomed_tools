#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подключение к 1С УТ 11.5 (этап 5): закупочные цены и сопоставление товаров.

ТОЛЬКО ЧТЕНИЕ. Модуль не содержит и не вызывает ни одного метода записи:
ни в 1С (COM/OData — только GET), ни в SQLite (БД не открывается).

Источники данных:
- COM (V83.COMConnector) — основной: справочник «Номенклатура» + цены регистра
  «Цены номенклатуры поставщиков» (вид цены и поставщик из config.ini, как в
  старом боте) и добор из «Цены номенклатуры». Один сеанс на весь пакет.
  Таймауты реальные: COM-работа выполняется в контролируемом подпроцессе
  app/core/_com_worker.py; при превышении лимитов родитель убивает процесс.
- OData (http://<сервер>:4001/<база>/odata/standard.odata/) — только справочник
  Catalog_Номенклатура (GET). Стандартная публикация УТ 11.5 НЕ отдаёт
  содержимое информационных регистров (цен): ответы приходят как RecordSet,
  $expand=RecordSet — HTTP 501. Поэтому цены через OData помечаются N/A,
  фиктивные значения не выдаются.

Сопоставление товара (приоритет, см. docs/CONTEXT.md):
  1. GUID 1С (Ref_Key);
  2. нормализованное наименование (регистр/пробелы/ё→е);
  3. артикул — только подсказка, не доказательство идентичности.
Нечёткое сопоставление для автоматического выбора НЕ используется. Совпадение
только по артикулу НЕ считается доказанной идентификацией: результату
присваивается статус NEEDS_CONFIRM (кандидат для подтверждения владельцем),
а цена одного товара никогда не подставляется вместо другого. Цена принимается
как проверенная только при доказанной идентификации (GUID или единственное
наименование) И точном источнике (нужный вид цены и поставщик из config.ini).

Статусы результата: OK / NO_PRICE / NOT_FOUND / AMBIGUOUS / NEEDS_CONFIRM /
AUTH_ERROR / CONNECT_ERROR / TIMEOUT / NOT_AVAILABLE / ERROR. Ошибка
подключения отделяется от отсутствия цены: CONNECT_ERROR ≠ NOT_FOUND.
Некорректный ответ источника приравнивается к ошибке, а не к нулю.
Для каждого результата сохраняются фактические: регистр, вид цены, поставщик,
дата цены, цена и статус достоверности источника (source_status). GUID,
полученный из OData, привязывается к цене COM только после проверки, что оба
источника указывают на один объект (merge_odata_guid); РАЗНЫЕ GUID даже при
совпавших наименованиях — конфликт идентичности, цена не допускается к расчёту.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Iterable

__all__ = [
    'COM_WORKER', 'ItemStatus', 'OnecError', 'OnecItem', 'OnecRequest',
    'OnecSource', 'ComSource', 'OdataSource', 'fetch_from_source',
    'normalize_name', 'resolve_candidates', 'merge_odata_guid', 'Params',
]

# Путь к воркеру-подпроцессу COM (запускается тем же интерпретатором).
COM_WORKER = Path(__file__).resolve().with_name('_com_worker.py')

DEFAULT_PRICE_TYPE = 'Закупочная'
DEFAULT_SUPPLIER = 'ГАВРЮШКИН'

# Таймауты по умолчанию, сек (реально контролируются подпроцессом).
DEFAULT_TIMEOUT_CONNECT = 300.0   # подключение COM в старом боте ~3,5 минуты
DEFAULT_TIMEOUT_READ = 120.0


class ItemStatus(str, Enum):
    """Статус строки результата (правила этапа 5)."""
    OK = 'OK'                        # товар сопоставлен, цена есть
    NO_PRICE = 'NO_PRICE'            # товар найден, цены нет
    NOT_FOUND = 'NOT_FOUND'          # товар не найден
    AMBIGUOUS = 'AMBIGUOUS'          # несколько подходящих объектов/цен
    NEEDS_CONFIRM = 'NEEDS_CONFIRM'  # кандидат/цена требуют подтверждения
    AUTH_ERROR = 'AUTH_ERROR'        # неверные учётные данные
    CONNECT_ERROR = 'CONNECT_ERROR'  # сеть/подключение/сервер
    TIMEOUT = 'TIMEOUT'              # превышен таймаут подключения/чтения
    NOT_AVAILABLE = 'NOT_AVAILABLE'  # источник не даёт данных (OData-цены)
    ERROR = 'ERROR'                  # прочая ошибка источника


class OnecError(Exception):
    """Ошибка уровня источника 1С (подключение/чтение/ответ)."""


@dataclass
class OnecRequest:
    """Запрос сопоставления: известные вызывающему данные о товаре.

    guid_1c — GUID 1С (Ref_Key), основной идентификатор;
    name_1c — наименование (резервный после GUID);
    sku_1c  — артикул (подсказка, не доказательство).
    """
    sku_1c: str | None = None
    name_1c: str | None = None
    guid_1c: str | None = None


@dataclass
class OnecItem:
    """Строка результата сопоставления.

    price — Decimal без преждевременного округления (полная точность 1С);
            None, если цены нет или товар не сопоставлен.
    price_date — дата действия цены (ISO-строка).
    price_type / supplier — вид цены / поставщик (контроль источников);
    register — фактический регистр 1С, из которого взята цена;
    source_status — достоверность источника ('exact' — точный вид+поставщик /
        'vid_only' — вид точный, поставщик не сверен / 'any' — без условий /
        'fallback_register' — добор из «Цены номенклатуры»).
    """
    sku_1c: str | None = None
    name_1c: str | None = None
    guid_1c: str | None = None
    price: Decimal | None = None
    price_date: str | None = None
    price_type: str | None = None
    supplier: str | None = None
    register: str | None = None
    source_status: str | None = None
    source: str = 'unknown'
    status: ItemStatus = ItemStatus.NOT_FOUND
    detail: str = ''
# ── Нормализация наименований ──────────────────────────────────────────────
_AB = str.maketrans({'Ё': 'Е', 'ё': 'е'})


def normalize_name(s: str | None) -> str:
    """Нормализация наименования: регистр, лишние пробелы, ё→е."""
    if not s:
        return ''
    return ' '.join(str(s).translate(_AB).split()).lower()


# ── Кандидат из 1С (строка источника) ──────────────────────────────────────


@dataclass
class _Cand:
    sku: str | None
    name: str | None
    guid: str | None
    price: Decimal | None
    price_date: str | None
    price_type: str | None
    supplier: str | None
    register: str | None = None
    source_status: str | None = None
    deletion_mark: bool = False


def _guid_eq(a: str | None, b: str | None) -> bool:
    """Сравнение GUID без учёта регистра/дефисов/скобок."""
    if not a or not b:
        return False
    norm = lambda s: ''.join(ch for ch in s.lower() if ch.isalnum())
    return norm(a) == norm(b)


def _significant(c: _Cand) -> tuple:
    """Существенные данные кандидата для определения неоднозначности:
    цена/дата/вид/поставщик могут различать реально разные объекты."""
    return (str(c.price), c.price_date, c.price_type, c.supplier)


def _by_guid(guid: str, cands: list[_Cand]) -> list[_Cand]:
    g = str(guid).strip()
    return [c for c in cands if _guid_eq(c.guid, g)]


def _by_name(name: str, cands: list[_Cand]) -> list[_Cand]:
    n = normalize_name(name)
    return [c for c in cands if c.name and normalize_name(c.name) == n]


def _by_sku(sku: str, cands: list[_Cand]) -> list[_Cand]:
    s = str(sku).strip()
    return [c for c in cands if c.sku and str(c.sku).strip() == s]


def _to_item(cand: _Cand, source: str, how: str,
             expected_price_type: str | None = None,
             expected_supplier: str | None = None) -> OnecItem:
    """Собирает результат. Правила (КОНТРОЛЬНАЯ ДОРАБОТКА этапа 5):
    - цена None → NO_PRICE (цена никогда не заменяется нулём);
    - точный источник (source_status exact/None) + доказанная идентификация
      (GUID или уникальное наименование) → OK;
    - совпадение ТОЛЬКО по артикулу → NEEDS_CONFIRM (не готовое сопоставление);
    - цена не из нужного источника (vid_only/any/fallback_register) →
      NEEDS_CONFIRM, чтобы не объявлять её проверенной;
    - фактический поставщик/вид цены, отличающиеся от заданных →
      NEEDS_CONFIRM (цена другого поставщика не принимается автоматически).
    """
    if cand.price is None:
        status = ItemStatus.NO_PRICE
        why = ''
    elif cand.source_status not in (None, 'exact'):
        status = ItemStatus.NEEDS_CONFIRM
        why = f'источник цены не точный ({cand.source_status})'
    elif how == 'sku':
        status = ItemStatus.NEEDS_CONFIRM
        why = 'сопоставление только по артикулу — требует подтверждения'
    elif expected_price_type and cand.price_type and \
            cand.price_type != expected_price_type:
        status = ItemStatus.NEEDS_CONFIRM
        why = f'вид цены {cand.price_type!r} ≠ заданный {expected_price_type!r}'
    elif expected_supplier and cand.supplier and \
            expected_supplier.lower() not in cand.supplier.lower():
        status = ItemStatus.NEEDS_CONFIRM
        why = f'поставщик {cand.supplier!r} ≠ заданный {expected_supplier!r}'
    else:
        status = ItemStatus.OK
        why = ''
    return OnecItem(
        sku_1c=cand.sku, name_1c=cand.name, guid_1c=cand.guid,
        price=cand.price, price_date=cand.price_date,
        price_type=cand.price_type, supplier=cand.supplier,
        register=cand.register, source_status=cand.source_status,
        source=source, status=status,
        detail=f'сопоставлено по {how}' + (f'; {why}' if why else ''))


def _ambiguous_from_guid(req: OnecRequest, got: list[_Cand],
                         source: str) -> OnecItem:
    """Несколько РАЗНЫХ кандидатов по одному GUID → AMBIGUOUS (не первый)."""
    uniq = []
    for c in got:
        if c not in uniq:
            uniq.append(c)
    return OnecItem(
        sku_1c=req.sku_1c, name_1c=req.name_1c, guid_1c=req.guid_1c,
        source=source, status=ItemStatus.AMBIGUOUS,
        detail=f'GUID {req.guid_1c}: {len(uniq)} разных кандидатов'
               f' (цены {", ".join(sorted({str(c.price) for c in uniq}))})')


def resolve_candidates(req: OnecRequest, cands: list[_Cand],
                       source: str,
                       expected_price_type: str | None = None,
                       expected_supplier: str | None = None) -> OnecItem:
    """Выбирает единственного кандидата по приоритету GUID → наименование →
    артикул-подсказка. GUID первичен даже при изменённом артикуле; несколько
    разных кандидатов с одним GUID → AMBIGUOUS; совпадение только по артикулу —
    NEEDS_CONFIRM, а не готовое автоматическое сопоставление.
    """
    if req.guid_1c:
        got = _by_guid(req.guid_1c, cands)
        if len(got) == 1:
            return _to_item(got[0], source, 'guid',
                            expected_price_type, expected_supplier)
        if len(got) > 1:
            # одинаковые по содержанию записи считаем одним кандидатом
            if len({_significant(c) for c in got}) == 1:
                return _to_item(got[0], source, 'guid',
                                expected_price_type, expected_supplier)
            return _ambiguous_from_guid(req, got, source)
        # GUID передан, но не найден: не «добираем» по артикулу — это значит,
        # что локальная привязка товара отличается от 1С.
        return OnecItem(
            sku_1c=req.sku_1c, name_1c=req.name_1c, guid_1c=req.guid_1c,
            source=source, status=ItemStatus.NOT_FOUND,
            detail=f'GUID {req.guid_1c} не найден в 1С')
    if req.name_1c:
        got = _by_name(req.name_1c, cands)
        if len(got) == 1:
            return _to_item(got[0], source, 'name',
                            expected_price_type, expected_supplier)
        if len(got) > 1:
            return OnecItem(
                sku_1c=req.sku_1c, name_1c=req.name_1c, source=source,
                status=ItemStatus.AMBIGUOUS,
                detail=f'наименование даёт {len(got)} совпадений')
    if req.sku_1c:
        got = _by_sku(req.sku_1c, cands)
        if len(got) == 1:
            return _to_item(got[0], source, 'sku',
                            expected_price_type, expected_supplier)
        if len(got) > 1:
            return OnecItem(
                sku_1c=req.sku_1c, name_1c=req.name_1c, source=source,
                status=ItemStatus.AMBIGUOUS,
                detail=f'артикул {req.sku_1c}: {len(got)} совпадений')
    return OnecItem(
        sku_1c=req.sku_1c, name_1c=req.name_1c,
        source=source, status=ItemStatus.NOT_FOUND,
        detail='кандидаты в 1С не найдены')


def merge_odata_guid(com: OnecItem, oda: OnecItem) -> OnecItem:
    """Связывает цену COM с GUID (Ref_Key) из OData.

    Правила (ФИНАЛЬНАЯ ПРАВКА этапа 5):
    1. GUID есть с обеих сторон и совпадают → объединение разрешено (GUID —
       главный идентификатор, расхождение артикулов при этом не мешает).
    2. GUID есть с обеих сторон и РАЗЛИЧАЮТСЯ → конфликт идентичности: объекты
       НЕ считаются одинаковыми, даже если совпадают наименования; статус
       AMBIGUOUS, цена не допускается к автоматическому расчёту (price=None).
    3. GUID только в OData → привязка допустима при однозначном совпадении
       нормализованного наименования И артикула, если артикул имеется с обеих
       сторон (противоречие артикулов → NEEDS_CONFIRM).
    4. Совпадение только по артикулу без надёжного подтверждения личности →
       NEEDS_CONFIRM.
    5. Отсутствие подтверждения НИКОГДА не даёт OK: цена не допускается
       (price=None), GUID из OData не привязывается.
    """
    from copy import deepcopy
    out = deepcopy(com)
    # нет цены или OData не нашёл карточку — связывать нечего
    if com.price is None or oda.status not in (ItemStatus.OK,
                                               ItemStatus.NOT_AVAILABLE):
        return out
    if not oda.guid_1c:
        return out
    guid_both = bool(com.guid_1c)
    guid_eq = _guid_eq(com.guid_1c, oda.guid_1c)
    name_eq = bool(com.name_1c and oda.name_1c and
                   normalize_name(com.name_1c) == normalize_name(oda.name_1c))
    sku_both = bool(com.sku_1c and oda.sku_1c)
    sku_eq = bool(sku_both and
                  str(com.sku_1c).strip() == str(oda.sku_1c).strip())
    if guid_both and guid_eq:
        # 1. GUID совпадают → объединение разрешено
        out.guid_1c = oda.guid_1c
        return out
    if guid_both:
        # 2. GUID имеются, но РАЗНЫЕ → конфликт идентичности
        out.price = None
        out.status = ItemStatus.AMBIGUOUS
        out.detail = ('GUID COM и OData РАЗНЫЕ — конфликт идентичности; '
                      f'цена {com.price} не допускается к автоматическому '
                      'расчёту')
        return out
    # дальше GUID только в OData (COM без GUID)
    if name_eq and (not sku_both or sku_eq):
        # 3. наименование совпало, артикулы (если есть) не противоречат
        out.guid_1c = oda.guid_1c
        return out
    if name_eq:
        # противоречие наименования и артикула
        out.price = None
        out.status = ItemStatus.NEEDS_CONFIRM
        out.detail = ('наименования совпадают, но артикулы COM/OData РАЗНЫЕ '
                      f'({com.sku_1c} vs {oda.sku_1c}) — требуется '
                      f'подтверждение; цена {com.price} не допускается к '
                      'расчёту')
        return out
    if sku_eq:
        # 4. только артикул — личность не подтверждена
        out.price = None
        out.status = ItemStatus.NEEDS_CONFIRM
        out.detail = ('совпадение только по артикулу — личность товара не '
                      f'подтверждена; цена {com.price} не допускается к '
                      'расчёту')
        return out
    # 5. подтверждения идентичности нет вовсе
    out.price = None
    out.status = ItemStatus.NOT_FOUND
    out.detail = ('цена COM и карточка OData указывают на РАЗНЫЕ объекты '
                  'номенклатуры — цена не связана')
    return out


# ── Параметры подключения (config.ini), секреты не выводятся ───────────────


@dataclass
class Params:
    """Параметры источника 1С. Все значения — из локального config.ini."""
    server: str = ''
    base: str = ''
    user: str = ''
    password: str = ''
    price_type: str = DEFAULT_PRICE_TYPE
    supplier: str = DEFAULT_SUPPLIER
    url_odata: str = ''
    timeout_connect: float = DEFAULT_TIMEOUT_CONNECT
    timeout_read: float = DEFAULT_TIMEOUT_READ
    as_of_dates: tuple[str, ...] = ()   # исторические срезы цен (версии дат)
    history_articles: tuple[str, ...] = ()   # артикулы для прямой истории цен

    @classmethod
    def from_file(cls, path) -> 'Params':
        """Читает config.ini (секции [1C], [Цены], [Расчёт])."""
        import configparser
        cfg = configparser.ConfigParser()
        if not cfg.read(path, encoding='utf-8'):
            raise OnecError(f'config.ini не найден или пуст: {path}')
        p = cls()
        if cfg.has_section('1C'):
            g = cfg['1C']
            p.server = g.get('server', '').strip()
            p.base = g.get('base', '').strip()
            p.user = g.get('user', '').strip()
            p.password = g.get('password', '')
            p.url_odata = g.get('url_odata', '').strip()
            mode = g.get('mode', 'com').strip().lower()
            p._mode = mode
        if cfg.has_section('Цены'):
            g = cfg['Цены']
            p.price_type = g.get('вид_цены', p.price_type).strip()
            p.supplier = g.get('поставщик', p.supplier).strip()
        if not p.server or not p.base:
            raise OnecError('config.ini: не заполнены server/base (секция [1C])')
        return p


# ── Протокол источника ─────────────────────────────────────────────────────


class OnecSource:
    """Интерфейс источника данных 1С (COM или OData). Только чтение."""

    def fetch(self, requests: Iterable[OnecRequest]) -> list[OnecItem]:
        raise NotImplementedError

    def close(self) -> None:
        """Освобождает ресурсы (COM-соединение и т.п.). Идемпотентно."""


def fetch_from_source(source: OnecSource, requests: Iterable[OnecRequest],
                      ) -> list[OnecItem]:
    """Единая точка запуска: форсирует one-shot смерть источника после fetch
    и нормализует исключения в статусы (ошибка ≠ отсутствие цены)."""
    reqs = list(requests)
    try:
        items = source.fetch(reqs)
    except OnecError as e:
        status = _status_from_error(e)
        items = [OnecItem(sku_1c=r.sku_1c, name_1c=r.name_1c,
                          guid_1c=r.guid_1c, source='unknown',
                          status=status, detail=str(e))
                 for r in reqs]
    finally:
        source.close()
    if len(items) != len(reqs):
        raise OnecError(
            f'источник вернул {len(items)} строк вместо {len(reqs)}')
    return items


def _status_from_error(e: OnecError) -> ItemStatus:
    msg = str(e)
    if '401' in msg or '401' in getattr(e, 'code', '') or \
            'auth' in msg.lower() or 'авторизац' in msg.lower():
        return ItemStatus.AUTH_ERROR
    if 'timeout' in msg.lower() or 'таймаут' in msg.lower() \
            or 'убит' in msg.lower():
        return ItemStatus.TIMEOUT
    return ItemStatus.CONNECT_ERROR


# ── COM-адаптер ────────────────────────────────────────────────────────────


class ComSource(OnecSource):
    """Один COM-сеанс на пакет, исполняется в контролируемом подпроцессе.

    Живое подключение выполняет app/core/_com_worker.py (тот же интерпретатор).
    Родитель ждёт результатов не дольше таймаутов и убивает процесс — это
    реально работающее ограничение для блокирующих вызовов COM.
    """

    def __init__(self, params: Params, python=None, worker=None):
        self._params = params
        self._python = python or sys.executable
        self._worker = str(worker or COM_WORKER)
        self._proc: subprocess.Popen | None = None
        self._closed = False
        self._last_data: dict | None = None

    # ---- подпроцесс -----------------------------------------------------
    def _run_worker(self, payload: dict):
        cmd = [self._python, self._worker]
        env = dict(os.environ)
        # Дочерний процесс может быть запущен не из консоли: фиксируем UTF-8,
        # чтобы и stdout (JSON), и stderr читались родителем как utf-8.
        env['PYTHONIOENCODING'] = 'utf-8'
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env)
        try:
            out, err = self._proc.communicate(
                json.dumps(payload, ensure_ascii=False),
                timeout=(self._params.timeout_connect
                         + self._params.timeout_read))
        except subprocess.TimeoutExpired:
            self._kill()
            raise OnecError(
                'COM: превышен суммарный таймаут, процесс убит '
                f'({self._params.timeout_connect}s + '
                f'{self._params.timeout_read}s)')
        if self._proc.returncode != 0:
            tail = (err or '').strip().splitlines()
            msg = tail[-1] if tail else f'код возврата {self._proc.returncode}'
            raise OnecError(f'COM-worker завершился с ошибкой: {msg}')
        try:
            return json.loads(out.strip())
        except Exception as e:
            raise OnecError(f'COM-worker вернул некорректный JSON: {e}')

    def _kill(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc.wait(timeout=10)

    # ---- основной метод ----------------------------------------------------
    def fetch(self, requests: Iterable[OnecRequest]) -> list[OnecItem]:
        reqs = list(requests)
        if not reqs:
            return []
        skus = [r.sku_1c for r in reqs if r.sku_1c]
        guids = [r.guid_1c for r in reqs if r.guid_1c]
        payload = {
            'articles': skus,
            'guids': guids,
            'server': self._params.server,
            'base': self._params.base,
            'user': self._params.user,
            'password': self._params.password,
            'price_type': self._params.price_type,
            'supplier': self._params.supplier,
            'timeout_connect': self._params.timeout_connect,
            'timeout_read': self._params.timeout_read,
            'as_of_dates': list(self._params.as_of_dates or ()),
            'history_articles': list(self._params.history_articles or ()),
        }
        data = self._run_worker(payload)
        self._last_data = data
        cands_by = data.get('candidates', [])
        cands_all: list[_Cand] = []
        for p in cands_by:
            cands_all.append(_Cand(
                sku=p.get('sku'), name=p.get('name'), guid=p.get('guid'),
                price=(Decimal(p['price']) if p.get('price') is not None
                       else None),
                price_date=p.get('price_date'), price_type=p.get('price_type'),
                supplier=p.get('supplier'), register=p.get('register'),
                source_status=p.get('source_status'),
                deletion_mark=bool(p.get('deletion_mark'))))
        notes = data.get('notes', {})
        items = []
        for r in reqs:
            # Полный список кандидатов даёт GUID-приоритет даже при изменённом
            # артикуле: resolver сам отберёт по GUID → наименованию → артикулу.
            item = resolve_candidates(r, cands_all, 'com',
                                      self._params.price_type,
                                      self._params.supplier)
            if item.status == ItemStatus.OK and not item.price_type:
                item.price_type = self._params.price_type
            if r.sku_1c and notes.get(r.sku_1c):
                item.detail = f'{item.detail}; {notes[r.sku_1c]}'
            items.append(item)
        return items

    def close(self) -> None:
        if self._closed:
            return
        self._kill()
        self._closed = True


# ── OData-адаптер (только справочник; цены НЕ публикуются) ─────────────────


class OdataSource(OnecSource):
    """Чтение Catalog_Номенклатура через стандартный OData (только GET).

    Цены через OData НЕ запрашиваются: в стандартной публикации УТ 11.5
    информационные регистры отдают лишь заглушку RecordSet ($expand=RecordSet
    — HTTP 501). Для всех запросов с ожиданием цены выставляется
    статус NOT_AVAILABLE — без выдуманных значений.
    """

    BASE = 'http://{server}:4001/{base}/odata/standard.odata/'

    def __init__(self, params: Params, opener=None):
        self._params = params
        self._base = (params.url_odata or self.BASE.format(
            server=params.server, base=params.base)).rstrip('/')
        self._opener = opener or urllib.request.build_opener()
        self._auth = base64.b64encode(
            f'{params.user}:{params.password}'.encode('utf-8')).decode()
        self._closed = False

    def _get(self, path: str, query: str):
        url = f'{self._base}/{urllib.parse.quote(path, safe="")}'
        if query:
            url += '?' + query
        req = urllib.request.Request(url, method='GET')
        req.add_header('Authorization', f'Basic {self._auth}')
        req.add_header('Accept', 'application/json')
        try:
            with self._opener.open(req, timeout=self._params.timeout_read) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise OnecError('ODATA: HTTP 401 — ошибка авторизации')
            if e.code in (404, 501):
                raise OnecError(
                    f'ODATA: HTTP {e.code} — объект не публикуется')
            raise OnecError(f'ODATA: HTTP {e.code}')
        except urllib.error.ContentTooShortError:
            raise OnecError('ODATA: ответ обрезан (ContentTooShort)')
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OnecError(f'ODATA: {type(e).__name__}: {e}')

    def fetch(self, requests: Iterable[OnecRequest]) -> list[OnecItem]:
        reqs = list(requests)
        out: list[OnecItem] = []
        for r in reqs:
            item = self._fetch_one(r)
            if item.status == ItemStatus.OK:
                # Цена через OData недоступна в этой публикации — честный N/A.
                item.status = ItemStatus.NOT_AVAILABLE
                item.detail = (item.detail + '; цена в OData не публикуется').strip(';')
            out.append(item)
        return out

    def _fetch_one(self, r: OnecRequest) -> OnecItem:
        if r.guid_1c:
            return self._by_guid(r)
        if r.sku_1c:
            item = self._by_sku(r)
            if item.status != ItemStatus.NOT_FOUND:
                return item
        if r.name_1c:
            return self._by_name(r)
        return OnecItem(sku_1c=r.sku_1c, name_1c=r.name_1c,
                        source='odata', status=ItemStatus.NOT_FOUND)

    def _by_guid(self, r: OnecRequest) -> OnecItem:
        select = self._quote('Ref_Key,Description,Артикул,DeletionMark')
        filt = self._quote(f"Ref_Key eq guid'{r.guid_1c}'")
        recs = self._rows('Catalog_Номенклатура', select, filt, top=2)
        if len(recs) == 1:
            return self._rec_to_item(r, recs[0])
        if len(recs) > 1:
            return OnecItem(sku_1c=r.sku_1c, guid_1c=r.guid_1c, source='odata',
                            status=ItemStatus.AMBIGUOUS)
        return OnecItem(sku_1c=r.sku_1c, guid_1c=r.guid_1c, source='odata',
                        status=ItemStatus.NOT_FOUND)

    def _by_sku(self, r: OnecRequest) -> OnecItem:
        select = self._quote('Ref_Key,Description,Артикул,DeletionMark')
        filt = self._quote(f"Артикул eq '{r.sku_1c}'")
        recs = self._rows('Catalog_Номенклатура', select, filt, top=10)
        if len(recs) == 1:
            return self._rec_to_item(r, recs[0])
        if len(recs) > 1:
            return OnecItem(sku_1c=r.sku_1c, source='odata',
                            status=ItemStatus.AMBIGUOUS,
                            detail=f'{len(recs)} карточек по артикулу')
        return OnecItem(sku_1c=r.sku_1c, source='odata',
                        status=ItemStatus.NOT_FOUND)

    def _by_name(self, r: OnecRequest) -> OnecItem:
        select = self._quote('Ref_Key,Description,Артикул,DeletionMark')
        filt = self._quote("Description eq '" + (r.name_1c or '') + "'")
        recs = self._rows('Catalog_Номенклатура', select, filt, top=10)
        if len(recs) == 1:
            return self._rec_to_item(r, recs[0])
        if len(recs) > 1:
            return OnecItem(sku_1c=r.sku_1c, name_1c=r.name_1c, source='odata',
                            status=ItemStatus.AMBIGUOUS,
                            detail=f'наименование: {len(recs)} совпадений')
        return OnecItem(sku_1c=r.sku_1c, name_1c=r.name_1c, source='odata',
                        status=ItemStatus.NOT_FOUND)

    def _rows(self, path: str, select: str, filt: str, top: int) -> list[dict]:
        data = self._get(path, f'$top={top}&$select={select}&$filter={filt}')
        if isinstance(data, list):
            return data[:top]
        return (data or {}).get('value', [])[:top]

    @staticmethod
    def _quote(s: str) -> str:
        return urllib.parse.quote(s, safe='')

    @staticmethod
    def _rec_to_item(r: OnecRequest, rec: dict) -> OnecItem:
        return OnecItem(
            sku_1c=(rec.get('Артикул') or r.sku_1c),
            name_1c=(rec.get('Description') or r.name_1c),
            guid_1c=rec.get('Ref_Key'), source='odata', status=ItemStatus.OK,
            detail='найдено в справочнике 1С (цена N/A)',)

    def close(self) -> None:
        if not self._closed:
            try:
                self._opener.close()
            except Exception:
                pass
            self._closed = True