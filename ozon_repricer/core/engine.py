"""Логика репрайсера: синхронизация, история, проверка правил, автовозврат и удержание цены."""
import json
import math
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import db
from .ozon import make_client, OzonError

MODES = {"watch": "Только следить", "guard": "Держать в коридоре", "hold": "Держать цену"}


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def main_image(inf: dict) -> str:
    """Главное фото товара из ответа /v3/product/info/list (поле бывает строкой, списком или объектом)."""
    def first(v):
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            for x in v:
                r = first(x)
                if r:
                    return r
        if isinstance(v, dict):
            return first(v.get("primary")) or first(v.get("url")) or first(v.get("images"))
        return ""
    return first(inf.get("primary_image")) or first(inf.get("images")) or first(inf.get("color_image")) or ""


def buyer_estimate(item: dict):
    """Расчётная цена покупателя по индексу цен (Ozon больше не отдаёт её напрямую)."""
    pi = item.get("price_indexes") or {}
    for key in ("ozon_index_data", "external_index_data", "self_marketplaces_index_data"):
        d = pi.get(key) or {}
        comp, idx = _f(d.get("min_price")), _f(d.get("price_index_value"))
        if comp and idx and comp > 0 and idx > 0:
            if idx < 1:
                return round(idx * comp)
            if idx > 1 and idx < 2:
                return round(comp / (2 - idx))
            return round(comp)
    return None


def control_basis(rule, s):
    """Тип контроля для товара: у правила приоритет над общей настройкой."""
    if rule:
        b = rule.get("control_basis")
        if b in ("seller", "buyer"):
            return b
    if s:
        b = s.get("control_basis")
        if b in ("seller", "buyer"):
            return b
    return "seller"


def max_strategy_of(rule):
    """Стратегия при максимуме: 0 — не превышать максимум, 1 — строгое удержание."""
    try:
        return 1 if int(rule.get("max_strategy") or 0) == 1 else 0
    except (TypeError, ValueError):
        return 0


def effective_max(mx, rule, s):
    """Верхняя граница цены селлера: максимум или жёсткий предел при стратегии «строгое удержание»."""
    if not mx:
        return None
    if max_strategy_of(rule) != 1:
        return mx
    strict = _f(s.get("strict_overshoot_pct"))
    if strict is None:
        strict = 100
    return mx * (1 + strict / 100)


def controlled_price(p: dict, rule=None, s=None):
    """Какую цену контролируем: со скидками продавца (точно) или расчётную покупательскую (≈).
    Тип контроля — у правила товара, иначе из настроек."""
    if isinstance(rule, str):  # совместимость: раньше сюда передавали строку с типом контроля
        basis = rule if rule in ("seller", "buyer") else "seller"
    else:
        basis = control_basis(rule, s)
    if basis == "buyer" and p.get("buyer_est"):
        return p["buyer_est"]
    sp = p.get("seller_price")
    if sp and sp > 0:
        return sp
    return p.get("price")


def status_of(p: dict, rule: dict, s):
    """Статус товара. Мин/Макс — границы цены селлера (того, что отправляем в Ozon),
    цель удержания — для контролируемой цены."""
    c = controlled_price(p, rule, s)
    price = p.get("price")
    mn, mx, tg = _f(rule.get("min_price")), _f(rule.get("max_price")), _f(rule.get("target"))
    if not price or price <= 0 or c is None:
        return "nodata", "нет цены"
    hi = effective_max(mx, rule, s)
    if mn and price < mn - 0.5:
        return "low", f"цена ниже минимума на {round(mn - price)} ₽"
    if hi and price > hi + 0.5:
        if mx and hi > mx + 0.5:
            return "high", f"цена выше жёсткого предела на {round(price - hi)} ₽"
        return "high", f"цена выше максимума на {round(price - mx)} ₽"
    if rule.get("mode") == "hold" and tg:
        tol = _f(rule.get("tolerance")) or 2
        dev = (c - tg) / tg * 100
        if abs(dev) > tol:
            ratio = min(max((c or 0) / price, 0.2), 1.2)
            needed = tg / ratio
            if mx and needed > mx + 0.5:
                if hi and needed > hi + 0.5:
                    return "hitmax", "цель недостижима: упёрлись в жёсткий предел максимума"
                return "hitmax", "цель недостижима: упёрлись в максимум"
            return "off", f"отклонение от цели {dev:+.1f}%"
    if not (mn or mx or (rule.get("mode") == "hold" and tg)):
        return "norule", "правила не заданы"
    return "ok", "в норме"


