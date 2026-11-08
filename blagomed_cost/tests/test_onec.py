#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты модуля 1С (этап 5): интерфейс, сопоставление, COM-подпроцесс, OData.

Только синтетические данные и моки (без рабочей 1С и локальных файлов), чтобы
чистый GitHub-клон проходил проверки. Живые интеграционные проверки — вручную
через tools/onec_diag.py.

Покрытие (ТЗ этапа 5, раздел 10):
  1. чтение цены через интерфейс источника;
  2. один сеанс COM на весь пакет товаров;
  3. таймаут подключения;
  4. таймаут чтения;
  5. ошибка авторизации;
  6. отсутствующая цена (NO_PRICE);
  7. неизвестный товар (NOT_FOUND);
  8. неоднозначное сопоставление (AMBIGUOUS);
  9. совпадение по нормализованному наименованию;
 10. неверный артикул при верном GUID — первичен GUID;
 11. сохранение копеек и точного Decimal;
 12. отсутствие записи в 1С (только чтение);
 13. закрытие источника при ошибке;
 14. ручная цена (manual_price=1) не переопределяется данными 1С;
 15. некорректный OData-ответ не превращается в цену 0.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.calc import calc_product
from app.core.db import init_db
from app.core.onec import (
    ComSource, ItemStatus, OdataSource, OnecError, OnecItem, OnecRequest,
    Params, fetch_from_source, normalize_name, resolve_candidates,
)

BASE = Path(__file__).resolve().parents[1]


def _params(**kw):
    base = dict(server='srv', base='ut', user='u', password='p',
                price_type='Закупочная', supplier='ГАВРЮШКИН')
    base.update(kw)
    return Params(**base)


def _cand(**kw):
    from app.core.onec import _Cand
    d = dict(sku=None, name=None, guid=None, price=None, price_date=None,
             price_type=None, supplier=None, deletion_mark=False)
    d.update(kw)
    return _Cand(**d)


class _FakeSource:
    """Минимальный источник-мок для проверки интерфейса и статусов."""

    def __init__(self, rows, *, error: str | None = None, closed=None):
        self.rows = rows
        self.error = error
        self.closed = closed if closed is not None else []

    def fetch(self, requests):
        if self.error == 'auth':
            raise OnecError('ODATA: HTTP 401 — ошибка авторизации')
        if self.error == 'connect':
            raise OnecError('сеть недоступна')
        if self.error == 'timeout':
            raise OnecError('COM: превышен таймаут, процесс убит')
        # Отдаём ВСЕ кандидаты от источника; выбор критерия — за resolver'ом
        # (по правилам этапа 5), как это и происходит с реальным COM/OData.
        return [resolve_candidates(r, list(self.rows), 'fake')
                for r in requests]

    def close(self):
        self.closed.append(True)


# ── 1. Чтение цены через интерфейс источника ───────────────────────────────

def test_1_read_price_through_interface():
    src = _FakeSource([_cand(sku='232899', name='Подушка Memory',
                             guid='AAAA', price=Decimal('290.50'),
                             price_date='2026-07-01')])
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].status == ItemStatus.OK
    assert items[0].price == Decimal('290.50')
    assert items[0].guid_1c == 'AAAA'
    assert items[0].price_date == '2026-07-01'
    assert src.closed == [True]


# ── 2. Один COM-сеанс на пакет (Popen создаётся один раз) ──────────────────

def test_2_com_one_process_for_batch(monkeypatch):
    import subprocess
    calls = []

    class _P:
        def __init__(self, *a, **kw):
            calls.append(kw)
            self.returncode = 0

        def communicate(self, data, timeout=0):
            payload = (
                '{"candidates": ['
                '{"sku":"1","name":"A","guid":"g1","price":"100.5",'
                '"price_date":"2026-07-01","price_type":"Закупочная",'
                '"supplier":"G"},'
                '{"sku":"2","name":"B","guid":"g2","price":"200.25",'
                '"price_date":"2026-07-02","price_type":"Закупочная",'
                '"supplier":"G"}],'
                '"notes":{}, "source":"com"}')
            return payload, ''

        def poll(self):
            return 0

        def wait(self, timeout=0):
            return 0

    monkeypatch.setattr(subprocess, 'Popen', _P)
    src = ComSource(_params(timeout_connect=1, timeout_read=1))
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='1'), OnecRequest(sku_1c='2')])
    assert len(calls) == 1                       # один процесс на два товара
    assert [i.price for i in items] == [Decimal('100.5'), Decimal('200.25')]
    assert items[0].source == 'com'


