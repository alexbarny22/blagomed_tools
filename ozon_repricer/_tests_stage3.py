# -*- coding: utf-8 -*-
"""Тесты этапа 3 — прибыль с продажи и безопасный минимум.

Проверяют: расчёт прибыли по формуле ТЗ (комиссия/налог — % от цены, логистика и
себестоимость — фикс. ₽), безопасный минимум с заданной маржой, импорт себестоимости
из Excel (формат blagomed_cost.py и простой файл), колонку себестоимости в CSV,
«Ozon добавит в акцию ниже минимума» в attention_for, миграцию базы с версии этапа 2.

Без внешних зависимостей: простые функции, запуск:  python _tests_stage3.py
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import db, engine  # noqa: E402
from core.ozon import MockOzonClient  # noqa: E402


class Ctx:
    """Временная БД и изолированный мок-клиент Ozon (как в _tests_stage2.py)."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="rp3_test_")
        self._old_path = db.DB_PATH

    def __enter__(self):
        self._old_mock = os.environ.get("OZON_MOCK")
        os.environ["OZON_MOCK"] = "1"
        db.DB_PATH = os.path.join(self.tmp, "repricer.db")
        db.init()
        db.set_setting("dry_run", "0")
        db.set_setting("client_id", "1")
        db.set_setting("api_key", "test-key")
        MockOzonClient._state = None
        MockOzonClient._actions = None
        self.mock = MockOzonClient()
        self.rep = engine.Repricer()
        return self

    def __exit__(self, *a):
        if self._old_mock is None:
            os.environ.pop("OZON_MOCK", None)
        else:
            os.environ["OZON_MOCK"] = self._old_mock
        db.DB_PATH = self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)


def put(pid, price, offer_id=None, **extra):
    """Товар в базе: цена, артикул и произвольные поля (cost, comm_*, stock_*...)."""
    cols = ["product_id", "offer_id", "price", "active"] + list(extra)
    pids = ", ".join(f":{c}" for c in cols)
    row = {"product_id": pid, "offer_id": offer_id or f"T-{pid}", "price": float(price), "active": 1}
    row.update(extra)
    sets = ", ".join(f"{c}=excluded.{c}" for c in ("offer_id", "price", "active") + tuple(extra))
    db.ex((f"INSERT INTO products({', '.join(cols)}) VALUES({pids}) "
           f"ON CONFLICT(product_id) DO UPDATE SET {sets}"), row)


def fbo_product(pid=1, price=5000.0, seller=4000.0, cost=1000.0, tax=None):
    """Товар со схемой FBO: комиссия 17%, логистика макс 240, мин 120, миля 55, эквайринг 14."""
    put(pid, price, seller_price=seller, cost=cost, stock_fbo=5, stock_fbs=0,
        acquiring=14.0, comm_sales_pct_fbo=17.0, comm_sales_pct_fbs=9.0,
        comm_direct_max_fbo=240.0, comm_direct_max_fbs=130.0,
        comm_direct_min_fbo=120.0, comm_direct_min_fbs=60.0,
        comm_mile_fbo=55.0, comm_mile_fbs=85.0)
    if tax is not None:
        db.set_setting("tax_pct", str(tax))
    return pid


def fbs_product(pid=2, price=3000.0, seller=2500.0, cost=800.0):
    put(pid, price, seller_price=seller, cost=cost, stock_fbo=0, stock_fbs=3,
        acquiring=14.0, comm_sales_pct_fbo=17.0, comm_sales_pct_fbs=9.0,
        comm_direct_max_fbo=240.0, comm_direct_max_fbs=130.0,
        comm_direct_min_fbo=120.0, comm_direct_min_fbs=60.0,
        comm_mile_fbo=55.0, comm_mile_fbs=85.0)
    return pid


