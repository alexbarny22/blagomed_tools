# -*- coding: utf-8 -*-
"""Тесты парсера отчётов MPSPACE (этап 6): WB и OZON форматы.

Синтетические XLSX строятся точно по утверждённой структуре 11.2026:
WB — лист «Себестоимость за YYYY-MM», шапка строка 2, данные с строки 3,
колонки: Артикул WB | Артикул поставщика | Размер | Бренд | Себестоимость товара | ...
OZON — лист «Себестоимость», шапка строка 3, данные с строки 4,
колонки: Артикул Ozon | Артикул продавца | Бренд | Себестоимость товара | ...
"""
from __future__ import annotations

import pytest
from openpyxl import Workbook

from app.core.mapping import MappingFormatError, parse


def make_wb(path, rows):
    """rows: список кортежей (mp, sup, size, brand, cost); данные с строки 3."""
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
    wb.save(path)
    return path


def make_ozon(path, rows):
    """rows: список кортежей (mp, sup, brand, cost); данные с строки 4."""
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


def test_wb_correct_column_parse(tmp_path):
    p = make_wb(tmp_path / 'wb.xlsx', [
        (158144489, '232951', '0', None, 1100),
        (215148140, '232947', '0', None, 1207),
    ])
    rows = parse(p, 'wb')
    assert [r.row_number for r in rows] == [3, 4]
    assert rows[0].mp_article == '158144489'
    assert rows[0].supplier_article == '232951'
    assert rows[0].size == '0'
    assert rows[0].current_cost == 1100
    assert rows[1].supplier_article == '232947'
    # Артикул маркетплейса и поставщика не путаются
    assert rows[0].supplier_article != rows[0].mp_article


def test_ozon_correct_column_parse(tmp_path):
    p = make_ozon(tmp_path / 'oz.xlsx', [
        (761235340, '232922', 'Бренда нет', 563),
        (0, '232876', 'Бренда нет', None),
    ])
    rows = parse(p, 'ozon')
    assert [r.row_number for r in rows] == [4, 5]
    assert rows[0].mp_article == '761235340'
    assert rows[0].supplier_article == '232922'
    assert rows[0].current_cost == 563
    assert rows[0].size is None         # у OZON размера нет
    assert rows[1].mp_article == '0'    # числовой ноль не теряется
    assert rows[1].current_cost is None


def test_row_order_preserved(tmp_path):
    p = make_wb(tmp_path / 'wb.xlsx', [
        ('B1', 'S1', '0', None, 1),
        ('B2', 'S2', '0', None, 2),
        ('B3', 'S3', '0', None, 3),
    ])
    rows = parse(p, 'wb')
    assert [r.supplier_article for r in rows] == ['S1', 'S2', 'S3']
    assert [r.row_number for r in rows] == [3, 4, 5]


def test_duplicate_cards_kept(tmp_path):
    p = make_wb(tmp_path / 'wb.xlsx', [
        ('B100', 'S77', '0', None, 10),
        ('B200', 'S77', '0', None, 10),   # та же карточная пара другой артикул WB
    ])
    rows = parse(p, 'wb')
    assert len(rows) == 2
    assert rows[0].mp_article == 'B100'
    assert rows[1].mp_article == 'B200'
    assert rows[1].supplier_article == 'S77'
def test_empty_article_not_none_string(tmp_path):
    p = make_ozon(tmp_path / 'oz.xlsx', [
        (None, '232876', 'Бренда нет', None),       # пустой артикул Ozon
        ('761235340', None, 'Бренда нет', 563),     # пустой артикул продавца
    ])
    rows = parse(p, 'ozon')
    assert rows[0].mp_article is None
    assert rows[0].supplier_article == '232876'
    assert rows[1].mp_article == '761235340'
    assert rows[1].supplier_article is None        # НЕ строка "None"
    for r in rows:
        assert r.supplier_article != 'None'
        assert r.mp_article != 'None'


def test_incorrect_header_stops(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Себестоимость за 2026-11'
    ws.append([None])
    ws.append(['Артикул WB', 'Артикул поставщика', 'Размер', 'Бренд',
               'СЕБЕСТОИМОСТЬ', 'Стоимость доставки'])
    ws.cell(row=3, column=1, value='B1')
    ws.cell(row=3, column=2, value='S1')
    ws.cell(row=3, column=5, value=100)
    p = tmp_path / 'wb_bad.xlsx'
    wb.save(p)
    with pytest.raises(MappingFormatError) as ei:
        parse(p, 'wb')
    assert 'Себестоимость товара' in str(ei.value)


def test_incorrect_column_position_stops(tmp_path):
    """«Себестоимость товара» на изменённой позиции — формат изменился, стоп."""
    wb = Workbook()
    ws = wb.active
    ws.title = 'Себестоимость'
    ws.append([None, None, None, 'ноябрь'])
    ws.append([None, None, None, 'зелёные колонки'])
    ws.append(['Артикул Ozon', 'Артикул продавца', 'Себестоимость товара',
               'Бренд'])                       # cost на 3-й позиции
    ws.cell(row=4, column=1, value='761235340')
    ws.cell(row=4, column=2, value='232922')
    ws.cell(row=4, column=3, value=563)
    p = tmp_path / 'oz_bad.xlsx'
    wb.save(p)
    with pytest.raises(MappingFormatError):
        parse(p, 'ozon')


def test_leading_zeros_preserved(tmp_path):
    p = make_wb(tmp_path / 'wb.xlsx', [
        ('0077', '00099', '00', None, 10),     # текстовые значения из MPSPACE
        (123, 456, None, None, 20),            # числовые — нормализуются в '123'
    ])
    rows = parse(p, 'wb')
    assert rows[0].mp_article == '0077'
    assert rows[0].supplier_article == '00099'
    assert rows[0].size == '00'
    assert rows[1].mp_article == '123'
    assert rows[1].supplier_article == '456'


def test_no_data_rows_stops(tmp_path):
    p = make_wb(tmp_path / 'wb.xlsx', [])
    with pytest.raises(MappingFormatError):
        parse(p, 'wb')


def test_unknown_marketplace_stops(tmp_path):
    p = make_wb(tmp_path / 'wb.xlsx', [('B1', 'S1', '0', None, 1)])
    with pytest.raises(MappingFormatError):
        parse(p, 'unknown')