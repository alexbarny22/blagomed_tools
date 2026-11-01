# -*- coding: utf-8 -*-
"""
Телеграм-бот: фотографируете этикетки коробок -> бот копит их в корзину ->
по кнопке "Готово" создаёт в 1С УТ 11.5 непроведённые "Заказы клиента",
отдельный документ на каждый маркетплейс.

Запуск:  python bot.py
"""

import asyncio
import configparser
import logging
import os
import tempfile

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from rapidfuzz import fuzz, process

import label
import onec

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("zakaz")

cfg = configparser.ConfigParser()
cfg.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")
ALLOWED = {int(x) for x in cfg["telegram"]["allowed_ids"].split(",") if x.strip().isdigit()}
PARTNERS = {k.upper(): v for k, v in cfg["partners"].items()}

bot = Bot(token=cfg["telegram"]["token"])
dp = Dispatcher()

carts = {}    # user_id -> [ {code, name, guid, qty, mp}, ... ]
pending = {}  # user_id -> незавершённая позиция, ждущая уточнения
nomencl = []  # локальный кеш номенклатуры


def allowed(m):
    return m.from_user.id in ALLOWED


# ---------- 1С в отдельном потоке ----------

def _refresh():
    with onec.OneC(cfg) as c:
        return c.dump_nomenclature()


def _make_orders(groups, comment):
    """groups: {"ВБ": [ {guid, qty}, ... ], ...} -> [(мп, номер, дата), ...]"""
    made = []
    with onec.OneC(cfg) as c:
        for mp, lines in groups.items():
            tmpl = cfg["templates"].get(mp.lower(), "").strip()
            num, date = c.create_order(PARTNERS[mp], lines, tmpl, comment)
            made.append((mp, num, date))
    return made


async def refresh():
    global nomencl
    nomencl = await asyncio.to_thread(_refresh)
    return len(nomencl)


def search_nomencl(text, limit=6):
    if not nomencl:
        return []
    keys = [f'{i["name"]} {i["art"]}' for i in nomencl]
    hits = process.extract(text, keys, scorer=fuzz.WRatio, limit=limit)
    return [nomencl[i] for _, sc, i in hits if sc >= 50]


# ---------- корзина ----------

def cart_text(uid):
    items = carts.get(uid, [])
    if not items:
        return "Корзина пуста. Пришлите фото этикетки."
    out = ["<b>Корзина</b>", ""]
    for mp in sorted({i["mp"] for i in items}):
        out.append(f"<u>{mp} → {PARTNERS.get(mp, '?')}</u>")
        agg = {}
        for i in items:
            if i["mp"] != mp:
                continue
            a = agg.setdefault(i["code"], {"name": i["name"], "boxes": 0, "total": 0})
            a["boxes"] += 1
            a["total"] += i["qty"]
        for code, a in agg.items():
            box = f"{a['boxes']} кор. × " if a["boxes"] > 1 else ""
            out.append(f"  {code} · {a['name'][:40]} — {box}<b>{a['total']}</b> шт.")
        out.append("")
    out.append(f"Всего коробок: {len(items)}")
    return "\n".join(out)


