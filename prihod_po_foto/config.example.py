# -*- coding: utf-8 -*-
# ============================================================
#  НАСТРОЙКИ бота «Приход по фото»
#  Скопируйте этот файл в config.py и заполните. config.py в GitHub не попадает.
# ============================================================

# --- 1. Telegram -----------------------------------------------------------
TELEGRAM_TOKEN = ""          # токен от @BotFather
ALLOWED_IDS = [0]            # кому можно пользоваться ботом; свой ID бот пришлёт на /start
TELEGRAM_PROXY = ""          # если Telegram не открывается: "http://логин:пароль@адрес:порт" (обычно пусто)

# --- 2. Подключение к 1С (как в move_bot / инструменте Ozon-WB) -----------
ODATA_URL = "http://91.199.149.224:4001/ut12"
ONEC_SERVER = "91.199.149.224"   # нужно только для setup_odata_prihod.bat
ONEC_BASE = "ut"                 # нужно только для setup_odata_prihod.bat
ONEC_USER = ""
ONEC_PASSWORD = ""

# --- 3. Документ в 1С ------------------------------------------------------
ORGANIZATION = "ИП Липихин Александр Алексеевич"   # на кого приходуем
OWN_INNS = ["222301170569"]   # ИНН своих организаций — чтобы бот не спутал покупателя с поставщиком
WAREHOUSE = ""        # точное название общего склада в 1С; пусто — склад из прошлого поступления
POST = True           # True — проводить сразу, False — только записать
DATE_MODE = "today"   # "today" — дата поступления = сегодня; "document" — дата накладной поставщика
EXCLUDE_NAME_PARTS = ["(МГ)"]   # такие позиции не предлагать (магазинные карточки)
CACHE_DAYS = 7        # номенклатура хранится в файле; из 1С заново раз в столько дней (или /refresh)

# --- 4. Нейросети ------------------------------------------------------------
# Пробуются по порядку: если первая ошиблась в суммах — бот сам спросит следующую
# и возьмёт лучший результат. Ненужную можно выключить: "enabled": False.
#
# Агрегатор (vsellm и подобные): адрес API и точные имена моделей — в личном кабинете.
# Список моделей можно посмотреть: двойной щелчок по test_models.bat → пункт «список моделей».
VSELLM_URL = "https://api.vsellm.ru/v1"     # адрес API агрегатора (заканчивается на /v1)
VSELLM_KEY = ""                        # ключ API агрегатора

MODELS = [
    {"name": "Claude Sonnet 5.5", "type": "openai", "base_url": VSELLM_URL, "api_key": VSELLM_KEY,
     "model": "anthropic/claude-sonnet-5.5"},
    {"name": "Gemini 3.1 Pro", "type": "openai", "base_url": VSELLM_URL, "api_key": VSELLM_KEY,
     "model": "google/gemini-3.1-pro-preview"},
    # DeepSeek через тот же агрегатор. Имя модели — из списка (test_models.bat → 2, раздел deepseek).
    # Пока стоит ЗАМЕНИТЬ — строка пропускается. Если модель не принимает фото, тест покажет ошибку.
    {"name": "DeepSeek", "type": "openai", "base_url": VSELLM_URL, "api_key": VSELLM_KEY,
     "model": "ЗАМЕНИТЬ (например deepseek/...)"},
    # GigaChat: ключ авторизации (Authorization key) из личного кабинета developers.sber.ru
    # GigaChat 3 Ultra — новая модель, физлицам бесплатно (freemium); своя адресная строка API
    {"name": "GigaChat 3 Ultra", "type": "gigachat", "api_key": "", "model": "GigaChat-3-Ultra",
     "base_url": "https://api.giga.chat/v1", "scope": "GIGACHAT_API_PERS", "verify_ssl": False,
     "enabled": True},
    {"name": "GigaChat Max", "type": "gigachat", "api_key": "", "model": "GigaChat-2-Max",
     "scope": "GIGACHAT_API_PERS", "verify_ssl": False, "enabled": False},
]

# --- 5. Автообновление ------------------------------------------------------
GITHUB_TOKEN = ""     # только если репозиторий станет приватным
