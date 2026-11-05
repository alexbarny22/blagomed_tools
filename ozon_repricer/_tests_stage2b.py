# -*- coding: utf-8 -*-
"""Тесты этапа 2б — акции Ozon по новой логике «единой предельной цены» (с 19.10.2026).

Проверяют: миграцию старых режимов акций в promo_auto_add, чтение и сохранение
auto_add_to_ozon_actions_list_enabled, отправку настройки автодобавления в /v1/product/import/prices
(вместе с ценой и без изменения цены), тренировку и «Под MPSpace», ошибку Ozon по этому полю,
диагностику участников с ценой выше требуемой, CSV с новым столбцом и старые файлы,
отсутствие невыполнимых кнопок входа/выхода в интерфейсе. Замечания ревью: пауза/лимит
цены не роняет весь список через None в import_prices, повтор автоотправки не чаще 6 часов
и остановка после 2 неудач, тренировочные записи «что сделал бы» тоже не чаще 6 часов,
«нет цены» — без отправки и с записью в журнал.

Без внешних зависимостей: простые функции, запуск:  python _tests_stage2b.py
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import db, engine  # noqa: E402
from core.ozon import MockOzonClient  # noqa: E402

MOCK_TITLE = "Скидка продавца: медтехника"


class Ctx:
    """Временная БД и изолированный экземпляр мок-клиента Ozon."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="rp2b_test_")
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
    """Товар в базе: цена, артикул и произвольные поля (auto_add_enabled, cost...)."""
    cols = ["product_id", "offer_id", "price", "active"] + list(extra)
    pids = ", ".join(f":{c}" for c in cols)
    row = {"product_id": pid, "offer_id": offer_id or f"T-{pid}", "price": float(price), "active": 1}
    row.update(extra)
    sets = ", ".join(f"{c}=excluded.{c}" for c in ("offer_id", "price", "active") + tuple(extra))
    db.ex((f"INSERT INTO products({', '.join(cols)}) VALUES({pids}) "
           f"ON CONFLICT(product_id) DO UPDATE SET {sets}"), row)


def act_row(pid, aid, kind, action_price, price=None, max_action_price=None):
    return (pid, aid, kind, MOCK_TITLE if aid == 63301 else f"акция {aid}", None, None,
            None, price, float(action_price) if action_price else None,
            float(max_action_price) if max_action_price else None)


