# -*- coding: utf-8 -*-
"""
«Мозг» бота «Приход по фото» — вся логика, общая для MAX и Telegram:
фото -> распознавание -> поставщик, товары, заказы -> итог с правками -> проведение в 1С.

С мессенджером работает через переходник `ui` (max_bot.py или tg_bot.py), у которого есть:
  send(chat, text, kb=None, menu=False) -> ref    отправить сообщение (HTML), вернуть ссылку на него
  edit(chat, ref, text, kb=None)                    изменить сообщение
  delete(chat, ref)                                 удалить сообщение
  drop_kb(cb)                                       убрать кнопки у сообщения, где нажали кнопку
  toast(cb, text="", alert=False)                   ответ на нажатие кнопки
  send_file(chat, path, filename, caption)          отправить файл
  name, max_len, file_hint                          название мессенджера и ограничения
Кнопки: kb = [[(надпись, данные), ...], ...] — ряды кнопок.
"""
import asyncio
import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import mapping  # noqa: E402
import onec_odata as onec  # noqa: E402
import recognize as rec_mod  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(os.path.join(HERE, "prihod_bot.log"), encoding="utf-8"),
                              logging.StreamHandler()])
log = logging.getLogger("prihod")

ui = None                     # переходник мессенджера — задаёт max_bot.py / tg_bot.py
c1 = onec.OneC(config, log=log.info)
rec_mod.OWN_INNS = set(getattr(config, "OWN_INNS", []))
rec_mod.MODEL_TIMEOUT = getattr(config, "MODEL_TIMEOUT", 120)
rec_mod.RACE_DEADLINE = getattr(config, "RACE_DEADLINE", 150)
MODELS = [m for m in config.MODELS if m.get("enabled", True) and m.get("api_key")
          and "ЗАМЕНИТЬ" not in m.get("base_url", "") + m.get("model", "")]
HISTORY = os.path.join(HERE, "history")

B_TABLE, B_REFRESH, B_RESET = "📋 Таблица сопоставлений", "🔄 Обновить товары из 1С", "✖ Начать заново"
B_RESTART = "⚙ Перезапустить бота"
MENU_KB = [[(B_TABLE, "m:table")], [(B_REFRESH, "m:refresh"), (B_RESET, "m:reset")], [(B_RESTART, "m:restart")]]
RESTART_WORDS = {"/restart", "перезапуск", "перезапустить", "перезагрузка", "перезагрузить", "рестарт"}
CANCEL = [("✖ Отменить приход", "cancel")]          # ряд с отменой — под каждым шагом прихода
STOP_WORDS = {"отмена", "отменить", "стоп", "сброс", "cancel", "/cancel", "/stop", "выход"}
GO_WORDS = {"далее", "дальше", "продолжить", "продолжай", "next", "/next"}

S = {}       # user_id -> состояние сеанса
CHATS = {}   # user_id -> чат с ним (куда сообщить об обновлении бота)


SESS_FILE = os.path.join(HERE, "sessions.pkl")
SKIP_KEYS = {"timer", "live", "status"}


def save_sessions():
    """Начатые приходы — на диск (фото, распознанное, выбранные товары)."""
    import pickle
    try:
        data = {"S": {u: {k: v for k, v in d.items() if k not in SKIP_KEYS} for u, d in S.items()
                      if d.get("photos") or d.get("rec")},
                "CHATS": dict(CHATS), "t": time.time()}
        tmp = SESS_FILE + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(data, f)
        os.replace(tmp, SESS_FILE)
    except Exception as ex:
        log.warning("Сеансы не сохранены: %s", ex)


def load_sessions():
    import pickle
    try:
        with open(SESS_FILE, "rb") as f:
            data = pickle.load(f)
        if time.time() - data.get("t", 0) > 2 * 86400:      # старше двух суток — не поднимаем
            return
        for u, d in data.get("S", {}).items():
            S[u] = dict(d, timer=None, status=None)
        CHATS.update(data.get("CHATS", {}))
        if S:
            log.info("Восстановлены начатые приходы: %d", len(S))
    except FileNotFoundError:
        pass
    except Exception as ex:
        log.warning("Сеансы не восстановлены: %s", ex)


def seen(uid, chat):
    if chat is not None:
        CHATS[uid] = chat


def st(uid):
    return S.setdefault(uid, {"photos": [], "status": None, "timer": None})


def reset(uid):
    old = S.get(uid, {})
    if old.get("timer"):
        old["timer"].cancel()
    S[uid] = {"photos": [], "status": None, "timer": None}


def allowed(uid):
    ids = getattr(config, "ALLOWED_IDS", [])
    if ui and ui.name == "MAX":          # в MAX у людей другие ID, чем в Telegram
        ids = getattr(config, "MAX_ALLOWED_IDS", None) or ids
    return uid in ids


def e(s):
    return html.escape(str(s))


def money(x):
    return f"{x:,.2f}".replace(",", " ")


async def send_long(chat, text, kb=None, menu=False):
    """Длинный текст — несколькими сообщениями; кнопки — у последнего."""
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > ui.max_len:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    parts.append(cur)
    ref = None
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        ref = await ui.send(chat, p, kb if last else None, menu=menu and last)
    return ref


# ------------------------------------------------------------------ защита от «тихих» ошибок
def run(coro, chat=None):
    """Запустить обработчик в фоне. Если он упал — записать в лог и показать человеку, что делать дальше."""
    async def guard():
        try:
            await coro
            save_sessions()
        except Exception as ex:
            log.exception("Ошибка в обработчике")
            if chat is not None:
                try:
                    await ui.send(chat, f"⚠️ Что-то пошло не так: {e(str(ex)[:300])}\n\n"
                                        "Можно продолжить с того же места или отменить приход.",
                                  [[("▶ Продолжить", "next")], CANCEL])
                except Exception:
                    log.exception("Не удалось сообщить об ошибке")
    return asyncio.create_task(guard())


