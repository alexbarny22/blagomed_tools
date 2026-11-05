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

# Автодобавление в акции Ozon (этап 2б, новая логика «единой предельной цены» с 19.10.2026):
#  promo_auto_add: '' — не трогать (как настроено в Ozon), 'on' — разрешить, 'off' — запретить.
PROMO_AUTO_ADD = {"": "не трогать", "on": "разрешить автодобавление", "off": "запретить автодобавление"}


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


# ---------- прибыль с продажи и безопасный минимум (этап 3) ----------
def cost_scheme(p: dict) -> str:
    """Схема отгрузки для расчёта расходов Ozon: FBS, если остаток только на FBS, иначе FBO."""
    fbo = (_f(p.get("stock_fbo")) or 0) > 0
    fbs = (_f(p.get("stock_fbs")) or 0) > 0
    return "FBS" if fbs and not fbo else "FBO"


def ozon_fees(p: dict):
    """Комиссия Ozon по схеме товара (FBO/FBS): dict с разбивкой или None, если данных нет.

    Логистика берётся консервативно по максимуму (*_direct_flow_trans_max_amount),
    минимум сохраняется для подсказки. Эквайринг — поле acquiring.
    """
    scheme = cost_scheme(p)
    sfx = "_fbo" if scheme == "FBO" else "_fbs"
    sales = _f(p.get("comm_sales_pct" + sfx))
    direct = _f(p.get("comm_direct_max" + sfx))
    direct_min = _f(p.get("comm_direct_min" + sfx))
    mile = _f(p.get("comm_mile" + sfx))
    if sales is None and direct is None and mile is None:
        return None
    return {"scheme": scheme, "commission_pct": sales or 0,
            "logistics_direct": direct or 0, "logistics_direct_min": direct_min or 0,
            "last_mile": mile or 0, "acquiring": _f(p.get("acquiring")) or 0}


def profit_of(p: dict, s: dict = None):
    """Прибыль с продажи по цене с акциями продавца (этап 3).

    Возвращает dict с расчётом либо {"error": "..."}:
      — "нет себестоимости" — себестоимость не заполнена;
      — "нет данных Ozon о комиссиях" — Ozon не прислал комиссии/эквайринг.
    Формула: прибыль = цена_с_акциями − комиссия%×цена − логистика − эквайринг − налог%×цена − себестоимость.
    Процент прибыли — от цены с акциями продавца.
    """
    cost = _f(p.get("cost"))
    fees = ozon_fees(p)
    if cost is None:
        return {"error": "нет себестоимости"}
    if not fees:
        return {"error": "нет данных Ozon о комиссиях"}
    sell = p.get("seller_price") or p.get("price")
    if not sell or sell <= 0:
        return {"error": "нет цены со скидками продавца"}
    tax_pct = _f((s or {}).get("tax_pct")) or 0
    commission = sell * fees["commission_pct"] / 100
    tax = sell * tax_pct / 100
    logistics = fees["logistics_direct"] + fees["last_mile"]
    expenses = commission + logistics + fees["acquiring"] + tax + cost
    profit = sell - expenses
    return {"sell": sell, "cost": cost, "scheme": fees["scheme"],
            "commission_pct": fees["commission_pct"], "commission": commission,
            "logistics_direct": fees["logistics_direct"], "logistics_min": fees["logistics_direct_min"],
            "last_mile": fees["last_mile"], "acquiring": fees["acquiring"],
            "tax_pct": tax_pct, "tax": tax, "profit": profit,
            "profit_pct": profit / sell * 100 if sell else 0}