def test_profit_manual_calc():
    """Прибыль совпадает с ручным расчётом на 3 товарах (FBO, FBS, с налогом)."""
    with Ctx():
        db.set_setting("tax_pct", "")
        fbo_product(1)
        fbs_product(2)
        fbo_product(3)
        s0 = db.get_settings()  # налог не указан (как в равных условиях ТЗ)

        # 1) FBO, налог 0: 4000 − 17%×4000 (680) − (240+55) − 14 − 0 − 1000
        p1 = db.q("SELECT * FROM products WHERE product_id=1", one=True)
        r1 = engine.profit_of(p1, s0)
        assert not r1.get("error"), r1
        assert abs(r1["profit"] - (4000 - 680 - 295 - 14 - 1000)) < 0.01, r1
        assert abs(r1["profit_pct"] - r1["profit"] / 4000 * 100) < 0.01
        assert r1["scheme"] == "FBO"

        # 2) FBS, налог 0: 2500 − 9%×2500 (225) − (130+85) − 14 − 0 − 800
        p2 = db.q("SELECT * FROM products WHERE product_id=2", one=True)
        r2 = engine.profit_of(p2, s0)
        assert not r2.get("error"), r2
        assert r2["scheme"] == "FBS"
        assert abs(r2["profit"] - (2500 - 225 - 215 - 14 - 800)) < 0.01, r2

        # 3) FBO + налог 6%: 4000 − 680 − 295 − 14 − 6%×4000 (240) − 1000 = 1771
        db.set_setting("tax_pct", "6")
        s6 = db.get_settings()
        p3 = db.q("SELECT * FROM products WHERE product_id=3", one=True)
        r3 = engine.profit_of(p3, s6)
        assert not r3.get("error"), r3
        assert r3["tax_pct"] == 6
        assert abs(r3["profit"] - (4000 - 680 - 295 - 14 - 240 - 1000)) < 0.01, r3

        # подсказка (серверная разбивка) не падает
        import app as A
        tip = A.profit_tip_text(r3)
        assert "Комиссия Ozon" in tip and "Себестоимость" in tip and "Прибыль" in tip


def test_profit_no_cost_and_no_comm():
    """Без себестоимости — «—» и причина; без комиссий Ozon — отдельная причина."""
    with Ctx():
        db.set_setting("tax_pct", "")
        put(11, 2000, seller_price=2000, stock_fbo=1)                     # нет себестоимости
        put(12, 2000, cost=500, seller_price=2000, stock_fbo=1)           # нет комиссий
        put(13, 2000, cost=500, seller_price=2000, stock_fbo=1,
            comm_sales_pct_fbo=17.0, comm_direct_max_fbo=240.0, comm_mile_fbo=55.0,
            acquiring=14.0)
        s = db.get_settings()
        r_no_cost = engine.profit_of(db.q("SELECT * FROM products WHERE product_id=11", one=True), s)
        assert r_no_cost.get("error") == "нет себестоимости", r_no_cost
        r_no_comm = engine.profit_of(db.q("SELECT * FROM products WHERE product_id=12", one=True), s)
        assert r_no_comm.get("error") == "нет данных Ozon о комиссиях", r_no_comm
        r_ok = engine.profit_of(db.q("SELECT * FROM products WHERE product_id=13", one=True), s)
        assert not r_ok.get("error") and r_ok["profit"] > 0, r_ok


def test_safe_min_math():
    """Безопасный минимум: мин = (себестоимость+логистика+миля+эквайринг)/(1−комиссия%−налог%−маржа%)."""
    with Ctx():
        db.set_setting("tax_pct", "")
        db.set_setting("min_margin_pct", "10")
        fbo_product(1)  # cost=1000, log=240+55=295, acq=14, комиссия 17%
        p = db.q("SELECT * FROM products WHERE product_id=1", one=True)
        v, reason = engine.safe_min_calc(p, db.get_settings())
        # (1000 + 295 + 14) / (1 − 0.17 − 0.10) = 1309 / 0.73 = 1793,15 → вверх = 1794
        assert reason == "" and v == 1794, (v, reason)
        # на этой цене прибыль даёт заданную маржу
        assert abs(1794 - (1794 * 0.17 + 295 + 14 + 1000)) / 1794 * 100 >= 10 - 0.05

        # маржа недостижима: комиссия+налог+маржа уже покрывают цену
        put(21, 2000, cost=1, seller_price=2000, stock_fbo=5, comm_sales_pct_fbo=95.0,
            comm_direct_max_fbo=0.0, comm_mile_fbo=0.0, acquiring=0.0)
        db.set_setting("tax_pct", "30")
        v, reason = engine.safe_min_calc(db.q("SELECT * FROM products WHERE product_id=21", one=True),
                                         db.get_settings())
        assert v is None and "недостижима" in reason, (v, reason)