def attention_for(p: dict, rule: dict, s):
    """Причины «требует внимания» для товара. Пустой список — всё в порядке."""
    reasons = []
    price = p.get("price")
    mn, mx, tg = _f(rule.get("min_price")), _f(rule.get("max_price")), _f(rule.get("target"))
    hi = effective_max(mx, rule, s)
    if mn and price and price < mn - 0.5:
        reasons.append("цена ниже минимума селлера")
    if hi and price and price > hi + 0.5:
        reasons.append("цена выше максимума" if hi == mx else "цена выше жёсткого предела максимума")
    if rule.get("mode") == "hold" and tg and price:
        c = controlled_price(p, rule, s)
        if c:
            tol = _f(rule.get("tolerance")) or 2
            dev = (c - tg) / tg * 100
            if abs(dev) > tol and mx:
                ratio = min(max(c / price, 0.2), 1.2)
                if tg / ratio > mx + 0.5:
                    reasons.append("цель не достигается из-за максимума")
    blocked = db.q("SELECT ts FROM changes WHERE product_id=? AND source='отложено' AND ok=0 "
                   "AND error LIKE '%акция с фиксированной%' ORDER BY id DESC LIMIT 1",
                   (p["product_id"],), one=True)
    if blocked:
        reasons.append("изменение отложено: акция с фиксированной ценой")
    err = db.q("SELECT ts FROM changes WHERE product_id=? AND ok=0 AND dry_run=0 AND source!='отложено' "
               "ORDER BY id DESC LIMIT 1", (p["product_id"],), one=True)
    if err:
        reasons.append("ошибка при последней отправке цены в Ozon")
    if rule.get("mode") in ("guard", "hold") and not rule.get("locked"):
        if p.get("stock_total") is not None and p["stock_total"] == 0:
            reasons.append("остаток 0")
    if (p.get("color_index") or "").upper() == "RED":
        reasons.append("красный индекс цен")
    return reasons


def old_price_for(new_price: float, cur_price: float, cur_old: float) -> float:
    """Зачёркнутая цена: сохраняем прежний размер «скидки» и правила Ozon по разнице."""
    if not cur_old or cur_old <= 0:
        return 0
    ratio = cur_old / cur_price if cur_price else 1.0
    new_old = math.ceil(new_price * max(ratio, 1.0))
    if new_price < 400:
        need = new_price + 20
    elif new_price <= 10000:
        need = math.ceil(new_price * 1.05)
    else:
        need = new_price + 500
    return float(max(new_old, need))


