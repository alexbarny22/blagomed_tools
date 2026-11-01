# -*- coding: utf-8 -*-
"""
Быстрая проверка (секунды, без COM): какие объекты 1С бот видит через OData.
Если всё «есть» — setup_odata_prihod.bat запускать не нужно.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import onec_odata as O  # noqa: E402

NEED = [
    ("Document_ПриобретениеТоваровУслуг", "Поступление товаров (главное)", True),
    ("Catalog_Контрагенты", "Поставщики", True),
    ("Catalog_Номенклатура", "Товары", True),
    ("Catalog_Склады", "Склады", True),
    ("Catalog_Организации", "Организации", True),
    ("Catalog_СоглашенияСПоставщиками", "Соглашения с поставщиками", False),
    ("Catalog_ДоговорыКонтрагентов", "Договоры", False),
]

od = O.OData(config.ODATA_URL, config.ONEC_USER, config.ONEC_PASSWORD, 180)
print(f"Проверяю OData: {config.ODATA_URL}\n")
missing_main = False
for entity, title, main in NEED:
    try:
        od.get(entity, **{"$top": "1"})
        print(f"  ✔ есть   {title}")
    except Exception as e:
        msg = str(e)
        if "401" in msg:
            print(f"  ✖ 1С не пустила по логину/паролю — проверьте ONEC_USER / ONEC_PASSWORD в config.py")
            break
        print(f"  {'✖ НЕТ ' if main else '· нет '}  {title}\n           причина: {msg[:300]}")
        missing_main |= main

if missing_main:
    print("\nЧего-то главного (✖) нет — нужен setup_odata_prihod.bat (или включить объект в публикации OData вручную).")
else:
    print("\nВсё главное есть — setup_odata_prihod.bat запускать НЕ нужно, бот может проводить поступления.")
    try:
        sk = od.get("Catalog_Склады", **{"$filter": "DeletionMark eq false",
                                         "$select": "Description"})
        names = [r["Description"] for r in sk]
        hit = [n for n in names if getattr(config, "WAREHOUSE", "").lower() in n.lower()]
        print(f"\nСклад из config.py «{config.WAREHOUSE}» — подходят: {', '.join(hit) or 'НИ ОДИН'}")
        if len(hit) != 1:
            print("Все склады в 1С: " + "; ".join(names))
    except Exception:
        pass
input("\nНажмите Enter, чтобы закрыть...")