# ------------------------------------------------------------------ команды и меню
async def on_start(uid, chat):
    seen(uid, chat)
    if not allowed(uid):
        await ui.send(chat, f"Ваш ID в {ui.name}: {uid}\nДобавьте его в config.py "
                            f"({'MAX_ALLOWED_IDS' if ui.name == 'MAX' else 'ALLOWED_IDS'}).")
        return
    reset(uid)
    await ui.send(chat, "Пришлите фото накладной — все страницы (можно несколько фото сразу).\n"
                        f"{ui.file_hint}\n\n"
                        f"{B_TABLE} — бот пришлёт таблицу; исправьте её в Excel и отправьте файл обратно\n"
                        f"{B_REFRESH} — если в 1С завели новый товар\n"
                        f"{B_RESET} — бросить текущую накладную\n"
                        f"{B_RESTART} — если бот завис или нужно срочно обновить\n\n"
                        "Отменить приход можно в любой момент: кнопка «✖ Отменить приход» или слово «отмена».",
                        menu=True)


async def cmd_cancel(uid, chat):
    reset(uid)
    await ui.send(chat, "✖ Приход отменён, в 1С ничего не записано. Присылайте фото новой накладной.", menu=True)


async def cmd_table(uid, chat):
    try:
        rows = await asyncio.to_thread(mapping.load)
    except Exception as ex:
        await ui.send(chat, f"Не открылась таблица: {e(ex)}")
        return
    sup = {}
    for r in rows:
        k = r.get("Поставщик") or r.get("ИНН поставщика") or "?"
        sup[k] = sup.get(k, 0) + 1
    by = "\n".join(f"• {e(k)}: {v}" for k, v in sorted(sup.items(), key=lambda kv: -kv[1])) or "пока пусто"
    pend = mapping.pending_count()
    await ui.send_file(chat, mapping.FILE, "Сопоставления.xlsx",
                       (f"Таблица сопоставлений — {len(rows)} строк:\n{by}\n\n"
                        + (f"⚠️ Ещё {pend} строк ждут записи — файл открыт в Excel на компьютере бота.\n\n"
                           if pend else "")
                        + "Исправьте в Excel и просто отправьте файл сюда — бот заменит таблицу.")[:1000])


async def cmd_refresh(uid, chat):
    await ui.send(chat, "Загружаю номенклатуру из 1С…")
    try:
        n = await asyncio.to_thread(c1.load_items, True)
        await ui.send(chat, f"Готово, позиций: {n}.", menu=True)
    except Exception as ex:
        await ui.send(chat, f"Ошибка: {e(ex)}")


# ------------------------------------------------------------------ приём фото и таблицы
async def on_xlsx(uid, chat, data):
    if not allowed(uid):
        return
    try:
        n = await asyncio.to_thread(mapping.replace_file, data)
        await ui.send(chat, f"✅ Таблица сопоставлений заменена: {n} строк.")
    except Exception as ex:
        await ui.send(chat, f"❌ Таблица не заменена: {e(ex)}")


async def show_photo_status(uid, chat):
    await asyncio.sleep(1.5)            # ждём, пока долетят все фото
    s = st(uid)
    if s.get("status"):
        try:
            await ui.delete(chat, s["status"])
        except Exception:
            pass
    s["status"] = await ui.send(chat, f"Фото получено: {len(s['photos'])}. Если это все страницы — жмите «Распознать».",
                                [[("🔍 Распознать", "go")], CANCEL])


async def on_image(uid, chat, data):
    seen(uid, chat)
    if not allowed(uid):
        return
    s = st(uid)
    if s.get("rec"):                    # дослали забытую страницу — распознаем всё заново вместе с ней
        for k in ("rec", "lines", "ready", "ka", "asking", "search_for", "await_num"):
            s.pop(k, None)
        await ui.send(chat, "Добавляю страницу к этой накладной — распознаю заново все фото.\n"
                            f"Если это другая накладная — сначала «{B_RESET}».")
    s["photos"].append(data)
    if s.get("timer"):
        s["timer"].cancel()
    s["timer"] = asyncio.create_task(show_photo_status(uid, chat))


# ------------------------------------------------------------------ живой индикатор хода работы
ICON = {"run": "⏳", "ok": "✅", "warn": "⚠️", "err": "❌", "wait": "▫️", "skip": "➖"}


def bar(frac, width=12):
    full = int(round(frac * width))
    return "▰" * full + "▱" * (width - full)


def render(steps, title, finished=False):
    lines = [f"<b>{e(title)}</b>",
             f"{bar(1.0 if finished else steps.fraction())}  {int(steps.elapsed())} с", ""]
    for it in steps.items:
        sec = int(steps.elapsed(it)) if it["t0"] else 0
        tail = f" — {e(it['note'])}" if it["note"] else ""
        dots = "…" if it["state"] == "run" else ""
        lines.append(f"{ICON[it['state']]} {e(it['title'])}{dots} <i>{sec} с</i>{tail}")
    return "\n".join(lines)


class Live:
    """Сообщение, которое само перерисовывается каждые 3 секунды, пока идёт работа."""
    def __init__(self, chat, ref, steps, title):
        self.chat, self.ref, self.steps, self.title = chat, ref, steps, title
        self.last = ""
        self.task = asyncio.create_task(self._loop())

    async def _draw(self, finished=False):
        txt = render(self.steps, self.title, finished)
        if txt != self.last:
            try:
                await ui.edit(self.chat, self.ref, txt)
                self.last = txt
            except Exception:
                pass            # лимиты мессенджера — пропускаем кадр

    async def _loop(self):
        while True:
            await asyncio.sleep(3)
            await self._draw()

    async def close(self, title=None):
        self.task.cancel()
        if title:
            self.title = title
        await self._draw(finished=True)


