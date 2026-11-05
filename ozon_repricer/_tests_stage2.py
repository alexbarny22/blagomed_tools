# -*- coding: utf-8 -*-
"""Тесты этапа 2 — поддержка акций Ozon.

Проверяют: чтение акций по актуальным методам v2 (пагинация last_id/offset, limit<=100),
решения promo_decisions, sync, авто/ручные входы и выходы. Запись (вход/выход в Ozon) пока
не реализована — с 19.10.2026 Ozon отключил методы акций v1, новые не подтверждены, поэтому
всё, что движок считает «войти/выйти», фиксируется в журнале ТОЛЬКО как рекомендации (dry_run=1).

Без внешних зависимостей: простые функции, запуск:  python _tests_stage2.py
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
from core.ozon import MockOzonClient, OzonClient  # noqa: E402

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


def test_promo_decisions():
    with Ctx() as c:
        put_product(101, 2000.0)
        put_product(102, 700.0)
        put_product(103, 700.0)
        put_product(104, 700.0)
        put_product(105, 700.0)
        put_product(106, 700.0)
        put_product(107, 700.0)
        db.save_actions([
            act_row(101, 63301, "participant", 1950, price=2000),      # участник, цена допустима
            act_row(102, 63301, "participant", 300, price=600),        # участник, цена ниже минимума
            act_row(103, 63301, "candidate", 1950, price=2000),        # кандидат, выгоден (цель 1800)
            act_row(104, 63301, "candidate", 1950, price=2000),        # кандидат, режим RECOMMEND
            act_row(105, 63301, "candidate", 300, price=700),          # кандидат, ниже минимума — запрет
            act_row(106, 63301, "participant", 1950, price=2000),      # режим 0 — не трогаем
            act_row(107, 63301, "participant", 1950, price=2000),      # режим 1 — выходим
        ])
        s = db.get_settings()

        # участник, цена акции выше допустимой (цель 2000 при допуске 5%: 1900) — остаёмся
        r = db.get_rule(101)
        r.update(promo_mode=3, promo_entry="AUTO", min_price=400, target=2000, promo_deviation_pct=5)
        d = engine.promo_decisions({"product_id": 101}, r, s)[0]
        assert d["decision"] == "keep", d

        # участник с ценой акции ниже минимума селлера — выходим всегда (защита цены, даже в режиме 0)
        db.save_rule(102, min_price=500)
        d = engine.promo_decisions({"product_id": 102}, db.get_rule(102), s)[0]
        assert d["decision"] == "exit", d

        # кандидат: вход выгоден (цель 1800, цена акции 1950, минимум 500)
        db.save_rule(103, promo_mode=3, promo_entry="AUTO", min_price=500, target=1800)
        d = engine.promo_decisions({"product_id": 103}, db.get_rule(103), s)[0]
        assert d["decision"] == "enter", d

        # кандидат при режиме «только рекомендации» — hint, не автоматический вход
        db.save_rule(104, promo_mode=3, promo_entry="RECOMMEND", min_price=500, target=2000)
        d = engine.promo_decisions({"product_id": 104}, db.get_rule(104), s)[0]
        assert d["decision"] == "hint", d

        # кандидат с ценой акции ниже минимума селлера — вход запрещён
        db.save_rule(105, promo_mode=3, promo_entry="AUTO", min_price=500, target=2000)
        d = engine.promo_decisions({"product_id": 105}, db.get_rule(105), s)[0]
        assert d["decision"] == "ban", d

        # режим 0 — акции не трогаем (участник остаётся)
        db.save_rule(106, promo_mode=0)
        d = engine.promo_decisions({"product_id": 106}, db.get_rule(106), s)[0]
        assert d["decision"] == "keep", d

        # режим 1 — выходим из акций
        db.save_rule(107, promo_mode=1)
        d = engine.promo_decisions({"product_id": 107}, db.get_rule(107), s)[0]
        assert d["decision"] == "exit", d


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


def test_apply_promo_recommends_only():
    """Автовход/автовыход даже в боевом режиме пишут ТОЛЬКО рекомендации, мок не меняется."""
    with Ctx() as c:
        par = {p["id"]: p for p in c.mock.get_action_products(63301)}
        cand = {p["id"]: p for p in c.mock.get_action_candidates(63302)}
        assert par and cand
        pid_enter = sorted(cand)[0]
        pid_exit = sorted(par)[0]
        s_price = MockOzonClient._state
        put_product(pid_enter, s_price[pid_enter]["price"])
        put_product(pid_exit, s_price[pid_exit]["price"])

        # вход: кандидат из «Умной скидки» (цель 1800, цена акции 1950)
        db.save_rule(pid_enter, promo_mode=3, promo_entry="AUTO", min_price=500, target=1800)
        # выход: участник с ценой акции ниже минимума 500 — защитный выход
        db.save_rule(pid_exit, promo_mode=1, min_price=500)
        db.save_actions([
            act_row(pid_enter, 63302, "candidate", 1950, price=s_price[pid_enter]["price"]),
            act_row(pid_exit, 63301, "participant", 300, price=s_price[pid_exit]["price"]),
        ])

        done = c.rep._apply_promo(c.mock, db.get_settings(), dry=False)
        assert done == 2, f"ожидали 2 рекомендации, записано {done}"

        # фактического входа/выхода НЕТ — мок не изменился
        assert pid_enter in {p["id"] for p in c.mock.get_action_candidates(63302)}, "попал в участники?"
        assert pid_enter not in {p["id"] for p in c.mock.get_action_products(63302)}
        assert pid_exit in {p["id"] for p in c.mock.get_action_products(63301)}, "вышел из акции?"

        ch = db.q("SELECT source, dry_run, ok, reason FROM changes ORDER BY ts")
        sources = {(r["source"], r["dry_run"], r["ok"]) for r in ch}
        assert ("акция: вход", 1, 1) in sources, sources
        assert ("акция: выход", 1, 1) in sources, sources
        assert all("только рекомендация" in (r["reason"] or "") for r in ch if r["source"].startswith("акция")), \
            [r["reason"] for r in ch]


def test_apply_promo_dryrun():
    with Ctx() as c:
        cand = {p["id"]: p for p in c.mock.get_action_candidates(63302)}
        pid_enter = sorted(cand)[0]
        put_product(pid_enter, MockOzonClient._state[pid_enter]["price"])
        db.save_rule(pid_enter, promo_mode=3, promo_entry="AUTO", min_price=500, target=1800)
        db.save_actions([act_row(pid_enter, 63302, "candidate", 1950, price=2000.0)])
        db.set_setting("dry_run", "1")

        done = c.rep._apply_promo(c.mock, db.get_settings(), dry=True)
        assert done == 1
        # в мок вход НЕ произошёл, но в журнал запись есть
        assert pid_enter not in {p["id"] for p in c.mock.get_action_products(63302)}
        ch = db.q("SELECT dry_run, ok FROM changes", one=True)
        assert ch and ch["dry_run"] == 1 and ch["ok"] == 1


def test_action_manual_recommends_only():
    """Ручной вход/выход НЕ трогает Ozon: рекомендация в журнал (dry_run=1), мок без изменений."""
    with Ctx() as c:
        put_product(900001, 2000.0)
        db.save_rule(900001, mode="guard", min_price=100)
        db.save_actions([act_row(900001, 63302, "candidate", 1950, price=2000.0)])

        ok, msg = c.rep.action_manual(900001, 63302, "enter")
        assert ok, msg
        assert "рекомендация" in msg.lower(), msg
        # товар НЕ вошёл в акцию
        assert 900001 not in {p["id"] for p in c.mock.get_action_products(63302)}
        assert 900001 in {p["id"] for p in c.mock.get_action_candidates(63302)}

        ok, msg = c.rep.action_manual(900001, 63302, "exit")
        assert ok, msg
        ch = db.q("SELECT source, dry_run, ok FROM changes ORDER BY ts")
        assert [r["source"] for r in ch] == ["акция: вход (вручную)", "акция: выход (вручную)"], ch
        assert all(r["dry_run"] == 1 and r["ok"] == 1 for r in ch)


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
        db.save_rule(500001, promo_mode=3, promo_entry="AUTO", min_price=400)
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
        assert r1["promo_mode"] == 3 and r1["promo_entry"] == "AUTO", "старый CSV затёр промо-настройки"
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

        # массовый вход в акцию locked-товары пропускает
        r = cl.post("/api/action/batch", json={"ids": [p_a, p_b], "action_id": 63302, "op": "enter"})
        assert r.status_code == 200 and r.json["done"] == 0 and r.json["skipped"] == 2

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
        # p_a — повышение цены до минимума (шаг 20% < лимита 25%);
        # p_b — выгодный вход в «Умную скидку» (цена акции ≤ 0.8·цены, мин ниже, цель выше)
        db.save_rule(p_a, mode="guard", min_price=round(pa * 1.2), max_price=round(pa * 1.2))
        db.save_rule(p_b, mode="guard", promo_mode=3, promo_entry="AUTO",
                     min_price=round(pb * 0.3), max_price=round(pb * 2), target=round(pb * 1.5))
        c.rep._sync_actions(c.mock)
        before = {pid: MockOzonClient._state[pid]["price"] for pid in (p_a, p_b)}
        db.set_setting("dry_run", "1")
        c.rep.apply_rules(c.mock, db.get_settings())
        after = {pid: MockOzonClient._state[pid]["price"] for pid in (p_a, p_b)}
        assert after == before, "тренировка отправила цену в Ozon!"
        rows = db.q("SELECT source, dry_run, ok FROM changes WHERE ok=1 ORDER BY ts")
        assert rows, "в тренировке не записано ни одного изменения"
        assert all(r["dry_run"] == 1 for r in rows), rows
        assert any(r["source"].startswith("акция") for r in rows), "рекомендации по акциям не попали в журнал"


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