def plan_change(p: dict, rule: dict, s: dict):
    """Возвращает (план изменения | None, пояснение).

    Мин/Макс — границы цены селлера (price). Цель удержания (target) — для контролируемой
    цены: нужный price считается через отношение controlled/price (влияние акций и скидок).
    """
    if rule.get("locked"):
        return None, "под MPSpace — не меняем"
    mode = rule.get("mode") or "watch"
    if mode not in ("guard", "hold"):
        return None, "режим «только следить»"
    price, c = p.get("price"), controlled_price(p, rule, s)
    if not price or price <= 0 or not c:
        return None, "нет данных о цене"
    mn, mx, tg = _f(rule.get("min_price")), _f(rule.get("max_price")), _f(rule.get("target"))
    hi = effective_max(mx, rule, s)  # жёсткий предел при стратегии «строгое удержание»
    push_min = s.get("push_min_to_ozon") == "1" and mn
    desired, reason, overshoot = None, "", False

    if mode == "hold":
        if not tg:
            return None, "не задана цена удержания"
        tol = _f(rule.get("tolerance")) or 2
        dev = (c - tg) / tg * 100
        if abs(dev) > tol:
            # насколько акции/скидки уменьшают базовую цену
            ratio = min(max(c / price, 0.2), 1.2)
            desired = tg / ratio
            reason = f"удержание: контр. цена {round(c)} ₽ → цель {round(tg)} ₽"
        elif mn and price < mn - 0.5:
            desired, reason = mn, f"цена ниже минимума селлера: {round(price)} ₽ < {round(mn)} ₽"
        elif mx and hi and price > hi + 0.5:
            desired, reason = hi, f"цена выше максимума: {round(price)} ₽ > {round(hi)} ₽"
        if desired is not None:
            if mn and desired < mn:
                desired = mn
                if "ниже минимума" not in reason:
                    reason = f"цена селлера по минимуму {round(mn)} ₽"
            if mx and desired > mx:
                if max_strategy_of(rule) == 1:
                    hm = hi if hi else mx * (1 + 100 / 100)
                    if desired > hm:
                        desired = hm
                        reason = f"строгое удержание: жёсткий предел {round(hm)} ₽ (цель недостижима)"
                    else:
                        desired = round(desired)
                        overshoot = True
                        reason = f"строгое удержание: максимум {round(mx)} ₽ превышен до {desired} ₽"
                else:
                    desired = mx
                    reason = f"уперлись в максимум {round(mx)} ₽: цель недостижима"
    else:  # guard — только границы цены селлера
        if mn and price < mn - 0.5:
            desired, reason = mn, f"цена ниже минимума: {round(price)} ₽ < {round(mn)} ₽"
        elif mx and hi and price > hi + 0.5:
            desired, reason = hi, f"цена выше максимума: {round(price)} ₽ > {round(hi)} ₽"

    if desired is None:
        # цена в норме — при необходимости только сверяем минимальную цену в Ozon
        if push_min and abs((p.get("ozon_min_price") or 0) - mn) >= 1 and mn <= price:
            return {"price": price, "old_price": p.get("old_price") or 0, "min_price": mn,
                    "reason": f"минимальная цена в Ozon: {round(p.get('ozon_min_price') or 0)} → {round(mn)} ₽",
                    "controlled": c, "min_only": True}, ""
        return None, "в норме"

    if desired == price:
        return None, "в норме"
    new_price = math.ceil(desired) if desired >= price else math.floor(desired)

    step = (_f(s.get("max_step_pct")) or 25) / 100
    lo, hst = math.floor(price * (1 - step)), math.ceil(price * (1 + step))
    limited = ""
    if new_price > hst:
        new_price, limited = hst, f" (ограничено шагом {int(step * 100)}%)"
    if new_price < lo:
        new_price, limited = lo, f" (ограничено шагом {int(step * 100)}%)"
    if mn and new_price < mn:
        new_price = math.ceil(mn)
    if mx and hi and new_price > hi:
        new_price = math.floor(hi)
    if abs(new_price - price) < 1:
        return None, "изменение меньше 1 ₽"

    # превышение максимума возможно только при стратегии «строгое удержание»
    # и только если итоговая цена действительно ушла за максимум
    if mx and max_strategy_of(rule) == 1 and new_price > mx:
        overshoot = True
        if "превышен" not in reason:
            reason = f"строгое удержание: максимум {round(mx)} ₽ превышен до {new_price} ₽"

    plan = {"price": float(new_price), "old_price": old_price_for(new_price, price, p.get("old_price") or 0),
            "reason": reason + limited, "controlled": c, "min_only": False, "overshoot": overshoot}
    if push_min:
        plan["min_price"] = float(min(mn, new_price))
    elif (p.get("ozon_min_price") or 0) > new_price:
        # старая минимальная цена в Ozon выше новой цены — опускаем её, иначе Ozon не даст снизить цену
        plan["min_price"] = float(new_price)
    return plan, ""


def guard_against_loops(p, plan, s):
    """Защита от бесконечного подъёма, когда акция держит фиксированную цену."""
    pid = p["product_id"]
    cool = int(_f(s.get("cooldown_min")) or 60)
    if db.changes_since(pid, cool) > 0:
        return f"пауза после прошлого изменения ({cool} мин)"
    if db.changes_since(pid, 24 * 60, include_min_only=True) >= int(_f(s.get("max_changes_day")) or 8):
        return "достигнут лимит изменений за сутки"
    last = db.last_change(pid)
    if last and not plan.get("min_only") and last["controlled_before"] is not None:
        same_dir = (plan["price"] - p["price"]) * ((last["price_after"] or 0) - (last["price_before"] or 0)) > 0
        base_applied = abs((p["price"] or 0) - (last["price_after"] or 0)) < 1
        not_moved = abs(plan["controlled"] - last["controlled_before"]) < 1
        if same_dir and base_applied and not_moved:
            return ("базовая цена уже изменена, а контролируемая не сдвинулась — вероятно, акция с фиксированной "
                    "ценой. Проверьте акции товара")
    return None