# ── 3. Таймаут подключения: процесс убивается родителем ───────────────────

def test_3_com_timeout_kills_process(monkeypatch):
    import subprocess

    class _P:
        def __init__(self, *a, **kw):
            self.killed = False
            self.returncode = None

        def communicate(self, data, timeout=0):
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)

        def kill(self):
            self.killed = True

        def poll(self):
            return 1 if self.killed else None

        def wait(self, timeout=0):
            return None

    monkeypatch.setattr(subprocess, 'Popen', _P)
    src = ComSource(_params(timeout_connect=0.001, timeout_read=0.001))
    items = fetch_from_source(src, [OnecRequest(sku_1c='1')])
    assert items[0].status == ItemStatus.TIMEOUT
    assert 'убит' in items[0].detail


# ── 4. Таймаут чтения (OData: сеть не отвечает в срок) ─────────────────────

def test_4_odata_read_timeout():
    class _Opener:
        def open(self, req, timeout=0):
            raise TimeoutError('медленная сеть')

        def close(self):
            pass

    src = OdataSource(_params(timeout_read=0.001), opener=_Opener())
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].status == ItemStatus.TIMEOUT
    assert 'медленная сеть' in items[0].detail


# ── 5. Ошибка авторизации отделена от отсутствия цены ─────────────────────

def test_5_auth_error_is_not_no_price():
    src = _FakeSource([], error='auth')
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].status == ItemStatus.AUTH_ERROR
    assert items[0].price is None
    assert src.closed == [True]


# ── 6. Отсутствующая цена → NO_PRICE ───────────────────────────────────────

def test_6_no_price_status():
    src = _FakeSource([_cand(sku='232899', name='Подушка', guid='g1',
                             price=None)])
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].status == ItemStatus.NO_PRICE
    assert items[0].price is None
    assert items[0].guid_1c == 'g1'


# ── 7. Неизвестный товар → NOT_FOUND ───────────────────────────────────────

def test_7_not_found_status():
    src = _FakeSource([])
    items = fetch_from_source(src, [OnecRequest(sku_1c='999999')])
    assert items[0].status == ItemStatus.NOT_FOUND


# ── 8. Неоднозначное сопоставление → AMBIGUOUS ─────────────────────────────

def test_8_ambiguous_duplicate_guids():
    src = _FakeSource([
        _cand(sku='A', name='Подушка круглая', guid='g1', price=Decimal(10)),
        _cand(sku='A', name='Подушка круглая', guid='g2', price=Decimal(11)),
    ])
    items = fetch_from_source(src, [OnecRequest(sku_1c='A')])
    assert items[0].status == ItemStatus.AMBIGUOUS


# ── 9. Совпадение по нормализованному наименованию ────────────────────────

def test_9_match_by_normalized_name():
    src = _FakeSource([
        _cand(sku='232899', name='  Подушка-кольцо  БЛАГО-ППК01,',
              guid='g1', price=Decimal(290)),
    ])
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='неизвестный',
                          name_1c='подушка-кольцо благо-ппк01,')])
    assert items[0].status == ItemStatus.OK
    assert items[0].guid_1c == 'g1'
    assert 'name' in items[0].detail


def test_9b_normalize_yo():
    assert normalize_name('Ёлка  Сосна') == 'елка сосна'


# ── 10. Неверный артикул при верном GUID — первичен GUID ──────────────────

def test_10_guid_wins_over_sku():
    src = _FakeSource([
        _cand(sku='232899', name='Подушка Memory', guid='RIGHT-GUID',
              price=Decimal(290)),
    ])
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='ОШИБОЧНЫЙ', guid_1c='right-guid')])
    assert items[0].status == ItemStatus.OK
    assert items[0].guid_1c == 'RIGHT-GUID'
    assert 'guid' in items[0].detail


