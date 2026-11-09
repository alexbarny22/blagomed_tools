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
    # Идентификация по GUID (правила контрольной доработки: совпадение только
    # по артикулу → NEEDS_CONFIRM, поэтому проверяем OK по GUID).
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232899', guid_1c='AAAA')])
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


def test_10b_conflicting_guid_blocks():
    """Сохранённый GUID не соответствует объекту по артикулу → блокировка
    (AMBIGUOUS: конфликт привязки), цена не допускается."""
    src = _FakeSource([
        _cand(sku='232899', name='Подушка Memory', guid='OTHER',
              price=Decimal(290)),
    ])
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232899', guid_1c='не-тот-guid')])
    assert items[0].status == ItemStatus.AMBIGUOUS
    assert items[0].price is None
    assert 'конфликт' in items[0].detail.lower()


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
    """В исходниках модуля 1С нет ни операций записи 1С, ни SQL-запросов,
    ни записывающих HTTP-методов (COM и OData — только чтение)."""
    for fname in ('onec.py', '_com_worker.py'):
        code = (BASE / 'app' / 'core' / fname).read_text(encoding='utf-8')
        for bad in ('Записать', 'Провести', 'УстановитьПометкуУдаления',
                    'INSERT INTO', 'UPDATE products', 'DELETE FROM',
                    'CREATE TABLE'):
            assert bad not in code, f'{fname}: недопустимая строка {bad!r}'
    # OData: только GET; никаких записывающих методов HTTP.
    code = (BASE / 'app' / 'core' / 'onec.py').read_text(encoding='utf-8')
    for bad in ("method='POST'", "method='PUT'", "method='PATCH'",
                "method='DELETE'"):
        assert bad not in code, f'onec.py: недопустимый HTTP-метод {bad!r}'
    assert "method='GET'" in code
    # в модуле onec.py нет даже скрытых SQL-инструкций (только в worker —
    # запросы ЧТЕНИЯ 1С: ВЫБРАТЬ/СрезПоследних)
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
# ── 16+. КОНТРОЛЬНАЯ ДОРАБОТКА: регрессионные тесты ─────────────────────

def test_16_guid_wins_when_sku_changed():
    """Верный GUID при изменённом артикуле: кандидат находится по GUID,
    даже если артикул в 1С уже другой."""
    src = _FakeSource([_cand(sku='232899-НОВЫЙ', name='Подушка Memory',
                             guid='GUID-ПРАВИЛЬНЫЙ', price=Decimal('310.25'))])
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232899-СТАРЫЙ', guid_1c='GUID-ПРАВИЛЬНЫЙ')])
    assert items[0].status == ItemStatus.OK
    assert items[0].price == Decimal('310.25')
    assert items[0].guid_1c == 'GUID-ПРАВИЛЬНЫЙ'


def test_com_guid_wins_when_sku_changed_end_to_end(monkeypatch):
    """Те же правила на полном пути ComSource (JSON из воркера)."""
    import subprocess

    class _P:
        def __init__(self, *a, **kw):
            self.returncode = 0

        def communicate(self, data, timeout=0):
            payload = (
                '{"candidates": [{"sku":"232899-НОВЫЙ",'
                '"name":"Подушка Memory","guid":"GUID-ПРАВИЛЬНЫЙ",'
                '"price":"310.25","price_date":"2026-07-01",'
                '"price_type":"Закупочная","supplier":"ГАВРЮШКИН",'
                '"register":"ЦеныНоменклатурыПоставщиков",'
                '"source_status":"exact"}], "notes":{}, "source":"com"}')
            return payload, ''

        def poll(self):
            return 0

        def wait(self, timeout=0):
            return 0

    monkeypatch.setattr(subprocess, 'Popen', _P)
    src = ComSource(_params(timeout_connect=1, timeout_read=1))
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232899-СТАРЫЙ', guid_1c='GUID-ПРАВИЛЬНЫЙ')])
    assert items[0].status == ItemStatus.OK
    assert items[0].price == Decimal('310.25')


def test_17_wrong_guid_with_matching_sku_is_conflict():
    """Неверный GUID при совпадающем артикуле → блокировка (AMBIGUOUS:
    конфликт привязки); совпадение по артикулу не подменяет конфликт GUID."""
    src = _FakeSource([_cand(sku='232899', name='Подушка Memory',
                             guid='GUID-ВЕРНЫЙ', price=Decimal('290'))])
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232899', guid_1c='GUID-НЕВЕРНЫЙ')])
    assert items[0].status == ItemStatus.AMBIGUOUS
    assert items[0].price is None
    assert 'конфликт' in items[0].detail.lower()