class RecordingClient(MockOzonClient):
    """Мок Ozon, который запоминает тела /v1/product/import/prices и умеет отклонять
    только настройку автодобавления (как живой Ozon при ошибке по этому полю)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sent = []
        self.fail_auto = False

    def import_prices(self, prices):
        self.sent.extend([dict(p) for p in prices])
        if not self.fail_auto:
            return super().import_prices(prices)
        res = []
        for p in prices:
            s = MockOzonClient._state.get(int(p["product_id"]))
            if "auto_add_to_ozon_actions_list_enabled" in p:
                res.append({"product_id": p["product_id"], "updated": False,
                            "errors": [{"code": "INVALID_AUTO_ADD", "message": "поле не принято"}]})
                if s and "price" in p:
                    s["price"] = float(p["price"])
            else:
                res.append({"product_id": p["product_id"], "updated": True, "errors": []})
        return res


def _make_stage3_db(path):
    """База формата 2026.11.24-2: без promo_auto_add и auto_add_enabled."""
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE products (
        product_id INTEGER PRIMARY KEY, offer_id TEXT, sku INTEGER, name TEXT,
        price REAL, old_price REAL, seller_price REAL, ozon_min_price REAL, buyer_est REAL,
        color_index TEXT, idx_ozon_min REAL, idx_ozon_val REAL, idx_ext_min REAL, idx_ext_val REAL,
        actions TEXT, active INTEGER DEFAULT 1, updated_at TEXT,
        stock_fbo REAL, stock_fbs REAL, stock_fbp REAL, stock_total REAL, image TEXT,
        cost REAL, acquiring REAL, comm_sales_pct_fbo REAL, comm_sales_pct_fbs REAL,
        comm_direct_max_fbo REAL, comm_direct_max_fbs REAL,
        comm_direct_min_fbo REAL, comm_direct_min_fbs REAL, comm_mile_fbo REAL, comm_mile_fbs REAL);
    CREATE TABLE rules (
        product_id INTEGER PRIMARY KEY, mode TEXT DEFAULT 'watch',
        min_price REAL, max_price REAL, target REAL, tolerance REAL DEFAULT 2,
        max_strategy INTEGER DEFAULT 0, control_basis TEXT DEFAULT '', tags TEXT DEFAULT '',
        note TEXT, locked INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0,
        promo_mode INTEGER DEFAULT 0, promo_entry TEXT DEFAULT 'RECOMMEND',
        promo_deviation_pct REAL DEFAULT 0);
    CREATE TABLE price_history (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, ts TEXT,
        price REAL, old_price REAL, seller_price REAL, buyer_est REAL, ozon_min_price REAL, color_index TEXT);
    CREATE TABLE changes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, product_id INTEGER, offer_id TEXT,
        controlled_before REAL, price_before REAL, price_after REAL, old_price_after REAL, min_after REAL,
        reason TEXT, source TEXT, dry_run INTEGER, ok INTEGER, error TEXT);
    CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT);
    CREATE TABLE product_actions (product_id INTEGER NOT NULL, action_id INTEGER NOT NULL, kind TEXT NOT NULL,
        title TEXT, date_from TEXT, date_to TEXT, status TEXT, price REAL, action_price REAL,
        max_action_price REAL, PRIMARY KEY (product_id, action_id, kind));
    CREATE TABLE auto_add (product_id INTEGER NOT NULL, action_id INTEGER NOT NULL,
        price_to_auto_add REAL, updated_at TEXT, PRIMARY KEY (product_id, action_id));
    """)
    for pid in (1, 2, 3, 4):
        con.execute("INSERT INTO products(product_id, offer_id, name, price, active, cost) "
                    "VALUES(?, ?, 'Товар', 1000, 1, 500)", (pid, f"OLD-{pid}"))
    con.execute("INSERT INTO rules(product_id, mode, min_price, promo_mode, promo_entry) "
                "VALUES(1, 'guard', 400, 1, 'RECOMMEND')")
    con.execute("INSERT INTO rules(product_id, mode, min_price, promo_mode, promo_entry) "
                "VALUES(2, 'guard', 500, 3, 'AUTO')")
    con.execute("INSERT INTO rules(product_id, mode, min_price, promo_mode, promo_entry) "
                "VALUES(3, 'hold', 600, 3, 'RECOMMEND')")
    con.execute("INSERT INTO rules(product_id, mode, min_price, promo_mode, promo_entry) "
                "VALUES(4, 'watch', 0, 0, 'RECOMMEND')")
    con.execute("INSERT INTO settings(key, value) VALUES('client_id', '12345')")
    con.execute("INSERT INTO settings(key, value) VALUES('api_key', 'secret-key')")
    con.execute("INSERT INTO settings(key, value) VALUES('dry_run', '0')")
    con.commit()
    con.close()


