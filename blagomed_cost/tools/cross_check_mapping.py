"""Сверка: артикулы продавца в отчётах MPSPACE vs каталог legacy.

Вывод (UTF-8, файл cross_check.txt):
- все уникальные артикулы продавца из WB и OZON файлов;
- которые отсутствуют в legacy/wb_catalog.json;
- есть ли в отчётах 232886 и 232894.
"""
# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path

from openpyxl import load_workbook

BASE = Path(__file__).resolve().parents[1]

CATALOG = json.loads((BASE / "legacy" / "wb_catalog.json").read_text(encoding="utf-8"))
catalog = {str(it["sup"]).strip() for it in CATALOG.get("items", [])}
# ещё FIXED_COSTS из старого бота известны как 'известные артикулы'
known_extra = {
    "232924", "232983", "232991", "232943", "232923", "232948", "232951",
    "232942", "232955", "232936", "232965", "232934", "232897", "232937",
    "232935", "333003", "232954", "232925", "232800", "232962",
}

out = []
out.append(f"Всего артикулов в каталоге legacy: {len(catalog)}")


def read_arts(path: str, header_row: int, col: int) -> set:
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    res = set()
    for idx, row in enumerate(ws.iter_rows(values_only=True), 1):
        if idx < header_row + 1:
            continue
        if not row or len(row) <= col:
            continue
        v = row[col]
        if v is None:
            continue
        s = str(v).strip()
        if s:
            res.add(s)
    wb.close()
    return res


def analyze(tag, path, header_row, col):
    arts = read_arts(path, header_row, col)
    missing = sorted(arts - catalog - known_extra)
    out.append(f"--- {tag}: уникальных артикулов продавца = {len(arts)}")
    out.append(f"    не знаю в каталоге/MO known: {missing}")
    out.append(f"    232886 в отчёте: {'ДА' if '232886' in arts else 'НЕТ'}")
    out.append(f"    232894 в отчёте: {'ДА' if '232894' in arts else 'НЕТ'}")
    return arts


wb_arts = analyze("WB", str(BASE / "input" / "Благомед_Себестоимость_11.2026_ВБ.xlsx"), 2, 1)
oz_arts = analyze("OZON", str(BASE / "input" / "OZON_Себестоимость_11.2026.xlsx"), 3, 1)
out.append(f"WB ∩ OZON: {len(wb_arts & oz_arts)} артикулов")

(BASE / "cross_check.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")