def test_18_same_guid_different_prices_ambiguous():
    """Несколько кандидатов с одним GUID и РАЗНЫМИ ценами → AMBIGUOUS,
    а не «первый попавшийся»."""
    src = _FakeSource([
        _cand(sku='232899', name='Подушка Memory', guid='G-1',
              price=Decimal('290'), price_type='Закупочная',
              supplier='ГАВРЮШКИН', source_status='exact'),
        _cand(sku='232899', name='Подушка Memory', guid='G-1',
              price=Decimal('305'), price_type='Закупочная',
              supplier='ИНОЙ', source_status='exact'),
    ])
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232899', guid_1c='G-1')])
    assert items[0].status == ItemStatus.AMBIGUOUS
    assert '290' in items[0].detail and '305' in items[0].detail


def test_19_unique_sku_auto_ok():
    """Артикул точно совпал с ЕДИНСТВЕННЫМ товаром 1С и цена из утверждённого
    источника → автоматическое сопоставление, статус OK (без ручного
    подтверждения)."""
    src = _FakeSource([_cand(sku='232899', name='Подушка Memory',
                             guid='G-1', price=Decimal('290'),
                             source_status='exact')])
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].status == ItemStatus.OK
    assert items[0].price == Decimal('290')
    assert items[0].guid_1c == 'G-1'
    assert 'sku' in items[0].detail


def test_19b_duplicate_sku_disambiguated_by_name():
    """Дубли артикула различаются по наименованию запроса → однозначный выбор."""
    src = _FakeSource([
        _cand(sku='232899', name='Подушка Memory 41см', guid='G-41',
              price=Decimal('290'), source_status='exact'),
        _cand(sku='232899', name='Подушка Memory 51см', guid='G-51',
              price=Decimal('310'), source_status='exact'),
    ])
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232899', name_1c='подушка memory 51см')])
    assert items[0].status == ItemStatus.OK
    assert items[0].guid_1c == 'G-51'
    assert items[0].price == Decimal('310')


def test_19c_duplicate_sku_ambiguous_characteristics():
    """Дубли артикула без однозначных характеристик → AMBIGUOUS; выбор
    произвольного кандидата запрещён, различие зафиксировано в detail."""
    src = _FakeSource([
        _cand(sku='232899', name='Подушка Memory 41см', guid='G-41',
              price=Decimal('290'), source_status='exact'),
        _cand(sku='232899', name='Подушка Memory 51см', guid='G-51',
              price=Decimal('310'), source_status='exact'),
    ])
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].status == ItemStatus.AMBIGUOUS
    assert items[0].price is None
    assert '41см' in items[0].detail and '51см' in items[0].detail


def test_19d_sku_name_contradiction_needs_confirm():
    """Артикул совпал, но наименование противоречит → NEEDS_CONFIRM (не OK)."""
    src = _FakeSource([
        _cand(sku='232899', name='Подушка Memory', guid='G-1',
              price=Decimal('290'), source_status='exact'),
    ])
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232899', name_1c='Совершенно другой товар')])
    assert items[0].status == ItemStatus.NEEDS_CONFIRM
    assert items[0].price == Decimal('290')
    assert 'характеристик' in items[0].detail
def test_20_sku_only_no_price_is_no_price():
    """Отсутствие цены не заменяется нулём и не становится OK даже при
    совпадении по артикулу: NO_PRICE + price=None."""
    from app.core.onec import _Cand
    cand = _Cand(sku='232899', name='Подушка', guid='G-1', price=None,
                 price_date=None, price_type='Закупочная',
                 supplier='ГАВРЮШКИН', register='ЦеныНоменклатурыПоставщиков',
                 source_status='exact', deletion_mark=False)
    item = resolve_candidates(OnecRequest(sku_1c='232899'), [cand], 'com',
                              expected_price_type='Закупочная', expected_supplier='ГАВРЮШКИН')
    assert item.status == ItemStatus.NO_PRICE
    assert item.price is None


def test_21_com_odata_different_guids_conflict():
    """COM и OData дают РАЗНЫЕ GUID → конфликт идентичности: AMBIGUOUS, цена
    COM не допускается к автоматическому расчёту (price=None), GUID из OData
    не привязывается."""
    from app.core.onec import merge_odata_guid
    com = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо БЛАГО-ППК01',
                   guid_1c='GUID-COM', price=Decimal('290'), source='com',
                   status=ItemStatus.OK, price_date='2026-07-01')
    oda = OnecItem(sku_1c='232905', name_1c='Ремень для фиксации',
                   guid_1c='GUID-ODATA', source='odata',
                   status=ItemStatus.NOT_AVAILABLE,
                   detail='найдено в справочнике 1С (цена N/A)')
    out = merge_odata_guid(com, oda)
    assert out.status == ItemStatus.AMBIGUOUS
    assert out.status != ItemStatus.OK
    assert out.price is None
    assert out.guid_1c == 'GUID-COM'  # GUID из OData не привязывается
    assert 'конфликт' in out.detail.lower()