def test_migration_from_2026_11_24_2():
    """Миграция базы 2026.11.24-2: promo_mode=1 → 'off', 3+AUTO → 'on', остальное → '';
    событие в «События», старые данные и ключи целы, новые колонки добавлены."""
    with Ctx() as c:
        old = os.path.join(c.tmp, "old_repricer.db")
        _make_stage3_db(old)
        db.DB_PATH = old
        db.init()

        got = {r["product_id"]: r["promo_auto_add"] for r in db.q("SELECT * FROM rules")}
        assert got == {1: "off", 2: "on", 3: "", 4: ""}, got
        # данные целы
        assert db.get_rule(1)["min_price"] == 400 and db.get_rule(3)["mode"] == "hold"
        assert db.q("SELECT cost FROM products WHERE product_id=1", one=True)["cost"] == 500
        s = db.get_settings()
        assert s["client_id"] == "12345" and s["api_key"] == "secret-key"
        # новые колонки
        assert "promo_auto_add" in {r["name"] for r in db.q("PRAGMA table_info(rules)")}
        assert "auto_add_enabled" in {r["name"] for r in db.q("PRAGMA table_info(products)")}
        row = db.q("SELECT auto_add_enabled FROM products WHERE product_id=1", one=True)
        assert row["auto_add_enabled"] is None
        # событие о миграции с числом товаров
        msg = " ".join(e["message"] for e in db.q("SELECT message FROM events"))
        assert "настройка автодобавления перенесена" in msg, msg
        assert "«запретить» — 1 товаров, «разрешить» — 1, «не трогать» — 2" in msg, msg


def test_send_auto_add_off_no_price_change():
    """'off' при auto_add_enabled=1: в боевом режиме уходит auto_add..=false вместе с
    ТЕКУЩИМИ price/old_price (цена не меняется); в тренировке — только журнал;
    у «Под MPSpace» — ничего не отправляется."""
    with Ctx() as c:
        cl = RecordingClient(c.mock)
        pid = sorted(MockOzonClient._state)[0]
        put(pid, 1000, auto_add_enabled=1)
        db.save_rule(pid, promo_auto_add="off", mode="watch")

        sent, errors, skipped, promo_done = c.rep.apply_rules(cl, db.get_settings())
        assert promo_done == 1 and errors == 0, (sent, errors, skipped, promo_done)
        body = cl.sent[0]
        assert body["auto_add_to_ozon_actions_list_enabled"] is False
        assert body["price"] == "1000" and body["old_price"] == "0", body  # цена передана, но та же
        assert db.q("SELECT auto_add_enabled FROM products WHERE product_id=?", (pid,), one=True)[
            "auto_add_enabled"] == 0
        ch = db.q("SELECT source, dry_run, ok FROM changes ORDER BY id DESC LIMIT 1", one=True)
        assert ch["source"] == "акции: автодобавление" and ch["dry_run"] == 0 and ch["ok"] == 1, ch

        # тренировка: ничего не отправляется, журнал dry_run=1 «что сделал бы»
        cl2 = RecordingClient(c.mock)
        put(pid, 1000, auto_add_enabled=1)
        db.set_setting("dry_run", "1")
        c.rep.apply_rules(cl2, db.get_settings())
        assert not cl2.sent, "тренировка отправила в Ozon!"
        ch = db.q("SELECT source, dry_run, ok FROM changes WHERE dry_run=1 "
                  "AND source='акции: автодобавление' ORDER BY id DESC LIMIT 1", one=True)
        assert ch and ch["ok"] == 1, ch

        # «Под MPSpace»: ничего не отправляется и не записывается
        db.set_setting("dry_run", "0")
        db.set_locked(pid, True)
        put(pid, 1000, auto_add_enabled=1)
        before = len(db.q("SELECT 1 FROM changes"))
        cl3 = RecordingClient(c.mock)
        c.rep.apply_rules(cl3, db.get_settings())
        assert not cl3.sent, "MPSpace отправлен в Ozon"
        assert len(db.q("SELECT 1 FROM changes")) == before, "MPSpace попал в журнал"


