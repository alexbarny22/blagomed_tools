#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Форматы входных отчётов MPSPACE (этап 6): чтение листов WB и Ozon.

Свериено с реальными файлами 11.2026 (SHA-256 в отчёте этапа):
- WB:   Благомед_Себестоимость_YYYY.MM_ВБ.xlsx, лист «Себестоимость за YYYY-MM»;
        строка 1 — служебный заголовок, строка 2 — заголовки колонок,
        данные с строки 3. Колонки: Артикул WB | Артикул поставщика | Размер |
        Бренд | Себестоимость товара | Стоимость доставки | ... (11 колонок, 80 строк данных).
- OZON: OZON_Себестоимость_YYYY.MM.xlsx, лист «Себестоимость»;
        строки 1–2 служебные, строка 3 — заголовки, данные с строки 4.
        Колонки: Артикул Ozon | Артикул продавца | Бренд | Себестоимость товара | ... (10 колонок, 102 строки).

Требования парсера:
- заголовки целевых колонок проверяются по тексту, а не по номеру; изменение
  формата MPSPACE останавливается явной ошибкой MappingFormatError (писать в
  неверную колонку запрещено);
- артикулы читаются без потери ведущих нулей и без превращения пустых значений
  в строку "None";
- одна строка = один MpRow; повторяющиеся карточки одного товара не удаляются;
- файл открывается только на чтение и никогда не сохраняется.

Нумерация колонок — 1-based (1 = первая колонка листа).
Сопоставление с 1С — по колонке «Артикул поставщика/продавца» (артикул 1С).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

__all__ = ['MappingFormatError', 'MpRow', 'MAPPING', 'parse', 'column_map']

# Ожидаемая структура листов. 1-based индексы колонок.
MAPPING = {
    'wb': {
        'sheet': {                      # лист «Себестоимость за <YYYY-MM>»
            'prefix': 'Себестоимость за ',
            'header_row': 2,
            'data_row': 3,
            'columns': {                # имя колонки -> (ожидаемый 1-based индекс, текст заголовка)
                'mp_article': (1, 'Артикул WB'),
                'supplier_article': (2, 'Артикул поставщика'),
                'size': (3, 'Размер'),
                'cost': (5, 'Себестоимость товара'),
            },
        },
        'cost_col': 5,                  # Е — единственная разрешённая колонка записи
    },
    'ozon': {
        'sheet': {
            'prefix': 'Себестоимость',
            'header_row': 3,
            'data_row': 4,
            'columns': {
                'mp_article': (1, 'Артикул Ozon'),
                'supplier_article': (2, 'Артикул продавца'),
                'cost': (4, 'Себестоимость товара'),
            },
        },
        'cost_col': 4,                  # D — единственная разрешённая колонка записи
    },
}


def _sheet_matches(kind: str, prefix: str, title: str) -> bool:
    """WB — имя листа начинается с префикса (период в названии), OZON — равно."""
    if kind == 'wb':
        return title.startswith(prefix) and len(title) > len(prefix)
    return title == prefix


class MappingFormatError(ValueError):
    """Формат отчёта MPSPACE не соответствует утверждённому — обработка стоп."""


@dataclass(frozen=True)
class MpRow:
    """Одна строка отчёта маркетплейса.

    mp_article / supplier_article — строки без потери ведущих нулей
    (None, если ячейка пуста — никогда не строка "None").
    size — размер (WB; у Ozon всегда None).
    current_cost — значение, стоящее в исходной ячейке себестоимости
    (не считается проверенным в текущем запуске).
    row_number — физический 1-based номер строки на листе.
    """
    marketplace: str
    row_number: int
    mp_article: str | None
    supplier_article: str | None
    size: str | None
    current_cost: int | float | Decimal | None
