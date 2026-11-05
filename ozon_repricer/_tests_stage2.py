# -*- coding: utf-8 -*-
"""Тесты этапа 2 — поддержка акций Ozon (чтение и синхронизация).

Проверяют: чтение акций по актуальным методам v2 (пагинация last_id/offset, limit<=100),
синхронизацию акций/автодобавлений, интервал перечитывания, защиту от потери данных.
Рекомендации входа/выхода из акций удалены в этапе 2б: с 19.10.2026 Ozon ввёл «единую
предельную цену» (участие решается ценой), отдельные методы входа/выхода не используются;
управляется только автодобавление (см. _tests_stage2b.py).

Без внешних зависимостей: простые функции, запуск:  python _tests_stage2.py
"""
from datetime import datetime
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
from core.ozon import MockOzonClient, OzonClient, OzonError  # noqa: E402

MOCK_TITLE = "Скидка продавца: медтехника"


class Ctx:
    """Временная БД и изолированный экземпляр мок-клиента Ozon."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="rp_test_")
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


def put_product(pid, price):
    db.ex("INSERT INTO products(product_id, offer_id, price, active) VALUES(?,?,?,1)", (pid, f"T-{pid}", float(price)))


def act_row(pid, aid, kind, action_price, price=None, max_action_price=None):
    return (pid, aid, kind, MOCK_TITLE if aid == 63301 else f"акция {aid}", None, None,
            None, price, float(action_price) if action_price else None,
            float(max_action_price) if max_action_price else None)


class FakeOzonClient(OzonClient):
    """OzonClient без сети: POST имитирует живые ответы v2 на основе мок-акций."""

    def __init__(self, mock):
        super().__init__("1", "test-key")
        self.mock = mock
        self.calls = []

    def _post(self, path, body):
        self.calls.append((path, body))
        assert body["limit"] <= 100, "limit акций не должен превышать 100"
        aid = body["action_id"]
        d = MockOzonClient._actions[aid]
        if path == "/v2/actions/products":
            return {"result": self.mock._page(d["participants"], body["limit"], body.get("last_id", ""))}
        if path == "/v2/actions/candidates":
            return {"result": self.mock._page(d["candidates"], body["limit"], body.get("last_id", ""))}
        if path == "/v2/actions/auto-add/products/list":
            rows = [{"product_id": k} for k in d["candidates"]]
            start = body.get("offset", 0)
            page = rows[start:start + body["limit"]]
            return {"result": {"products": page, "total": len(rows)}}
        raise AssertionError(f"неожиданный метод: {path}")


def test_mock_actions():
    with Ctx() as c:
        acts = {a["id"]: a for a in c.mock.get_actions()}
        assert 63301 in acts and acts[63301]["title"]
        cand = {p["id"] for p in c.mock.get_action_candidates(63301)}
        par = {p["id"] for p in c.mock.get_action_products(63301)}
        assert cand and par and not (cand & par)

        # auto-add список (имитация: Ozon «добавит» всех кандидатов, id — поле product_id)
        auto = c.mock.get_auto_add_products(63302)
        assert auto and all("product_id" in p and "action_price_to_auto_add" in p for p in auto)

        # чтение не «мутирует» мок: после двух проходов состав тот же
        assert c.mock.get_action_products(63301) and c.mock.get_action_candidates(63301)
        assert {p["id"] for p in c.mock.get_action_candidates(63301)} == cand


def test_action_pages_v2():
    """Клиент ходит по v2: пагинация last_id (products/candidates) и offset (auto-add), limit<=100."""
    with Ctx() as c:
        cl = FakeOzonClient(c.mock)

        prods = cl.get_action_products(63301)
        assert {p["id"] for p in prods} == set(MockOzonClient._actions[63301]["participants"]), prods
        assert cl.get_action_candidates(63302), "кандидаты должны читаться"

        # маленькая страница (limit=2) — по last_id собирает всех участников
        cl.calls.clear()
        small = cl._action_pages("/v2/actions/products", 63301, limit=2)
        assert {p["id"] for p in small} == set(MockOzonClient._actions[63301]["participants"]), small
        assert all(b["limit"] <= 100 for _, b in cl.calls)
        assert cl.calls[0][1]["last_id"] == ""      # первая страница — с пустым last_id
        assert any(b["last_id"] for _, b in cl.calls[1:])

        # auto-add идёт по offset, без last_id
        cl.calls.clear()
        auto = cl.get_auto_add_products(63302)
        assert auto, "auto-add список пуст"
        auto_calls = [b for p, b in cl.calls if p.endswith("/auto-add/products/list")]
        assert auto_calls and all("offset" in b and "last_id" not in b for b in auto_calls)


def test_sync_actions():
    with Ctx() as c:
        for pid, s in MockOzonClient._state.items():
            put_product(pid, s["price"])
        c.rep._sync_actions(c.mock)
        rows = db.q("SELECT product_id, action_id, kind FROM product_actions")
        assert rows, "sync() не записал участия в акциях"
        got63301_p = {r["product_id"] for r in rows if r["action_id"] == 63301 and r["kind"] == "participant"}
        got63301_c = {r["product_id"] for r in rows if r["action_id"] == 63301 and r["kind"] == "candidate"}
        got63302_c = {r["product_id"] for r in rows if r["action_id"] == 63302 and r["kind"] == "candidate"}
        assert got63301_p == set(MockOzonClient._actions[63301]["participants"])
        assert got63301_c == set(MockOzonClient._actions[63301]["candidates"])
        assert got63302_c == set(MockOzonClient._actions[63302]["candidates"])
        stamped = db.q("SELECT value FROM settings WHERE key='actions_synced_at'", one=True)
        assert stamped and stamped["value"]


def test_migration_from_old_db():
    """Общая проверка задания: миграция базы 2026.11.10 (этап 1) -> схема этапа 2 без потери данных."""
    with Ctx() as c:
        # 1) собираем «старую» базу этапа 1 (нет product_actions и промо-колонок)
        old = os.path.join(c.tmp, "old_repricer.db")
        con = sqlite3.connect(old)
        con.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY, offer_id TEXT, sku INTEGER, name TEXT,
            price REAL, old_price REAL, seller_price REAL, ozon_min_price REAL, buyer_est REAL,
            color_index TEXT, idx_ozon_min REAL, idx_ozon_val REAL, idx_ext_min REAL, idx_ext_val REAL,
            actions TEXT, active INTEGER DEFAULT 1, updated_at TEXT,
            stock_fbo REAL, stock_fbs REAL, stock_fbp REAL, stock_total REAL, image TEXT);
        CREATE TABLE rules (
            product_id INTEGER PRIMARY KEY, mode TEXT DEFAULT 'watch',
            min_price REAL, max_price REAL, target REAL, tolerance REAL DEFAULT 2,
            max_strategy INTEGER DEFAULT 0, control_basis TEXT DEFAULT '', tags TEXT DEFAULT '',
            note TEXT, locked INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0);
        CREATE TABLE price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, ts TEXT,
            price REAL, old_price REAL, seller_price REAL, buyer_est REAL, ozon_min_price REAL, color_index TEXT);
        CREATE TABLE changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, product_id INTEGER, offer_id TEXT,
            controlled_before REAL, price_before REAL, price_after REAL, old_price_after REAL, min_after REAL,
            reason TEXT, source TEXT, dry_run INTEGER, ok INTEGER, error TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT);
        """)
        con.execute("INSERT INTO products(product_id, offer_id, name, price, active) VALUES(777, 'OLD-1', 'Старый товар', 1200.0, 1)")
        con.execute("INSERT INTO rules(product_id, mode, min_price, max_price) VALUES(777, 'guard', 500, 1500)")
        con.execute("INSERT INTO settings(key, value) VALUES('dry_run','0')")
        con.commit()
        con.close()

        # 2) подменяем базу репрайсера на старую и запускаем миграцию (как при запуске app)
        db.DB_PATH = old
        db.init()

        # 3) проверки
        cols = {r["name"] for r in db.q("PRAGMA table_info(rules)")}
        assert {"promo_mode", "promo_entry", "promo_deviation_pct"} <= cols, cols
        assert db.q("SELECT name FROM sqlite_master WHERE type='table' AND name='product_actions'", one=True), \
            "таблица product_actions не создана"
        r = db.get_rule(777)
        assert r["mode"] == "guard" and r["min_price"] == 500 and r["max_price"] == 1500    # данные целы
        assert r["locked"] == 0 and r["tags"] == ""
        assert r["promo_mode"] == 0 and r["promo_entry"] == "RECOMMEND" and r["promo_deviation_pct"] == 0
        assert db.get_settings()["dry_run"] == "0", "миграция этапа 2 не должна включать тренировку"


