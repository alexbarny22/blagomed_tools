#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автоответы на отзывы Wildberries — Благомед.

Берёт неотвеченные отзывы через API WB, по оценке (1–5 звёзд) выбирает
случайный вариант ответа из «otvety.txt» и отправляет его.
Подряд один и тот же вариант для одной оценки не повторяется.

Рассчитан на работу под менеджером ботов (manager.py): работает постоянно,
проверяет отзывы раз в interval_min минут и обновляет heartbeat_wb_reviews.txt,
чтобы менеджер видел, что бот жив.

Файлы (лежат в папке менеджера, рядом с остальными ботами):
  wb_reviews_bot.py          — этот скрипт
  wb_reviews_config.json     — токен и настройки
  otvety.txt                 — тексты ответов по звёздам (правится в Блокноте)
  wb_reviews_state.json      — служебный, создаётся сам
  wb_reviews.log             — журнал работы

Запуск:
  python wb_reviews_bot.py          постоянная работа (так его запускает менеджер)
  python wb_reviews_bot.py --once   один проход и выход
  python wb_reviews_bot.py --test   один проход: только показать ответы, ничего не отправлять
Дополнительные пакеты не нужны.
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BOT_NAME = "wb_reviews"     # должно совпадать с именем бота в manager_config.json

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "wb_reviews_config.json")
TEMPLATES = os.path.join(BASE, "otvety.txt")
STATE = os.path.join(BASE, "wb_reviews_state.json")
LOG = os.path.join(BASE, "wb_reviews.log")
HEARTBEAT = os.path.join(BASE, "heartbeat_{}.txt".format(BOT_NAME))
API = "https://feedbacks-api.wildberries.ru"
LOG_MAX = 5 * 1024 * 1024


class FatalError(Exception):
    """Ошибка, которую повтором не исправить (токен, файлы настроек)."""


# ---------- служебное ----------

def heartbeat():
    try:
        with open(HEARTBEAT, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def log(msg):
    heartbeat()
    line = "{:%Y-%m-%d %H:%M:%S} {}".format(datetime.now(), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".old")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sleep(sec):
    """Пауза, во время которой бот продолжает отмечаться для менеджера."""
    end = time.time() + sec
    while True:
        heartbeat()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(60, left))