# ------------------------------------------------------------------ распознавание
async def do_recognize(uid, chat, other_model=False):
    s = st(uid)
    start = 0
    if other_model and s.get("rec"):
        names = [m["name"] for m in MODELS]
        used = names.index(s["rec"]["model"]) if s["rec"]["model"] in names else -1
        start = (used + 1) % len(MODELS)
    steps = rec_mod.Steps()
    title = f"🔍 Распознаю накладную · {len(s['photos'])} фото"
    ref = await ui.send(chat, render(steps, title))
    live = Live(chat, ref, steps, title)
    try:
        r = await asyncio.to_thread(rec_mod.recognize, s["photos"], MODELS, log.info, start, steps,
                                    getattr(config, "RACE", 2))
    except Exception as ex:
        log.exception("recognize")
        await live.close("❌ Не получилось распознать")
        await ui.send(chat, e(ex), [[("🔁 Ещё раз", "go")]])
        return
    s["rec"] = r
    s["live"] = live
    await prepare(uid, chat)


async def prepare(uid, chat):
    """Поставщик + сопоставление строк, затем вопросы по неизвестным позициям."""
    s = st(uid)
    d = s["rec"]["data"]
    inn = d["supplier"].get("inn", "")
    live = s.pop("live", None)
    step = live.steps.add("onec", "Ищу поставщика и товары в 1С") if live else None
    s.pop("orders_note", None)
    try:
        if not c1.items:
            await asyncio.to_thread(c1.load_items)
        warm = WARM.get(inn)
        fresh = warm and time.time() - warm["t"] < getattr(config, "WARM_MINUTES", 10) * 60 * 1.5
        ka = warm["ka"] if fresh else await asyncio.to_thread(c1.find_supplier, inn, d["supplier"].get("name", ""))
        s["orders"] = None
        if fresh and getattr(config, "USE_ORDERS", True):
            s["orders"] = warm["orders"]       # заготовка; перед проведением всё равно перечитаем
        elif ka and getattr(config, "USE_ORDERS", True):
            if step:
                live.steps.finish(step, "ok", "поставщик найден")
                step = live.steps.add("onec", "Читаю открытые заказы поставщику")
            try:
                s["orders"] = await asyncio.wait_for(asyncio.to_thread(c1.orders_context, ka),
                                                     timeout=getattr(config, "ORDERS_TIMEOUT", 90))
            except asyncio.TimeoutError:
                log.warning("Заказы поставщику: 1С не ответила вовремя")
                s["orders_note"] = "1С долго отдаёт заказы — продолжаю без них. Нажмите «🔁 Пересчитать заказы» позже."
            except Exception as ex:
                log.warning("Заказы поставщику не прочитаны: %s", ex)
                s["orders_note"] = f"Заказы не прочитаны: {ex}"
        if step:
            known = sum(1 for ln in d["lines"] if c1.match_line(inn, ln)["key"])
            oc = s.get("orders")
            live.steps.finish(step, "ok" if ka and not s.get("orders_note") else "warn",
                              (f"знакомых товаров {known} из {len(d['lines'])}"
                               + (f", открытых заказов {len(oc['orders'])}" if oc else
                                  (", заказы не прочитаны" if s.get("orders_note") else ", открытых заказов нет"))
                               if ka else "поставщик не найден"))
            await live.close(f"✅ Накладная прочитана за {int(live.steps.elapsed())} с")
    except Exception as ex:
        if step:
            live.steps.finish(step, "err", "нет связи с 1С")
            await live.close("⚠️ Накладная прочитана, но 1С не ответила")
        await ui.send(chat, f"❌ Ошибка связи с 1С: {e(ex)}", [[("🔁 Повторить", "sup_retry")]])
        return
    s["ka"], s["inn"] = ka, inn
    if not ka:
        await ui.send(chat,
                      f"Поставщик <b>{e(d['supplier'].get('name', '?'))}</b>, ИНН {e(inn or 'не прочитан')} "
                      f"не найден в 1С.\nЗаведите контрагента в 1С (с этим ИНН) и нажмите «Найти снова».",
                      [[("🔁 Найти снова", "sup_retry"), ("✖ Отмена", "cancel")]])
        return
    s["lines"] = []
    for ln in d["lines"]:
        mt = c1.match_line(inn, ln)
        s["lines"].append({"line": ln, **mt, "skip": False})
    unknown = [L for L in s["lines"] if not L["key"]]
    ai_model = next((m for m in MODELS if m.get("type", "openai") == "openai"), None)
    if unknown and ai_model and getattr(config, "AI_MATCH", True):
        note = await ui.send(chat, f"🤖 Подбираю наши товары для {len(unknown)} новых позиций…")
        await asyncio.to_thread(c1.ai_suggest, unknown, ai_model, rec_mod.ask_text)
        try:
            await ui.delete(chat, note)
        except Exception:
            pass
    await next_step(uid, chat)


async def next_step(uid, chat):
    """Спрашиваем первую несопоставленную строку; если таких нет — итог с кнопкой «Провести»."""
    s = st(uid)
    for i, L in enumerate(s["lines"]):
        if not L["key"] and not L["skip"]:
            log.info("Следующий вопрос: строка %s (чат %s)", L["line"]["n"], chat)
            await ask_line(uid, chat, i)
            log.info("Вопрос по строке %s отправлен", L["line"]["n"])
            return
    log.info("Все строки сопоставлены — итог (чат %s)", chat)
    await summary(uid, chat)


