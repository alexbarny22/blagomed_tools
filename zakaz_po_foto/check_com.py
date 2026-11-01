# -*- coding: utf-8 -*-
"""
Проверка COM-связи с 1С. Запускать первым, до бота:

    python check_com.py

Ничего не меняет в базе — только читает.
"""

import configparser
import os
import platform
import struct
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

print("Python:", platform.python_version(), f"({struct.calcsize('P') * 8}-бит)")
print("Разрядность платформы 1С должна быть такой же!\n")

cfg = configparser.ConfigParser()
cfg.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")

import onec  # noqa: E402

try:
    with onec.OneC(cfg) as c:
        print("[+] Соединение установлено")
        print("    Текущая дата в базе:", c.conn.ТекущаяДата())

        names = c.header_attribute_names()
        print(f"[+] Реквизитов шапки ЗаказКлиента: {len(names)}")
        print("    ", ", ".join(names[:15]), "…" if len(names) > 15 else "")
        print("[+] Колонки табличной части Товары:")
        print("    ", ", ".join(c.goods_column_names()))

        for mp, pname in cfg["partners"].items():
            p = c.find_partner(pname)
            tmpl = c.find_template(p, cfg["templates"].get(mp, "").strip())
            print(f"[+] {mp.upper()}: партнёр «{pname}», "
                  f"образец №{tmpl.Номер} от {tmpl.Дата}, "
                  f"строк: {tmpl.Товары.Количество()}")

        items = c.dump_nomenclature()
        print(f"[+] Номенклатура выгружена в nomenclature.json: {len(items)} позиций")

    print("\nВсё в порядке, можно запускать import_codes.py, потом bot.py")
except Exception as e:
    print("\n[!] ОШИБКА:", e)
    print("\nЧто проверить:")
    print(" 1. Зарегистрирована ли comcntr.dll (regsvr32 от администратора)")
    print(" 2. Совпадает ли разрядность Python и платформы 1С")
    print(" 3. Верны ли логин/пароль и имя базы в config.ini")
    print(" 4. Точно ли наименования партнёров в config.ini совпадают с 1С")
    print(" 5. Есть ли в базе хотя бы один заказ на каждого партнёра (образец)")
    sys.exit(1)
