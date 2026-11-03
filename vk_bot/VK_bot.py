#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Объединённый VK-бот Благомед — два сообщества в одном процессе.

  • Прокат средств реабилитации (группа 168264636) — каталог из rental.json
  • Прокат фотоламп            (группа 177921324) — фотолампы из Google-таблицы

Каждое сообщество слушается в своём потоке. Если одно упадёт, оно само
перезапустится через 30 секунд, а второе в это время продолжит работать.

Токены лежат в config.py (рядом с этим файлом, в репозиторий не попадает).
Рядом также должен лежать credentials.json (нужен для фотоламп).

Обновление: при запуске и каждую ночь в 03:00 бот сам проверяет папку vk_bot
в репозитории alexbarny22/blagomed_tools; если там новая версия — скачивает
её и перезапускается.

Запуск:
    python VK_bot.py      (или start.bat)
"""

import datetime
import json
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import threading
import time

import requests
import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.utils import get_random_id
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

try:
    import config
except ImportError:
    print("Нет файла config.py рядом с VK_bot.py.\n"
          "Скопируйте config.example.py в config.py и впишите токены сообществ.")
    sys.exit(1)

# ─────────────────────────────────────────────
# НАСТРОЙКИ
# ─────────────────────────────────────────────

# Сообщество «Прокат средств реабилитации»
RENTAL_VK_TOKEN = config.RENTAL_VK_TOKEN
RENTAL_GROUP_ID = 168264636
PRICES_URL = "https://blagomedi.ru/rental.json"   # собирает price_sync.php из Google-таблицы

# Сообщество «Прокат фотоламп»
LAMP_VK_TOKEN = config.LAMP_VK_TOKEN
LAMP_GROUP_ID = 177921324
LAMP_SPREADSHEET_ID = "1RkRBspudQa8b3ZFJcIBiRitEMyztHeGyIkS287pnEUo"
LAMP_SHEET_ID = 515080082
LAMP_CREDENTIALS = os.path.join(BASE_DIR, "credentials.json")

CACHE_TTL = 3600          # прайс обновляется раз в час
RESTART_DELAY = 30        # пауза перед перезапуском упавшего сообщества, сек
UPDATE_HOUR = 3           # в этот час ночи бот проверяет обновления в репозитории

# ─────────────────────────────────────────────
# ЛОГИРОВАНИЕ — один файл VK_bot.log (5 МБ × 3) + консоль
# ─────────────────────────────────────────────
_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)-7s] %(name)s :: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
_file = logging.handlers.RotatingFileHandler(
    os.path.join(BASE_DIR, "VK_bot.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8"
)
_file.setFormatter(_fmt)
_console = logging.StreamHandler()
_console.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_file, _console])
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("vk_api").setLevel(logging.WARNING)


# ─────────────────────────────────────────────
# ОБЩИЕ ФУНКЦИИ
# ─────────────────────────────────────────────

def drive_direct_url(url):
    """Ссылку Google Drive превращает в прямую ссылку на скачивание."""
    if not url or "drive.google.com" not in url:
        return None
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        return None
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}"


def main_menu(faq_label):
    keyboard = {
        "one_time": False,
        "buttons": [
            [
                {"action": {"type": "text", "label": "Каталог"}, "color": "primary"},
                {"action": {"type": "text", "label": "Как арендовать"}, "color": "secondary"},
            ],
            [
                {"action": {"type": "text", "label": "Контакты"}, "color": "secondary"},
                {"action": {"type": "text", "label": faq_label}, "color": "secondary"},
            ],
        ],
    }
    return json.dumps(keyboard, ensure_ascii=False)


def menu_with_cancel(options):
    # VK: не больше 10 строк, подпись кнопки — до 40 символов
    buttons = [[{"action": {"type": "text", "label": o[:40]}, "color": "primary"}] for o in options]
    buttons.append([{"action": {"type": "text", "label": "Отмена"}, "color": "negative"}])
    return json.dumps({"one_time": True, "buttons": buttons}, ensure_ascii=False)


def two_column_menu(options):
    rows, row = [], []
    for o in options:
        row.append({"action": {"type": "text", "label": o[:40]}, "color": "primary"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"action": {"type": "text", "label": "Отмена"}, "color": "negative"}])
    return json.dumps({"one_time": True, "buttons": rows}, ensure_ascii=False)


# Сетевые сбои: ВК оборвал соединение, таймаут, недочитанный ответ.
# Это не ошибка бота — просто ждём и переподключаемся.
NET_ERRORS = (
    requests.exceptions.ConnectionError,      # в т.ч. RemoteDisconnected
    requests.exceptions.Timeout,              # ReadTimeout / ConnectTimeout
    requests.exceptions.ChunkedEncodingError,
)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(NET_ERRORS),
    reraise=True,     # после 3 попыток отдаём исходную ошибку, а не RetryError
)
def check_longpoll(longpoll):
    return longpoll.check()


# ─────────────────────────────────────────────
# БАЗОВЫЙ БОТ ОДНОГО СООБЩЕСТВА
# ─────────────────────────────────────────────

class GroupBot:
    NAME = "base"

    def __init__(self, token, group_id):
        self.token = token
        self.group_id = group_id
        self.log = logging.getLogger(self.NAME)
        self.session = None
        self.api = None
        self.items = []
        self.items_ts = 0.0
        self.user_states = {}     # у каждого сообщества свои состояния

    # ---- данные ----
    def load_items(self):
        return []

    def refresh_items(self, force=False):
        if not force and time.time() - self.items_ts < CACHE_TTL:
            return
        self.items_ts = time.time()     # следующая попытка — через час, даже при ошибке
        fresh = self.load_items()
        if fresh:
            self.items = fresh
            self.log.info(f"Прайс загружен: {len(fresh)} позиций")
        else:
            self.log.warning("Свежие данные пустые, прайс не заменён")

    # ---- VK ----
    def connect(self):
        http = requests.Session()
        http.mount("https://", requests.adapters.HTTPAdapter(max_retries=3))
        self.session = vk_api.VkApi(token=self.token, session=http)
        self.session.http.timeout = 60
        self.api = self.session.get_api()
        self.log.info("Подключение к VK API установлено")

    def send(self, peer_id, message, keyboard=None, attachment=None):
        params = {
            "peer_id": peer_id,
            "message": message,
            "random_id": get_random_id(),
            "group_id": self.group_id,
        }
        if keyboard:
            params["keyboard"] = keyboard
        if attachment:
            params["attachment"] = attachment
        try:
            self.api.messages.send(**params)
        except Exception as e:
            self.log.error(f"Ошибка отправки сообщения user={peer_id}: {e}")

    def send_card(self, peer_id, text, photo_url, keyboard):
        """Карточка товара с фото, если фото есть и загрузилось; иначе без фото."""
        direct = drive_direct_url((photo_url or "").strip())
        if direct:
            tmp_path = None
            try:
                r = requests.get(direct, timeout=30)
                if r.status_code == 200:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
                        f.write(r.content)
                        tmp_path = f.name
                    photo = vk_api.VkUpload(self.session).photo_messages(tmp_path)[0]
                    self.send(peer_id, text, keyboard=keyboard,
                              attachment=f"photo{photo['owner_id']}_{photo['id']}")
                    return
                self.log.warning(f"Фото не скачалось: HTTP {r.status_code}")
            except Exception as e:
                self.log.warning(f"Не удалось отправить фото, отправляю без него: {e}")
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        self.send(peer_id, text, keyboard=keyboard)

    def handle(self, user_id, text):
        raise NotImplementedError

    # ---- цикл LongPoll ----
    def new_longpoll(self):
        """Новый сервер и ключ LongPoll. None, если ВК сейчас недоступен."""
        try:
            return VkLongPoll(self.session, wait=25)
        except Exception as e:
            self.log.warning(f"Не удалось получить сервер LongPoll: {e}")
            return None

    def listen(self):
        self.refresh_items(force=True)
        self.connect()
        longpoll = VkLongPoll(self.session, wait=25)
        self.log.info(f"LongPoll запущен (группа {self.group_id})")
        net_pause = 5          # пауза после обрыва связи, растёт до 60 сек
        net_fails = 0          # обрывов подряд

        while True:
            if longpoll is None:
                time.sleep(net_pause)
                net_pause = min(net_pause * 2, 60)
                longpoll = self.new_longpoll()
                continue
            try:
                events = check_longpoll(longpoll)
                if net_fails:
                    self.log.info(f"Связь с ВК восстановлена (обрывов подряд: {net_fails})")
                    net_fails, net_pause = 0, 5
                for event in events:
                    if event.type == VkEventType.MESSAGE_NEW and not event.from_me:
                        text = (event.text or "").strip()
                        self.log.info(f"user={event.user_id}: '{text[:60]}'")
                        try:
                            self.handle(event.user_id, text)
                        except Exception:
                            # ошибка в одном сообщении не должна останавливать бота
                            self.log.exception(f"Ошибка обработки сообщения user={event.user_id}")

            except NET_ERRORS as e:
                # Обрыв связи — одна строка в логе вместо простыни traceback
                net_fails += 1
                level = logging.WARNING if net_fails < 10 else logging.ERROR
                self.log.log(level, f"Обрыв связи с ВК ({type(e).__name__}), "
                                    f"переподключение через {net_pause} сек")
                time.sleep(net_pause)
                net_pause = min(net_pause * 2, 60)
                longpoll = self.new_longpoll()

            except vk_api.exceptions.ApiError as e:
                code = getattr(e, "code", None)
                if code in (503, 504):
                    self.log.warning(f"Временная ошибка VK API [{code}], повтор через 5 сек")
                    time.sleep(5)
                elif code == 1:
                    self.log.info("Ключ LongPoll устарел, обновляю")
                    longpoll = self.new_longpoll()
                else:
                    raise   # серьёзная ошибка — перезапуск через run_forever

            except Exception as e:
                self.log.exception(f"Ошибка LongPoll: {e}")
                time.sleep(10)
                try:
                    longpoll = self.new_longpoll()
                except Exception:
                    pass

    def run_forever(self):
        while True:
            try:
                self.listen()
            except Exception:
                self.log.exception("Бот сообщества остановился с ошибкой")
            self.log.warning(f"Перезапуск через {RESTART_DELAY} сек")
            time.sleep(RESTART_DELAY)


# ─────────────────────────────────────────────
# СООБЩЕСТВО «ПРОКАТ СРЕДСТВ РЕАБИЛИТАЦИИ»
# ─────────────────────────────────────────────

CATEGORY_ORDER = [
    "Костыли",
    "Ходунки",
    "Кресла-коляски",
    "Кресло-туалет",
    "Трости",
    "Ортезы и туторы",
    "Медицинское оборудование",
    "Носилки",
    "Реабилитационные тренажёры",
    "Остальные категории",
]

# Пункты проката. "open_from" — дата открытия: до неё пункт показывается
# с пометкой «откроется …», после — как обычный.
RENTAL_STORES = [
    {"address": "пр. Ленина, 128",
     "hours": "Пн-Пт: 10:00–19:00, Сб: 10:00–18:00, Вс: выходной",
     "phone": "8(913)210-01-46"},
    {"address": "ул. Юрина, 225",
     "hours": "Пн-Пт: 10:00–19:00, Сб: 10:00–16:00, Вс: выходной",
     "phone": "8(913)080-07-59"},
    {"address": "ул. 50 лет СССР, 16",
     "hours": "Пн-Пт: 10:00–19:00, Сб-Вс: 10:00–18:00",
     "phone": "8(983)605-97-12"},
    {"address": "Павловский тракт, 134",
     "hours": "Без выходных. Пн-Пт: 10:00–19:00, Сб-Вс: уточняйте по тел. 595-157",
     "phone": "595-157",
     "open_from": datetime.date(2026, 11, 8)},
]

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
COUNT_WORDS = {1: "Один пункт", 2: "Два пункта", 3: "Три пункта", 4: "Четыре пункта",
               5: "Пять пунктов", 6: "Шесть пунктов"}


def store_title(store):
    """Адрес пункта; если он ещё не открылся — с пометкой о дате открытия."""
    d = store.get("open_from")
    if d and datetime.date.today() < d:
        return f"{store['address']} (откроется {d.day} {MONTHS_GEN[d.month - 1]})"
    return store["address"]


def stores_count_text():
    n = len([s for s in RENTAL_STORES
             if not s.get("open_from") or datetime.date.today() >= s["open_from"]])
    return COUNT_WORDS.get(n, f"{n} пунктов") + " проката"


RENTAL_WELCOME = (
    "Здравствуйте! 👋\n"
    "Я — бот компании Благомед.\n"
    "Помогу узнать цены и условия проката средств реабилитации:\n"
    "костыли, ходунки, инвалидные коляски, ортезы и многое другое.\n\n"
    "📞 Единая справочная: 595-157\n"
    "🌐 https://blagomedi.ru\n\n"
    "Выберите нужный раздел:"
)

def rental_features():
    return (
        "Почему выбирают Благомед:\n\n"
        "1. 💰 Доступные цены и удобные сроки проката\n"
        "2. 📝 Быстрое оформление. Без залога\n"
        f"3. 🏪 Широкий выбор. {stores_count_text()}\n"
        "4. 📜 Официальный договор\n"
        "5. ❤️ Надёжность и забота о клиенте\n\n"
        "Выберите опцию:"
    )

RENTAL_HOWTO = (
    "📋 Как взять в прокат средство реабилитации:\n\n"
    "1. 📞 Позвоните и уточните наличие нужного оборудования: 595-157\n"
    "2. 🏪 Приходите в удобный пункт проката\n"
    "3. 📝 Возьмите с собой паспорт с пропиской Барнаула или Новоалтайска\n"
    "4. ✍️ Оформите договор — залог не требуется\n\n"
    "📖 Полные правила проката:\n"
    "https://blagomedi.ru/pravila-prokata-tekhnicheskikh-sredstv-reabilitaczii"
)


def get_category(name):
    n = (name or "").lower()
    if not n:
        return "Остальные категории"
    if "костыл" in n:
        return "Костыли"
    if "ходунк" in n:
        return "Ходунки"
    if "кресло-коляска" in n or "кресло коляска" in n:
        return "Кресла-коляски"
    if "кресло-туалет" in n or "кресло туалет" in n:
        return "Кресло-туалет"
    if "трост" in n:
        return "Трости"
    if "ортез" in n or "тутор" in n:
        return "Ортезы и туторы"
    if any(k in n for k in ("аппарат", "концентратор", "штатив", "облучатель", "ингалятор", "весы")):
        return "Медицинское оборудование"
    if "носилки" in n:
        return "Носилки"
    if "велотренажер" in n or "велотренажёр" in n:
        return "Реабилитационные тренажёры"
    return "Остальные категории"


def fmt_price(v):
    v = str(v or "").strip()
    return f"{v} р." if v and v != "0" else "—"


class RentalBot(GroupBot):
    NAME = "vk.rental"
    FAQ_LABEL = "Вопросы и ответы"

    def load_items(self):
        """rental.json → список позиций. При ошибке — пустой список."""
        try:
            r = requests.get(PRICES_URL, params={"v": int(time.time() // 600)}, timeout=20)
            r.raise_for_status()
            result = []
            for it in r.json().get("items", []):
                name = (it.get("name") or "").strip()
                if not name:
                    continue
                result.append({
                    "name": name,
                    "daily": it.get("min") == "day",
                    "min_key": it.get("min", "week2"),
                    "min_label": it.get("min_label", ""),
                    "day": it.get("day") or "",
                    "week1": it.get("week1") or "",
                    "week2": it.get("week2") or "",
                    "week3": it.get("week3") or "",
                    "week4": it.get("week4") or "",
                    "photo": it.get("photo", ""),
                })
            return result
        except Exception as e:
            self.log.error(f"Ошибка при получении rental.json: {e}")
            return []

    def menu(self):
        return main_menu(self.FAQ_LABEL)

    def item_names(self, category):
        return [i["name"] for i in self.items if get_category(i["name"]) == category]

    def card_text(self, item):
        lines = [f"🛠️ {item['name']}", ""]
        if item["daily"]:
            lines.append(f"💰 Цена за 1 сутки: {fmt_price(item['day'])}")
        else:
            lines.append("💰 Стоимость проката:")
            periods = [("1 неделя ", "week1"), ("2 недели ", "week2"),
                       ("3 недели ", "week3"), ("4 недели ", "week4")]
            for label, key in periods:
                if key == "week1" and item["min_key"] != "week1":
                    continue   # 1 неделю показываем, только если с неё можно брать
                p = fmt_price(item[key])
                if p != "—":
                    lines.append(f"   • {label}— {p}")
        lines += [
            "",
            f"⏳ Мин. срок аренды: {item['min_label'] or 'уточните по телефону'}",
            "",
            "📍 Пункты проката:",
            *[f"   ✔️ {store_title(st)}" for st in RENTAL_STORES],
            "",
            "📝 Для оформления нужен паспорт с пропиской (Барнаул / Новоалтайск)",
            "⚠️ Уточняйте наличие по тел. 📞 595-157",
        ]
        return "\n".join(lines)

    def faq_prices_block(self):
        rows = [("Костыли", "🦯"), ("Ходунки", "🚶"), ("Кресла-коляски", "♿"), ("Кресло-туалет", "🚽")]
        lines = []
        for cat, icon in rows:
            prices = []
            for i in self.items:
                v = str(i["week2"]).strip()
                if get_category(i["name"]) == cat and v.isdigit() and int(v) > 0:
                    prices.append(int(v))
            if prices:
                label = "Кресло-коляска" if cat == "Кресла-коляски" else cat
                lines.append(f"   {icon} {label} — от {min(prices)} р.")
        if not lines:
            return ""
        return "💰 Примерные цены (за 2 недели):\n" + "\n".join(lines) + "\n\n"

    def handle(self, user_id, text):
        self.refresh_items()
        t = text.lower().strip()
        st = self.user_states.setdefault(user_id, {"welcomed": False})

        if not st.get("welcomed"):
            self.send(user_id, RENTAL_WELCOME, keyboard=self.menu())
            self.user_states[user_id] = {"welcomed": True}
            return

        if t in ("start", "начать", "старт", "привет", "здравствуйте", "меню"):
            self.send(user_id, rental_features(), keyboard=self.menu())
            self.user_states[user_id] = {"welcomed": True}
            return

        if t == "отмена":
            self.send(user_id, "Хорошо, возвращаемся в главное меню.", keyboard=self.menu())
            self.user_states[user_id] = {"welcomed": True}
            return

        if t == "каталог":
            self.user_states[user_id] = {"welcomed": True}
            if not self.items:
                self.send(user_id, "Данные временно недоступны. Позвоните нам: 595-157 📞",
                          keyboard=self.menu())
                return
            available = {get_category(i["name"]) for i in self.items}
            categories = [c for c in CATEGORY_ORDER if c in available]
            self.send(user_id, "Выберите категорию: 📋", keyboard=two_column_menu(categories))
            return

        # Выбор категории
        cats = {c.lower(): c for c in CATEGORY_ORDER}
        if t in cats and "category" not in st:
            cat = cats[t]
            names = self.item_names(cat)
            if names:
                st["category"] = cat
                st["available"] = [n.lower() for n in names]
                self.send(user_id, f"Категория «{cat}». Выберите товар: 🛠️",
                          keyboard=menu_with_cancel(names))
            else:
                self.send(user_id, "В этой категории пока нет товаров. 🚫", keyboard=self.menu())
                self.user_states[user_id] = {"welcomed": True}
            return

        # Выбор товара
        if "category" in st:
            if t in st.get("available", []):
                item = next((i for i in self.items if i["name"].lower() == t), None)
                if item:
                    self.send_card(user_id, self.card_text(item), item["photo"],
                                   keyboard=menu_with_cancel(["Каталог"]))
                else:
                    self.send(user_id, "Товар не найден. ⚠️", keyboard=self.menu())
                self.user_states[user_id] = {"welcomed": True}
            else:
                self.send(user_id, "Пожалуйста, выберите товар из списка ниже 📋",
                          keyboard=menu_with_cancel(self.item_names(st["category"])))
            return

        if t == "как арендовать":
            self.send(user_id, RENTAL_HOWTO, keyboard=self.menu())
            return

        if t == "контакты":
            lines = ["📍 Наши пункты проката:\n"]
            for store in RENTAL_STORES:
                lines += [f"🏠 {store_title(store)}", f"📞 {store['phone']}",
                          f"⏰ {store['hours']}", ""]
            lines += ["📞 Единая справочная: 595-157", "🌐 https://blagomedi.ru"]
            self.send(user_id, "\n".join(lines), keyboard=self.menu())
            return

        if t in ("ответы", "вопросы и ответы", "faq"):
            response = (
                "❓ Часто задаваемые вопросы:\n\n"
                + self.faq_prices_block() +
                "⏳ Сроки аренды:\n"
                "   • Минимальный срок — 2 недели (для большинства позиций)\n"
                "   • Аренда на 1 / 2 / 3 / 4 недели\n"
                "   • Для носилок — посуточно\n\n"
                "📋 Что нужно для оформления:\n"
                "   • Паспорт с пропиской (Барнаул или Новоалтайск)\n"
                "   • Личное посещение пункта проката\n"
                "   • Залог не требуется\n\n"
                "🔄 Продление аренды:\n"
                "   Мы напомним о конце срока по SMS / Max / звонку.\n\n"
                "📞 Остались вопросы? Звоните: 595-157"
            )
            self.send(user_id, response, keyboard=self.menu())
            return

        self.send(user_id, "Не понял ваш запрос. Воспользуйтесь кнопками меню 👇",
                  keyboard=self.menu())


# ─────────────────────────────────────────────
# СООБЩЕСТВО «ПРОКАТ ФОТОЛАМП»
# ─────────────────────────────────────────────

LAMP_PERIODS = ["1 сутки", "2 суток", "3 суток", "4 суток", "5 суток"]
LAMP_COL_NAME = "Наименование технического средства реабилитации"

LAMP_WELCOME = (
    "Здравствуйте! 👋\n"
    "Официальный бот компании Blagomed.\n"
    "Прокат фотоламп:\n"
    "📞 тел. 595-157\n"
    "Вопросы и пожелания: @blagomed22\n\n"
    "Напишите 'start', чтобы начать."
)

LAMP_FEATURES = (
    "1. Доступная стоимость и удобные сроки проката 💰\n"
    "2. Быстрое и удобное оформление. Без залога 📝\n"
    "3. Выбор фотоламп. Пункт проката: пр. Ленина, 128 🏪\n"
    "4. Официальный договор 📜\n"
    "5. Надежность и забота о клиентах ❤️\n\n"
    "Выберите опцию:"
)

LAMP_HOWTO = (
    "Как арендовать фотолампу:\n"
    "1. 📖 Ознакомьтесь с правилами аренды: https://prokat-blagomed.ru/pravila-prokata-tekhnicheskikh-sredstv-reabilitaczii\n"
    "2. 📞 Уточните по телефону наличие фотоламп\n"
    "3. 📝 Для оформления договора потребуется паспорт с пропиской г. Барнаула или г. Новоалтайска\n"
    "4. 🏪 Посетите пункт проката\n\n"
    "📞 Единая справочная: 595-157"
)

LAMP_CONTACTS = (
    "📍 Контакты:\n"
    "пр. Ленина, 128\n"
    "📞 8(913)210-01-46\n"
    "⏰ Пн-Пт: с 10 до 19 ч, Сб: с 10 до 18 ч, Вс: выходной\n\n"
    "📞 Единая справочная: 595-157"
)

LAMP_FAQ = (
    "Ответы на частые вопросы:\n\n"
    "Какова стоимость аренды фотолампы?\n"
    "Фотолампа ОФТН-03 АКСИОН (нижняя):\n"
    "- 1 сутки: 550р.\n"
    "- 2 суток: 1 100р.\n"
    "- 3 суток: 1 650р.\n"
    "- 4 суток: 2 200р.\n"
    "- 5 суток: 2 750р.\n"
    "Фотолампа КУРС ФН-01 (верхняя):\n"
    "- 1 сутки: 550р.\n"
    "- 2 суток: 1 100р.\n"
    "- 3 суток: 1 650р.\n"
    "- 4 суток: 2 200р.\n"
    "- 5 суток: 2 750р.\n\n"
    "На какой период можно арендовать?\n"
    "Минимальный период — 1 сутки.\n\n"
    "Что нужно для аренды?\n"
    "1) Паспорт с пропиской (Барнаул/Новоалтайск)\n"
    "2) Посетить пункт проката: пр. Ленина, 128\n"
    "3) Оформить договор аренды\n\n"
    "Нужно ли посещать пункт при продлении?\n"
    "Нет! Продление можно оплатить онлайн. Сотрудники напомнят о сроке и подтвердят оплату через SMS, WhatsApp или звонок."
)


class LampBot(GroupBot):
    NAME = "vk.lamp"
    FAQ_LABEL = "Ответы"

    def load_items(self):
        """Фотолампы из Google-таблицы. При ошибке — пустой список."""
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            scope = ["https://www.googleapis.com/auth/spreadsheets",
                     "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_file(LAMP_CREDENTIALS, scopes=scope)
            sheet = None
            for ws in gspread.authorize(creds).open_by_key(LAMP_SPREADSHEET_ID):
                if ws.id == LAMP_SHEET_ID:
                    sheet = ws
                    break
            if sheet is None:
                self.log.error(f"Лист с ID {LAMP_SHEET_ID} не найден")
                return []
            values = sheet.get_all_values()
            headers = values[0]
            rows = [dict(zip(headers, r)) for r in values[1:]]
            return [r for r in rows if "Фотолампа" in r.get(LAMP_COL_NAME, "")]
        except Exception as e:
            self.log.error(f"Ошибка при чтении Google-таблицы: {e}")
            return []

    def menu(self):
        return main_menu(self.FAQ_LABEL)

    def card_text(self, item):
        name = item.get(LAMP_COL_NAME, "Неизвестный товар")
        text = f"💰 Стоимость проката '{name}':\n"
        for period in LAMP_PERIODS:
            text += f"- {period}: {item.get(f'Цена проката за {period}', 'Не указана')}\n"
        text += f"⏳ Мин. период проката: {item.get('Минимальный срок аренды', '1 сутки')}\n"
        text += "🚚 Пункт проката:\n✔️ пр. Ленина, 128\n"
        text += "📝 Для оформления договора проката требуется паспорт с местной пропиской\n"
        text += "⚠️ Уточняйте наличие! ⚠️\n📞 Тел. для справок 595-157"
        return text

    def handle(self, user_id, text):
        self.refresh_items()
        t = text.lower().strip()
        st = self.user_states.setdefault(user_id, {"welcomed": False})

        if not st.get("welcomed"):
            self.send(user_id, LAMP_WELCOME, keyboard=self.menu())
            st["welcomed"] = True
            return

        if t == "start":
            self.send(user_id, LAMP_FEATURES, keyboard=self.menu())
            self.user_states[user_id] = {"welcomed": True}
            return

        if t == "отмена":
            self.send(user_id, "Действие отменено. Выберите опцию: 🚪", keyboard=self.menu())
            self.user_states[user_id] = {"welcomed": True}
            return

        if t == "каталог":
            self.user_states[user_id] = {"welcomed": True}
            self.send(user_id, "Выберите категорию: 📋", keyboard=menu_with_cancel(["Фотолампы"]))
            return

        if t == "фотолампы":
            if self.items:
                names = [i[LAMP_COL_NAME] for i in self.items]
                labels = [n[:37] + "..." if len(n) > 40 else n for n in names]
                st["mapping"] = {l.lower(): n for l, n in zip(labels, names)}
                st["labels"] = labels
                self.send(user_id, "Выберите фотолампу из списка: 🛠️",
                          keyboard=menu_with_cancel(labels))
            else:
                self.send(user_id, "Фотоламп в наличии нет. 🚫", keyboard=self.menu())
            return

        if t == "как арендовать":
            self.send(user_id, LAMP_HOWTO, keyboard=self.menu())
            return

        if t == "контакты":
            self.send(user_id, LAMP_CONTACTS, keyboard=self.menu())
            return

        if t == "ответы":
            self.send(user_id, LAMP_FAQ, keyboard=self.menu())
            return

        # Выбор лампы из списка
        if "mapping" in st:
            full_name = st["mapping"].get(t)
            if full_name:
                item = next((i for i in self.items if i[LAMP_COL_NAME] == full_name), None)
                if item:
                    self.send_card(user_id, self.card_text(item), item.get("Фото", ""),
                                   keyboard=menu_with_cancel(["Каталог"]))
                else:
                    self.send(user_id, "Фотолампа не найдена. ⚠️", keyboard=self.menu())
                self.user_states[user_id] = {"welcomed": True}
            else:
                self.send(user_id, "Пожалуйста, выберите фотолампу из списка. 📋",
                          keyboard=menu_with_cancel(st["labels"]))
            return

        self.send(user_id, "Не понял команду. Выберите опцию из меню 👇", keyboard=self.menu())


# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────

def restart_self(log):
    """Новая версия скачана — завершаемся с кодом 3, чтобы BotPanel
    (или цикл в start.bat) запустил бота заново уже с новыми файлами."""
    log.info("Скачана новая версия — перезапуск бота")
    logging.shutdown()
    os._exit(3)


def try_update(log):
    """Проверяет репозиторий; True — если файлы обновились."""
    try:
        import update
        return update.main()
    except Exception as e:
        log.warning(f"Проверка обновлений не удалась: {e}")
        return False


if __name__ == "__main__":
    log = logging.getLogger("vk")
    log.info("=" * 50)

    if "--no-update" not in sys.argv and try_update(log):
        restart_self(log)

    try:
        with open(os.path.join(BASE_DIR, "version.txt"), encoding="utf-8") as f:
            version = f.read().strip()
    except OSError:
        version = "—"
    log.info(f"Старт объединённого VK-бота, версия {version}")

    bots = [
        RentalBot(RENTAL_VK_TOKEN, RENTAL_GROUP_ID),
        LampBot(LAMP_VK_TOKEN, LAMP_GROUP_ID),
    ]
    threads = [threading.Thread(target=b.run_forever, name=b.NAME, daemon=True) for b in bots]
    for th in threads:
        th.start()

    checked_day = None
    try:
        while True:
            time.sleep(30)
            now = datetime.datetime.now()
            # раз в сутки ночью — проверка обновлений в репозитории
            if now.hour == UPDATE_HOUR and checked_day != now.date():
                checked_day = now.date()
                if try_update(log):
                    restart_self(log)
    except KeyboardInterrupt:
        log.info("Остановка VK-бота")