async def ask_line(uid, chat, i, cands=None):
    s = st(uid)
    L = s["lines"][i]
    s["asking"] = i
    s["search_for"] = None
    cands = cands if cands is not None else L["cands"]
    s["shown"] = [it for it, _ in cands]
    left = sum(1 for x in s["lines"] if not x["key"] and not x["skip"])
    rows = [[((f"🤖 {it['name'][:55]}" if it.get("ai") else f"{it['name'][:55]} ({sc}%)"), f"pick:{i}:{k}")]
            for k, (it, sc) in enumerate(cands)]
    rows.append([("🔎 Найти по слову", f"find:{i}"), ("⏭ Не приходовать", f"skip:{i}")])
    rows.append(CANCEL)
    ln = L["line"]
    art = f"\nКод поставщика: {e(ln['article'])}" if ln.get("article") else ""
    await ui.send(
        chat,
        f"❓ Строка {ln['n']} (осталось неясных: {left})\n<b>{e(ln['name'])}</b>{art}\n"
        f"{ln['qty']:g} {e(ln['unit'])} × {money(ln['price'])} = {money(ln['sum'])}\n\n"
        f"Что это у нас в 1С? Выбор запишется в таблицу сопоставлений — в следующий раз не спрошу."
        + ("\n🤖 — вариант, который предложила нейросеть." if any(it.get("ai") for it, _ in cands) else ""),
        rows)


# ------------------------------------------------------------------ итог
async def summary(uid, chat):
    s = st(uid)
    r = s["rec"]
    d, chk = r["data"], r["check"]
    ka = s["ka"]
    use = [L for L in s["lines"] if L["key"] and not L["skip"]]
    total = sum(L["line"]["sum"] for L in use)
    doc_total = d["total"].get("sum")
    oc = s.get("orders")
    alloc = c1.allocate(oc, [{"line": L["line"], "key": L["key"]} for L in use]) if oc else []
    where = {id(L["line"]): a for L, a in zip(use, alloc)}
    over, pricey = [], []
    lines_txt = []
    for L in s["lines"]:
        ln = L["line"]
        mark = "⏭" if L["skip"] else ("⚠️" if ln["n"] in chk["bad_lines"] else "✅")
        tail = "не приходуется" if L["skip"] else e(L["name1c"])
        txt = (f"{mark} {ln['n']}. {e(ln['name'][:60])}\n"
               f"     {ln['qty']:g} × {money(ln['price'])} = <b>{money(ln['sum'])}</b> → {tail}")
        parts = where.get(id(ln))
        if parts:
            got = [f"№ {rr['number']} ({q:g}{', сверх заказа' if rr.get('extra') else ''})" for rr, q in parts if rr]
            free = sum(q for rr, q in parts if not rr)
            if got:
                txt += "\n     📦 заказ " + ", ".join(got)
            inv_price = ln["sum"] / ln["qty"] if ln["qty"] else 0       # сверка цены: накладная против заказа
            diffs = sorted({rr["price"] for rr, q in parts if rr and not rr.get("extra") and rr.get("price") and
                            abs(inv_price - rr["price"]) > max(0.5, rr["price"] * 0.005)})
            if diffs:
                pct = (inv_price - diffs[0]) / diffs[0] * 100
                txt += (f"\n     💰 цена в заказе {', '.join(money(x) for x in diffs)}, "
                        f"в накладной {money(inv_price)} ({pct:+.1f}%)")
                pricey.append(ln["n"])
            if free > 0.0001:
                txt += f"\n     ⚠️ {free:g} шт нет в открытых заказах"
                over.append(ln["n"])
        lines_txt.append(txt)
    head = (f"📄 <b>{e(d.get('doc_type', 'Документ'))} № {e(d.get('number', '?'))} от {e(d.get('date', '?'))}</b>\n"
            f"Поставщик: {e(ka['Description'])} (ИНН {e(ka.get('ИНН') or s['inn'])})\n"
            f"Склад: {e(getattr(config, 'WAREHOUSE', '') or 'как в прошлом поступлении')}\n"
            f"Распознал: {e(r['model'])}\n")
    if s.get("orders_note"):
        head += f"⚠️ {e(s['orders_note'])}\n"
    if oc:
        head += f"Открытых заказов поставщику: {len(oc['orders'])} — строки разложены по ним, от старых к новым\n"
    if over:
        head += (f"\n⚠️ <b>{'Строка' if len(over) == 1 else 'Строки'} {', '.join(map(str, over))}</b> — "
                 "открытых заказов у поставщика нет, привязать не к чему. "
                 "1С может не провести поступление: заведите заказ, затем «🔁 Пересчитать заказы».\n")
    if pricey:
        head += (f"\n💰 <b>Цена не как в заказе: {'строка' if len(pricey) == 1 else 'строки'} "
                 f"{', '.join(map(str, pricey))}</b> — проверьте, прежде чем проводить.\n")
    if chk["issues"]:
        head += "\n⚠️ <b>Проверьте:</b>\n" + "\n".join("• " + e(x) for x in chk["issues"][:10]) + "\n"
    else:
        head += "\n✔️ Все суммы сошлись с итогом документа.\n"
    foot = (f"\nК приходу: строк {len(use)} из {len(s['lines'])}, сумма <b>{money(total)}</b>"
            + (f" (итог в документе {money(doc_total)})" if doc_total else "") +
            "\n\nНашли ошибку — «✏️ Исправить строку».")
    s["ready"] = True
    kb = [[("✅ Провести в 1С", "post")], [("✏️ Исправить строку", "fix")]]
    if getattr(config, "USE_ORDERS", True):
        kb.append([("🔁 Пересчитать заказы", "reorders")])
    kb.append([("🔁 Другой нейросетью", "other_model"), ("✖ Отмена", "cancel")])
    await send_long(chat, head + "\n" + "\n".join(lines_txt) + "\n" + foot, kb)


