# -*- coding: utf-8 -*-
"""
Телеграм-бот «Приход по фото».
Фото накладной (1 или несколько страниц) -> нейросеть читает таблицу -> бот сверяет суммы,
находит поставщика по ИНН и товары в 1С (неизвестные спрашивает кнопками и запоминает) ->
создаёт и проводит «Приобретение товаров и услуг» в 1С:УТ 11.5 на общий склад.

Запуск: start.bat
"""
import asyncio
import datetime as dt
import html
import json
import logging
import os
import re
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import onec_odata as onec  # noqa: E402
import recognize as rec_mod  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(os.path.join(HERE, "prihod_bot.log"), encoding="utf-8"),
                              logging.StreamHandler()])
log = logging.getLogger("prihod")

session = AiohttpSession(proxy=config.TELEGRAM_PROXY) if getattr(config, "TELEGRAM_PROXY", "") else None
bot = Bot(token=config.TELEGRAM_TOKEN, session=session) if session else Bot(token=config.TELEGRAM_TOKEN)
dp = Dispatcher()
c1 = onec.OneC(config, log=log.info)
rec_mod.OWN_INNS = set(getattr(config, "OWN_INNS", []))
MODELS = [m for m in config.MODELS if m.get("enabled", True)]
HISTORY = os.path.join(HERE, "history")

S = {}   # user_id -> состояние сеанса


def st(uid):
    return S.setdefault(uid, {"photos": [], "status": None, "timer": None})


def reset(uid):
    old = S.get(uid, {})
    if old.get("timer"):
        old["timer"].cancel()
    S[uid] = {"photos": [], "status": None, "timer": None}


def allowed(uid):
    return uid in config.ALLOWED_IDS


def e(s):
    return html.escape(str(s))


def money(x):
    return f"{x:,.2f}".replace(",", " ")


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


async def send_long(chat_id, text, markup=None):
    """Telegram режет сообщения длиннее 4096 символов — делим по строкам."""
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    parts.append(cur)
    for i, p in enumerate(parts):
        await bot.send_message(chat_id, p, parse_mode="HTML",
                               reply_markup=markup if i == len(parts) - 1 else None)


# ------------------------------------------------------------------ приём фото
@dp.message(CommandStart())
async def cmd_start(m: Message):
    if not allowed(m.from_user.id):
        await m.answer(f"Ваш Telegram ID: {m.from_user.id}\nДобавьте его в ALLOWED_IDS в config.py.")
        return
    reset(m.from_user.id)
    await m.answer("Пришлите фото накладной — все страницы (можно альбомом).\n"
                   "Лучше отправлять «как файл» — без сжатия Telegram, так цифры читаются точнее.\n\n"
                   "/refresh — перезагрузить номенклатуру из 1С\n/cancel — начать заново")


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    reset(m.from_user.id)
    await m.answer("Сброшено. Присылайте фото новой накладной.")


@dp.message(Command("refresh"))
async def cmd_refresh(m: Message):
    if not allowed(m.from_user.id):
        return
    await m.answer("Загружаю номенклатуру из 1С…")
    try:
        n = await asyncio.to_thread(c1.load_items, True)
        await m.answer(f"Готово, позиций: {n}.")
    except Exception as ex:
        await m.answer(f"Ошибка: {e(ex)}", parse_mode="HTML")


async def show_photo_status(uid, chat_id):
    await asyncio.sleep(1.5)            # ждём, пока долетит весь альбом
    s = st(uid)
    if s.get("status"):
        try:
            await bot.delete_message(chat_id, s["status"])
        except Exception:
            pass
    msg = await bot.send_message(chat_id, f"Фото получено: {len(s['photos'])}. Если это все страницы — жмите «Распознать».",
                                 reply_markup=kb([Btn(text="🔍 Распознать", callback_data="go"),
                                                  Btn(text="✖ Сбросить", callback_data="cancel")]))
    s["status"] = msg.message_id


