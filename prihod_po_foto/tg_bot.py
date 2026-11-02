# -*- coding: utf-8 -*-
"""
Переходник бота «Приход по фото» для Telegram (библиотека aiogram).
Вся логика — в flow.py. Telegram из России может требовать VPN — на сервере лучше MAX (max_bot.py).
"""
import asyncio

import config
import flow
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

session = AiohttpSession(proxy=config.TELEGRAM_PROXY) if getattr(config, "TELEGRAM_PROXY", "") else None
bot = Bot(token=config.TELEGRAM_TOKEN, session=session) if session else Bot(token=config.TELEGRAM_TOKEN)
dp = Dispatcher()
log = flow.log

# постоянные кнопки внизу экрана (в Telegram они есть, в MAX — нет)
MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=flow.B_TABLE)],
                                     [KeyboardButton(text=flow.B_REFRESH), KeyboardButton(text=flow.B_RESET)],
                                     [KeyboardButton(text=flow.B_RESTART)]],
                           resize_keyboard=True, is_persistent=True,
                           input_field_placeholder="Пришлите фото накладной")


def _inline(kb):
    if not kb:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=p) for t, p in row]
                                                 for row in kb])


class TgUI:
    name = "Telegram"
    max_len = 3900
    file_hint = "Лучше отправлять «как файл» (скрепка → Файл) — без сжатия Telegram цифры читаются точнее."

    async def send(self, chat, text, kb=None, menu=False):
        markup = _inline(kb) or (MENU if menu else None)
        m = await bot.send_message(chat, text, parse_mode="HTML", reply_markup=markup)
        return m.message_id

    async def edit(self, chat, ref, text, kb=None):
        await bot.edit_message_text(text, chat_id=chat, message_id=ref, parse_mode="HTML",
                                    reply_markup=_inline(kb))

    async def delete(self, chat, ref):
        await bot.delete_message(chat, ref)

    async def drop_kb(self, cb):
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    async def toast(self, cb, text="", alert=False):
        try:
            await cb.answer(text or None, show_alert=alert)
        except Exception:
            pass                # на одно нажатие — один ответ; повторный просто пропускаем

    async def send_file(self, chat, path, filename, caption):
        await bot.send_document(chat, FSInputFile(path, filename=filename), caption=caption)


@dp.message(CommandStart())
async def on_start(m: Message):
    await flow.on_start(m.from_user.id, m.chat.id)


@dp.message(F.photo | F.document)
async def on_file(m: Message):
    uid, chat = m.from_user.id, m.chat.id
    if m.document and (m.document.file_name or "").lower().endswith(".xlsx"):
        buf = await bot.download(m.document)
        await flow.on_xlsx(uid, chat, buf.read())
        return
    if m.document and not (m.document.mime_type or "").startswith("image/"):
        await flow.ui.send(chat, "Принимаю фото накладных (jpg, png, heic) и таблицу сопоставлений (.xlsx).")
        return
    buf = await bot.download(m.document if m.document else m.photo[-1])
    await flow.on_image(uid, chat, buf.read())


@dp.message(F.text)
async def on_text(m: Message):
    flow.run(flow.on_text(m.from_user.id, m.chat.id, m.text), m.chat.id)


@dp.callback_query()
async def on_callback(c: CallbackQuery):
    flow.run(flow.on_callback(c.from_user.id, c.message.chat.id, c.message.message_id, c.data or "", c),
             c.message.chat.id)


async def main():
    flow.ui = TgUI()
    flow.banner("Telegram")
    print("  Проверяю связь с Telegram…")
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=30)
    except Exception as ex:
        print(f"\n  ✖ Telegram не отвечает: {ex}\n"
              "    Скорее всего, Telegram недоступен без VPN. Переключите бота на MAX: PLATFORM = \"max\" в config.py.\n")
        raise SystemExit(1)
    print(f"  ✔ Telegram: бот @{me.username} на связи.")
    print("  Напишите боту /start в Telegram. Это окно не закрывайте — бот работает, пока оно открыто.\n")
    flow.start_background()
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Начало и кнопки"),
                                   BotCommand(command="sopost", description="Таблица сопоставлений"),
                                   BotCommand(command="refresh", description="Обновить товары из 1С"),
                                   BotCommand(command="cancel", description="Начать заново")])
    except Exception as ex:
        log.warning("Меню команд не установлено: %s", ex)
    await dp.start_polling(bot, handle_signals=False)


def run():
    asyncio.run(main())