# ------------------------------------------------------------------ правки
FIELDS = {"qty": "количество", "price": "цену за единицу", "sum": "сумму строки"}
EDIT_QP = re.compile(r"^\s*(\d+)\s*[=:]\s*([\d\s.,]+?)\s*[xхX×*]\s*([\d\s.,]+)\s*$")
EDIT_SUM = re.compile(r"^\s*(\d+)\s+сумма\s+([\d\s.,]+)\s*$", re.I)
EDIT_DEL = re.compile(r"^\s*(\d+)\s+(удалить|убрать|нет)\s*$", re.I)
EDIT_ASK = re.compile(r"^\s*(\d+)\s*\?\s*$")


def apply_edit(ln, field, value):
    """Меняет количество / цену / сумму строки и пересчитывает связанные суммы."""
    vat = rec_mod.vat_percent(ln["vat_rate"])
    if field in ("qty", "price"):
        ln[field] = value
        ln["sum_without_vat"] = round(ln["qty"] * ln["price"], 2)
        ln["sum"] = ln["sum_without_vat"] if vat == 0 else round(ln["sum_without_vat"] * (1 + vat / 100), 2)
        ln["vat_sum"] = round(ln["sum"] - ln["sum_without_vat"], 2)
    else:
        ln["sum"] = value
        if ln["qty"]:
            base = value / (1 + vat / 100) if vat else value
            ln["price"] = round(base / ln["qty"], 2)
            ln["sum_without_vat"] = round(base, 2)
            ln["vat_sum"] = round(value - base, 2)
    ln["unclear"] = False


def line_by_n(s, n):
    for i, L in enumerate(s.get("lines") or []):
        if L["line"]["n"] == n:
            return i, L
    return None, None


async def on_text(uid, chat, text):
    seen(uid, chat)
    t = (text or "").strip()
    if t.split("@")[0] in ("/start", "start", "Начать", "/menu", "меню", "Меню"):
        await on_start(uid, chat)
        return
    if not allowed(uid):
        await on_start(uid, chat)
        return
    if t in (B_RESET,) or t.lower().strip(" .!") in STOP_WORDS:
        await cmd_cancel(uid, chat)
        return
    if t == B_RESTART or t.lower().strip(" .!") in RESTART_WORDS:
        await ask_restart(uid, chat)
        return
    if t in ("/sopost", B_TABLE):
        await cmd_table(uid, chat)
        return
    if t in ("/refresh", B_REFRESH):
        await cmd_refresh(uid, chat)
        return
    s = st(uid)
    if t.lower().strip(" .!") in GO_WORDS:
        await resume(uid, chat)
        return
    if s.get("await_num"):
        i, field = s["await_num"]
        val = rec_mod.num(t)
        if val is None or val < 0:
            await ui.send(chat, "Нужно число, например 450 или 12,5. Попробуйте ещё раз.")
            return
        s.pop("await_num")
        apply_edit(s["lines"][i]["line"], field, val)
        s["rec"]["check"] = rec_mod.check(s["rec"]["data"])
        await summary(uid, chat)
        return
    if s.get("search_for") is not None:
        res = c1.search(t, 6)
        if not res:
            await ui.send(chat, f"Ничего не нашёл. Попробуйте другое слово или «{B_REFRESH}», "
                                "если товар только что заведён.")
            return
        await ask_line(uid, chat, s["search_for"], res)
        return
    if not s.get("lines"):
        await ui.send(chat, "Пришлите фото накладной.", menu=True)
        return
    for rx, kind in ((EDIT_QP, "qp"), (EDIT_SUM, "sum"), (EDIT_DEL, "del"), (EDIT_ASK, "ask")):
        mt = rx.match(t)
        if not mt:
            continue
        i, L = line_by_n(s, int(mt.group(1)))
        if L is None:
            await ui.send(chat, f"Строки {mt.group(1)} нет.")
            return
        ln = L["line"]
        if kind == "qp":
            apply_edit(ln, "qty", rec_mod.num(mt.group(2)) or ln["qty"])
            apply_edit(ln, "price", rec_mod.num(mt.group(3)) or ln["price"])
        elif kind == "sum":
            apply_edit(ln, "sum", rec_mod.num(mt.group(2)) or ln["sum"])
        elif kind == "del":
            L["skip"] = True
        elif kind == "ask":
            try:
                await asyncio.to_thread(onec.forget, s["inn"], ln)
            except mapping.Locked as ex:
                await ui.send(chat, f"⚠️ {e(ex)}")
            L.update(key=None, name1c="", how="", skip=False, cands=c1.search(ln["name"], 4))
            await ask_line(uid, chat, i)
            return
        if kind in ("qp", "sum"):
            s["rec"]["check"] = rec_mod.check(s["rec"]["data"])
        await summary(uid, chat)
        return
    await ui.send(chat, "Не понял. Чтобы поправить строку — кнопка «✏️ Исправить строку» под итогом.")