@dp.message(F.photo | F.document)
async def on_photo(m: Message):
    uid = m.from_user.id
    if not allowed(uid):
        return
    if m.document and not (m.document.mime_type or "").startswith("image/"):
        await m.answer("Принимаю только фотографии (jpg, png, heic).")
        return
    s = st(uid)
    if s.get("rec"):                    # дослали забытую страницу — распознаем всё заново вместе с ней
        for k in ("rec", "lines", "ready", "ka", "asking", "search_for"):
            s.pop(k, None)
        await m.answer("Добавляю страницу к этой накладной — распознаю заново все фото.\n"
                       "Если это другая накладная — сначала /cancel.")
    f = m.document if m.document else m.photo[-1]
    buf = await bot.download(f)
    s["photos"].append(buf.read())
    if s.get("timer"):
        s["timer"].cancel()
    s["timer"] = asyncio.create_task(show_photo_status(uid, m.chat.id))


# ------------------------------------------------------------------ живой индикатор хода работы
ICON = {"run": "⏳", "ok": "✅", "warn": "⚠️", "err": "❌", "wait": "▫️"}


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
    """Сообщение в Telegram, которое само перерисовывается каждые 3 секунды, пока идёт работа."""
    def __init__(self, msg, steps, title):
        self.msg, self.steps, self.title = msg, steps, title
        self.last = ""
        self.task = asyncio.create_task(self._loop())

    async def _draw(self, finished=False):
        txt = render(self.steps, self.title, finished)
        if txt != self.last:
            try:
                await self.msg.edit_text(txt, parse_mode="HTML")
                self.last = txt
            except Exception:
                pass            # «message is not modified» и лимиты Telegram — пропускаем кадр

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
@dp.callback_query(F.data.in_({"go", "other_model"}))
async def cb_go(c: CallbackQuery):
    uid = c.from_user.id
    s = st(uid)
    if not s["photos"]:
        await c.answer("Сначала пришлите фото", show_alert=True)
        return
    await c.answer()
    start = 0
    if c.data == "other_model" and s.get("rec"):
        used = [m["name"] for m in MODELS].index(s["rec"]["model"]) if s["rec"]["model"] in [m["name"] for m in MODELS] else -1
        start = (used + 1) % len(MODELS)
    await c.message.edit_reply_markup(reply_markup=None)
    steps = rec_mod.Steps()
    title = f"🔍 Распознаю накладную · {len(s['photos'])} фото"
    msg = await c.message.answer(render(steps, title), parse_mode="HTML")
    live = Live(msg, steps, title)
    try:
        r = await asyncio.to_thread(rec_mod.recognize, s["photos"], MODELS, log.info, start, steps)
    except Exception as ex:
        log.exception("recognize")
        await live.close("❌ Не получилось распознать")
        await c.message.answer(f"{e(ex)}", parse_mode="HTML",
                               reply_markup=kb([Btn(text="🔁 Ещё раз", callback_data="go")]))
        return
    s["rec"] = r
    s["live"] = live
    await prepare(uid, c.message.chat.id)


async def prepare(uid, chat_id):
    """Поставщик + сопоставление строк, затем вопросы по неизвестным позициям."""
    s = st(uid)
    d = s["rec"]["data"]
    inn = d["supplier"].get("inn", "")
    live = s.pop("live", None)
    step = live.steps.add("onec", "Ищу поставщика и товары в 1С") if live else None
    try:
        if not c1.items:
            await asyncio.to_thread(c1.load_items)
        ka = await asyncio.to_thread(c1.find_supplier, inn, d["supplier"].get("name", ""))
        if step:
            known = sum(1 for ln in d["lines"] if c1.match_line(inn, ln)["key"])
            live.steps.finish(step, "ok" if ka else "warn",
                              (f"поставщик найден, знакомых товаров {known} из {len(d['lines'])}" if ka
                               else "поставщик не найден"))
            await live.close(f"✅ Накладная прочитана за {int(live.steps.elapsed())} с")
    except Exception as ex:
        if step:
            live.steps.finish(step, "err", "нет связи с 1С")
            await live.close("⚠️ Накладная прочитана, но 1С не ответила")
        await bot.send_message(chat_id, f"❌ Ошибка связи с 1С: {e(ex)}", parse_mode="HTML",
                               reply_markup=kb([Btn(text="🔁 Повторить", callback_data="sup_retry")]))
        return
    s["ka"], s["inn"] = ka, inn
    if not ka:
        await bot.send_message(
            chat_id,
            f"Поставщик <b>{e(d['supplier'].get('name', '?'))}</b>, ИНН <code>{e(inn or 'не прочитан')}</code> "
            f"не найден в 1С.\nЗаведите контрагента в 1С (с этим ИНН) и нажмите «Найти снова».",
            parse_mode="HTML",
            reply_markup=kb([Btn(text="🔁 Найти снова", callback_data="sup_retry"),
                             Btn(text="✖ Отмена", callback_data="cancel")]))
        return
    s["lines"] = []
    for ln in d["lines"]:
        mt = c1.match_line(inn, ln)
        s["lines"].append({"line": ln, **mt, "skip": False})
    await next_step(uid, chat_id)