def test_old_csv_import():
    """Общая проверка задания: импорт СТАРОГО CSV (этап 1, без промо-колонок) не ломается."""
    with Ctx() as c:
        import app as A  # импорт после подмены db.DB_PATH (модульный init() упадёт во временную БД)
        # товар с уже заданными параметрами акций — старый файл не должен их затереть
        put_product(500001, 2000.0)
        db.save_rule(500001, promo_auto_add="on", min_price=400)
        put_product(500002, 1000.0)          # через CSV пометим «Под MPSpace»
        cl = A.app.test_client()
        old_csv = (
            "product_id;Артикул;Название;Цена;Контролируемая цена;Режим (watch/guard/hold);Минимум;Максимум;"
            "Цена удержания;Допуск %;Под MPSpace (1 = да);Тип контроля (seller/buyer);Стратегия при максимуме (0/1);"
            "Теги;Остаток (шт.)\r\n"
            "500001;T-500001;Товар 1;2000;0;guard;600;2500;0;2;;;0;старые-теги;\r\n"
            "500002;T-500002;Товар 2;1000;0;hold;700;3000;1000;2;1;buyer;1;;\r\n")
        r = cl.post("/rules/import",
                    data={"file": (io.BytesIO(old_csv.encode("utf-8-sig")), "old_rules.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
        r1 = db.get_rule(500001)
        assert r1["mode"] == "guard" and r1["min_price"] == 600 and r1["tags"] == "старые-теги"
        assert r1["promo_auto_add"] == "on", "старый CSV затёр настройку автодобавления"
        r2 = db.get_rule(500002)
        assert r2["locked"] == 1 and r2["mode"] == "watch", "«Под MPSpace» из старого CSV не применён/режим изменён"


def test_mpspace_general():
    """Общая проверка задания: отметка «Под MPSpace» держит защиту на уровне API."""
    with Ctx() as c:
        import app as A
        cand = sorted({p["id"] for p in c.mock.get_action_candidates(63302)})
        p_a, p_b = cand[0], cand[1]
        for pid in (p_a, p_b):
            put_product(pid, MockOzonClient._state[pid]["price"])
        cl = A.app.test_client()

        # массовая отметка
        r = cl.post("/api/lock", json={"ids": [p_a, p_b], "locked": True})
        assert r.status_code == 200 and r.json["done"] == 2
        assert db.get_rule(p_a)["locked"] == 1 and db.get_rule(p_b)["locked"] == 1

        # правило с режимом для locked не сохраняется
        r = cl.post("/api/rule", json={"product_id": p_a, "mode": "guard", "min": 500, "max": 2000})
        assert r.status_code == 400

        # вход/выход в акции удалён из интерфейса и API (этап 2б: Ozon решает участие ценой)
        assert cl.post(f"/api/action/{p_a}", json={"action_id": 63302, "op": "enter"}).status_code == 404
        assert cl.post("/api/action/batch", json={"ids": [p_a, p_b], "action_id": 63302,
                                                  "op": "enter"}).status_code == 404

        # ручная цена под MPSpace — только с force
        r = cl.post(f"/api/manual/{p_a}", json={"price": 1200})
        assert r.status_code == 409 and r.json.get("need_force")

        # снятие отметки разблокирует правки
        cl.post("/api/lock", json={"ids": [p_a], "locked": False})
        r = cl.post("/api/rule", json={"product_id": p_a, "mode": "guard", "min": 500, "max": 2000})
        assert r.status_code == 200


def test_training_price_dryrun():
    """Общая проверка задания: тренировка (dry_run=1) — цены в Ozon не отправляются, в журнале dry_run=1."""
    with Ctx() as c:
        cand = sorted({p["id"] for p in c.mock.get_action_candidates(63302)})
        p_a, p_b = cand[0], cand[1]
        for pid in (p_a, p_b):
            put_product(pid, MockOzonClient._state[pid]["price"])
        pa, pb = MockOzonClient._state[p_a]["price"], MockOzonClient._state[p_b]["price"]
        db.save_rule(p_a, mode="guard", min_price=round(pa * 1.2), max_price=round(pa * 1.2))
        db.save_rule(p_b, mode="guard", min_price=round(pb * 0.3), max_price=round(pb * 2))
        before = {pid: MockOzonClient._state[pid]["price"] for pid in (p_a, p_b)}
        db.set_setting("dry_run", "1")
        c.rep.apply_rules(c.mock, db.get_settings())
        after = {pid: MockOzonClient._state[pid]["price"] for pid in (p_a, p_b)}
        assert after == before, "тренировка отправила цену в Ozon!"
        rows = db.q("SELECT source, dry_run, ok FROM changes WHERE ok=1 ORDER BY ts")
        assert rows, "в тренировке не записано ни одного изменения"
        assert all(r["dry_run"] == 1 for r in rows), rows


def test_sync_actions_error_does_not_break_run():
    """Общая проверка 1: ValueError при чтении одной акции НЕ роняет run_once,
    правила цен применяются, в «События» пишется предупреждение."""
    with Ctx() as c:
        for pid, s in MockOzonClient._state.items():
            put_product(pid, s["price"])
        cand = sorted({p["id"] for p in c.mock.get_action_candidates(63302)})
        p_a = cand[0]
        price_a = MockOzonClient._state[p_a]["price"]
        db.save_rule(p_a, mode="guard", min_price=round(price_a * 1.2), max_price=round(price_a * 1.2))
        db.set_setting("dry_run", "1")

        class Fragile(MockOzonClient):
            def get_action_candidates(self, action_id):
                if action_id == 63302:
                    raise ValueError("сломанные кандидаты")
                return super().get_action_candidates(action_id)

        old_make = engine.make_client
        engine.make_client = lambda *a, **k: Fragile(*a, **k)
        try:
            res = c.rep.run_once()
        finally:
            engine.make_client = old_make
        assert "Ошибка" not in res, res
        # правила цен применились (тренировка: в журнал попали записи dry_run=1)
        rows = db.q("SELECT source, dry_run, ok FROM changes WHERE ok=1 ORDER BY ts")
        assert rows, "после ошибки акций цены не проверялись"
        assert all(r["dry_run"] == 1 for r in rows), rows
        warns = " ".join(r["message"] for r in db.q("SELECT message FROM events WHERE level='warn'"))
        assert "63302" in warns, warns


def test_sync_actions_keeps_rows_for_failed_and_gone():
    """Общая проверка 2: OzonError по одной акции — её прежние строки в product_actions
    сохраняются; удаляются только акции, которых нет в /v1/actions или с истёкшим date_end."""
    with Ctx() as c:
        for pid, s in MockOzonClient._state.items():
            put_product(pid, s["price"])
        c.rep._sync_actions(c.mock, force=True)
        def key(aid, kind):
            return {(r["product_id"], r["action_id"], r["kind"]) for r in db.q(
                "SELECT product_id, action_id, kind FROM product_actions WHERE action_id=? AND kind=?", (aid, kind))}
        before_63301 = key(63301, "participant")
        before_63302 = key(63302, "candidate")
        assert before_63301 and before_63302

        class Fragile(MockOzonClient):
            def get_action_candidates(self, action_id):
                if action_id == 63302:
                    raise OzonError("510: временный сбой чтения кандидатов")
                return super().get_action_candidates(action_id)

        c.rep._sync_actions(Fragile("1", "test-key"), force=True)
        # прежние строки упавшей акции целы
        assert key(63302, "candidate") == before_63302, "строки упавшей акции стёрты"
        # 63301 перечитана нормально
        assert key(63301, "participant") == before_63301
        warns = " ".join(r["message"] for r in db.q("SELECT message FROM events WHERE level='warn'"))
        assert "63302" in warns and "временный сбой" in warns, warns

        # акция 63301 исчезла из /v1/actions — её строки удаляются полностью
        saved = MockOzonClient._actions
        MockOzonClient._actions = {aid: a for aid, a in saved.items() if aid != 63301}
        try:
            c.rep._sync_actions(c.mock, force=True)
            rest = {r["action_id"] for r in db.q("SELECT DISTINCT action_id FROM product_actions")}
            assert rest == {63302}, rest
            assert not db.q("SELECT 1 FROM auto_add WHERE action_id=63301"), "auto_add 63301 не удалён"
        finally:
            MockOzonClient._actions = saved


def test_many_active_actions_warn():
    """Общая проверка 3: если активных акций больше 40 — предупреждение в «События»."""
    with Ctx() as c:
        for pid, s in MockOzonClient._state.items():
            put_product(pid, s["price"])
        saved = MockOzonClient._actions
        base = dict(saved)
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(45):  # 45 дополнительных активных акций
            base[70100 + i] = {"id": 70100 + i, "title": f"акция {i}", "date_start": now,
                               "date_end": "", "status": "STARTED", "participants": {}, "candidates": {}}
        MockOzonClient._actions = base
        try:
            c.rep._sync_actions(c.mock, force=True)
            warns = " ".join(r["message"] for r in db.q("SELECT message FROM events WHERE level='warn'"))
            assert "больше 40" in warns, warns
        finally:
            MockOzonClient._actions = saved


def test_actions_interval_and_prices_every_run():
    """Общая проверка 4: акции перечитываются не чаще actions_interval_min (по умолчанию 120)
    и при force; решения по ценам считаются на каждом запуске."""
    with Ctx() as c:
        for pid, s in MockOzonClient._state.items():
            put_product(pid, s["price"])
        db.set_setting("actions_interval_min", "120")
        c.rep._sync_actions(c.mock, force=True)
        stamped1 = db.get_settings()["actions_synced_at"]
        assert stamped1, "actions_synced_at не установлена"
        cand34 = MockOzonClient._actions[63302]["candidates"]
        p_a = next(iter(cand34))
        removed = cand34.pop(p_a)
        try:
            # без force — перечитывать ещё рано, таблица и метка не меняются
            c.rep._sync_actions(c.mock)
            rows_a = db.q("SELECT 1 FROM product_actions WHERE action_id=63302 AND kind='candidate' "
                          "AND product_id=?", (p_a,))
            assert rows_a, "акции перечитаны раньше интервала"
            assert db.get_settings()["actions_synced_at"] == stamped1
            # с force — перечитываются сразу
            c.rep._sync_actions(c.mock, force=True)
            rows_b = db.q("SELECT 1 FROM product_actions WHERE action_id=63302 AND kind='candidate' "
                          "AND product_id=?", (p_a,))
            assert not rows_b, "force-синхронизация не перечитала акции"
        finally:
            cand34[p_a] = removed
        # цены решаются на каждом запуске независимо от интервала акций
        db.save_rule(p_a, mode="guard", min_price=round(MockOzonClient._state[p_a]["price"] * 1.2),
                     max_price=round(MockOzonClient._state[p_a]["price"] * 1.2))
        db.set_setting("dry_run", "1")
        before = len(db.q("SELECT 1 FROM changes"))
        sent, errors, skipped, promo_done = c.rep.apply_rules(c.mock, db.get_settings())
        assert len(db.q("SELECT 1 FROM changes")) > before, "правила цен не сработали на пустом запуске"


def test_run_now_button_forces_actions():
    """Общая проверка: кнопка «Проверить сейчас» (base.html → POST /api/run) заставляет
    перечитать акции даже внутри actions_interval_min; флаг force_actions потребляется run_once."""
    with Ctx() as c:
        import app as A
        for pid, s in MockOzonClient._state.items():
            put_product(pid, s["price"])
        db.set_setting("actions_interval_min", "120")
        A.rp._sync_actions(c.mock, force=True)  # «сейчас» — полная синхронизация со штампом
        assert db.get_settings()["actions_synced_at"], "actions_synced_at не установлена"
        cand34 = MockOzonClient._actions[63302]["candidates"]
        p_a = next(iter(cand34))
        removed = cand34.pop(p_a)
        try:
            # внутри интервала обычный запуск акции НЕ перечитывает
            A.rp._sync_actions(c.mock)
            assert db.q("SELECT 1 FROM product_actions WHERE action_id=63302 AND kind='candidate' "
                        "AND product_id=?", (p_a,)), "обычный запуск перечитал акции раньше интервала"
            # кнопка «Проверить сейчас» → /api/run взводит force_actions
            A.rp.force_actions = False
            r = A.app.test_client().post("/api/run")
            assert r.status_code == 200 and r.json.get("ok") is True, r
            assert A.rp.force_actions is True, "/api/run не взвёл force_actions"
            # run_once потребляет флаг и перечитывает акции
            res = A.rp.run_once()
            assert "Ошибка" not in res, res
            assert A.rp.force_actions is False, "force_actions не сброшен после run_once"
            assert not db.q("SELECT 1 FROM product_actions WHERE action_id=63302 AND kind='candidate' "
                            "AND product_id=?", (p_a,)), "«Проверить сейчас» не перечитало акции"
        finally:
            cand34[p_a] = removed


def test_auto_add_in_product_card():
    """Общая проверка 5: в карточке товара — «Ozon добавит сам по цене X», метка при цене
    ниже минимума; ошибка метода автодобавления — только предупреждение."""
    with Ctx() as c:
        import app as A
        for pid, s in MockOzonClient._state.items():
            put_product(pid, s["price"])
        c.rep._sync_actions(c.mock, force=True)
        aa = db.q("SELECT product_id, action_id, price_to_auto_add FROM auto_add")
        assert aa, "автодобавления не загружены в auto_add"
        row = aa[0]
        pid, price = row["product_id"], row["price_to_auto_add"]
        cl = A.app.test_client()
        html = cl.get(f"/product/{pid}").get_data(as_text=True)
        assert "Ozon добавит сам по цене" in html, "блок автодобавления не показан"
        assert f"по цене <b>{A.fmt(price)} ₽</b>" in html, html
        # цена автодобавления ниже минимума селлера — метка в таблице акций
        # и причина «Требует внимания» в карточке
        db.save_rule(pid, mode="guard", min_price=price + 1)
        html2 = cl.get(f"/product/{pid}").get_data(as_text=True)
        assert "ниже вашего минимума" in html2, html2
        assert "Ozon добавит в акцию ниже минимума" in html2
        # ошибка метода автодобавления — только предупреждение, карточка живёт
        class FragileAA(MockOzonClient):
            def get_auto_add_products(self, action_id):
                if action_id == 63302:
                    raise KeyError("no 'result' in response")
                return super().get_auto_add_products(action_id)

        c.rep._sync_actions(FragileAA("1", "test-key"), force=True)
        warns = " ".join(r["message"] for r in db.q("SELECT message FROM events WHERE level='warn'"))
        assert "автодобавление не получено" in warns, warns
        assert db.q("SELECT 1 FROM auto_add WHERE product_id=?", (pid,)), "старые auto_add стёрты"
        assert "Ozon добавит сам по цене" in cl.get(f"/product/{pid}").get_data(as_text=True)


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
    print(f"\n{len(tests) - failed}/{len(tests)} тестов прошло")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())