def test_send_auto_add_along_with_price_and_no_cooldown():
    """Автодобавление не мешает изменению цены в том же запуске (одно тело запроса),
    не включает паузу после изменения и не тратит суточный лимит изменений цены."""
    with Ctx() as c:
        cl = RecordingClient(c.mock)
        pid = sorted(MockOzonClient._state)[0]
        put(pid, 1000, auto_add_enabled=0)
        db.save_rule(pid, mode="guard", min_price=1200, max_price=1200, promo_auto_add="on")

        sent, errors, skipped, promo_done = c.rep.apply_rules(cl, db.get_settings())
        assert sent == 1 and promo_done == 1 and errors == 0, (sent, errors, skipped, promo_done)
        body = cl.sent[0]
        assert body["price"] == "1200" and body["auto_add_to_ozon_actions_list_enabled"] is True
        # 2 записи: цена (source 'авто') и автодобавление
        srcs = sorted(r["source"] for r in db.q("SELECT source FROM changes WHERE ok=1 AND dry_run=0"))
        assert srcs == ["авто", "акции: автодобавление"], srcs
        # пауза считает только изменение цены (автодобавление не в счёт)
        assert db.changes_since(pid, 60) == 1

        # теперь цена в норме, меняется только автодобавление — паузу/лимит это не трогает
        put(pid, 1200, auto_add_enabled=1, old_price=0)
        db.save_rule(pid, mode="guard", min_price=1200, max_price=1200, promo_auto_add="off")
        cl2 = RecordingClient(c.mock)
        rr = c.rep.apply_rules(cl2, db.get_settings())
        assert rr[0] == 0 and rr[3] == 1, rr
        assert db.changes_since(pid, 60) == 1, "автодобавление повлияло на паузу"
        last = db.last_change(pid)
        assert last and last["source"] != "акции: автодобавление", "автодобавление учтено как изменение цены"


def test_auto_add_error_does_not_break_prices():
    """Ошибка Ozon по полю автодобавления — в журнал и «Требует внимания» (и событие),
    а цены остальных товаров проверены и отправлены как обычно."""
    with Ctx() as c:
        pids = sorted(MockOzonClient._state)[:2]
        p_auto, p_price = pids[0], pids[1]
        cl = RecordingClient(c.mock)
        cl.fail_auto = True
        put(p_auto, 1000, auto_add_enabled=0)
        db.save_rule(p_auto, promo_auto_add="on", mode="watch")
        put(p_price, 1000)
        db.save_rule(p_price, mode="guard", min_price=1200, max_price=1200)

        sent, errors, skipped, promo_done = c.rep.apply_rules(cl, db.get_settings())
        assert sent == 1 and promo_done == 0, (sent, errors, skipped, promo_done)
        # журнал: ошибка по автодобавлению ok=0
        bad = db.q("SELECT source, ok, error FROM changes WHERE source='акции: автодобавление' "
                   "ORDER BY id DESC LIMIT 1", one=True)
        assert bad and bad["ok"] == 0 and "поле не принято" in (bad["error"] or ""), bad
        # событие-предупреждение
        warns = " ".join(e["message"] for e in db.q("SELECT message FROM events WHERE level='warn'"))
        assert "Ozon не принял настройку автодобавления" in warns, warns
        # «Требует внимания» для товара с отклонённой настройкой
        p = db.q("SELECT * FROM products WHERE product_id=?", (p_auto,), one=True)
        atn = engine.attention_for(p, db.get_rule(p_auto), db.get_settings())
        assert "Ozon не принял настройку автодобавления" in atn, atn
        # цена другого товара всё равно отправлена в том же запуске
        price_body = [b for b in cl.sent if b["product_id"] == p_price][0]
        assert price_body["price"] == "1200", price_body
        ok_row = db.q("SELECT ok FROM changes WHERE product_id=? ORDER BY id DESC LIMIT 1",
                      (p_price,), one=True)
        assert ok_row and ok_row["ok"] == 1