class Repricer:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = {"running": False, "last_run": None, "last_result": "ещё не запускался",
                       "last_ok": None, "next_run": None}
        self._wake = threading.Event()
        self._stop = False

    # ---------- синхронизация ----------
    def sync(self, client, s):
        items = client.get_all_prices(s.get("visibility") or "ALL")
        # остатки FBO/FBS/FBP; ошибка получения остатков НЕ должна ломать проверку цен
        stocks = {}
        try:
            for st in client.get_stocks(s.get("visibility") or "ALL"):
                pid = int(st.get("product_id") or 0)
                if not pid or "stocks" not in st:
                    continue
                fbo = fbs = fbp = total_ = 0
                for entry in st.get("stocks") or []:
                    typ = (entry.get("type") or "").upper()
                    present = _f(entry.get("present")) or 0
                    if typ == "FBO":
                        fbo += present
                    elif typ in ("FBS", "RFBS"):
                        fbs += present
                    elif typ == "FBP":
                        fbp += present
                    total_ += present
                stocks[pid] = (fbo, fbs, fbp, total_)
        except Exception as e:
            db.log("warn", f"Не удалось получить остатки товаров: {e}")
        seen, ts = set(), db.now()
        for it in items:
            pid = int(it["product_id"])
            seen.add(pid)
            pr = it.get("price") or {}
            pi = it.get("price_indexes") or {}
            oz, ext = pi.get("ozon_index_data") or {}, pi.get("external_index_data") or {}
            actions = ((it.get("marketing_actions") or {}).get("actions")) or []
            row = {
                "product_id": pid, "offer_id": it.get("offer_id"),
                "price": _f(pr.get("price")), "old_price": _f(pr.get("old_price")) or 0,
                "seller_price": _f(pr.get("marketing_seller_price")),
                "ozon_min_price": _f(pr.get("min_price")) or 0,
                "buyer_est": buyer_estimate(it), "color_index": pi.get("color_index"),
                "idx_ozon_min": _f(oz.get("min_price")), "idx_ozon_val": _f(oz.get("price_index_value")),
                "idx_ext_min": _f(ext.get("min_price")), "idx_ext_val": _f(ext.get("price_index_value")),
                "actions": json.dumps([{"title": a.get("title"), "from": a.get("date_from"),
                                        "to": a.get("date_to")} for a in actions], ensure_ascii=False),
            }
            row["stock_fbo"], row["stock_fbs"], row["stock_fbp"], row["stock_total"] = \
                stocks.get(pid, (None, None, None, None))
            db.ex("""INSERT INTO products(product_id, offer_id, price, old_price, seller_price, ozon_min_price,
                     buyer_est, color_index, idx_ozon_min, idx_ozon_val, idx_ext_min, idx_ext_val, actions,
                     active, updated_at, stock_fbo, stock_fbs, stock_fbp, stock_total)
                     VALUES(:product_id,:offer_id,:price,:old_price,:seller_price,:ozon_min_price,:buyer_est,
                     :color_index,:idx_ozon_min,:idx_ozon_val,:idx_ext_min,:idx_ext_val,:actions,1,:ts,
                     :stock_fbo,:stock_fbs,:stock_fbp,:stock_total)
                     ON CONFLICT(product_id) DO UPDATE SET offer_id=excluded.offer_id, price=excluded.price,
                     old_price=excluded.old_price, seller_price=excluded.seller_price,
                     ozon_min_price=excluded.ozon_min_price, buyer_est=excluded.buyer_est,
                     color_index=excluded.color_index, idx_ozon_min=excluded.idx_ozon_min,
                     idx_ozon_val=excluded.idx_ozon_val, idx_ext_min=excluded.idx_ext_min,
                     idx_ext_val=excluded.idx_ext_val, actions=excluded.actions, active=1,
                     stock_fbo=excluded.stock_fbo, stock_fbs=excluded.stock_fbs,
                     stock_fbp=excluded.stock_fbp, stock_total=excluded.stock_total,
                     updated_at=excluded.updated_at""", {**row, "ts": ts})
            last = db.q("SELECT * FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 1", (pid,), one=True)
            keys = ("price", "old_price", "seller_price", "buyer_est", "ozon_min_price", "color_index")
            if not last or any(last[k] != row[k] for k in keys):
                db.ex("""INSERT INTO price_history(product_id, ts, price, old_price, seller_price, buyer_est,
                         ozon_min_price, color_index) VALUES(?,?,?,?,?,?,?,?)""",
                      (pid, ts, row["price"], row["old_price"], row["seller_price"], row["buyer_est"],
                       row["ozon_min_price"], row["color_index"]))
        if seen:
            db.ex(f"UPDATE products SET active=0 WHERE product_id NOT IN ({','.join(map(str, seen))})")
        # названия — для новых товаров и раз в сутки для всех
        need = [r["product_id"] for r in db.q("SELECT product_id FROM products WHERE active=1 AND "
                                              "(name IS NULL OR name='' OR image IS NULL)")]
        last_names = db.get_settings().get("names_synced_at", "")
        if not last_names or last_names < (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"):
            need = list(seen)
            db.set_setting("names_synced_at", db.now())
        if need:
            try:
                for inf in client.get_product_info(need):
                    sku = inf.get("sku") or next((x.get("sku") for x in inf.get("sources") or [] if x.get("sku")), None)
                    db.ex("UPDATE products SET name=?, sku=COALESCE(?, sku), image=? WHERE product_id=?",
                          (inf.get("name"), sku, main_image(inf), int(inf.get("id"))))
            except Exception as e:
                db.log("warn", f"Не удалось получить названия товаров: {e}")
        return len(items)

    # ---------- применение правил ----------
    def apply_rules(self, client, s):
        dry = s.get("dry_run") == "1"
        rules = {r["product_id"]: r for r in db.q(
            "SELECT * FROM rules WHERE mode IN ('guard','hold') AND COALESCE(locked, 0)=0")}
        if not rules:
            return 0, 0, 0
        products = db.q(f"SELECT * FROM products WHERE active=1 AND product_id IN "
                        f"({','.join(map(str, rules))})")
        to_send, meta, skipped = [], [], 0
        for p in products:
            plan, _ = plan_change(p, rules[p["product_id"]], s)
            if not plan:
                continue
            block = guard_against_loops(p, plan, s)
            if block:
                skipped += 1
                prev = db.q("SELECT error FROM changes WHERE product_id=? ORDER BY id DESC LIMIT 1",
                            (p["product_id"],), one=True)
                if not prev or prev["error"] != block:
                    db.ex("""INSERT INTO changes(ts, product_id, offer_id, controlled_before, price_before,
                             price_after, old_price_after, min_after, reason, source, dry_run, ok, error)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                          (db.now(), p["product_id"], p["offer_id"], plan["controlled"], p["price"],
                           plan["price"], plan["old_price"], plan.get("min_price"), plan["reason"],
                           "отложено", int(dry), block))
                continue
            if dry:
                prev = db.q("SELECT * FROM changes WHERE product_id=? AND dry_run=1 ORDER BY id DESC LIMIT 1",
                            (p["product_id"],), one=True)
                recent = prev and prev["ts"] >= (datetime.now() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
                if recent and prev["price_after"] == plan["price"]:
                    continue
                db.ex("""INSERT INTO changes(ts, product_id, offer_id, controlled_before, price_before, price_after,
                         old_price_after, min_after, reason, source, dry_run, ok) VALUES(?,?,?,?,?,?,?,?,?,?,1,1)""",
                      (db.now(), p["product_id"], p["offer_id"], plan["controlled"], p["price"], plan["price"],
                       plan["old_price"], plan.get("min_price"), plan["reason"],
                       "мин. цена" if plan.get("min_only") else "авто"))
                continue
            body = {"product_id": p["product_id"], "offer_id": p["offer_id"], "currency_code": "RUB",
                    "price": str(int(round(plan["price"]))), "old_price": str(int(round(plan["old_price"])))}
            if plan.get("min_price"):
                body["min_price"] = str(int(round(plan["min_price"])))
                body["min_price_for_auto_actions_enabled"] = True
            to_send.append(body)
            meta.append((p, plan))
        sent = errors = 0
        if to_send:
            res = client.import_prices(to_send)
            by_pid = {int(r.get("product_id") or 0): r for r in res}
            for p, plan in meta:
                r = by_pid.get(p["product_id"], {})
                ok = bool(r.get("updated"))
                err = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in (r.get("errors") or []))
                db.ex("""INSERT INTO changes(ts, product_id, offer_id, controlled_before, price_before, price_after,
                         old_price_after, min_after, reason, source, dry_run, ok, error)
                         VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                      (db.now(), p["product_id"], p["offer_id"], plan["controlled"], p["price"], plan["price"],
                       plan["old_price"], plan.get("min_price"), plan["reason"],
                       "мин. цена" if plan.get("min_only") else "авто", int(ok),
                       err or (None if ok else "Ozon не подтвердил изменение")))
                if ok:
                    sent += 1
                    db.ex("UPDATE products SET price=?, old_price=?, ozon_min_price=COALESCE(?, ozon_min_price) "
                          "WHERE product_id=?", (plan["price"], plan["old_price"], plan.get("min_price"),
                                                  p["product_id"]))
                else:
                    errors += 1
        return sent, errors, skipped

    def manual_set(self, pid, price, old_price, min_price):
        s = db.get_settings()
        p = db.q("SELECT * FROM products WHERE product_id=?", (pid,), one=True)
        if not p:
            raise OzonError("Товар не найден")
        client = make_client(s["client_id"], s["api_key"])
        body = {"product_id": pid, "offer_id": p["offer_id"], "currency_code": "RUB",
                "price": str(int(round(price))), "old_price": str(int(round(old_price or 0)))}
        if min_price:
            body["min_price"] = str(int(round(min_price)))
            body["min_price_for_auto_actions_enabled"] = True
        res = client.import_prices([body])
        r = res[0] if res else {}
        ok = bool(r.get("updated"))
        err = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in (r.get("errors") or []))
        db.ex("""INSERT INTO changes(ts, product_id, offer_id, controlled_before, price_before, price_after,
                 old_price_after, min_after, reason, source, dry_run, ok, error) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)""",
              (db.now(), pid, p["offer_id"], controlled_price(p, db.get_rule(pid), s), p["price"], price,
               old_price or 0, min_price, "ручное изменение", "вручную", int(ok), err or None))
        if ok:
            db.ex("UPDATE products SET price=?, old_price=? WHERE product_id=?", (price, old_price or 0, pid))
        return ok, err

    # ---------- цикл ----------
    def run_once(self):
        if not self.lock.acquire(blocking=False):
            return "уже выполняется"
        self.status["running"] = True
        t0 = time.time()
        try:
            s = db.get_settings()
            client = make_client(s["client_id"], s["api_key"])
            n = self.sync(client, s)
            sent, errors, skipped = self.apply_rules(client, s)
            db.cleanup(int(_f(s.get("history_days")) or 365))
            mode = "тренировка" if s.get("dry_run") == "1" else "боевой"
            msg = (f"Проверено товаров: {n}. Изменено цен: {sent}, ошибок: {errors}, отложено: {skipped}. "
                   f"Режим: {mode}. {time.time() - t0:.1f} с")
            db.log("error" if errors else "info", msg)
            self.status.update(last_result=msg, last_ok=errors == 0)
            return msg
        except Exception as e:
            msg = f"Ошибка: {e}"
            db.log("error", msg + "\n" + traceback.format_exc()[-1500:])
            self.status.update(last_result=msg, last_ok=False)
            return msg
        finally:
            self.status["running"] = False
            self.status["last_run"] = db.now()
            self.lock.release()

    def trigger(self):
        self._wake.set()

    def loop(self):
        time.sleep(3)
        force = False
        while not self._stop:
            s = db.get_settings()
            has_keys = bool(s.get("client_id") and s.get("api_key")) or os.environ.get("OZON_MOCK") == "1"
            auto = s.get("auto_enabled") == "1"
            if force or (auto and has_keys):
                self.run_once()
            interval = max(5, int(_f(s.get("interval_min")) or 30))
            nxt = datetime.now() + timedelta(minutes=interval)
            self.status["next_run"] = nxt.strftime("%Y-%m-%d %H:%M:%S") if auto else None
            force = self._wake.wait(timeout=interval * 60)
            self._wake.clear()

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()