def test_safe_min_api_apply_and_mpspace():
    """API: применяется к полю «Мин», для «Под MPSpace» — 409 и не применяется."""
    with Ctx():
        db.set_setting("tax_pct", "")
        db.set_setting("min_margin_pct", "10")
        fbo_product(1)
        db.save_rule(1, mode="guard", min_price=1500)
        fbo_product(2)
        db.set_locked(2, True)
        import app as A
        cl = A.app.test_client()

        r = cl.post("/api/safe_min/1", json={})
        assert r.status_code == 200 and r.json["ok"], r.data
        got = db.get_rule(1)["min_price"]
        assert got == 1794, got

        r = cl.post("/api/safe_min/2", json={})
        assert r.status_code == 409 and "MPSpace" in r.json["message"]
        assert db.get_rule(2)["min_price"] is None

        # массово: обычный применён, MPSpace пропущен
        fbo_product(3)
        r = cl.post("/api/safe_min/batch", json={"ids": [1, 2, 3]})
        assert r.status_code == 200
        assert r.json["done"] == 2 and r.json["skipped"] == 1, r.json
        assert db.get_rule(2)["min_price"] is None  # MPSpace не тронут


def _make_blagomed_xlsx():
    """Файл как у blagomed_cost.py: лист «Себестоимость», шапка во 2-й строке,
    «Артикул поставщика» в B, «Себестоимость» в E, значения уже с наценкой ×1.15."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Себестоимость"
    ws["A1"] = "Благомед_себестоимость_2026_11"
    headers = ["Артикул WB", "Артикул поставщика", "Размер", "Бренд", "Себестоимость", "Доп. расходы"]
    for col, h in enumerate(headers, 1):
        ws.cell(row=2, column=col, value=h)
    ws.cell(row=3, column=1, value="wb111")
    ws.cell(row=3, column=2, value="ART-B1")
    ws.cell(row=3, column=5, value=1500)          # с наценкой ×1.15
    ws.cell(row=4, column=2, value="ART-B2")
    ws.cell(row=4, column=5, value=2300)
    ws.cell(row=5, column=2, value="NO-SUCH")
    ws.cell(row=5, column=5, value=100)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_simple_xlsx():
    """Простой файл: колонки «Артикул» + «Себестоимость» в первой строке."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.cell(row=1, column=1, value="Артикул")
    ws.cell(row=1, column=2, value="Себестоимость")
    ws.cell(row=2, column=1, value="ART-S1")
    ws.cell(row=2, column=2, value="999,50")      # строка как в Excel-ячейках от пользователя
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_blagomed_dups_xlsx():
    """Формат blagomed_cost.py + дубли «Артикула поставщика» с разной себестоимостью:
    ART-B1 встречается ещё раз с 1600 (больше исходных 1500), ART-B2 — с 2000 (меньше 2300)."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(_make_blagomed_xlsx()))
    ws = wb["Себестоимость"]
    ws.cell(row=6, column=2, value="ART-B1")
    ws.cell(row=6, column=5, value=1600)
    ws.cell(row=7, column=2, value="ART-B2")
    ws.cell(row=7, column=5, value=2000)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_cost_import_duplicates_take_max():
    """Дубли «Артикула поставщика» в файле: берётся БОЛЬШАЯ себестоимость,
    такие артикулы перечисляются в отчёте об импорте."""
    with Ctx():
        put(1, 4000, offer_id="ART-B1")
        put(2, 3000, offer_id="ART-B2")
        import app as A
        rows, dups = A.parse_cost_rows(_make_blagomed_dups_xlsx())
        assert rows == [("ART-B1", 1600), ("ART-B2", 2300), ("NO-SUCH", 100)], rows
        assert dups == ["ART-B1", "ART-B2"], dups

        cl = A.app.test_client()
        db.set_setting("cost_divider", "1.15")
        r = cl.post("/cost/import",
                    data={"file": (io.BytesIO(_make_blagomed_dups_xlsx()),
                                   "Благомед_себестоимость_2026_11.xlsx")},
                    content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
        # взята БОЛЬШАЯ: для ART-B1 — 1600, а не исходная 1500
        assert abs(db.q("SELECT cost FROM products WHERE product_id=1", one=True)["cost"] - 1600 / 1.15) < 0.01
        assert abs(db.q("SELECT cost FROM products WHERE product_id=2", one=True)["cost"] - 2300 / 1.15) < 0.01
        # список дублей в журнале событий
        msgs = " ".join(e["message"] for e in db.q("SELECT message FROM events WHERE level='info'"))
        assert "дублей артикулов 2 (ART-B1, ART-B2)" in msgs, msgs
        # и в flash-сообщении на странице
        html = r.data.decode("utf-8", "ignore")
        assert "Дубли в файле — взята большая себестоимость: ART-B1, ART-B2" in html


def test_cost_import_excel_blagomed_and_simple():
    """Импорт себестоимости: формат blagomed_cost.py (делитель по умолчанию 1.15),
    простой файл (делитель 1), отчёт «загружено / не найдено», ручная запись в карточке."""
    with Ctx():
        put(1, 4000, offer_id="ART-B1")
        put(2, 3000, offer_id="ART-B2")
        put(3, 2500, offer_id="ART-S1")
        put(4, 2000, offer_id="ART-MAN")
        import app as A
        rows, dups = A.parse_cost_rows(_make_blagomed_xlsx())
        assert rows == [("ART-B1", 1500), ("ART-B2", 2300), ("NO-SUCH", 100)], rows
        assert dups == []
        rs, d2 = A.parse_cost_rows(_make_simple_xlsx())
        assert rs == [("ART-S1", 999.5)] and d2 == []

        cl = A.app.test_client()
        # файл «Благомед...» при делителе 1.15
        db.set_setting("cost_divider", "1.15")
        r = cl.post("/cost/import",
                    data={"file": (io.BytesIO(_make_blagomed_xlsx()), "Благомед_себестоимость_2026_11.xlsx")},
                    content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
        assert abs(db.q("SELECT cost FROM products WHERE product_id=1", one=True)["cost"] - 1500 / 1.15) < 0.01
        assert abs(db.q("SELECT cost FROM products WHERE product_id=2", one=True)["cost"] - 2300 / 1.15) < 0.01
        # отчёт о не найденном артикуле
        msgs = " ".join(e["message"] for e in db.q("SELECT message FROM events WHERE level='info'"))
        assert "NO-SUCH" in msgs, msgs

        # простой файл при делителе 1 — значение как есть
        db.set_setting("cost_divider", "1")
        cl.post("/cost/import", data={"file": (io.BytesIO(_make_simple_xlsx()), "simple.xlsx")},
                content_type="multipart/form-data", follow_redirects=True)
        assert abs(db.q("SELECT cost FROM products WHERE product_id=3", one=True)["cost"] - 999.5) < 0.01

        # ручная запись в карточке
        rr = cl.post("/api/cost", json={"product_id": 4, "cost": 777})
        assert rr.json["ok"] and db.q("SELECT cost FROM products WHERE product_id=4", one=True)["cost"] == 777


def test_csv_cost_column():
    """CSV: новый столбец «Себестоимость, ₽» в конец; файл прежних версий (18 колонок)
    загружается без ошибок; себестоимость из нового файла применяется."""
    import csv as csvmod
    with Ctx():
        put(1, 4000, offer_id="ART-1", cost=1234.5)
        put(2, 3000, offer_id="ART-2")
        import app as A
        cl = A.app.test_client()
        data = cl.get("/rules.csv").get_data(as_text=True)
        assert "Себестоимость, ₽" in data and "Автодобавление в акции (on/off/пусто)" in data
        head = data.splitlines()[0].split(";")
        # с этапа 2б новые столбцы добавляются только в конец: последний — автодобавление,
        # предпоследний — себестоимость
        assert head[-1] == "Автодобавление в акции (on/off/пусто)", head[-1]
        assert head[-2] == "Себестоимость, ₽", head[-2]

        rows = list(csvmod.reader(io.StringIO(data), delimiter=";"))
        found = [row for row in rows[1:] if row[0] == "1"]
        assert found and abs(float(found[0][18].replace(",", ".")) - 1234.5) < 0.01, found

        old_head = ("product_id;Артикул;Название;Цена;Контролируемая цена;Режим (watch/guard/hold);"
                    "Минимум;Максимум;Цена удержания;Допуск %;Под MPSpace (1 = да);"
                    "Тип контроля (seller/buyer);Стратегия при максимуме (0/1);Теги;Остаток (шт.);"
                    "Акции (0/1/3);Вход в акции (RECOMMEND/AUTO);Отклонение от целевой %")
        old = old_head + "\n" + "1;ART-1;Название;4000;4000;guard;1500;5000;;;;;;;5;0;RECOMMEND;0"
        r = cl.post("/rules/import", data={"file": (io.BytesIO(old.encode("utf-8-sig")), "old.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
        rl = db.get_rule(1)
        assert rl["mode"] == "guard" and rl["min_price"] == 1500

        # новый файл с себестоимостью в 19-й колонке
        new = old + ";888"
        cl.post("/rules/import", data={"file": (io.BytesIO(new.encode("utf-8-sig")), "new.csv")},
                content_type="multipart/form-data", follow_redirects=True)
        assert db.q("SELECT cost FROM products WHERE product_id=1", one=True)["cost"] == 888


def test_attention_autoadd_below_min():
    """«Ozon добавит в акцию ниже минимума» — причина в attention_for (→ таблица и фильтр)."""
    with Ctx():
        put(1, 2000, seller_price=2000, stock_fbo=1)
        put(2, 2000, seller_price=2000, stock_fbo=1)
        db.save_rule(1, mode="guard", min_price=1000)
        db.save_rule(2, mode="guard", min_price=1000)
        # Ozon добавит товар 1 в акцию по цене 700 — ниже минимума 1000; товар 2 — по 1300
        db.ex("INSERT INTO auto_add(product_id, action_id, price_to_auto_add, updated_at) "
              "VALUES(1, 63302, 700, '2026-11-01 00:00:00'), (2, 63302, 1300, '2026-11-01 00:00:00')")
        s = db.get_settings()
        a1 = engine.attention_for(db.q("SELECT * FROM products WHERE product_id=1", one=True),
                                  db.get_rule(1), s)
        a2 = engine.attention_for(db.q("SELECT * FROM products WHERE product_id=2", one=True),
                                  db.get_rule(2), s)
        assert "Ozon добавит в акцию ниже минимума" in a1, a1
        assert "Ozon добавит в акцию ниже минимума" not in a2, a2

        # в таблице (api/products) причина видна — туда же попадает фильтр «Требует внимания»
        import app as A
        cl = A.app.test_client()
        data = {str(x["product_id"]): x for x in cl.get("/api/products").get_json()}
        assert "Ozon добавит в акцию ниже минимума" in data["1"]["attention"]
        assert "Ozon добавит в акцию ниже минимума" not in data["2"]["attention"]


def test_pages_and_api_products_fields():
    """Страницы и /api/products отдают поля этапа 3 (profit, safe_min, cost)."""
    with Ctx():
        db.set_setting("tax_pct", "")
        db.set_setting("min_margin_pct", "10")
        fbo_product(1)
        import app as A
        cl = A.app.test_client()
        for url in ("/", f"/product/1", "/settings", "/rules.csv"):
            assert cl.get(url).status_code == 200, url
        html = cl.get(f"/product/1").get_data(as_text=True)
        assert "Прибыль" in html and "Безопасный минимум" in html and "Себестоимость" in html

        pj = {x["product_id"]: x for x in cl.get("/api/products").get_json()}
        assert "profit" in pj[1] and "safe_min" in pj[1] and pj[1]["cost"] == 1000.0
        assert pj[1]["safe_min"]["value"] == 1794, pj[1]["safe_min"]

        # без себестоимости — страницы не падают
        put(7, 1000, seller_price=1000)
        assert cl.get("/product/7").status_code == 200
        assert cl.get("/api/products").get_json()


def test_migration_from_stage2_db():
    """Миграция базы этапа 2 (без cost/comm_*): колонки добавляются, данные пользователя
    (правила, ключи, история) сохраняются."""
    old_path = db.DB_PATH
    tmp_dir = tempfile.mkdtemp(prefix="rp3_mig_")
    here = os.path.join(tmp_dir, "old.db")
    try:
        con = sqlite3.connect(here)
        con.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            offer_id TEXT, sku INTEGER, name TEXT,
            price REAL, old_price REAL, seller_price REAL, ozon_min_price REAL, buyer_est REAL,
            color_index TEXT, idx_ozon_min REAL, idx_ozon_val REAL, idx_ext_min REAL, idx_ext_val REAL,
            actions TEXT, active INTEGER DEFAULT 1, updated_at TEXT,
            stock_fbo REAL, stock_fbs REAL, stock_fbp REAL, stock_total REAL, image TEXT);
        CREATE TABLE rules (
            product_id INTEGER PRIMARY KEY,
            mode TEXT DEFAULT 'watch', min_price REAL, max_price REAL, target REAL,
            tolerance REAL DEFAULT 2, max_strategy INTEGER DEFAULT 0, control_basis TEXT DEFAULT '',
            tags TEXT DEFAULT '', note TEXT,
            locked INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0,
            promo_mode INTEGER DEFAULT 0, promo_entry TEXT DEFAULT 'RECOMMEND',
            promo_deviation_pct REAL DEFAULT 0);
        CREATE TABLE price_history (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER,
            ts TEXT, price REAL, old_price REAL, seller_price REAL, buyer_est REAL,
            ozon_min_price REAL, color_index TEXT);
        CREATE TABLE changes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, product_id INTEGER,
            offer_id TEXT, controlled_before REAL, price_before REAL, price_after REAL,
            old_price_after REAL, min_after REAL, reason TEXT, source TEXT, dry_run INTEGER,
            ok INTEGER, error TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT);
        """)
        con.execute("INSERT INTO settings(key, value) VALUES('client_id','1')")
        con.execute("INSERT INTO settings(key, value) VALUES('api_key','old-key')")
        con.execute("INSERT INTO products(product_id, offer_id, price, old_price, active) "
                    "VALUES(1, 'OLD-1', 2500, 3200, 1)")
        con.execute("INSERT INTO rules(product_id, mode, min_price, max_price, locked, hidden, "
                    "promo_mode, promo_entry, promo_deviation_pct) "
                    "VALUES(1, 'guard', 2000, 3000, 1, 0, 1, 'AUTO', 5)")
        con.execute("INSERT INTO price_history(product_id, ts, price) VALUES(1, '2026-11-01 00:00:00', 2400)")
        con.execute("INSERT INTO changes(ts, product_id, price_before, price_after, reason, source, ok) "
                    "VALUES('2026-11-01 00:00:00', 1, 2400, 2500, 'test', 'avto', 1)")
        con.commit()
        con.close()

        db.DB_PATH = here
        db.init()
        with db.connect() as con:
            pcols = [r[1] for r in con.execute("PRAGMA table_info(products)")]
        for col in ("cost", "acquiring", "comm_sales_pct_fbo", "comm_sales_pct_fbs",
                    "comm_direct_max_fbo", "comm_direct_min_fbo", "comm_mile_fbo"):
            assert col in pcols, (col, pcols)
        # данные пользователя сохранились
        assert db.get_settings()["api_key"] == "old-key"
        r = db.get_rule(1)
        assert r["mode"] == "guard" and r["min_price"] == 2000 and r["locked"] == 1
        assert r["promo_mode"] == 1 and r["promo_entry"] == "AUTO"
        assert db.q("SELECT COUNT(*) n FROM price_history", one=True)["n"] == 1
        assert db.q("SELECT COUNT(*) n FROM changes", one=True)["n"] == 1
    finally:
        db.DB_PATH = old_path
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {t.__name__} (exception): {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} test passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())