def _norm_article(value) -> str | None:
    """Артикул в текст без потери ведущих нулей; пустое -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, Decimal):
        return str(value)
    s = str(value).strip()
    return s or None


def _norm_cost(value):
    """Исходное значение ячейки себестоимости (только чтение, без проверки)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    if isinstance(value, str):
        try:
            d = Decimal(value)
            return int(d) if d == d.to_integral_value() else float(d)
        except Exception:
            return value
    return value


def _cells(row) -> dict[int, object]:
    """индекс (1-based) -> значение для строки iter_rows(values_only=True)."""
    return {i + 1: v for i, v in enumerate(row)}


def column_map(path, marketplace: str) -> dict[str, int]:
    """1-based индексы колонок после проверки заголовков листа.

    Поднимает MappingFormatError при изменении формата (лист/заголовки/порядок).
    """
    spec = MAPPING.get(marketplace)
    if spec is None:
        raise MappingFormatError(f'Неизвестный маркетплейс: {marketplace!r}')
    sheet_spec = spec['sheet']
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = None
        for title in wb.sheetnames:
            if _sheet_matches(marketplace, sheet_spec['prefix'], title):
                sheet = wb[title]
                break
        if sheet is None:
            raise MappingFormatError(
                f'{marketplace}: лист «{sheet_spec["prefix"]}…» не найден. '
                f'Доступные листы: {wb.sheetnames}')

        header = None
        for idx, row in enumerate(sheet.iter_rows(values_only=True), 1):
            if idx == sheet_spec['header_row']:
                header = list(row)
                break
        if header is None:
            raise MappingFormatError(
                f'{marketplace}: строка заголовков (№{sheet_spec["header_row"]}) не '
                f'найдена на листе «{sheet.title}» (max_row={sheet.max_row}).')

        found = _cells(header)
        result: dict[str, int] = {}
        for key, (expected_col, expected_text) in sheet_spec['columns'].items():
            actual_text = found.get(expected_col)
            if actual_text is None or str(actual_text).strip() != expected_text:
                raise MappingFormatError(
                    f'{marketplace}: изменение формата — в колонке {expected_col} '
                    f'ожидался заголовок {expected_text!r}, найдено {actual_text!r}. '
                    'Обработка остановлена, в неверную колонку ничего не пишется.')
            result[key] = expected_col
        return result
    finally:
        wb.close()


def parse(path, marketplace: str) -> list[MpRow]:
    """Читает строки отчёта MPSPACE (mapping.MpRow) или бросает MappingFormatError.

    path — путь к исходному файлу; marketplace — 'wb' | 'ozon'.
    Файл открывается только на чтение и не изменяется.
    """
    spec = MAPPING.get(marketplace)
    if spec is None:
        raise MappingFormatError(f'Неизвестный маркетплейс: {marketplace!r}')
    sheet_spec = spec['sheet']
    cols = column_map(path, marketplace)

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = next(wb[t] for t in wb.sheetnames
                     if _sheet_matches(marketplace, sheet_spec['prefix'], t))
        rows: list[MpRow] = []
        c_mp = cols['mp_article']
        c_sup = cols['supplier_article']
        c_size = cols.get('size')
        c_cost = cols['cost']
        for idx, row in enumerate(sheet.iter_rows(values_only=True), 1):
            if idx < sheet_spec['data_row']:
                continue
            cells = _cells(row)
            mp_art = _norm_article(cells.get(c_mp))
            sup_art = _norm_article(cells.get(c_sup))
            size = _norm_article(cells.get(c_size)) if c_size else None
            cost = _norm_cost(cells.get(c_cost))
            if mp_art is None and sup_art is None and size is None and cost is None:
                continue          # пустая строка под данными
            rows.append(MpRow(
                marketplace=marketplace, row_number=idx,
                mp_article=mp_art, supplier_article=sup_art,
                size=size, current_cost=cost))
        if not rows:
            raise MappingFormatError(
                f'{marketplace}: на листе нет ни одной строки данных '
                f'(данные ожидаются начиная с строки {sheet_spec["data_row"]}).')
        return rows
    finally:
        wb.close()