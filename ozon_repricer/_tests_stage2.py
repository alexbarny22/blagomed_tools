# -*- coding: utf-8 -*-
"""Тесты этапа 2 — поддержка акций Ozon (решения promo_decisions, мок-клиент, sync, авто/ручные входы и выходы).

Без внешних зависимостей: простые функции, запуск:  python _tests_stage2.py
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import db, engine  # noqa: E402
from core.ozon import MockOzonClient  # noqa: E402

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

        pid = sorted(cand)[0]
        ok, rej = c.mock.activate_action(63301, [{"product_id": pid, "action_price": 100}])
        assert pid in ok and not rej
        assert pid in {p["id"] for p in c.mock.get_action_products(63301)}
        assert pid not in {p["id"] for p in c.mock.get_action_candidates(63301)}

        ok, rej = c.mock.deactivate_action(63301, [pid])
        assert pid in ok and not rej
        assert pid not in {p["id"] for p in c.mock.get_action_products(63301)}


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


def test_apply_promo_auto():
    with Ctx() as c:
        # участник и кандидат есть в начале
        par = {p["id"]: p for p in c.mock.get_action_products(63301)}
        cand = {p["id"]: p for p in c.mock.get_action_candidates(63302)}
        assert par and cand
        pid_enter = sorted(cand)[0]
        pid_exit = sorted(par)[0]
        s_price = MockOzonClient._state
        put_product(pid_enter, s_price[pid_enter]["price"])
        put_product(pid_exit, s_price[pid_exit]["price"])

        # вход: кандидат из «Умной скидки» (цель 1800, цена акции 1950) — вошёл автоматически
        db.save_rule(pid_enter, promo_mode=3, promo_entry="AUTO", min_price=500, target=1800)
        # выход: участник с ценой акции ниже минимума 500 — защитный выход
        db.save_rule(pid_exit, promo_mode=1, min_price=500)
        db.save_actions([
            act_row(pid_enter, 63302, "candidate", 1950, price=s_price[pid_enter]["price"]),
            act_row(pid_exit, 63301, "participant", 300, price=s_price[pid_exit]["price"]),
        ])

        done = c.rep._apply_promo(c.mock, db.get_settings(), dry=False)
        assert done == 2, f"ожидали 2 действия, выполнено {done}"

        assert pid_enter in {p["id"] for p in c.mock.get_action_products(63302)}, "не вошёл в акцию"
        assert pid_exit not in {p["id"] for p in c.mock.get_action_products(63301)}, "не вышел из акции"

        ch = db.q("SELECT source, ok FROM changes ORDER BY ts")
        sources = {(r["source"], r["ok"]) for r in ch}
        assert ("акция: вход", 1) in sources, sources
        assert ("акция: выход", 1) in sources, sources


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


def test_action_manual():
    with Ctx() as c:
        put_product(900001, 2000.0)
        db.save_rule(900001, mode="guard", min_price=100)
        db.save_actions([act_row(900001, 63302, "candidate", 1950, price=2000.0)])

        ok, msg = c.rep.action_manual(900001, 63302, "enter")
        assert ok, msg
        assert 900001 in {p["id"] for p in c.mock.get_action_products(63302)}

        ok, msg = c.rep.action_manual(900001, 63302, "exit")
        assert ok, msg
        assert 900001 not in {p["id"] for p in c.mock.get_action_products(63302)}


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