# ------------------------------------------------------------------ нажатия кнопок
async def on_callback(uid, chat, ref, data, cb):
    seen(uid, chat)
    """Все кнопки. ref — сообщение с кнопкой, cb — нажатие (для toast/drop_kb)."""
    s = st(uid)
    if not allowed(uid):
        await ui.toast(cb)
        return
    if data == "m:restart":
        await ui.toast(cb)
        await ask_restart(uid, chat)
        return
    if data == "restart_yes":
        await ui.toast(cb, "Перезапускаю…")
        await ui.drop_kb(cb)
        await restart(uid, chat)
        return
    if data == "restart_no":
        await ui.toast(cb)
        await ui.drop_kb(cb)
        await ui.send(chat, "Хорошо, бот работает дальше.")
        return
    if data.startswith("m:"):
        await ui.toast(cb)
        await {"m:table": cmd_table, "m:refresh": cmd_refresh, "m:reset": cmd_cancel}[data](uid, chat)
        return
    if data in ("go", "other_model"):
        if not s["photos"]:
            await ui.toast(cb, "Сначала пришлите фото", True)
            return
        await ui.toast(cb)
        await ui.drop_kb(cb)
        await do_recognize(uid, chat, data == "other_model")
        return
    if data == "cancel":
        reset(uid)
        await ui.toast(cb, "Приход отменён")
        await ui.drop_kb(cb)
        await ui.send(chat, "✖ Приход отменён, в 1С ничего не записано. Присылайте фото новой накладной.", menu=True)
        return
    if data == "sup_retry":
        await ui.toast(cb)
        if s.get("rec"):
            await prepare(uid, chat)
        return
    if data.startswith("pick:"):
        _, i, k = data.split(":")
        i, k = int(i), int(k)
        if "lines" not in s or i >= len(s["lines"]) or k >= len(s.get("shown", [])):
            await stale(uid, chat, cb)
            return
        it = s["shown"][k]
        L = s["lines"][i]
        log.info("Выбор: строка %s -> %s", L["line"]["n"], it["name"])
        L.update(key=it["key"], name1c=it["name"], how="выбрано", skip=False)
        try:
            await asyncio.wait_for(asyncio.to_thread(onec.remember, s["ka"]["Description"], s["inn"], L["line"], it),
                                   timeout=20)
            log.info("Записано в таблицу сопоставлений")
            await ui.toast(cb, "Записал в таблицу")
        except Exception as ex:
            log.warning("Таблица сопоставлений: %r", ex)
            await ui.toast(cb, f"Не записалось в таблицу: {ex}"[:190], True)
        try:
            await ui.edit(chat, ref, f"✅ Строка {L['line']['n']}: {e(L['line']['name'])}\n→ <b>{e(it['name'])}</b>")
        except Exception as ex:
            log.warning("Не удалось изменить сообщение: %s", ex)
        await next_step(uid, chat)
        return
    if data.startswith("skip:"):
        i = int(data.split(":")[1])
        if "lines" not in s or i >= len(s["lines"]):
            await stale(uid, chat, cb)
            return
        s["lines"][i]["skip"] = True
        await ui.toast(cb)
        try:
            await ui.edit(chat, ref, f"⏭ Строка {s['lines'][i]['line']['n']} не будет оприходована.")
        except Exception as ex:
            log.warning("Не удалось изменить сообщение: %s", ex)
        await next_step(uid, chat)
        return
    if data.startswith("find:"):
        s["search_for"] = int(data.split(":")[1])
        await ui.toast(cb)
        await ui.send(chat, "Напишите часть названия товара, как он называется у нас в 1С:", [CANCEL])
        return
    if data == "next":
        await ui.toast(cb)
        await ui.drop_kb(cb)
        await resume(uid, chat)
        return
    if data == "reorders":
        await reorders(uid, chat, cb)
        return
    if data in ("fix", "fixback") or data.startswith(("fixl:", "fixf:")):
        await fix(uid, chat, ref, data, cb)
        return
    if data in ("post", "post_force"):
        await post(uid, chat, ref, data, cb)
        return
    await stale(uid, chat, cb)


async def stale(uid, chat, cb):
    """Нажали кнопку из старого сообщения (бот перезапускался или приход уже завершён)."""
    await ui.toast(cb, "Эта кнопка уже не действует")
    s = st(uid)
    if s.get("lines") or s.get("rec") or s.get("photos"):
        await ui.send(chat, "Эта кнопка из старого сообщения. Продолжить текущий приход?",
                      [[("▶ Продолжить", "next")], CANCEL])
    else:
        await ui.send(chat, "Эта кнопка из старого сообщения — начатого прихода сейчас нет "
                            "(бот перезапускался или приход уже завершён).\n"
                            "Пришлите фото накладной заново.", menu=True)


async def resume(uid, chat):
    """Продолжить приход с того места, где остановились."""
    s = st(uid)
    if s.get("lines"):
        await next_step(uid, chat)
    elif s.get("rec"):
        await prepare(uid, chat)
    elif s.get("photos"):
        await ui.send(chat, f"Фото получено: {len(s['photos'])}.", [[("🔍 Распознать", "go")], CANCEL])
    else:
        await ui.send(chat, "Сейчас нет начатого прихода. Пришлите фото накладной.", menu=True)


async def reorders(uid, chat, cb):
    """Перечитать заказы поставщику из 1С (после того как завели/поправили заказ)."""
    s = st(uid)
    if not s.get("ka"):
        await stale(uid, chat, cb)
        return
    await ui.toast(cb, "Читаю заказы из 1С…")
    wait = await ui.send(chat, "⏳ Читаю заказы поставщику из 1С…")
    try:
        s["orders"] = await asyncio.wait_for(asyncio.to_thread(c1.orders_context, s["ka"]),
                                             timeout=getattr(config, "ORDERS_TIMEOUT", 90) * 2)
        s.pop("orders_note", None)
    except Exception as ex:
        await ui.edit(chat, wait, f"Заказы не прочитаны: {e(ex) or '1С не ответила вовремя'}")
        return
    await ui.delete(chat, wait)
    await summary(uid, chat)


