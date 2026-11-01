# -*- coding: utf-8 -*-
"""
Переходник бота «Приход по фото» для мессенджера MAX (библиотека maxapi).
Вся логика — в flow.py; здесь только «как отправить / изменить / удалить сообщение в MAX».
MAX работает в России без VPN, поэтому бот может жить прямо на сервере 1С.
"""
import asyncio
import os

import requests

import config
import flow
from maxapi import Bot, Dispatcher
from maxapi.enums.parse_mode import ParseMode
from maxapi.types import BotStarted, CallbackButton, InputMedia, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

bot = Bot(token=config.MAX_TOKEN, auto_requests=False)
dp = Dispatcher()
log = flow.log


def _markup(kb, menu=False):
    rows = list(kb or []) + (flow.MENU_KB if menu else [])
    if not rows:
        return []
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[CallbackButton(text=t[:64], payload=p) for t, p in row])
    return [b.as_markup()]


class MaxUI:
    name = "MAX"
    max_len = 3800
    file_hint = ("Лучше отправлять фото «файлом» (скрепка → Файл) — без сжатия цифры читаются точнее.")

    async def send(self, chat, text, kb=None, menu=False):
        r = await bot.send_message(chat_id=chat, text=text, attachments=_markup(kb, menu), format=ParseMode.HTML)
        return r.message.body.mid if r and r.message and r.message.body else None

    async def edit(self, chat, ref, text, kb=None):
        if ref:
            await bot.edit_message(message_id=ref, text=text, attachments=_markup(kb), format=ParseMode.HTML)

    async def delete(self, chat, ref):
        if ref:
            await bot.delete_message(message_id=ref)

    async def drop_kb(self, cb):
        """Убрать кнопки у сообщения, где нажали: тот же текст, без кнопок."""
        try:
            body = cb.message.body
            text = body.html_text or body.text
            await bot.edit_message(message_id=body.mid, text=text, attachments=[], format=ParseMode.HTML)
        except Exception as ex:
            log.debug("drop_kb: %s", ex)

    async def toast(self, cb, text="", alert=False):
        try:
            await cb.ack(notification=text or None)
        except Exception:
            pass                # на одно нажатие — один ответ; повторный просто пропускаем

    async def send_file(self, chat, path, filename, caption):
        await bot.send_message(chat_id=chat, text=caption, attachments=[InputMedia(path)])


def _download(url):
    """Скачать вложение MAX (фото или файл)."""
    r = requests.get(url, timeout=120)
    if r.status_code in (401, 403):
        r = requests.get(url, timeout=120, headers={"Authorization": config.MAX_TOKEN})
    r.raise_for_status()
    return r.content


@dp.bot_started()
async def on_bot_started(event: BotStarted):
    await flow.on_start(event.user.user_id, event.chat_id)


@dp.message_created()
async def on_message(event: MessageCreated):
    m = event.message
    if not m.sender or not m.body:
        return
    uid, chat = m.sender.user_id, m.recipient.chat_id
    if getattr(m.sender, "is_bot", False):
        return
    atts = m.body.attachments or []
    for a in atts:
        kind = str(getattr(a.type, "value", a.type))
        url = getattr(getattr(a, "payload", None), "url", None)
        if not url:
            continue
        name = (getattr(a, "filename", None) or "").lower()
        if kind == "file" and name.endswith(".xlsx"):
            data = await asyncio.to_thread(_download, url)
            asyncio.create_task(flow.on_xlsx(uid, chat, data))
        elif kind == "image" or (kind == "file" and name.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp"))):
            try:
                data = await asyncio.to_thread(_download, url)
            except Exception as ex:
                await flow.ui.send(chat, f"Не удалось скачать фото: {flow.e(ex)}")
                continue
            await flow.on_image(uid, chat, data)
        elif kind == "file":
            await flow.ui.send(chat, "Принимаю фото накладных (jpg, png, heic) и таблицу сопоставлений (.xlsx).")
    if m.body.text and not atts:
        asyncio.create_task(flow.on_text(uid, chat, m.body.text))


@dp.message_callback()
async def on_callback(event: MessageCallback):
    uid = event.callback.user.user_id
    chat = event.message.recipient.chat_id if event.message else None
    ref = event.message.body.mid if event.message and event.message.body else None
    asyncio.create_task(flow.on_callback(uid, chat, ref, event.callback.payload or "", event))


async def main():
    flow.ui = MaxUI()
    flow.banner("MAX")
    print("  Проверяю связь с MAX…")
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=30)
    except Exception as ex:
        print(f"\n  ✖ MAX не отвечает или неверный токен: {ex}\n"
              "    Проверьте MAX_TOKEN в config.py (токен бота из кабинета MAX для партнёров).\n")
        raise SystemExit(1)
    print(f"  ✔ MAX: бот «{getattr(me, 'first_name', None) or getattr(me, 'name', '')}» на связи.")
    print("  Напишите боту в MAX «/start». Окно не закрывайте — бот работает, пока оно открыто.\n")
    flow.start_background()
    await dp.start_polling(bot)


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