def load_json(path, default=None):
    if not os.path.exists(path):
        if default is not None:
            return default
        raise FatalError("Не найден файл: " + path)
    text = read_text(path)
    try:
        return json.loads(text)
    except ValueError as e:
        # Частая беда: при вставке токена внутрь кавычек попал перенос строки.
        # Склеиваем строки внутри кавычек и пробуем ещё раз.
        fixed = re.sub(r'"[^"]*"', lambda m: re.sub(r"\s+", "", m.group(0))
                       if "\n" in m.group(0) else m.group(0), text)
        try:
            return json.loads(fixed)
        except ValueError:
            raise FatalError("Ошибка в файле {} (проверьте кавычки и запятые): {}".format(
                path, e))


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_text(path):
    """Блокнот может сохранить файл в UTF-8 (с BOM или без), UTF-16 («Юникод») или ANSI (cp1251)."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def find_templates_file():
    """
    Обычно это otvety.txt. Если его нет (переименовали, или при распаковке
    имя испортилось) — берём любой .txt в папке бота с разделами «=== … ===».
    """
    if os.path.exists(TEMPLATES):
        return TEMPLATES
    for fname in sorted(os.listdir(BASE)):
        if not fname.lower().endswith(".txt") or fname.lower().startswith("heartbeat_"):
            continue
        full = os.path.join(BASE, fname)
        try:
            text = read_text(full)
        except OSError:
            continue
        if "===" in text and any(w in text.lower() for w in ("звезд", "звёзд")):
            return full
    raise FatalError("Не найден файл с ответами otvety.txt в папке " + BASE)


def load_templates():
    """
    Читает «otvety.txt»:
      === Подпись ===            — подпись под каждым ответом
      === 5 звёзд ===            — ответы на 5 звёзд, по одному на строку
      === 5 звёзд без текста === — ответы, если покупатель поставил только оценку
    Возвращает {"signature": str, "1": {"variants": [...], "variants_empty": [...]}, ...}
    """
    path = find_templates_file()
    result = {"signature": "", "greetings": []}
    signature = []
    section = None   # ("star", "5", "variants") / ("sig",) / ("greet",) / None
    for line in read_text(path).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("==="):
            title = s.strip("= ").lower()
            if "подпис" in title:
                section = ("sig",)
                continue
            if "приветств" in title:
                section = ("greet",)
                continue
            star = next((ch for ch in title if ch in "12345"), None)
            if star is None:
                log("В файле ответов непонятный заголовок, пропускаю: " + s)
                section = None
                continue
            kind = "variants_empty" if "без текст" in title else "variants"
            result.setdefault(star, {"variants": [], "variants_empty": []})
            section = ("star", star, kind)
            continue
        if section is None:
            continue
        if section[0] == "sig":
            signature.append(s)
        elif section[0] == "greet":
            result["greetings"].append(s)
        else:
            result[section[1]][section[2]].append(s)
    result["signature"] = "\n".join(signature)
    if not any(isinstance(v, dict) and (v["variants"] or v["variants_empty"])
               for v in result.values()):
        raise FatalError("В файле ответов нет ни одного ответа. "
                         "Проверьте заголовки вида === 5 звёзд ===")
    return result


# ---------- API Wildberries ----------

def api(method, path, token, params=None, body=None, retries=4):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": token,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "ignore")[:300]
            if e.code == 401:
                raise FatalError("Ошибка 401: токен WB неверный или просрочен. "
                                 "Создайте новый токен с доступом «Вопросы и отзывы».")
            if e.code == 403:
                raise FatalError("Ошибка 403: у токена нет доступа к «Вопросы и отзывы» "
                                 "или он создан «только на чтение».")
            if e.code == 429 or e.code >= 500:
                wait = 10 * attempt
                log("WB ответил {}, жду {} сек и повторяю...".format(e.code, wait))
                sleep(wait)
                continue
            raise RuntimeError("HTTP {}: {}".format(e.code, text))
        except urllib.error.URLError as e:
            wait = 10 * attempt
            log("Нет связи с WB ({}), повтор через {} сек".format(e.reason, wait))
            sleep(wait)
    raise RuntimeError("WB API не отвечает после {} попыток".format(retries))


def get_unanswered(token):
    """Все неотвеченные отзывы, от старых к новым."""
    result, skip, take = [], 0, 1000
    while True:
        resp = api("GET", "/api/v1/feedbacks", token, params={
            "isAnswered": "false", "take": take, "skip": skip, "order": "dateAsc",
        })
        items = ((resp or {}).get("data") or {}).get("feedbacks") or []
        result.extend(items)
        if len(items) < take:
            return result
        skip += take
        sleep(1)


def send_answer(token, feedback_id, text):
    api("POST", "/api/v1/feedbacks/answer", token, body={"id": feedback_id, "text": text})


# ---------- сборка текста ответа ----------

BAD_NAMES = {"", "покупатель", "покупательница", "клиент", "пользователь", "аноним"}


def first_name(user_name):
    name = (user_name or "").strip().split(" ")[0].strip(".,!")
    if name.lower() in BAD_NAMES or not name.isalpha() or len(name) < 2:
        return ""
    return name[0].upper() + name[1:].lower()


def short_product(name, limit=60):
    name = (name or "").strip()
    if len(name) <= limit:
        return name
    cut = name[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:- ")


def has_text(fb):
    return any((fb.get(k) or "").strip() for k in ("text", "pros", "cons"))


def pick_variant(star_cfg, star, fb, state):
    variants = star_cfg.get("variants") or []
    if not has_text(fb) and star_cfg.get("variants_empty"):
        variants = star_cfg["variants_empty"]
        key = "{}_empty".format(star)
    else:
        key = str(star)
    if not variants:
        return None
    last = state["last"].get(key)
    choices = [i for i in range(len(variants)) if i != last] or [0]
    idx = random.choice(choices)
    state["last"][key] = idx
    return variants[idx]


def make_greeting(greetings, name):
    """Случайное приветствие; если имени нет — убираем «, [имя]» аккуратно."""
    g = random.choice(greetings) if greetings else "Здравствуйте, [имя]!"
    for mark in ("[имя]", "[Имя]", "[ИМЯ]"):
        if name:
            g = g.replace(mark, name)
        else:
            for pattern in (", " + mark, "," + mark, " " + mark, mark):
                g = g.replace(pattern, "")
    return " ".join(g.split())


def build_answer(template, fb, signature, greetings=None):
    name = first_name(fb.get("userName"))
    details = fb.get("productDetails") or {}
    body = template.strip()
    for mark in ("[товар]", "[Товар]", "[ТОВАР]"):
        body = body.replace(mark, short_product(details.get("productName")) or "товар")
    for mark in ("[имя]", "[Имя]", "[ИМЯ]"):
        body = body.replace(mark, name)
    # приветствие добавляем сами, если человек не написал его в тексте
    if not body.lower().startswith(("здравствуйте", "добрый день", "привет")):
        body = make_greeting(greetings, name) + " " + body
    sig = (signature or "").strip()
    if sig:
        body += "\n\n" + sig
    return body[:5000]


# ---------- один проход ----------

def run_once(force_test=False):
    # файлы перечитываются каждый раз — правки текстов подхватываются без перезапуска
    cfg = load_json(CONFIG)
    templates = load_templates()
    state = load_json(STATE, default={"answered": [], "last": {}})
    state.setdefault("answered", [])
    state.setdefault("last", {})

    token = re.sub(r"\s+", "", str(cfg.get("token") or ""))
    if token.lower().startswith("bearer"):
        token = token[6:]
    if not token or "ВСТАВЬТЕ" in token:
        raise FatalError("В файле {} нет токена WB (поле \"token\"). "
                         "Вставьте токен именно в этот файл и сохраните (Ctrl+S).".format(CONFIG))

    dry = cfg.get("dry_run", True) or force_test
    if force_test:
        log("[ТЕСТ] Настройки: {}".format(CONFIG))
        log("[ТЕСТ] Токен прочитан: {}…{} ({} символов)".format(token[:8], token[-6:], len(token)))
    max_per_run = int(cfg.get("max_per_run", 100))
    pause = float(cfg.get("pause_sec", 1.5))
    skip_nm = set(int(x) for x in cfg.get("skip_nm_ids", []))
    answered = set(state["answered"])

    feedbacks = [fb for fb in get_unanswered(token) if fb.get("id") not in answered]
    if not feedbacks:
        log("{}Новых отзывов нет".format("[ТЕСТ] " if dry else ""))
        return
    log("{}Неотвеченных отзывов: {}".format("[ТЕСТ] " if dry else "", len(feedbacks)))

    sent = skipped = 0
    for fb in feedbacks:
        if sent >= max_per_run:
            log("Лимит {} ответов за проход, остальные — в следующий раз".format(max_per_run))
            break
        fid = fb.get("id")
        star = int(fb.get("productValuation") or 0)
        details = fb.get("productDetails") or {}
        nm = details.get("nmId")
        if not fid:
            continue
        star_cfg = templates.get(str(star))
        if not star_cfg or (nm and int(nm) in skip_nm):
            skipped += 1
            continue

        tpl = pick_variant(star_cfg, star, fb, state)
        if not tpl:
            skipped += 1
            continue
        text = build_answer(tpl, fb, templates.get("signature") or cfg.get("signature"),
                            templates.get("greetings"))
        product = short_product(details.get("productName"), 40)

        if dry:
            log("[ТЕСТ] {}★ арт.{} «{}»\n   Отзыв: {}\n   Ответ: {}".format(
                star, nm, product, (fb.get("text") or "—")[:150], text.replace("\n", " ")))
            sent += 1
            continue

        try:
            send_answer(token, fid, text)
        except FatalError:
            raise
        except Exception as e:
            log("Ошибка при ответе на отзыв {}: {}".format(fid, e))
            continue
        sent += 1
        answered.add(fid)
        state["answered"] = list(answered)[-10000:]
        save_json(STATE, state)
        log("Ответил: {}★ арт.{} «{}»".format(star, nm, product))
        sleep(pause)

    if not dry:
        save_json(STATE, state)
    log("Готово. {}: {}, пропущено (ручные/выключенные): {}".format(
        "Показано" if dry else "Отправлено", sent, skipped))


# ---------- постоянная работа ----------

def run_forever():
    log("Бот автоответов WB запущен")
    while True:
        try:
            run_once()
        except FatalError as e:
            # без правильного токена/настроек работать нельзя — пусть менеджер сообщит
            log("Остановка: {}".format(e))
            sys.exit(2)
        except Exception as e:
            log("Сбой прохода: {}. Попробую в следующий раз.".format(e))
        try:
            interval = float(load_json(CONFIG).get("interval_min", 30))
        except Exception:
            interval = 30
        sleep(max(interval, 5) * 60)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--test" in args or "--once" in args:
        try:
            run_once(force_test="--test" in args)
        except Exception as e:
            log("Сбой: {}".format(e))
            sys.exit(1)
    else:
        run_forever()
