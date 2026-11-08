# -*- coding: utf-8 -*-
"""Тесты экспорта Excel и запуска расчёта (этап 6).

Сценарии: использование calc_product(), отсутствие повторного расчёта,
приоритет ручной цены, датировка настроек/расходов, отказы записи при
NEEDS_CONFIRM/AMBIGUOUS/NO_PRICE, SHA-256 оригиналов, сохранение формул и
оформления, покрытие секретов в .gitignore.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from app.core import export as exmod
from app.core.calc import compute_cost
from app.core.db import init_db
from app.core.export import ExportError, run_exports
from app.core.onec import ItemStatus, OnecItem


# ── Синтетические XLSX (те же форматы, что в test_mapping) ──────────────────


def make_wb(path, rows, extra_formula=True):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Себестоимость за 2026-11'
    ws.append(['Себестоимость за 2026-11', None, None, None,
               'Изменяемыми являются только зеленые столбцы', None, None,
               None, None, None])
    ws.append(['Артикул WB', 'Артикул поставщика', 'Размер', 'Бренд',
               'Себестоимость товара', 'Стоимость доставки',
               'Стоимость фуллфилмента', 'Итоговая себестоимость',
               '% стоимости товара', '% стоимости доставки'])
    for idx, (mp, sup, size, brand, cost) in enumerate(rows):
        r = 3 + idx
        for col, val in ((1, mp), (2, sup), (3, size), (4, brand), (5, cost)):
            ws.cell(row=r, column=col).value = val
        if extra_formula:
            ws.cell(row=r, column=8).value = f'=E{r}'
    wb.save(path)
    return path


def make_ozon(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Себестоимость'
    ws.append([None, None, None, 'ноябрь 2026 года'])
    ws.append([None, None, None, 'Изменяемыми являются только зеленые столбцы'])
    ws.append(['Артикул Ozon', 'Артикул продавца', 'Бренд',
               'Себестоимость товара', 'Стоимость доставки',
               'Стоимость фуллфилмента', 'Итоговая себестоимость',
               '% стоимости товара', '% стоимости доставки'])
    for idx, (mp, sup, brand, cost) in enumerate(rows):
        r = 4 + idx
        for col, val in ((1, mp), (2, sup), (3, brand), (4, cost)):
            ws.cell(row=r, column=col).value = val
    wb.save(path)
    return path


# Строка-заглушка для пустых OZON-отчётов: артикул заведомо отсутствует в
# каталоге (NO_MATCH), чтобы тест не создавал «пустой» файл с нулём строк.
DUMMY_OZ = [('O9', 'NOMATCH-9', 'Бренд', None)]


# ── Фейковый источник цен 1С ────────────────────────────────────────────────


def ok_item(sku, price, price_date='2026-11-01', **kw):
    return OnecItem(sku_1c=sku, price=Decimal(str(price)), source='com',
                    source_status='exact', price_type='Закупочная',
                    supplier='ГАВРЮШКИН', price_date=price_date,
                    status=ItemStatus.OK, **kw)


def make_fetcher(by_sku):
    calls = []

    def fetch(requests):
        requests = list(requests)
        calls.append(requests)
        out = []
        for r in requests:
            out.append(by_sku.get(r.sku_1c) or OnecItem(
                sku_1c=r.sku_1c, source='com', status=ItemStatus.NOT_FOUND))
        return out

    fetch.calls = calls
    return fetch


# ── БД: схема 002 + базовые настройки ───────────────────────────────────────


def base_db(tmp_path, name='cost.db'):
    con = init_db(tmp_path / name)
    con.execute("INSERT INTO settings (key, value, valid_from)"
                " VALUES ('ppu_rub_per_kg', 405, '2026-07-01')")
    con.execute("INSERT INTO settings (key, value, valid_from)"
                " VALUES ('margin_pct', 15, '2026-07-01')")
    con.commit()
    return con


def add_product(con, sku, manual=0, weight=None, extras=(), manual_price=None,
                name=None, guid=None):
    cur = con.execute(
        'INSERT INTO products (sku_1c, manual_price, active, name_1c, guid_1c)'
        ' VALUES (?, ?, 1, ?, ?)', (sku, int(manual), name, guid))
    pid = cur.lastrowid
    if weight is not None:
        con.execute('INSERT INTO ppu_weights (product_id, weight_g, valid_from)'
                    ' VALUES (?, ?, ?)', (pid, int(weight), '2026-07-01'))
    for kind, rub in extras:
        con.execute("INSERT OR IGNORE INTO extra_kinds (name) VALUES (?)",
                    (kind,))
        kid = con.execute('SELECT id FROM extra_kinds WHERE name = ?',
                          (kind,)).fetchone()['id']
        con.execute('INSERT INTO extras (product_id, kind_id, rub, valid_from)'
                    ' VALUES (?, ?, ?, ?)', (pid, kid, float(rub), '2026-07-01'))
    if manual_price is not None:
        con.execute('INSERT INTO manual_prices (product_id, price, valid_from)'
                    ' VALUES (?, ?, ?)',
                    (pid, float(manual_price), '2026-07-01'))
    con.commit()
    return pid


# ── 11. Используется calc_product() (формула этапа 4) ───────────────────────


def test_uses_calc_product(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1', weight=270)
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', [('B1', 'S1', 'Бренда нет', None)])
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})

    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    expected = compute_cost(price=1000, weight_g=270, ppu_rate=405,
                            margin_pct=15)
    assert report.summary['ok'] == 2 and report.summary['skipped'] == 0
    assert not report.drafts
    ws = load_workbook(report.wb_path, data_only=True)['Себестоимость за 2026-11']
    assert ws.cell(row=3, column=5).value == expected.cost
    snap = con.execute('SELECT * FROM cost_snapshots').fetchone()
    assert snap['cost'] == expected.cost
    assert snap['price_1c_exact'] == '1000'
    assert snap['price_source'] == '1c'
    assert snap['ppu_weight_g'] == 270


# ── 12. Нет повторного расчёта одного товара в запуске ─────────────────────


def test_no_recalc_same_product_in_run(tmp_path, monkeypatch):
    con = base_db(tmp_path)
    add_product(con, 'S1', weight=270)
    p_wb = make_wb(tmp_path / 'wb.xlsx', [
        ('B1', 'S1', '0', None, None),      # две карточки WB
        ('B2', 'S1', '0', None, None),
    ])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', [('O1', 'S1', 'Бренда нет', None)])
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})
    counter = {'n': 0}
    real = exmod.calc_product

    def spy(*a, **kw):
        counter['n'] += 1
        return real(*a, **kw)

    monkeypatch.setattr(exmod, 'calc_product', spy)
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    assert report.summary['ok'] == 3          # 2 карточки WB + 1 OZON
    assert counter['n'] == 1                  # товар рассчитан один раз
    snaps = con.execute('SELECT * FROM cost_snapshots').fetchall()
    assert len(snaps) == 1                    # один снимок на товар в запуске


# ── 13. Приоритет ручной закупочной цены ────────────────────────────────────


def test_manual_price_priority(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1', manual=1, manual_price=500, weight=270)
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', [('O1', 'S1', '', None)])
    fetch = make_fetcher({'S1': ok_item('S1', '9999')})   # 1С не при чём
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='manual', persist=True)
    assert report.summary['ok'] == 2
    snap = con.execute('SELECT * FROM cost_snapshots').fetchone()
    assert snap['price_source'] == 'manual'
    assert Decimal(snap['price_1c_exact']) == Decimal('500')
    expected = compute_cost(price=500, weight_g=270, ppu_rate=405,
                                margin_pct=15).cost
    assert snap['cost'] == expected
    # Ручным товарам внешняя цена не запрашивается
    assert not fetch.calls


# ── 14. Выбор настройки (наценки) по дате ───────────────────────────────────


def test_settings_by_date(tmp_path):
    con = base_db(tmp_path)
    con.execute("INSERT INTO settings (key, value, valid_from)"
                " VALUES ('margin_pct', 20, '2026-11-01')")
    con.commit()
    add_product(con, 'S1')
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', [('O1', 'S1', '', None)])
    fetch = make_fetcher({'S1': ok_item('S1', '100')})
    run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb, ozon_path=p_oz,
                out_dir=tmp_path / 'out', fetch_prices=fetch,
                source_1c='com', persist=True)
    snap = con.execute('SELECT * FROM cost_snapshots').fetchone()
    assert Decimal(snap['margin_pct_exact']) == Decimal('20')
    assert snap['cost'] == compute_cost(price=100, margin_pct=20).cost


# ── 15. Замена доп. расходов по дате версии ─────────────────────────────────


def test_extras_replaced_by_date(tmp_path):
    con = base_db(tmp_path)
    pid = add_product(con, 'S1')
    con.execute("INSERT OR IGNORE INTO extra_kinds (name) VALUES ('Упаковка')")
    kid = con.execute("SELECT id FROM extra_kinds WHERE name = 'Упаковка'"
                      ).fetchone()['id']
    con.execute('INSERT INTO extras (product_id, kind_id, rub, valid_from)'
                ' VALUES (?, ?, 100, ?)', (pid, kid, '2026-07-01'))
    con.execute('INSERT INTO extras (product_id, kind_id, rub, valid_from)'
                ' VALUES (?, ?, 200, ?)', (pid, kid, '2026-10-01'))
    con.commit()
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', [('O1', 'S1', '', None)])
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})
    run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb, ozon_path=p_oz,
                out_dir=tmp_path / 'out', fetch_prices=fetch,
                source_1c='com', persist=True)
    snap = con.execute('SELECT * FROM cost_snapshots').fetchone()
    assert Decimal(snap['extras_rub_exact']) == Decimal('200')  # последняя версия
    assert snap['cost'] == compute_cost(price=1000, margin_pct=15,
                                        extra_items=[('Упаковка', 200)]).cost


# ── 16. Нет цены — нет расчёта (ноль не подставляется) ─────────────────────


def test_no_price_no_calc(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1')
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, 999)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': OnecItem(
        sku_1c='S1', source='com', status=ItemStatus.NO_PRICE)})
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    assert report.summary['no_price'] == 1
    assert report.summary['ok'] == 0
    assert report.drafts
    # прежнее значение в ячейке не тронуто
    ws = load_workbook(report.wb_path, data_only=True)['Себестоимость за 2026-11']
    assert ws.cell(row=3, column=5).value == 999
    assert con.execute('SELECT COUNT(*) c FROM cost_snapshots').fetchone()['c'] == 0


# ── 17. NEEDS_CONFIRM не записывается в Excel ───────────────────────────────


def test_needs_confirm_not_written(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1', weight=270)
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, 100)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': OnecItem(
        sku_1c='S1', source='com', status=ItemStatus.NEEDS_CONFIRM,
        price=Decimal('1000'), detail='сопоставление только по артикулу')})
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    assert report.summary['need_confirm'] == 1
    assert report.summary['ok'] == 0
    ws = load_workbook(report.wb_path, data_only=True)['Себестоимость за 2026-11']
    assert ws.cell(row=3, column=5).value == 100   # прежнее значение
    report_txt = Path(report.report_path).read_text(encoding='utf-8')
    assert 'NEEDS_CONFIRM' in report_txt
    assert 'прежнее значение' in report_txt


# ── 18. Разные GUID блокируют цену (AMBIGUOUS) ──────────────────────────────


def test_different_guid_blocks_price(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1', weight=270)
    add_product(con, 'S2', weight=270)
    p_wb = make_wb(tmp_path / 'wb.xlsx', [
        ('B1', 'S1', '0', None, None),
        ('B2', 'S2', '0', None, None),
    ])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({
        'S1': ok_item('S1', '1000'),
        'S2': OnecItem(sku_1c='S2', source='com', status=ItemStatus.AMBIGUOUS,
                       detail='GUID: 2 разных кандидата'),
    })

    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    assert report.summary['ok'] == 1
    assert report.summary['ambiguous'] == 1
    ws = load_workbook(report.wb_path, data_only=True)['Себестоимость за 2026-11']
    assert ws.cell(row=3, column=5).value is not None       # S1 рассчитан
    assert ws.cell(row=4, column=5).value is None           # S2 блокирован
    snaps = con.execute('SELECT product_id FROM cost_snapshots').fetchall()
    assert [s['product_id'] for s in snaps] == [con.execute(
        "SELECT id FROM products WHERE sku_1c='S1'").fetchone()['id']]


# ── 19. Товар без утверждённого ППУ НЕ получает статус «полностью проверен» ─


def test_ppu_not_approved_not_fully_verified(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1')                       # веса ППУ нет
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    row = [r for r in report.rows if r.supplier_article == 'S1'][0]
    assert row.status == 'PPU_UNCONFIRMED'   # отдельный статус, не OK
    assert row.ppu_approved is False
    assert row.cost is None                  # cost не выдаётся за проверенный
    assert any('вес ППУ не утверждён' in n for n in row.notes)
    assert report.summary['ppu_unconfirmed'] == 1
    assert report.summary['ppu_unapproved'] == 0
    assert report.summary['ok'] == 0
    assert report.drafts          # без утверждённого ППУ — только черновик
    report_txt = Path(report.report_path).read_text(encoding='utf-8')
    assert 'вес ППУ не утверждён' in report_txt


def test_manual_price_without_ppu_is_draft(tmp_path):
    # Ручная цена без утверждённого веса ППУ: рассчитано, но НЕ OK — только
    # черновик, файл «готово» не выпускается, cost в строке не выдаётся.
    con = base_db(tmp_path)
    add_product(con, 'S1', manual=1, manual_price=500)   # веса ППУ нет
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', [('O1', 'S1', '', None)])
    fetch = make_fetcher({'S1': ok_item('S1', '9999')})  # цена 1С не участвует
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='manual', persist=True)
    row = [r for r in report.rows if r.supplier_article == 'S1'][0]
    assert row.status == 'PPU_UNCONFIRMED'
    assert row.cost is None
    assert report.summary['ppu_unconfirmed'] == 2   # строка WB + строка OZON
    assert report.drafts
    assert list((tmp_path / 'out').glob('*_готово.xlsx')) == []
    drafts_ = list((tmp_path / 'out').glob('*_черновик.xlsx'))
    assert drafts_ and Path(drafts_[0]).exists()


# ── 20. Текущая цена не выдаётся за историческую ────────────────────────────


def test_current_price_not_presented_as_historical(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1', weight=270)
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': ok_item('S1', '1000',
                                        price_date='2026-12-10')})
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    assert report.summary['need_confirm'] == 1
    assert report.summary['ok'] == 0
    row = [r for r in report.rows if r.supplier_article == 'S1'][0]
    assert 'новее даты расчёта' in row.reason
    assert row.cost is None
    assert con.execute('SELECT COUNT(*) c FROM cost_snapshots'
                       ).fetchone()['c'] == 0


# ── 3–5. Сохранение заголовков, формул и оформления ─────────────────────────


def test_headers_formulas_formatting_preserved(tmp_path):
    from openpyxl.styles import PatternFill
    con = base_db(tmp_path)
    add_product(con, 'S1', weight=270)
    p_wb = tmp_path / 'wb.xlsx'
    make_wb(p_wb, [('B1', 'S1', '0', None, None)])
    # оформление в служебной ячейке и рядом с целевой (должно сохраниться)
    wb = load_workbook(p_wb)
    ws = wb['Себестоимость за 2026-11']
    ws['A2'].fill = PatternFill('solid', fgColor='FF00FF')
    ws['G3'].number_format = '0.00'
    wb.save(p_wb)

    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    wb2 = load_workbook(report.wb_path)
    ws2 = wb2['Себестоимость за 2026-11']
    # служебный заголовок и шапка на месте
    assert ws2['A1'].value == 'Себестоимость за 2026-11'
    assert ws2.cell(row=2, column=1).value == 'Артикул WB'
    assert ws2.cell(row=2, column=5).value == 'Себестоимость товара'
    # формула в колонке «Итоговая себестоимость» сохранена
    assert ws2.cell(row=3, column=8).value == '=E3'
    # оформление сохранено
    assert ws2['A2'].fill.fgColor.rgb == '00FF00FF'
    assert ws2['G3'].number_format == '0.00'


# ── 31. SHA-256 оригиналов до/после ─────────────────────────────────────────


def test_source_sha256_unchanged(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1', weight=270)
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    h_wb = exmod.sha256_file(p_wb)
    h_oz = exmod.sha256_file(p_oz)
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    assert exmod.sha256_file(p_wb) == h_wb
    assert exmod.sha256_file(p_oz) == h_oz
    assert report.hashes['wb'] == h_wb
    assert report.hashes['ozon'] == h_oz


# ── 32. Никаких записей в 1С / изменения каталога products ─────────────────


def test_no_side_effects_on_products_and_1c(tmp_path):
    con = base_db(tmp_path)
    pid = add_product(con, 'S1', weight=270, name='Подушка',
                      guid='AAAAAAAA-1111-2222-3333-444444444444')
    before = con.execute('SELECT guid_1c, name_1c, manual_price'
                         ' FROM products WHERE id = ?', (pid,)).fetchone()
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})
    run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb, ozon_path=p_oz,
                out_dir=tmp_path / 'out', fetch_prices=fetch,
                source_1c='com', persist=True)
    after = con.execute('SELECT guid_1c, name_1c, manual_price'
                        ' FROM products WHERE id = ?', (pid,)).fetchone()
    assert tuple(after) == tuple(before)
    # никаких GUID-привязок в автоматическом запуске; аудит пуст
    assert con.execute('SELECT COUNT(*) c FROM audit_log').fetchone()['c'] == 0


# ── 34. Отсутствующие цены не заменяются нулём ──────────────────────────────


def test_missing_price_not_written_as_zero(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1')
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': OnecItem(sku_1c='S1', source='com',
                                         status=ItemStatus.NO_PRICE)})
    report = run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb,
                         ozon_path=p_oz, out_dir=tmp_path / 'out',
                         fetch_prices=fetch, source_1c='com', persist=True)
    ws = load_workbook(report.wb_path, data_only=True)['Себестоимость за 2026-11']
    # ячейка осталась пустой (ноль не записан)
    assert ws.cell(row=3, column=5).value is None
    assert report.summary['no_price'] == 1


# ── 35. Нет данных по неподтверждённому GUID ────────────────────────────────


def test_no_unconfirmed_guid_data_written(tmp_path):
    con = base_db(tmp_path)
    add_product(con, 'S1', weight=270)
    p_wb = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, None)])
    p_oz = make_ozon(tmp_path / 'oz.xlsx', DUMMY_OZ)
    fetch = make_fetcher({'S1': ok_item('S1', '1000')})
    run_exports(con=con, calc_date='2026-11-01', wb_path=p_wb, ozon_path=p_oz,
                out_dir=tmp_path / 'out', fetch_prices=fetch,
                source_1c='com', persist=True)
    # автоматический расчёт не пишет GUID (confirm-guid используется явно)
    guids = con.execute('SELECT guid_1c FROM products WHERE guid_1c IS NOT NULL'
                        ).fetchall()
    assert guids == []


# ── 36. Секреты и коммерческие файлы закрыты в git ──────────────────────────


def test_gitignore_covers_secrets_and_xlsx():
    root = Path(__file__).resolve().parents[1]
    gi = (root / '.gitignore').read_text(encoding='utf-8-sig')
    for pattern in ('config.ini', '.env', '*.db', 'input/*.xlsx',
                    'output/*.xlsx', 'legacy/wb_catalog.json'):
        assert pattern in gi, f'.gitignore: нет шаблона {pattern!r}'