def safe_min_calc(p: dict, s: dict = None):
    """Минимальная цена селлера, при которой прибыль >= минимальной маржи (этап 3).

    Комиссия и налог — проценты от цены, логистика и себестоимость — фиксированные ₽:
        мин = (себестоимость + логистика + последняя_миля + эквайринг)
              / (1 − комиссия% − налог% − маржа%), округление вверх до рубля.
    Возвращает (значение | None, причина | ''). Причина непустая, когда посчитать нельзя.
    Считается для цены без скидок акций.
    """
    s = s or {}
    cost = _f(p.get("cost"))
    fees = ozon_fees(p)
    if cost is None:
        return None, "нет себестоимости: заполните её в карточке или загрузите из Excel"
    if not fees:
        return None, "нет данных Ozon о комиссиях"
    tax_pct = _f(s.get("tax_pct")) or 0
    margin = _f(s.get("min_margin_pct"))
    if margin is None:
        margin = 10
    denom = 1 - fees["commission_pct"] / 100 - tax_pct / 100 - margin / 100
    if denom <= 0:
        return None, (f"маржа недостижима: комиссия {fees['commission_pct']:g}% + налог {tax_pct:g}% "
                      f"+ маржа {margin:g}% уже покрывают всю цену")
    numer = cost + fees["logistics_direct"] + fees["last_mile"] + fees["acquiring"]
    return math.ceil(numer / denom), ""


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
    # «ошибка отправки» и «отложено: акция с фиксированной ценой» — только если это
    # ПОСЛЕДНЯЯ реальная запись журнала по товару (после успешной отправки причина пропадает)
    last_real = db.q("SELECT source, ok, error FROM changes WHERE product_id=? AND dry_run=0 "
                     "ORDER BY id DESC LIMIT 1", (p["product_id"],), one=True)
    if last_real:
        src = last_real.get("source") or ""
        err = last_real.get("error") or ""
        if src == "отложено" and not last_real["ok"] and "акция с фиксированной" in err:
            reasons.append("изменение отложено: акция с фиксированной ценой")
        elif src == "акции: автодобавление" and not last_real["ok"]:
            # этап 2б: Ozon не принял настройку автодобавления (ошибку видно в журнале)
            reasons.append("Ozon не принял настройку автодобавления")
        elif last_real["ok"] == 0 and src != "отложено":
            reasons.append("ошибка при последней отправке цены в Ozon")
    if rule.get("mode") in ("guard", "hold") and not rule.get("locked"):
        if p.get("stock_total") is not None and p["stock_total"] == 0:
            reasons.append("остаток 0")
    if (p.get("color_index") or "").upper() == "RED":
        reasons.append("красный индекс цен")
    # «Ozon добавит в акцию ниже минимума» — цена автодобавления из таблицы auto_add
    if mn:
        for _ in db.q("SELECT 1 FROM auto_add WHERE product_id=? AND price_to_auto_add IS NOT NULL "
                      "AND price_to_auto_add < ? - 0.5", (p["product_id"], mn)):
            reasons.append("Ozon добавит в акцию ниже минимума")
            break
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
        # подъём до минимума разрешён сразу — защита от убытка, даже если шаг меньше;
        # ограничение шагом снимаем всегда, раз мы выставляем минимум целиком
        new_price = math.ceil(mn)
        limited = ""
        limit_note = " (подъём до минимума без ограничения шага)" if new_price > hst else ""
    else:
        limit_note = ""
    if abs(new_price - price) < 1:
        return None, "изменение меньше 1 ₽"

    # превышение максимума возможно только при стратегии «строгое удержание»
    # и только если итоговая цена действительно ушла за максимум
    if mx and max_strategy_of(rule) == 1 and new_price > mx:
        overshoot = True
        if "превышен" not in reason:
            reason = f"строгое удержание: максимум {round(mx)} ₽ превышен до {new_price} ₽"

    plan = {"price": float(new_price), "old_price": old_price_for(new_price, price, p.get("old_price") or 0),
            "reason": reason + limited + limit_note, "controlled": c, "min_only": False, "overshoot": overshoot}
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


# ---------- акции Ozon (этапы 2, 2б) ----------
# С 19.10.2026 Ozon ввёл «единую предельную цену»: товар участвует в акции, пока цена
# селлера не выше требуемой (max_action_price), отдельных методов входа/выхода нет.
# Поэтому рекомендации/кнопки входа-выхода из этапа 2 удалены; управляется только
# автодобавление в будущие акции (promo_auto_add, поле auto_add_to_ozon_actions_list_enabled).