def test_ui_no_enter_exit_and_actions_table():
    """В интерфейсе нет кнопок «вход*/выход*» и старых настроек акций; таблица акций
    показывает требуемую цену, вашу цену и статус простыми словами."""
    with Ctx() as c:
        import app as A
        pid = sorted(MockOzonClient._state)[0]
        put(pid, 1000)
        db.save_rule(pid, mode="hold", target=900, min_price=500, promo_auto_add="on")
        db.save_actions([act_row(pid, 63302, "candidate", 900, price=1000, max_action_price=950)])
        db.ex("INSERT INTO auto_add(product_id, action_id, price_to_auto_add, updated_at) "
              "VALUES(?, 63302, 900, '2026-11-01 00:00:00')", (pid,))
        cl = A.app.test_client()
        html = cl.get(f"/product/{pid}").get_data(as_text=True)
        assert "вход*" not in html and "выход*" not in html
        assert "Режим акций" not in html and "Вход в акции" not in html and "Допустимо ниже цели" not in html
        assert "Автодобавление в акции" in html and "разрешить автодобавление" in html
        assert "войдёт, если ваша цена ≤" in html, "нет статуса кандидата"
        assert "Треб. цена" in html and "Ваша цена" in html
        assert "Ozon добавит сам по цене" in html, "не показано автодобавление по цене"
        assert "при цене удержания 900 ₽ товар попадёт в эту акцию" in html
        idx = cl.get("/").get_data(as_text=True)
        assert "Режим акций" not in idx and "вход*" not in idx and "Акция Ozon" not in idx
        assert "Акции: автодобав." in idx
        pj = {x["product_id"]: x for x in cl.get("/api/products").get_json()}
        assert pj[pid]["promo_auto_add"] == "on" and "auto_add_enabled" in pj[pid]


def test_actions_diag_warns_once_a_day():
    """Диагностика: участник с price > max_action_price (по новым правилам такого быть
    не должно) — одно предупреждение в «События» в сутки."""
    with Ctx() as c:
        for pid, s in MockOzonClient._state.items():
            put(pid, s["price"])
        c.rep._sync_actions(c.mock, force=True)
        n_over = db.q("SELECT COUNT(DISTINCT product_id) n FROM product_actions "
                      "WHERE kind='participant' AND price IS NOT NULL "
                      "AND max_action_price IS NOT NULL AND price > max_action_price", one=True)["n"]
        assert n_over > 0, "в моке нет участников с ценой выше требуемой"
        warns = db.q("SELECT message FROM events WHERE level='warn' "
                     "AND message LIKE 'Проверка правил Ozon:%'")
        assert warns, "диагностика не написала предупреждение"
        assert "сообщите разработчику" in warns[0]["message"]
        # повторная синхронизация в тот же день — без второго предупреждения
        c.rep._sync_actions(c.mock, force=True)
        warns2 = db.q("SELECT message FROM events WHERE level='warn' "
                      "AND message LIKE 'Проверка правил Ozon:%'")
        assert len(warns2) == len(warns), "предупреждение продублировано в тот же день"


def _row(*vals):
    return ";".join("" if v is None else str(v) for v in vals)