def kb_cart(uid):
    items = carts.get(uid, [])
    rows = []
    if items:
        rows.append([
            InlineKeyboardButton(text="✅ Готово — создать заказы", callback_data="make"),
        ])
        rows.append([
            InlineKeyboardButton(text="↩️ Убрать последнюю", callback_data="pop"),
            InlineKeyboardButton(text="🗑 Очистить", callback_data="clear"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_cart(msg_or_cb, uid, prefix=""):
    text = (prefix + "\n\n" if prefix else "") + cart_text(uid)
    if isinstance(msg_or_cb, CallbackQuery):
        await msg_or_cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_cart(uid))
    else:
        await msg_or_cb.answer(text, parse_mode="HTML", reply_markup=kb_cart(uid))


# ---------- команды ----------

@dp.message(CommandStart())
async def start(m: Message):
    if not allowed(m):
        await m.answer(f"Доступ закрыт. Ваш ID: <code>{m.from_user.id}</code>", parse_mode="HTML")
        return
    await m.answer(
        "Фотографируйте этикетки коробок — я соберу их в корзину.\n\n"
        "/cart — показать корзину\n"
        "/clear — очистить\n"
        "/refresh — обновить номенклатуру из 1С\n"
        "/ping — проверить связь с 1С"
    )


@dp.message(Command("ping"))
async def ping(m: Message):
    if not allowed(m):
        return
    try:
        n = await refresh()
        await m.answer(f"1С отвечает. Номенклатуры: {n}. Кодов в справочнике: {len(label.load_codes())}")
    except Exception as e:
        await m.answer(f"Ошибка 1С:\n<code>{e}</code>", parse_mode="HTML")


@dp.message(Command("refresh"))
async def do_refresh(m: Message):
    if not allowed(m):
        return
    msg = await m.answer("Обновляю…")
    try:
        await msg.edit_text(f"Готово, позиций: {await refresh()}")
    except Exception as e:
        await msg.edit_text(f"Ошибка:\n<code>{e}</code>", parse_mode="HTML")


@dp.message(Command("cart"))
async def cmd_cart(m: Message):
    if allowed(m):
        await show_cart(m, m.from_user.id)


@dp.message(Command("clear"))
async def cmd_clear(m: Message):
    if allowed(m):
        carts.pop(m.from_user.id, None)
        pending.pop(m.from_user.id, None)
        await m.answer("Корзина очищена.")


# ---------- фото ----------

@dp.message(F.photo)
async def on_photo(m: Message):
    if not allowed(m):
        return
    uid = m.from_user.id
    msg = await m.answer("Читаю этикетку…")
    path = os.path.join(tempfile.gettempdir(), f"lbl_{uid}.jpg")
    await bot.download(m.photo[-1], destination=path)

    try:
        r = await asyncio.to_thread(
            label.recognize, path, cfg["ocr"]["tesseract"], cfg["ocr"]["lang"]
        )
    except Exception as e:
        await msg.edit_text(f"Не смог распознать:\n<code>{e}</code>", parse_mode="HTML")
        return

    if not r["code"]:
        await msg.edit_text(
            "Код товара на этикетке не разобрал.\n"
            f"Что увидел: <code>{r['raw'][:200] or 'пусто'}</code>\n"
            "Переснимите поближе или пришлите код текстом в формате МН-01.",
            parse_mode="HTML",
        )
        return

    codes = label.load_codes()
    item = {
        "code": r["code"],
        "qty": r["qty"],
        "mp": r["mp"],
        "guid": codes.get(r["code"], {}).get("guid", ""),
        "name": codes.get(r["code"], {}).get("name", ""),
    }
    pending[uid] = item
    await msg.delete()
    await next_step(m, uid)


async def next_step(m, uid, note=""):
    """Дозапрашивает недостающее, потом кладёт позицию в корзину."""
    it = pending.get(uid)
    if not it:
        return

    if not it["guid"]:
        await m.answer(
            f"{note}\nКод <b>{it['code']}</b> мне незнаком.\n"
            "Пришлите текстом часть названия товара — найду в номенклатуре 1С.",
            parse_mode="HTML",
        )
        return

    if not it["qty"]:
        await m.answer(f"Количество на этикетке не разобрал. Пришлите число для {it['code']}.")
        return

    if not it["mp"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=mp, callback_data=f"mp:{mp}") for mp in PARTNERS
        ]])
        await m.answer("Маркетплейс не разобрал. Выберите:", reply_markup=kb)
        return

    carts.setdefault(uid, []).append(it)
    pending.pop(uid, None)
    await show_cart(m, uid, f"➕ {it['code']} · {it['name'][:40]} — {it['qty']} шт. ({it['mp']})")


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message):
    if not allowed(m):
        return
    uid = m.from_user.id
    it = pending.get(uid)
    if not it:
        return

    if not it["guid"]:
        found = search_nomencl(m.text.strip())
        if not found:
            await m.answer("Ничего похожего не нашёл. Попробуйте другие слова.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f["name"][:60], callback_data=f"nom:{n}")]
            for n, f in enumerate(found)
        ])
        it["found"] = found
        await m.answer(f"Что такое <b>{it['code']}</b>?", parse_mode="HTML", reply_markup=kb)
        return

    if not it["qty"]:
        txt = m.text.strip().replace(",", ".")
        if not txt.replace(".", "").isdigit():
            await m.answer("Нужно число.")
            return
        it["qty"] = int(float(txt))
        await next_step(m, uid)


# ---------- колбэки ----------

@dp.callback_query(F.data.startswith("nom:"))
async def pick_nom(c: CallbackQuery):
    uid = c.from_user.id
    it = pending.get(uid)
    if not it or "found" not in it:
        await c.answer()
        return
    chosen = it["found"][int(c.data.split(":")[1])]
    it["guid"], it["name"] = chosen["guid"], chosen["name"]
    it.pop("found", None)
    label.save_code(it["code"], chosen["guid"], chosen["name"])
    await c.message.edit_text(f"Запомнил: <b>{it['code']}</b> = {chosen['name']}", parse_mode="HTML")
    await c.answer()
    await next_step(c.message, uid)


@dp.callback_query(F.data.startswith("mp:"))
async def pick_mp(c: CallbackQuery):
    uid = c.from_user.id
    it = pending.get(uid)
    if not it:
        await c.answer()
        return
    it["mp"] = c.data.split(":")[1]
    await c.message.delete()
    await c.answer()
    await next_step(c.message, uid)


@dp.callback_query(F.data == "pop")
async def pop(c: CallbackQuery):
    items = carts.get(c.from_user.id, [])
    if items:
        items.pop()
    await show_cart(c, c.from_user.id)
    await c.answer("Убрал")


@dp.callback_query(F.data == "clear")
async def clear(c: CallbackQuery):
    carts.pop(c.from_user.id, None)
    await c.message.edit_text("Корзина очищена.")
    await c.answer()


@dp.callback_query(F.data == "make")
async def make(c: CallbackQuery):
    uid = c.from_user.id
    items = carts.get(uid, [])
    if not items:
        await c.answer("Корзина пуста", show_alert=True)
        return
    await c.message.edit_text("Создаю заказы в 1С…")

    groups = {}
    for i in items:
        groups.setdefault(i["mp"], []).append({"guid": i["guid"], "qty": i["qty"]})

    try:
        made = await asyncio.to_thread(
            _make_orders, groups, f"Создан ботом по фото этикеток, @{c.from_user.username}"
        )
    except Exception as e:
        log.exception("create_order failed")
        await c.message.edit_text(
            f"1С вернула ошибку:\n<code>{e}</code>\n\nКорзина сохранена, можно повторить.",
            parse_mode="HTML",
        )
        await c.answer()
        return

    carts.pop(uid, None)
    lines = [f"• {mp}: заказ <b>№{num}</b> от {date}" for mp, num, date in made]
    await c.message.edit_text(
        "✅ Создано:\n" + "\n".join(lines) +
        "\n\nДокументы непроведённые — откройте в 1С, заполните цены и проведите.",
        parse_mode="HTML",
    )
    await c.answer()


async def main():
    try:
        log.info("Номенклатура загружена: %s", await refresh())
    except Exception as e:
        log.error("1С недоступна на старте: %s", e)
        globals()["nomencl"] = onec.load_cache()
        log.info("Работаю на кеше: %s позиций", len(nomencl))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