@dp.callback_query(F.data == "sup_retry")
async def cb_sup_retry(c: CallbackQuery):
    await c.answer()
    if st(c.from_user.id).get("rec"):
        await prepare(c.from_user.id, c.message.chat.id)


async def next_step(uid, chat_id):
    """Спрашиваем первую несопоставленную строку; если таких нет — итог с кнопкой «Провести»."""
    s = st(uid)
    for i, L in enumerate(s["lines"]):
        if not L["key"] and not L["skip"]:
            await ask_line(uid, chat_id, i)
            return
    await summary(uid, chat_id)


async def ask_line(uid, chat_id, i, cands=None):
    s = st(uid)
    L = s["lines"][i]
    s["asking"] = i
    s["search_for"] = None
    cands = cands if cands is not None else L["cands"]
    s["shown"] = [it for it, _ in cands]
    left = sum(1 for x in s["lines"] if not x["key"] and not x["skip"])
    rows = [[Btn(text=f"{it['name'][:55]} ({sc}%)", callback_data=f"pick:{i}:{k}")] for k, (it, sc) in enumerate(cands)]
    rows.append([Btn(text="🔎 Найти по слову", callback_data=f"find:{i}"),
                 Btn(text="⏭ Не приходовать", callback_data=f"skip:{i}")])
    ln = L["line"]
    art = f"\nАртикул поставщика: {e(ln['article'])}" if ln.get("article") else ""
    await bot.send_message(
        chat_id,
        f"❓ Строка {ln['n']} (осталось неясных: {left})\n<b>{e(ln['name'])}</b>{art}\n"
        f"{ln['qty']:g} {e(ln['unit'])} × {money(ln['price'])} = {money(ln['sum'])}\n\n"
        f"Что это у нас в 1С? Выбор запомнится — в следующий раз не спрошу.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(c: CallbackQuery):
    uid = c.from_user.id
    s = st(uid)
    _, i, k = c.data.split(":")
    i, k = int(i), int(k)
    if "lines" not in s or i >= len(s["lines"]) or k >= len(s.get("shown", [])):
        await c.answer("Устарело", show_alert=True)
        return
    it = s["shown"][k]
    L = s["lines"][i]
    L.update(key=it["key"], name1c=it["name"], how="выбрано", skip=False)
    onec.remember(s["inn"], L["line"], it["key"], it["name"])
    await c.answer("Запомнил")
    await c.message.edit_text(f"✅ Строка {L['line']['n']}: {e(L['line']['name'])}\n→ <b>{e(it['name'])}</b>",
                              parse_mode="HTML")
    await next_step(uid, c.message.chat.id)


@dp.callback_query(F.data.startswith("skip:"))
async def cb_skip(c: CallbackQuery):
    s = st(c.from_user.id)
    i = int(c.data.split(":")[1])
    if "lines" not in s or i >= len(s["lines"]):
        await c.answer("Устарело", show_alert=True)
        return
    s["lines"][i]["skip"] = True
    await c.answer()
    await c.message.edit_text(f"⏭ Строка {s['lines'][i]['line']['n']} не будет оприходована.")
    await next_step(c.from_user.id, c.message.chat.id)


@dp.callback_query(F.data.startswith("find:"))
async def cb_find(c: CallbackQuery):
    s = st(c.from_user.id)
    s["search_for"] = int(c.data.split(":")[1])
    await c.answer()
    await c.message.answer("Напишите часть названия товара, как он называется у нас в 1С:")


# ------------------------------------------------------------------ итог и правки
async def summary(uid, chat_id):
    s = st(uid)
    r = s["rec"]
    d, chk = r["data"], r["check"]
    ka = s["ka"]
    use = [L for L in s["lines"] if L["key"] and not L["skip"]]
    total = sum(L["line"]["sum"] for L in use)
    doc_total = d["total"].get("sum")
    lines_txt = []
    for L in s["lines"]:
        ln = L["line"]
        mark = "⏭" if L["skip"] else ("⚠️" if ln["n"] in chk["bad_lines"] else "✅")
        tail = "не приходуется" if L["skip"] else e(L["name1c"])
        lines_txt.append(f"{mark} {ln['n']}. {e(ln['name'][:60])}\n"
                         f"     {ln['qty']:g} × {money(ln['price'])} = <b>{money(ln['sum'])}</b> → {tail}")
    head = (f"📄 <b>{e(d.get('doc_type', 'Документ'))} № {e(d.get('number', '?'))} от {e(d.get('date', '?'))}</b>\n"
            f"Поставщик: {e(ka['Description'])} (ИНН {e(ka.get('ИНН') or s['inn'])})\n"
            f"Склад: {e(getattr(config, 'WAREHOUSE', '') or 'как в прошлом поступлении')}\n"
            f"Распознал: {e(r['model'])}\n")
    if chk["issues"]:
        head += "\n⚠️ <b>Проверьте:</b>\n" + "\n".join("• " + e(x) for x in chk["issues"][:10]) + "\n"
    else:
        head += "\n✔️ Все суммы сошлись с итогом документа.\n"
    foot = (f"\nК приходу: строк {len(use)} из {len(s['lines'])}, сумма <b>{money(total)}</b>"
            + (f" (итог в документе {money(doc_total)})" if doc_total else "") +
            "\n\n<i>Поправить строку — напишите сообщением:</i>\n"
            "<code>3 = 5 x 450</code> — строка 3: 5 шт по 450\n"
            "<code>3 сумма 2250</code> — сумма строки 3\n"
            "<code>3 ?</code> — заново выбрать товар для строки 3\n"
            "<code>3 удалить</code> — не приходовать строку 3")
    s["ready"] = True
    await send_long(chat_id, head + "\n" + "\n".join(lines_txt) + "\n" + foot,
                    kb([Btn(text="✅ Провести в 1С", callback_data="post")],
                       [Btn(text="🔁 Другой нейросетью", callback_data="other_model"),
                        Btn(text="✖ Отмена", callback_data="cancel")]))


EDIT_QP = re.compile(r"^\s*(\d+)\s*[=:]\s*([\d\s.,]+?)\s*[xхX×*]\s*([\d\s.,]+)\s*$")
EDIT_SUM = re.compile(r"^\s*(\d+)\s+сумма\s+([\d\s.,]+)\s*$", re.I)
EDIT_DEL = re.compile(r"^\s*(\d+)\s+(удалить|убрать|нет)\s*$", re.I)
EDIT_ASK = re.compile(r"^\s*(\d+)\s*\?\s*$")


def line_by_n(s, n):
    for i, L in enumerate(s.get("lines") or []):
        if L["line"]["n"] == n:
            return i, L
    return None, None


@dp.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    if not allowed(uid):
        return
    s = st(uid)
    t = m.text.strip()
    if s.get("search_for") is not None:
        i = s["search_for"]
        res = c1.search(t, 6)
        if not res:
            await m.answer("Ничего не нашёл. Попробуйте другое слово или /refresh, если товар только что заведён.")
            return
        await ask_line(uid, m.chat.id, i, res)
        return
    if not s.get("lines"):
        await m.answer("Пришлите фото накладной.")
        return
    for rx, kind in ((EDIT_QP, "qp"), (EDIT_SUM, "sum"), (EDIT_DEL, "del"), (EDIT_ASK, "ask")):
        mt = rx.match(t)
        if not mt:
            continue
        i, L = line_by_n(s, int(mt.group(1)))
        if L is None:
            await m.answer(f"Строки {mt.group(1)} нет.")
            return
        ln = L["line"]
        if kind == "qp":
            ln["qty"] = rec_mod.num(mt.group(2)) or ln["qty"]
            ln["price"] = rec_mod.num(mt.group(3)) or ln["price"]
            k = 1 + rec_mod.vat_percent(ln["vat_rate"]) / 100
            ln["sum_without_vat"] = round(ln["qty"] * ln["price"], 2)
            ln["sum"] = ln["sum_without_vat"] if rec_mod.vat_percent(ln["vat_rate"]) == 0 else round(ln["sum_without_vat"] * k, 2)
            ln["vat_sum"] = round(ln["sum"] - ln["sum_without_vat"], 2)
        elif kind == "sum":
            ln["sum"] = rec_mod.num(mt.group(2)) or ln["sum"]
        elif kind == "del":
            L["skip"] = True
        elif kind == "ask":
            onec.forget(s["inn"], ln)
            L.update(key=None, name1c="", how="", skip=False, cands=c1.search(ln["name"], 4))
            await ask_line(uid, m.chat.id, i)
            return
        if kind in ("qp", "sum"):
            ln["unclear"] = False
            s["rec"]["check"] = rec_mod.check(s["rec"]["data"])
        await summary(uid, m.chat.id)
        return
    await m.answer("Не понял. Пример правки: <code>3 = 5 x 450</code>", parse_mode="HTML")


# ------------------------------------------------------------------ проведение
@dp.callback_query(F.data.in_({"post", "post_force"}))
async def cb_post(c: CallbackQuery):
    uid = c.from_user.id
    s = st(uid)
    if not s.get("ready") or not s.get("ka"):
        await c.answer("Нечего проводить", show_alert=True)
        return
    use = [{"line": L["line"], "key": L["key"]} for L in s["lines"] if L["key"] and not L["skip"]]
    if not use:
        await c.answer("Нет ни одной сопоставленной строки", show_alert=True)
        return
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    d = s["rec"]["data"]
    if c.data == "post":
        dup = await asyncio.to_thread(c1.find_duplicate, s["ka"]["Ref_Key"], d.get("number"))
        if dup:
            await c.message.answer(
                f"⚠️ В 1С уже есть поступление № {e(dup['Number'].strip())} от {e(dup['Date'][:10])} "
                f"по документу поставщика № {e(d.get('number'))}. Создать ещё одно?",
                parse_mode="HTML", reply_markup=kb([Btn(text="Да, создать", callback_data="post_force"),
                                                    Btn(text="✖ Нет", callback_data="cancel")]))
            return
    steps = rec_mod.Steps()
    steps.EXPECT = dict(steps.EXPECT, onec=10)
    msg = await c.message.answer(render(steps, "📦 Провожу поступление в 1С"), parse_mode="HTML")
    live = Live(msg, steps, "📦 Провожу поступление в 1С")
    st1 = steps.add("onec", "Беру настройки из прошлого поступления")
    try:
        def job():
            tpl, own = c1.template(s["ka"])
            steps.finish(st1, "ok", "от этого поставщика" if own else "поставщик новый — образец от другого")
            st2 = steps.add("onec", f"Записываю документ ({len(use)} строк) и провожу")
            doc = c1.build(s["ka"], d, use, tpl, own)
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
        await c.message.answer(e(ex), parse_mode="HTML",
                               reply_markup=kb([Btn(text="🔁 Повторить", callback_data="post_force")]))
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
    await c.message.answer(text, parse_mode="HTML")
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


@dp.callback_query(F.data == "cancel")
async def cb_cancel(c: CallbackQuery):
    reset(c.from_user.id)
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Отменено. Присылайте фото новой накладной.")


async def main():
    log.info("Запуск. Модели: %s", ", ".join(m["name"] for m in MODELS) or "НЕТ — заполните MODELS в config.py")
    try:
        await asyncio.to_thread(c1.load_items)
    except Exception as ex:
        log.error("Номенклатура не загрузилась (проверьте 1С): %s", ex)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