def test_csv_new_column_and_old_files():
    """CSV: новый столбец автодобавления в конце; файлы этапов 1/2/3 загружаются,
    старые столбцы акций переводятся по миграции, файл этапа 1 настройку не трогает."""
    with Ctx() as c:
        import app as A
        cl = A.app.test_client()
        put(1, 1000, offer_id="CSV-1")
        db.save_rule(1, mode="guard", min_price=900, promo_auto_add="on")

        # экспорт: заголовок и значение в строке товара
        csv_text = cl.get("/rules.csv").get_data(as_text=True)
        assert "Автодобавление в акции (on/off/пусто)" in csv_text
        export_rows = [r for r in csv_text.split("\r\n") if r.startswith("1;CSV-1")]
        assert export_rows and export_rows[0].endswith(";on"), export_rows

        # новый файл: колонка авто = off
        new_head = _row("product_id", "Артикул", "Название", "Цена", "Контролируемая цена",
                        "Режим (watch/guard/hold)", "Минимум", "Максимум", "Цена удержания", "Допуск %",
                        "Под MPSpace (1 = да)", "Тип контроля (seller/buyer)", "Стратегия при максимуме (0/1)",
                        "Теги", "Остаток (шт.)", "Акции (0/1/3)", "Вход в акции (RECOMMEND/AUTO)",
                        "Отклонение от целевой %", "Себестоимость, ₽", "Автодобавление в акции (on/off/пусто)")
        new_row = _row(1, "CSV-1", "Товар", 1000, 1000, "guard", 900, "", "", 2, "", "", "", "", "",
                       "", "", "", "", "off")
        r = cl.post("/rules/import",
                    data={"file": (io.BytesIO((new_head + "\n" + new_row).encode("utf-8-sig")), "new.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
        assert db.get_rule(1)["promo_auto_add"] == "off", "новый столбец не применён"

        old23_head = _row("product_id", "Артикул", "Название", "Цена", "Контролируемая цена",
                          "Режим (watch/guard/hold)", "Минимум", "Максимум", "Цена удержания", "Допуск %",
                          "Под MPSpace (1 = да)", "Тип контроля (seller/buyer)", "Стратегия при максимуме (0/1)",
                          "Теги", "Остаток (шт.)", "Акции (0/1/3)", "Вход в акции (RECOMMEND/AUTO)",
                          "Отклонение от целевой %", "Себестоимость, ₽")
        old23_row1 = old23_head + "\n" + _row(1, "CSV-1", "Товар", 1000, 1000, "guard", 900, "", "", 2,
                                              "", "", "", "", "", 1, "AUTO", 5, "")
        r = cl.post("/rules/import",
                    data={"file": (io.BytesIO(old23_row1.encode("utf-8-sig")), "old23.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
        assert db.get_rule(1)["promo_auto_add"] == "off", "старые колонки (1) должны дать 'off'"
        old23_row2 = old23_head + "\n" + _row(1, "CSV-1", "Товар", 1000, 1000, "guard", 900, "", "", 2,
                                              "", "", "", "", "", 3, "AUTO", 5, "")
        cl.post("/rules/import",
                data={"file": (io.BytesIO(old23_row2.encode("utf-8-sig")), "old23b.csv")},
                content_type="multipart/form-data", follow_redirects=True)
        assert db.get_rule(1)["promo_auto_add"] == "on", "старые колонки (3+AUTO) должны дать 'on'"

        # файл этапа 1 (15 колонок): настройку автодобавления не трогаем
        old1_head = _row("product_id", "Артикул", "Название", "Цена", "Контролируемая цена",
                         "Режим (watch/guard/hold)", "Минимум", "Максимум", "Цена удержания", "Допуск %",
                         "Под MPSpace (1 = да)", "Тип контроля (seller/buyer)",
                         "Стратегия при максимуме (0/1)", "Теги", "Остаток (шт.)")
        db.save_rule(1, promo_auto_add="on")
        old1_row = old1_head + "\n" + _row(1, "CSV-1", "Товар", 1000, 1000, "guard", 900, "", "", 2,
                                           "", "", "", "", "")
        r = cl.post("/rules/import",
                    data={"file": (io.BytesIO(old1_row.encode("utf-8-sig")), "old1.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
        assert db.get_rule(1)["promo_auto_add"] == "on", "файл этапа 1 затёр настройку автодобавления"


def test_sync_stores_auto_add_enabled():
    """Синхронизация цен сохраняет products.auto_add_enabled из ответа /v5/product/info/prices."""
    with Ctx() as c:
        for pid, s in MockOzonClient._state.items():
            put(pid, s["price"])
        n = c.rep.sync(c.mock, db.get_settings(), force_actions=True)
        assert n > 0, "sync не прочитал товары"
        rows = db.q("SELECT product_id, auto_add_enabled FROM products WHERE active=1")
        assert rows and all(r["auto_add_enabled"] in (0, 1) for r in rows), rows
        for r in rows:
            want = 1 if MockOzonClient._state[r["product_id"]].get("auto_add_enabled") else 0
            assert r["auto_add_enabled"] == want, (r, want)


def test_guard_block_does_not_break_list():
    """Замечание 1 (критично): товар на паузе (план заблокирован guard_against_loops)
    + товар с promo_auto_add='off' — в to_send не попадает None и import_prices не падает,
    цена другого товара меняется во всех 4 запусках подряд."""
    with Ctx() as c:
        pids = sorted(MockOzonClient._state)
        pa, pb, pc = pids[0], pids[1], pids[2]
        db.set_setting("cooldown_min", "60")
        # A — «на паузе»: последнее изменение цены записано только что
        put(pa, 1000)
        db.save_rule(pa, mode="guard", min_price=1200, max_price=1200)
        db.ex("INSERT INTO changes(ts, product_id, offer_id, controlled_before, price_before, price_after, "
              "old_price_after, min_after, reason, source, dry_run, ok, error) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,0,1,NULL)",
              (db.now(), pa, f"T-{pa}", 1000, 1000, 1200, 1200, None, "тест", "авто"))
        # B — меняется только автодобавление (off), цена без изменений
        put(pb, 1000, auto_add_enabled=1)
        db.save_rule(pb, mode="watch", promo_auto_add="off")
        db.save_rule(pc, mode="guard", min_price=1200, max_price=1200)
        sent_all = []
        for i in range(4):
            put(pc, 400 if i % 2 == 0 else 700)
            db.ex("DELETE FROM changes WHERE product_id=?", (pc,))  # снимаем паузу только у C
            cl = RecordingClient(c.mock)
            out = c.rep.apply_rules(cl, db.get_settings())  # раньше здесь падал import_prices
            assert not any(b is None for b in cl.sent), "None не должен попадать в import_prices"
            assert out[1] == 0, out  # errors == 0: список не «упал» из-за паузного товара
            sent_all += cl.sent
        prices_c = [b["price"] for b in sent_all if b["product_id"] == int(pc)]
        assert len(prices_c) == 4 and set(prices_c) == {"1200"}, prices_c


def test_training_auto_log_throttle():
    """Замечание 3: в тренировке запись «что сделал бы — автодобавление» не чаще раза в 6 часов
    на товар; при изменении настройки — новая запись сразу."""
    with Ctx() as c:
        pid = sorted(MockOzonClient._state)[0]
        db.set_setting("dry_run", "1")
        put(pid, 1000, auto_add_enabled=0)
        db.save_rule(pid, mode="hold", target=900, promo_auto_add="on")
        for _ in range(4):
            c.rep.apply_rules(RecordingClient(c.mock), db.get_settings())
        rows = db.q("SELECT reason FROM changes WHERE dry_run=1 AND source='акции: автодобавление' "
                    "AND reason LIKE '%что сделал бы%'")
        assert len(rows) == 1, rows
        # настройка изменена — «что сделал бы» пишется и вскоре (новое направление)
        db.save_rule(pid, mode="hold", target=900, promo_auto_add="off")
        put(pid, 1000, auto_add_enabled=1)
        c.rep.apply_rules(RecordingClient(c.mock), db.get_settings())
        rows2 = db.q("SELECT reason FROM changes WHERE dry_run=1 AND source='акции: автодобавление' "
                     "AND reason LIKE '%что сделал бы%'")
        assert len(rows2) == 2, len(rows2)
        assert any("выключить" in r["reason"] for r in rows2), rows2


def test_no_price_auto_not_sent():
    """Замечание 4: у товара без цены (price=0) автодобавление не отправляется,
    в журнал пишется «нет цены» (и в тренировке, и в боевом режиме)."""
    with Ctx() as c:
        pid = sorted(MockOzonClient._state)[0]
        db.save_rule(pid, mode="watch", promo_auto_add="on")
        # боевой режим
        put(pid, 0, auto_add_enabled=0)
        cl = RecordingClient(c.mock)
        out = c.rep.apply_rules(cl, db.get_settings())
        assert out[3] == 0 and not cl.sent, cl.sent
        row = db.q("SELECT reason, ok FROM changes WHERE source='акции: автодобавление' "
                   "ORDER BY id DESC LIMIT 1", one=True)
        assert row and "нет цены" in row["reason"] and row["ok"] == 1, row
        # тренировка — запись «нет цены» тоже есть, отправки нет
        db.set_setting("dry_run", "1")
        cl2 = RecordingClient(c.mock)
        out = c.rep.apply_rules(cl2, db.get_settings())
        assert out[0] == 0 and out[3] == 0 and not cl2.sent, (out, cl2.sent)
        row2 = db.q("SELECT reason, dry_run FROM changes WHERE source='акции: автодобавление' "
                    "ORDER BY id DESC LIMIT 1", one=True)
        assert row2 and "нет цены" in row2["reason"] and row2["dry_run"] == 1, row2


def test_auto_not_applied_throttle_and_stop():
    """Замечание 2: Ozon принял запрос, но products.auto_add_enabled так и не совпал с настройкой.
    Повтор не чаще раза в 6 часов на товар (4 запуска → 1 отправка); после 2 неудач —
    «Требует внимания», событие и остановка попыток до изменения настройки пользователем."""
    with Ctx() as c:
        pid = sorted(MockOzonClient._state)[0]
        db.set_setting("cooldown_min", "0")
        db.save_rule(pid, mode="watch", promo_auto_add="off")
        put(pid, 1000, auto_add_enabled=1)
        cl = RecordingClient(c.mock)
        assert c.rep.apply_rules(cl, db.get_settings())[3] == 1
        # «Ozon не применил»: после каждой синхронизации значение вновь 1 (прошло < 6 часов)
        for _ in range(3):
            put(pid, 1000, auto_add_enabled=1)
            cl2 = RecordingClient(c.mock)
            c.rep.apply_rules(cl2, db.get_settings())
            assert not [b for b in cl2.sent if "auto_add_to_ozon_actions_list_enabled" in b], \
                "повторная отправка раньше 6 часов"
        # прошло 6 часов, настройка так и не совпала — вторая попытка
        old = (datetime.now() - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
        db.save_auto_add_state(pid, last_attempt=old, fail_count=0, stopped=0, setting="off")
        put(pid, 1000, auto_add_enabled=1)
        cl3 = RecordingClient(c.mock)
        assert c.rep.apply_rules(cl3, db.get_settings())[3] == 1
        assert any("auto_add_to_ozon_actions_list_enabled" in b for b in cl3.sent), cl3.sent
        # ещё 6 часов и вторая неудача — попытки прекращены
        db.save_auto_add_state(pid, last_attempt=old, fail_count=1, stopped=0, setting="off")
        put(pid, 1000, auto_add_enabled=1)
        cl4 = RecordingClient(c.mock)
        assert c.rep.apply_rules(cl4, db.get_settings())[3] == 0
        assert not cl4.sent, cl4.sent
        st = db.get_auto_add_state(pid)
        assert st and st["stopped"] == 1 and st["fail_count"] == 2, st
        p = db.q("SELECT * FROM products WHERE product_id=?", (pid,), one=True)
        atn = engine.attention_for(p, db.get_rule(pid), db.get_settings())
        assert "Ozon не применяет настройку автодобавления" in atn, atn
        warns = " ".join(e["message"] for e in db.q("SELECT message FROM events WHERE level='warn'"))
        assert "Ozon не применяет настройку автодобавления" in warns, warns
        # ещё один запуск — не отправляем
        put(pid, 1000, auto_add_enabled=1)
        cl5 = RecordingClient(c.mock)
        assert c.rep.apply_rules(cl5, db.get_settings())[3] == 0
        assert not cl5.sent
        # настройка изменена пользователем — состояние сброшено, попытки возобновлены
        db.save_rule(pid, mode="watch", promo_auto_add="on")
        put(pid, 1000, auto_add_enabled=0)
        cl6 = RecordingClient(c.mock)
        assert c.rep.apply_rules(cl6, db.get_settings())[3] == 1
        assert any("auto_add_to_ozon_actions_list_enabled" in b for b in cl6.sent), cl6.sent


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
            print(f"FAIL  {t.__name__} (исключение): {type(e).__name__}: {e}")
            print(traceback.format_exc())
    print(f"\n{len(tests) - failed}/{len(tests)} тестов прошло")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
