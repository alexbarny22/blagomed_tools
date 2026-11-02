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
        """Отправить сообщение. Если MAX его не принял — повторить простым текстом, потом без кнопок."""
        last = None
        for how in ("html", "text", "text_nokb"):
            try:
                if how == "html":
                    r = await bot.send_message(chat_id=chat, text=text, attachments=_markup(kb, menu),
                                               format=ParseMode.HTML)
                else:
                    r = await bot.send_message(chat_id=chat, text=_plain(text),
                                               attachments=_markup(kb, menu) if how == "text" else [])
                mid = getattr(getattr(getattr(r, "message", None), "body", None), "mid", None)
                if not mid:
                    raise RuntimeError(f"MAX не принял сообщение: {str(r)[:300]}")
                if how != "html":
                    log.warning("Сообщение ушло только способом «%s» (до этого: %s)", how, last)
                return mid
            except Exception as ex:
                last = ex
                log.warning("MAX: отправка (%s) не прошла: %s | текст: %s", how, ex, text[:120].replace("\n", " "))
        raise last

    async def edit(self, chat, ref, text, kb=None):
        if ref:
            try:
                await bot.edit_message(message_id=ref, text=text, attachments=_markup(kb), format=ParseMode.HTML)
            except Exception as ex:
                log.warning("MAX: правка (html) не прошла: %s — пробую простым текстом", ex)
                await bot.edit_message(message_id=ref, text=_plain(text), attachments=_markup(kb))

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


def _plain(text):
    """HTML -> простой текст (на случай, если MAX не принял разметку)."""
    import html as _h
    import re as _re
    return _h.unescape(_re.sub(r"<[^>]+>", "", text or ""))[:3900]


def _download(url):
    """Скачать вложение MAX (фото или файл)."""
    r = requests.get(url, timeout=120)
    if r.status_code in (401, 403):
        r = requests.get(url, timeout=120, headers={"Authorization": config.MAX_TOKEN})
    r.raise_for_status()
    return r.content


@dp.bot_started()
async def on_bot_started(event: BotStarted):
    flow.seen(event.user.user_id, event.chat_id)
    await flow.on_start(event.user.user_id, event.chat_id)


@dp.message_created()
async def on_message(event: MessageCreated):
    m = event.message
    if not m.sender or not m.body:
        return
    uid, chat = m.sender.user_id, m.recipient.chat_id
    flow.seen(uid, chat)
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
            flow.run(flow.on_xlsx(uid, chat, data), chat)
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
        flow.run(flow.on_text(uid, chat, m.body.text), chat)


@dp.message_callback()
async def on_callback(event: MessageCallback):
    uid = event.callback.user.user_id
    msg = event.message
    chat = msg.recipient.chat_id if msg and msg.recipient else None
    if not chat:
        # MAX не всегда присылает исходное сообщение с нажатой кнопкой — берём чат из прошлых сообщений
        chat = flow.CHATS.get(uid) or flow.CHATS.get(("max_chat", uid))
    ref = msg.body.mid if msg and msg.body else None
    flow.log.info("Кнопка %s от %s (чат %s, сообщение %s)", event.callback.payload, uid, chat, ref)
    if not chat:
        try:
            await bot.send_message(user_id=uid, text="Не понял, в какой чат отвечать. Напишите боту «/start» "
                                                     "и повторите.")
        except Exception:
            flow.log.exception("Не удалось ответить на кнопку")
        return
    flow.run(flow.on_callback(uid, chat, ref, event.callback.payload or "", event), chat)


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
