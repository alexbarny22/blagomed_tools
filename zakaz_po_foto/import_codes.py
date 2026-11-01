# -*- coding: utf-8 -*-
"""
Разовый импорт таблицы кодов из codes.csv в codes.json.

Формат codes.csv (разделитель — точка с запятой, первая строка — заголовок):

    код;наименование или артикул в 1С
    МН-01;Наколенник магнитный
    ТР-03;347-215

Во второй колонке можно писать либо артикул (сопоставится точно),
либо наименование (сопоставится нечётко, покажу что нашёл).

Запуск:  python import_codes.py
"""

import configparser
import csv
import os
import sys

from rapidfuzz import fuzz, process

import label
import onec

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "codes.csv")

cfg = configparser.ConfigParser()
cfg.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")

items = onec.load_cache()
if not items:
    print("Нет кеша номенклатуры, тяну из 1С…")
    with onec.OneC(cfg) as c:
        items = c.dump_nomenclature()
print(f"Номенклатуры в кеше: {len(items)}")

by_art = {i["art"].strip().lower(): i for i in items if i["art"].strip()}
names = [i["name"] for i in items]

if not os.path.exists(CSV_FILE):
    print(f"Нет файла {CSV_FILE}")
    sys.exit(1)

ok = bad = 0
with open(CSV_FILE, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.reader(f, delimiter=";"))

for row in rows[1:]:
    if len(row) < 2 or not row[0].strip():
        continue
    code, target = row[0].strip().upper(), row[1].strip()

    hit = by_art.get(target.lower())
    how = "по артикулу"
    if hit is None:
        m = process.extractOne(target, names, scorer=fuzz.WRatio)
        if m and m[1] >= 75:
            hit = items[m[2]]
            how = f"по названию, {m[1]:.0f}%"

    if hit is None:
        print(f"  [!] {code:8s} — не нашёл: {target}")
        bad += 1
        continue

    label.save_code(code, hit["guid"], hit["name"])
    print(f"  [+] {code:8s} = {hit['name'][:55]}  ({how})")
    ok += 1

print(f"\nЗаписано в codes.json: {ok}. Не сопоставлено: {bad}.")
if bad:
    print("Несопоставленные коды бот спросит у вас при первом же фото "
          "и запомнит сам — править csv не обязательно.")