async def fix(uid, chat, ref, data, cb):
    s = st(uid)
    if not s.get("lines"):
        await stale(uid, chat, cb)
        return
    await ui.toast(cb)
    if data == "fixback":
        s.pop("await_num", None)
        await ui.delete(chat, ref)
        return
    if data == "fix":
        bad = s["rec"]["check"]["bad_lines"]
        rows = [[((f"{'⚠️' if L['line']['n'] in bad else ('⏭' if L['skip'] else '')} {L['line']['n']}. "
                   f"{L['line']['name'][:40]}").strip(), f"fixl:{i}")] for i, L in enumerate(s["lines"])]
        rows.append([("↩ Назад", "fixback")])
        rows.append(CANCEL)
        await ui.send(chat, "Какую строку исправить?", rows)
        return
    if data.startswith("fixl:"):
        i = int(data.split(":")[1])
        if i >= len(s["lines"]):
            return
        L = s["lines"][i]
        ln = L["line"]
        await ui.edit(chat, ref,
                      f"Строка {ln['n']}: <b>{e(ln['name'])}</b>\n{ln['qty']:g} × {money(ln['price'])} = "
                      f"{money(ln['sum'])}\nУ нас: {e(L['name1c'] or '—')}\n\nЧто исправить?",
                      [[("Количество", f"fixf:{i}:qty"), ("Цена", f"fixf:{i}:price"), ("Сумма", f"fixf:{i}:sum")],
                       [("🔁 Другой товар", f"fixf:{i}:item")],
                       [("⏭ Не приходовать" if not L["skip"] else "✅ Вернуть в приход", f"fixf:{i}:skip")],
                       [("↩ Назад", "fixback")], CANCEL])
        return
    _, i, field = data.split(":")
    i = int(i)
    if i >= len(s["lines"]):
        return
    L = s["lines"][i]
    if field == "skip":
        L["skip"] = not L["skip"]
        await ui.delete(chat, ref)
        await summary(uid, chat)
    elif field == "item":
        try:
            await asyncio.to_thread(onec.forget, s["inn"], L["line"])
        except Exception as ex:
            await ui.send(chat, f"⚠️ {e(ex)}")
        L.update(key=None, name1c="", how="", skip=False, cands=c1.search(L["line"]["name"], 4))
        await ui.delete(chat, ref)
        await ask_line(uid, chat, i)
    else:
        s["await_num"] = (i, field)
        await ui.edit(chat, ref, f"Напишите {FIELDS[field]} для строки {L['line']['n']} — только число.",
                      [[("↩ Назад", "fixback")], CANCEL])


# ------------------------------------------------------------------ проведение
async def post(uid, chat, ref, data, cb):
    s = st(uid)
    if not s.get("ready") or not s.get("ka"):
        await ui.toast(cb, "Нечего проводить", True)
        return
    use = [{"line": L["line"], "key": L["key"]} for L in s["lines"] if L["key"] and not L["skip"]]
    if not use:
        await ui.toast(cb, "Нет ни одной сопоставленной строки", True)
        return
    await ui.toast(cb)
    await ui.drop_kb(cb)
    d = s["rec"]["data"]
    if data == "post":
        dup = await asyncio.to_thread(c1.find_duplicate, s["ka"]["Ref_Key"], d.get("number"))
        if dup:
            await ui.send(chat,
                          f"⚠️ В 1С уже есть поступление № {e(dup['Number'].strip())} от {e(dup['Date'][:10])} "
                          f"по документу поставщика № {e(d.get('number'))}. Создать ещё одно?",
                          [[("Да, создать", "post_force"), ("✖ Нет", "cancel")]])
            return
    steps = rec_mod.Steps()
    steps.EXPECT = dict(steps.EXPECT, onec=10)
    title = "📦 Провожу поступление в 1С"
    live = Live(chat, await ui.send(chat, render(steps, title)), steps, title)
    try:
        def job():
            if getattr(config, "USE_ORDERS", True):
                # заказы могли закрыться/появиться, пока смотрели итог — перечитываем, но не дольше ORDERS_TIMEOUT
                sto = steps.add("onec", "Перечитываю открытые заказы поставщику")
                ex_ = ThreadPoolExecutor(max_workers=1)
                fut = ex_.submit(c1.orders_context, s["ka"])
                try:
                    s["orders"] = fut.result(timeout=getattr(config, "ORDERS_TIMEOUT", 90))
                    oc = s["orders"]
                    steps.finish(sto, "ok", f"открытых: {len(oc['orders'])}" if oc else "открытых заказов нет")
                except Exception as ex:
                    log.warning("Заказы не перечитаны перед проведением: %s", ex)
                    steps.finish(sto, "warn", "1С долго отвечает — беру заказы, прочитанные при распознавании")
                finally:
                    ex_.shutdown(wait=False)
            st1 = steps.add("onec", "Беру настройки из прошлого поступления")
            tpl, own = c1.template(s["ka"])
            steps.finish(st1, "ok", "от этого поставщика" if own else "поставщик новый — образец от другого")
            st2 = steps.add("onec", f"Записываю документ ({len(use)} строк) и провожу")
            doc = c1.build(s["ka"], d, use, tpl, own, s.get("orders"))
            res = c1.create(doc, getattr(config, "POST", True))
            steps.finish(st2, "ok" if res["posted"] or not getattr(config, "POST", True) else "warn",
                         f"№ {res['number']}")
            return doc, res
        doc, res = await asyncio.to_thread(job)
    except Exception as ex:
        log.exception("create")
        for it in steps.items:
            if it["state"] == "run":
                steps.finish(it, "err")
        await live.close("❌ 1С не приняла документ")
        await ui.send(chat, e(ex), [[("🔁 Повторить", "post_force")]])
        return
    await live.close("✅ Готово" if res["posted"] else "⚠️ Записано без проведения")
    save_history(uid, doc, res)
    if res["posted"]:
        text = f"✅ Поступление № <b>{e(res['number'])}</b> создано и проведено.\nСумма: {money(doc['СуммаДокумента'])}"
    elif getattr(config, "POST", True):
        text = (f"⚠️ Поступление № <b>{e(res['number'])}</b> записано, но НЕ проведено:\n{e(res['error'])}\n\n"
                f"Откройте его в 1С, поправьте и проведите вручную.")
    else:
        text = f"📝 Поступление № <b>{e(res['number'])}</b> записано (без проведения)."
    skipped = [L for L in s["lines"] if L["skip"]]
    if skipped:
        text += "\n\nНе оприходованы: " + ", ".join(str(L["line"]["n"]) for L in skipped)
    await ui.send(chat, text + "\n\nМожно присылать следующую накладную.", menu=True)
    reset(uid)