class Repricer:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = {"running": False, "last_run": None, "last_result": "ещё не запускался",
                       "last_ok": None, "next_run": None}
        self._wake = threading.Event()
        self._stop = False
        self.force_actions = False  # «Проверить сейчас»: перечитать акции даже внутри интервала

    # ---------- синхронизация ----------
    def sync(self, client, s, force_actions=False):
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
        got_stocks = bool(stocks)
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
            # расходы Ozon (этап 3): комиссии и эквайринг уже приходят в ответе цен —
            # сохраняются без дополнительных запросов; если Ozon их не прислал — прежние значения
            # остаются (не затираются)
            got_comm = it.get("commissions") is not None or "acquiring" in it
            if got_comm:
                comm = it.get("commissions") or {}
                row["acquiring"] = _f(it.get("acquiring"))
                row["comm_sales_pct_fbo"] = _f(comm.get("sales_percent_fbo"))
                row["comm_sales_pct_fbs"] = _f(comm.get("sales_percent_fbs"))
                row["comm_direct_max_fbo"] = _f(comm.get("fbo_direct_flow_trans_max_amount"))
                row["comm_direct_max_fbs"] = _f(comm.get("fbs_direct_flow_trans_max_amount"))
                row["comm_direct_min_fbo"] = _f(comm.get("fbo_direct_flow_trans_min_amount"))
                row["comm_direct_min_fbs"] = _f(comm.get("fbs_direct_flow_trans_min_amount"))
                row["comm_mile_fbo"] = _f(comm.get("fbo_deliv_to_customer_amount"))
                row["comm_mile_fbs"] = _f(comm.get("fbs_deliv_to_customer_amount"))
            # этап 2б: признак автодобавления в акции (/v5/product/info/prices);
            # если поле отсутствует — прежнее значение в базе сохраняется (не затираем)
            if "auto_add_to_ozon_actions_list_enabled" in pr:
                row["auto_add_enabled"] = bool(pr["auto_add_to_ozon_actions_list_enabled"])
            base_cols = ["product_id", "offer_id", "price", "old_price", "seller_price", "ozon_min_price",
                         "buyer_est", "color_index", "idx_ozon_min", "idx_ozon_val", "idx_ext_min",
                         "idx_ext_val", "actions", "active", "updated_at"]
            vals = [":product_id", ":offer_id", ":price", ":old_price", ":seller_price", ":ozon_min_price",
                    ":buyer_est", ":color_index", ":idx_ozon_min", ":idx_ozon_val", ":idx_ext_min",
                    ":idx_ext_val", ":actions", "1", ":ts"]
            sets = ["offer_id=excluded.offer_id", "price=excluded.price", "old_price=excluded.old_price",
                    "seller_price=excluded.seller_price", "ozon_min_price=excluded.ozon_min_price",
                    "buyer_est=excluded.buyer_est", "color_index=excluded.color_index",
                    "idx_ozon_min=excluded.idx_ozon_min", "idx_ozon_val=excluded.idx_ozon_val",
                    "idx_ext_min=excluded.idx_ext_min", "idx_ext_val=excluded.idx_ext_val",
                    "actions=excluded.actions", "active=1", "updated_at=excluded.updated_at"]
            # остатки обновляем только когда реально получены для этого товара:
            # при сбое запроса остатков прежние значения сохраняются (не затираются NULL)
            if got_stocks and pid in stocks:
                base_cols += ["stock_fbo", "stock_fbs", "stock_fbp", "stock_total"]
                vals += [":stock_fbo", ":stock_fbs", ":stock_fbp", ":stock_total"]
                sets += ["stock_fbo=excluded.stock_fbo", "stock_fbs=excluded.stock_fbs",
                         "stock_fbp=excluded.stock_fbp", "stock_total=excluded.stock_total"]
            if got_comm:
                base_cols += ["acquiring", "comm_sales_pct_fbo", "comm_sales_pct_fbs",
                              "comm_direct_max_fbo", "comm_direct_max_fbs",
                              "comm_direct_min_fbo", "comm_direct_min_fbs",
                              "comm_mile_fbo", "comm_mile_fbs"]
                vals += [":acquiring", ":comm_sales_pct_fbo", ":comm_sales_pct_fbs",
                         ":comm_direct_max_fbo", ":comm_direct_max_fbs",
                         ":comm_direct_min_fbo", ":comm_direct_min_fbs",
                         ":comm_mile_fbo", ":comm_mile_fbs"]
                sets += ["acquiring=excluded.acquiring",
                         "comm_sales_pct_fbo=excluded.comm_sales_pct_fbo",
                         "comm_sales_pct_fbs=excluded.comm_sales_pct_fbs",
                         "comm_direct_max_fbo=excluded.comm_direct_max_fbo",
                         "comm_direct_max_fbs=excluded.comm_direct_max_fbs",
                         "comm_direct_min_fbo=excluded.comm_direct_min_fbo",
                         "comm_direct_min_fbs=excluded.comm_direct_min_fbs",
                         "comm_mile_fbo=excluded.comm_mile_fbo",
                         "comm_mile_fbs=excluded.comm_mile_fbs"]
            if "auto_add_enabled" in row:
                base_cols += ["auto_add_enabled"]
                vals += [":auto_add_enabled"]
                sets += ["auto_add_enabled=excluded.auto_add_enabled"]
            db.ex((f"INSERT INTO products({', '.join(base_cols)}) VALUES({', '.join(vals)}) "
                   f"ON CONFLICT(product_id) DO UPDATE SET {', '.join(sets)}"), {**row, "ts": ts})
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
        if hasattr(client, "get_actions"):  # поддержка акций есть и у Ozon, и у мока
            try:
                self._sync_actions(client, force=force_actions)
            except Exception as e:
                # любая ошибка синхронизации акций не должна ронять проверку цен
                db.log("warn", f"Синхронизация акций: {e}")
        return len(items)

    # ---------- применение правил ----------
    def apply_rules(self, client, s):
        """Цены по правилам и настройка автодобавления в акции.

        Возвращает (sent, errors, skipped, promo_done): promo_done — сколько товаров получили
        настройку автодобавления (в тренировке — сколько записей «что сделал бы»).
        """
        dry = s.get("dry_run") == "1"
        return self._apply_prices(client, s, dry)

    def _apply_prices(self, client, s, dry):
        """Изменение цен по правилам min/max/цели и отправка настройки автодобавления в акции.

        Этап 2б: если promo_auto_add = 'on'/'off' и не совпадает с products.auto_add_enabled,
        в /v1/product/import/prices отправляется auto_add_to_ozon_actions_list_enabled.
        Если у товара в этом же запуске меняется цена — поле добавляется в то же тело; если
        нет — вместе с текущими price/old_price (и min_price, если включена передача минимума),
        чтобы цена не изменилась. Это НЕ изменение цены: в журнале источник «акции: автодобавление»,
        паузу и суточный лимит изменений цены не включает, guard_against_loops не применяется.
        Товары «Под MPSpace» не трогаются; в тренировке — только запись «что сделал бы».
        """
        rules = {r["product_id"]: r for r in db.q(
            "SELECT * FROM rules WHERE mode IN ('guard','hold') AND COALESCE(locked, 0)=0")}
        auto = {r["product_id"]: r.get("promo_auto_add") for r in db.q(
            "SELECT product_id, promo_auto_add FROM rules WHERE COALESCE(promo_auto_add,'') IN ('on','off') "
            "AND COALESCE(locked, 0)=0")}
        ids = sorted(set(rules) | set(auto))
        if not ids:
            return 0, 0, 0, 0
        products = db.q(f"SELECT * FROM products WHERE active=1 AND product_id IN "
                        f"({','.join(map(str, ids))})")
        push_min = s.get("push_min_to_ozon") == "1"

        def price_body(p, price, old_price, min_price=None):
            b = {"product_id": p["product_id"], "offer_id": p["offer_id"], "currency_code": "RUB",
                 "price": str(int(round(price))), "old_price": str(int(round(old_price)))}
            if min_price:
                b["min_price"] = str(int(round(min_price)))
                b["min_price_for_auto_actions_enabled"] = True
            return b

        def log_auto(p, dry_run, ok, reason, error=None):
            db.ex("""INSERT INTO changes(ts, product_id, offer_id, controlled_before, price_before,
                     price_after, old_price_after, min_after, reason, source, dry_run, ok, error)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (db.now(), p["product_id"], p["offer_id"], None, p.get("price"), p.get("price"),
                   p.get("old_price") or 0, None, reason, "акции: автодобавление",
                   int(dry_run), int(ok), error))
        to_send, meta, skipped = [], [], 0
        for p in products:
            pid = p["product_id"]
            rule = rules.get(pid)
            plan = None
            if rule:
                plan, _ = plan_change(p, rule, s)
            want = auto.get(pid)
            auto_target = None
            if want:
                auto_target = 1 if want == "on" else 0
                if p.get("auto_add_enabled") == auto_target:
                    auto_target = None  # в Ozon уже так — отправлять нечего
            if not plan and auto_target is None:
                continue
            if plan:
                block = guard_against_loops(p, plan, s)
                if block:
                    skipped += 1
                    prev = db.q("SELECT error FROM changes WHERE product_id=? ORDER BY id DESC LIMIT 1",
                                (pid,), one=True)
                    if not prev or prev["error"] != block:
                        db.ex("""INSERT INTO changes(ts, product_id, offer_id, controlled_before, price_before,
                                 price_after, old_price_after, min_after, reason, source, dry_run, ok, error)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                              (db.now(), pid, p["offer_id"], plan["controlled"], p["price"],
                               plan["price"], plan["old_price"], plan.get("min_price"), plan["reason"],
                               "отложено", int(dry), block))
                    plan = None
            if dry:
                if plan:
                    prev = db.q("SELECT * FROM changes WHERE product_id=? AND dry_run=1 ORDER BY id DESC LIMIT 1",
                                (pid,), one=True)
                    recent = prev and prev["ts"] >= (datetime.now() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
                    if not (recent and prev["price_after"] == plan["price"]):
                        db.ex("""INSERT INTO changes(ts, product_id, offer_id, controlled_before, price_before,
                                 price_after, old_price_after, min_after, reason, source, dry_run, ok)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,1,1)""",
                              (db.now(), pid, p["offer_id"], plan["controlled"], p["price"], plan["price"],
                               plan["old_price"], plan.get("min_price"), plan["reason"],
                               "мин. цена" if plan.get("min_only") else "авто"))
                if auto_target is not None:
                    log_auto(p, 1, 1, "тренировка: что сделал бы — автодобавление в акции "
                                      + ("включить" if auto_target else "выключить"))
                continue
            # боевой режим: одно тело на товар — цена и настройка автодобавления вместе
            body = None
            if plan:
                body = price_body(p, plan["price"], plan["old_price"], plan.get("min_price"))
            if auto_target is not None:
                if body is None:
                    mn = _f(rule.get("min_price")) if rule else None
                    body = price_body(p, p.get("price") or 0, p.get("old_price") or 0,
                                      mn if push_min and mn else None)
                body["auto_add_to_ozon_actions_list_enabled"] = bool(auto_target)
            to_send.append(body)
            meta.append((p, plan, auto_target))
        sent = errors = promo_done = 0
        if to_send:
            res = client.import_prices(to_send)
            by_pid = {int(r.get("product_id") or 0): r for r in res}
            for p, plan, auto_target in meta:
                r = by_pid.get(p["product_id"], {})
                ok = bool(r.get("updated"))
                err = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in (r.get("errors") or []))
                if plan:
                    db.ex("""INSERT INTO changes(ts, product_id, offer_id, controlled_before, price_before,
                             price_after, old_price_after, min_after, reason, source, dry_run, ok, error)
                             VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                          (db.now(), p["product_id"], p["offer_id"], plan["controlled"], p["price"],
                           plan["price"], plan["old_price"], plan.get("min_price"), plan["reason"],
                           "мин. цена" if plan.get("min_only") else "авто", int(ok),
                           err or (None if ok else "Ozon не подтвердил изменение")))
                    if ok:
                        sent += 1
                        db.ex("UPDATE products SET price=?, old_price=?, ozon_min_price=COALESCE(?, ozon_min_price) "
                              "WHERE product_id=?", (plan["price"], plan["old_price"], plan.get("min_price"),
                                                      p["product_id"]))
                    else:
                        errors += 1
                if auto_target is not None:
                    if ok:
                        promo_done += 1
                        db.set_auto_add_enabled(p["product_id"], auto_target)
                        log_auto(p, 0, 1, "автодобавление в акции Ozon "
                                          + ("включено" if auto_target else "выключено")
                                          + (" (вместе с изменением цены)" if plan else " (цена не менялась)"))
                    else:
                        # ошибка поля автодобавления — только журнал и «Требует внимания»,
                        # проверку цен не прерываем
                        msg = err or "Ozon не подтвердил настройку"
                        log_auto(p, 0, 0, "Ozon не принял настройку автодобавления", msg)
                        db.log("warn", f"Товар {p['offer_id']}: Ozon не принял настройку "
                                       f"автодобавления в акции: {msg}")
        return sent, errors, skipped, promo_done

    # ---------- акции ----------
    def _sync_actions(self, client, force=False):
        """Актуализация участия товаров в акциях Ozon (product_actions) и автодобавлений (auto_add).

        Акции перечитываются не чаще раза в actions_interval_min минут (по умолчанию 120)
        и всегда при принудительном запуске («Проверить сейчас»). Решения по ценам работают
        на каждом запуске независимо от этого интервала.

        Любая ошибка здесь (OzonError, ValueError, KeyError, ошибка разбора JSON) НЕ должна
        ронять проверку цен: пишем предупреждение в «События» и продолжаем. Для акции, которую
        прочитать не удалось, прежние строки в базе сохраняются. Полностью удаляются только
        акции, которых больше нет в /v1/actions или у которых истёк date_end.
        """
        s = db.get_settings()
        interval = max(0, int(_f(s.get("actions_interval_min")) or 120))
        last = s.get("actions_synced_at") or ""
        if not force and interval and last:
            try:
                last_dt = datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
                if datetime.now() - last_dt < timedelta(minutes=interval):
                    return  # ещё не время перечитывать акции
            except ValueError:
                pass
        try:
            all_actions = client.get_actions()
        except Exception as e:
            db.log("warn", f"Акции не получены (проверьте права ключа): {e}")
            return
        if not all_actions:
            # пустому списку не доверяем: ничего не стираем, чтобы не потерять данные акций
            db.log("warn", "Акции не получены (сервер вернул пустой список) — прежние данные сохранены")
            return
        now = datetime.utcnow()
        active, expired_aids, api_ids = [], set(), set()
        for a in all_actions:
            try:
                aid = int(a.get("id") or 0)
            except (TypeError, ValueError):
                aid = 0
            if aid:
                api_ids.add(aid)
            end = (a.get("date_end") or "").strip()
            if end:
                try:
                    end_dt = datetime.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    end_dt = None
                if end_dt is not None and end_dt < now:
                    if aid:
                        expired_aids.add(aid)
                    continue
            active.append(a)
        if len(active) > 40:
            db.log("warn", f"Активных акций больше 40 ({len(active)}) — перечитаны будут только "
                           f"первые 40, остальные сохраняют прежние данные")
        # удаляем полностью только акции, которых уже нет в /v1/actions, или у которых истёк date_end
        db_ids = {r["action_id"] for r in db.q("SELECT DISTINCT action_id FROM product_actions")}
        gone_aids = ((db_ids - api_ids) | expired_aids) & db_ids
        rows, auto_rows = [], []
        reset_pa, reset_aa = set(), set()
        for a in active[:40]:
            try:
                aid = int(a.get("id") or 0)
            except (TypeError, ValueError):
                aid = 0
            if not aid:
                continue
            title = a.get("title") or ""
            date_from, date_to = a.get("date_start"), a.get("date_end")
            act_rows = []
            try:
                for pr in client.get_action_products(aid) or []:
                    pid = int(pr.get("id") or pr.get("product_id") or 0)
                    if pid:
                        act_rows.append((pid, aid, "participant", title, date_from, date_to, "STARTED",
                                         _f(pr.get("price")), _f(pr.get("action_price")),
                                         _f(pr.get("max_action_price"))))
                for pr in client.get_action_candidates(aid) or []:
                    pid = int(pr.get("id") or pr.get("product_id") or 0)
                    if pid:
                        act_rows.append((pid, aid, "candidate", title, date_from, date_to, "STARTED",
                                         _f(pr.get("price")), _f(pr.get("action_price")),
                                         _f(pr.get("max_action_price"))))
            except Exception as e:
                db.log("warn", f"Акция «{title}» ({aid}): {e}")
                continue  # акция не прочитана — её прежние данные в базе сохраняются
            rows.extend(act_rows)
            reset_pa.add(aid)
            # «Ozon добавит сам по цене X» — ошибка этого метода только предупреждение
            try:
                aa_rows = []
                for pr in client.get_auto_add_products(aid) or []:
                    pid = int(pr.get("product_id") or pr.get("id") or 0)
                    ap = _f(pr.get("action_price_to_auto_add"))
                    if pid and ap:
                        aa_rows.append((pid, aid, ap, db.now()))
                auto_rows.extend(aa_rows)
                reset_aa.add(aid)
            except Exception as e:
                db.log("warn", f"Акция «{title}» ({aid}): автодобавление не получено: {e}")
        try:
            db.save_actions(rows, gone_aids, reset_pa, reset_aa, auto_rows)
            db.set_setting("actions_synced_at", db.now())
        except Exception as e:
            db.log("warn", f"Не удалось сохранить данные акций: {e}")
        # этап 2б (п.6): диагностика — по новым правилам Ozon товар-участник не может иметь
        # цену выше требуемой (max_action_price); если такое есть — одно предупреждение в
        # «События» не чаще раза в сутки (только запись, никаких действий)
        try:
            n_over = db.q("SELECT COUNT(DISTINCT product_id) AS n FROM product_actions "
                          "WHERE kind='participant' AND price IS NOT NULL "
                          "AND max_action_price IS NOT NULL AND price > max_action_price",
                          one=True)["n"] or 0
            if n_over:
                today = datetime.now().strftime("%Y-%m-%d")
                if s.get("actions_diag_warn_date") != today:
                    db.log("warn", f"Проверка правил Ozon: {n_over} товаров участвуют в акциях "
                                   f"при цене выше требуемой — сообщите разработчику")
                    db.set_setting("actions_diag_warn_date", today)
        except Exception:
            pass

    # Рекомендации входа/выхода в акции удалены в этапе 2б: с 19.10.2026 Ozon решает
    # участие ценой (товар участвует, пока цена селлера не выше требуемой), отдельных
    # методов входа/выхода нет. Управляется только автодобавление в будущие акции
    # (см. _apply_prices → auto_add_to_ozon_actions_list_enabled).

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
            force_actions = self.force_actions
            self.force_actions = False
            n = self.sync(client, s, force_actions=force_actions)
            sent, errors, skipped, promo_done = self.apply_rules(client, s)
            db.cleanup(int(_f(s.get("history_days")) or 365))
            mode = "тренировка" if s.get("dry_run") == "1" else "боевой"
            msg = (f"Проверено товаров: {n}. Изменено цен: {sent}, акций: {promo_done}, "
                   f"ошибок: {errors}, отложено: {skipped}. Режим: {mode}. {time.time() - t0:.1f} с")
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