def test_21b_com_odata_same_object_links_guid():
    """COM и OData указывают на один объект (совпадают наименование) —
    GUID из OData привязывается к цене COM."""
    from app.core.onec import merge_odata_guid
    com = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо БЛАГО-ППК01',
                   price=Decimal('290'), source='com', status=ItemStatus.OK,
                   price_date='2026-07-01')
    oda = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо БЛАГО-ППК01',
                   guid_1c='GUID-ODATA', source='odata',
                   status=ItemStatus.NOT_AVAILABLE)
    out = merge_odata_guid(com, oda)
    assert out.status == ItemStatus.OK
    assert out.price == Decimal('290')
    assert out.guid_1c == 'GUID-ODATA'


def test_22_other_supplier_price_not_accepted():
    """Цена другого поставщика не принимается автоматически даже при верном
    GUID: source_status не точный → NEEDS_CONFIRM, не OK."""
    from app.core.onec import _Cand
    cand = _Cand(sku='232899', name='Подушка Memory', guid='G-1',
                 price=Decimal('280'), price_date='2026-07-01',
                 price_type='Закупочная', supplier='ДРУГОЙ-ПОСТАВЩИК',
                 register='ЦеныНоменклатурыПоставщиков',
                 source_status='vid_only', deletion_mark=False)
    item = resolve_candidates(OnecRequest(sku_1c='232899', guid_1c='G-1'),
                              [cand], 'com',
                              expected_price_type='Закупочная',
                              expected_supplier='ГАВРЮШКИН')
    assert item.status == ItemStatus.NEEDS_CONFIRM
    assert item.price == Decimal('280')
    assert 'источник' in item.detail or 'поставщик' in item.detail


def test_23_missing_requested_price_type_is_no_price():
    """Отсутствие заданного вида закупочной цены: кандидат с ДРУГИМ видом цены
    не принимается как подтверждённый — NO_PRICE/NEEDS_CONFIRM, не OK."""
    from app.core.onec import _Cand
    cand = _Cand(sku='232899', name='Подушка Memory', guid='G-1',
                 price=Decimal('500'), price_date='2026-07-01',
                 price_type='Розничная', supplier='ГАВРЮШКИН',
                 register='ЦеныНоменклатурыПоставщиков',
                 source_status='exact', deletion_mark=False)
    item = resolve_candidates(OnecRequest(sku_1c='232899', guid_1c='G-1'),
                              [cand], 'com',
                              expected_price_type='Закупочная',
                              expected_supplier='ГАВРЮШКИН')
    assert item.status != ItemStatus.OK
    assert item.price is not None  # цена другого вида не теряется, но и не в OK
    assert ('вид цены' in item.detail
            or 'подтвержден' in item.detail.lower())
def test_24_fractional_price_via_com_json(monkeypatch):
    """Точная дробная цена сохраняется на всём пути COM → JSON → Python."""
    import subprocess

    class _P:
        def __init__(self, *a, **kw):
            self.returncode = 0

        def communicate(self, data, timeout=0):
            payload = (
                '{"candidates": [{"sku":"232956",'
                '"name":"Подушка круглая","guid":"G-PPRN01",'
                '"price":"289.5654","price_date":"2026-07-13",'
                '"price_type":"Закупочная","supplier":"ГАВРЮШКИН",'
                '"register":"ЦеныНоменклатурыПоставщиков",'
                '"source_status":"exact"}], "notes":{}, "source":"com"}')
            return payload, ''

        def poll(self):
            return 0

        def wait(self, timeout=0):
            return 0

    monkeypatch.setattr(subprocess, 'Popen', _P)
    src = ComSource(_params())
    items = fetch_from_source(
        src, [OnecRequest(sku_1c='232956', guid_1c='G-PPRN01')])
    assert items[0].status == ItemStatus.OK
    assert items[0].price == Decimal('289.5654')
    assert items[0].price.as_tuple().exponent == -4


def test_25_connect_error_not_not_found(monkeypatch):
    """Ошибки соединения НЕ превращаются в NOT_FOUND: статус CONNECT_ERROR
    с price=None (не отсутствие товара)."""
    import subprocess

    class _P:
        def __init__(self, *a, **kw):
            self.returncode = 1

        def communicate(self, data, timeout=0):
            return '', 'COM-WORKER ERROR: сеть недоступна'

        def poll(self):
            return 1

        def wait(self, timeout=0):
            return 0

    monkeypatch.setattr(subprocess, 'Popen', _P)
    src = ComSource(_params())
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].status == ItemStatus.CONNECT_ERROR
    assert items[0].status != ItemStatus.NOT_FOUND
    assert items[0].price is None