def save_history(uid, doc, res):
    """Фото и распознанные данные — на случай разбора, «откуда взялась цифра»."""
    try:
        s = st(uid)
        folder = os.path.join(HISTORY, dt.datetime.now().strftime("%Y-%m-%d_%H%M%S") + f"_{res['number']}")
        os.makedirs(folder, exist_ok=True)
        for i, b in enumerate(s["photos"], 1):
            with open(os.path.join(folder, f"photo{i}.jpg"), "wb") as f:
                f.write(b)
        with open(os.path.join(folder, "data.json"), "w", encoding="utf-8") as f:
            json.dump({"recognized": s["rec"], "result": res,
                       "lines": [{k: v for k, v in L.items() if k != "cands"} for L in s["lines"]]},
                      f, ensure_ascii=False, indent=1, default=str)
    except Exception:
        log.exception("history")


# ------------------------------------------------------------------ фоновые задачи
WARM = {}   # ИНН -> {"t": время, "ka": контрагент, "orders": заказы} — заготовка для частых поставщиков


async def warm_orders_loop():
    """Раз в N минут заранее читает открытые заказы частых поставщиков (в основном Гаврюшкин)."""
    inns = [i for i in getattr(config, "FREQUENT_INNS", []) if i]
    every = getattr(config, "WARM_MINUTES", 10) * 60
    while inns and getattr(config, "USE_ORDERS", True):
        for inn in inns:
            t0 = time.time()
            try:
                ka = await asyncio.to_thread(c1.find_supplier, inn, "")
                if ka:
                    oc = await asyncio.to_thread(c1.orders_context, ka)
                    WARM[inn] = {"t": time.time(), "ka": ka, "orders": oc}
                    await asyncio.to_thread(c1.template, ka, 0)     # образец шапки — тоже заранее
                    log.info("Заготовка заказов %s: %s открытых, %.0f с", ka["Description"],
                             len(oc["orders"]) if oc else 0, time.time() - t0)
            except Exception as ex:
                log.warning("Заготовка заказов %s не удалась: %s", inn, ex)
        await asyncio.sleep(every)


async def load_items_bg():
    """Справочник товаров грузится в фоне — бот отвечает сразу."""
    try:
        n = await asyncio.to_thread(c1.load_items)
        print(f"  Товары из 1С загружены: {n}")
    except Exception as ex:
        print(f"  ⚠ Товары из 1С не загрузились: {ex}\n    Проверьте 1С (логин/пароль, адрес ODATA_URL).")
        log.error("Номенклатура не загрузилась: %s", ex)


STARTED = time.time()


async def ask_restart(uid, chat):
    others = any((v.get("photos") or v.get("rec")) for u, v in S.items() if u != uid)
    mine = bool(st(uid).get("photos") or st(uid).get("rec"))
    warn = ""
    if mine:
        warn += "\n⚠️ Ваш начатый приход сбросится (в 1С ничего не записано)."
    if others:
        warn += "\n⚠️ Сейчас кто-то ещё проводит приход — он прервётся."
    await ui.send(chat, "Перезапустить бота? Он проверит обновления на GitHub, перезапустится и "
                        "через минуту снова будет на связи." + warn,
                  [[("✅ Да, перезапустить", "restart_yes"), ("✖ Нет", "restart_no")]])


async def restart(uid, chat):
    """Проверить обновления и выйти — пульт BotPanel (или start.bat) запустит бота заново."""
    note = "обновлений нет"
    try:
        import update
        if await asyncio.to_thread(update.main):
            note = f"обновился до версии {update.local_version()}"
    except Exception as ex:
        note = f"обновления не проверены: {ex}"
    log.info("Перезапуск по кнопке (%s)", note)
    await ui.send(chat, f"🔄 Перезапускаюсь ({note}). Через минуту напишите «/start» или пришлите фото.")
    await asyncio.sleep(1.5)
    # пульт BotPanel: быстрый выход с кодом 0 он считает «разовым скриптом» — поэтому код 3,
    # если бот проработал меньше 30 минут; пульт перезапустит его как после сбоя
    os._exit(0 if time.time() - STARTED > 1800 else 3)


def busy():
    """Кто-то сейчас проводит приход (есть фото или распознанная накладная)."""
    return any(v.get("photos") or v.get("rec") for v in S.values())


async def auto_update_loop():
    """
    Раз в AUTO_UPDATE_MINUTES проверяет GitHub. Новая версия и никто не проводит приход —
    скачивает её, предупреждает в мессенджере и завершается: пульт (или start.bat) запускает бота заново.
    """
    if not getattr(config, "AUTO_UPDATE", True):
        return
    import update
    every = max(10, getattr(config, "AUTO_UPDATE_MINUTES", 60)) * 60
    await asyncio.sleep(max(every, 35 * 60))     # первые 35 минут не трогаем (пульт так спокойнее перезапускает)
    while True:
        try:
            remote = await asyncio.to_thread(update.remote_version)
            if remote and remote != update.local_version():
                while busy():
                    await asyncio.sleep(60)
                if await asyncio.to_thread(update.main):
                    log.info("Обновлён до версии %s — перезапуск", remote)
                    for chat in {c for u, c in CHATS.items() if allowed(u)}:
                        try:
                            await ui.send(chat, f"🔄 Бот обновился до версии {remote} и перезапускается — "
                                                "через минуту снова на связи.")
                        except Exception:
                            pass
                    os._exit(0)
        except Exception as ex:
            log.warning("Проверка обновлений: %s", ex)
        await asyncio.sleep(every)


def start_background():
    load_sessions()
    asyncio.create_task(load_items_bg())
    asyncio.create_task(warm_orders_loop())
    asyncio.create_task(auto_update_loop())


def banner(platform):
    print("\n=== Бот «Приход по фото» ===")
    print(f"  Мессенджер: {platform}")
    print("  Нейросети:", ", ".join(m["name"] for m in MODELS) or "НЕТ — заполните MODELS в config.py")