def test_10b_provided_guid_not_found_never_falls_back_to_sku():
    src = _FakeSource([
        _cand(sku='232899', name='Подушка Memory', guid='OTHER',
              price=Decimal(290)),
    ])
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232899', guid_1c='не-тот-guid')])
    assert items[0].status == ItemStatus.NOT_FOUND
    assert items[0].guid_1c == 'не-тот-guid'
    assert 'GUID' in items[0].detail


# ── 11. Сохранение копеек и точного Decimal ────────────────────────────────

def test_11_precise_decimal():
    src = _FakeSource([
        _cand(sku='232956', name='Подушка круглая', guid='g',
              price=Decimal('289.5654'), price_date='2026-07-15'),
    ])
    items = fetch_from_source(src, [OnecRequest(sku_1c='232956')])
    assert items[0].price == Decimal('289.5654')
    assert items[0].price.as_tuple().exponent == -4


# ── 12. Отсутствие записи в 1С/БД (только чтение) ─────────────────────────

def test_12_no_write_api_in_source_code():
    """В исходниках модуля 1С нет ни операций записи 1С, ни SQL-запросов."""
    for fname in ('onec.py', '_com_worker.py'):
        code = (BASE / 'app' / 'core' / fname).read_text(encoding='utf-8')
        for bad in ('Записать', 'Провести', 'УстановитьПометкуУдаления',
                    'INSERT INTO', 'UPDATE products', 'DELETE FROM',
                    'CREATE TABLE'):
            assert bad not in code, f'{fname}: недопустимая строка {bad!r}'
    # в модуле onec.py нет даже скрытых SQL-инструкций (только в worker —
    # запросы ЧТЕНИЯ 1С: ВЫБРАТЬ/СрезПоследних)
    code = (BASE / 'app' / 'core' / 'onec.py').read_text(encoding='utf-8')
    assert 'ВЫБРАТЬ' not in code


# ── 13. Закрытие источника при ошибке ─────────────────────────────────────

def test_13_close_called_on_error():
    src = _FakeSource([], error='connect')
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].status == ItemStatus.CONNECT_ERROR
    assert src.closed == [True]


# ── 14. Ручная цена (manual_price=1) не переопределяется данными 1С ───────

def test_14_manual_price_not_overridden_by_1c(tmp_path):
    con = init_db(tmp_path / 'cost.db')
    try:
        pid = con.execute(
            'INSERT INTO products (guid_1c, sku_1c, manual_price, active) '
            'VALUES (?,?,?,?)',
            ('g-manual', '232971', 1, 1)).lastrowid
        for k, v in (('ppu_rub_per_kg', 405), ('margin_pct', 15)):
            con.execute('INSERT INTO settings (key, value, valid_from) '
                        'VALUES (?,?,?)', (k, v, '2026-07-01'))
        con.execute('INSERT INTO manual_prices (product_id, price, valid_from) '
                    'VALUES (?,?,?)', (pid, float(Decimal('332')), '2026-07-01'))

        # «живая» цена из 1С пришла бы сюда от источника — для manual_price=1
        # ядро обязано взять ручную, а не данные источника.
        r = calc_product(con, product_id=pid,
                         price=Decimal('1000'), calc_date='2026-07-01')
        assert r.price == Decimal('332')
        assert r.price_source == 'manual'
    finally:
        con.close()


# ── 15. Некорректный OData-ответ не превращается в цену 0 ─────────────────

def test_15_odata_bad_response_is_not_price_zero():
    class _BadOpener:
        def open(self, req, timeout=0):
            return _Resp()

        def close(self):
            pass

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            body = ('{"value": [{"Ref_Key": "g", "Description": "x", '
                    '"Артикул": "", "Цена": null}]}')
            return body.encode('utf-8')   # цена не в каталоге

    # OData-адаптер каталога: даже если ответ не подходит по схеме цен,
    # цена должна остаться None/неявной, но НЕ 0.
    src = OdataSource(_params(), opener=_BadOpener())
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].price is None
    # каталог найден → статус NOT_AVAILABLE (цены нет в OData), а не «0 руб.»
    assert items[0].status in (ItemStatus.NOT_AVAILABLE,
                               ItemStatus.NOT_FOUND,
                               ItemStatus.AMBIGUOUS)