def test_26_odata_error_not_not_found():
    """OData: сетевая ошибка — CONNECT_ERROR, а не NOT_FOUND и не 0."""

    class _Opener:
        def open(self, req, timeout=0):
            import urllib.error
            raise urllib.error.URLError('сеть недоступна')

        def close(self):
            pass

    src = OdataSource(_params(), opener=_Opener())
    items = fetch_from_source(src, [OnecRequest(sku_1c='232899')])
    assert items[0].status == ItemStatus.CONNECT_ERROR
    assert items[0].status != ItemStatus.NOT_FOUND
    assert items[0].price is None


def test_27_different_guids_same_name_is_conflict():
    """Разные GUID при ОДИНАКОВОМ наименовании — объекты НЕ считаются
    одинаковыми: статус AMBIGUOUS, цена не допускается (price=None)."""
    from app.core.onec import merge_odata_guid
    com = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо',
                   guid_1c='GUID-COM', price=Decimal('290'), source='com',
                   status=ItemStatus.OK, price_date='2026-07-01')
    oda = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо',
                   guid_1c='GUID-ODATA', source='odata',
                   status=ItemStatus.NOT_AVAILABLE)
    out = merge_odata_guid(com, oda)
    assert out.status == ItemStatus.AMBIGUOUS
    assert out.status != ItemStatus.OK
    assert out.price is None
    assert 'GUID COM и OData РАЗНЫЕ' in out.detail


def test_28_same_guid_different_sku_ok():
    """Оба GUID совпадают → объединение разрешено даже при разном артикуле
    (GUID первичен), цена и статус COM сохраняются."""
    from app.core.onec import merge_odata_guid
    com = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо',
                   guid_1c='GUID-X', price=Decimal('290'), source='com',
                   status=ItemStatus.OK, price_date='2026-07-01')
    oda = OnecItem(sku_1c='999999', name_1c='Подушка-кольцо',
                   guid_1c='GUID-X', source='odata',
                   status=ItemStatus.NOT_AVAILABLE)
    out = merge_odata_guid(com, oda)
    assert out.status == ItemStatus.OK
    assert out.price == Decimal('290')
    assert out.guid_1c == 'GUID-X'


def test_29_com_without_guid_oda_guid_name_sku_match_links():
    """GUID только в OData, наименование и артикул совпадают → GUID OData
    привязывается, цена COM сохраняется."""
    from app.core.onec import merge_odata_guid
    com = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо',
                   price=Decimal('290'), source='com',
                   status=ItemStatus.OK, price_date='2026-07-01')
    oda = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо',
                   guid_1c='GUID-ODATA', source='odata',
                   status=ItemStatus.NOT_AVAILABLE)
    out = merge_odata_guid(com, oda)
    assert out.status == ItemStatus.OK
    assert out.price == Decimal('290')
    assert out.guid_1c == 'GUID-ODATA'


def test_30_com_without_guid_name_sku_contradiction():
    """GUID только в OData, наименования совпадают, но артикулы РАЗНЫЕ →
    конфликт подтверждений: NEEDS_CONFIRM, цена не допускается (price=None)."""
    from app.core.onec import merge_odata_guid
    com = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо',
                   price=Decimal('290'), source='com',
                   status=ItemStatus.OK, price_date='2026-07-01')
    oda = OnecItem(sku_1c='888888', name_1c='Подушка-кольцо',
                   guid_1c='GUID-ODATA', source='odata',
                   status=ItemStatus.NOT_AVAILABLE)
    out = merge_odata_guid(com, oda)
    assert out.status == ItemStatus.NEEDS_CONFIRM
    assert out.status != ItemStatus.OK
    assert out.price is None
    assert 'артикулы COM/OData РАЗНЫЕ' in out.detail


def test_31_sku_only_match_price_not_auto_used():
    """Совпадение только по артикулу без подтверждения личности → NEEDS_CONFIRM,
    цена не допускается к автоматическому расчёту (price=None, не OK)."""
    from app.core.onec import merge_odata_guid
    com = OnecItem(sku_1c='232899', name_1c=None,
                   price=Decimal('290'), source='com',
                   status=ItemStatus.OK, price_date='2026-07-01')
    oda = OnecItem(sku_1c='232899', name_1c='Подушка-кольцо',
                   guid_1c='GUID-ODATA', source='odata',
                   status=ItemStatus.NOT_AVAILABLE)
    out = merge_odata_guid(com, oda)
    assert out.status == ItemStatus.NEEDS_CONFIRM
    assert out.status != ItemStatus.OK
    assert out.price is None
    assert 'только по артикулу' in out.detail