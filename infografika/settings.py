# -*- coding: utf-8 -*-
"""Настройки программы: значения по умолчанию + то, что изменено на странице «Настройки» (settings.json)."""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PATH = os.path.join(HERE, "settings.json")

try:
    import config
except ImportError:
    config = None

DEFAULTS = {
    "limit_rub": 150,             # потолок на серию по умолчанию
    "profile": "Баланс",
    # профили: нейросети-художники по порядку; варианты обложки берут их по очереди
    "profiles": {
        "Эконом": ["openai/gpt-image-2"],
        "Баланс": ["google/gemini-3.1-flash-image", "openai/gpt-image-2"],
        "Максимум": ["google/gemini-3-pro-image", "google/gemini-3.1-flash-image"],
        "Ультра": ["openai/gpt-image-2.5-sunburst", "openai/gpt-image-2.5-flare", "openai/gpt-image-2"],
    },
    # свои текстовые модели и качество картинок для профиля (если не указано — общие ниже)
    "profile_text": {
        "Ультра": {"script": "openai/gpt-6-astra", "check": "openai/gpt-6.1-sol", "quality": "high", "limit": 300},
    },
    # фактические цены vsellm за картинку (по списаниям 01.11.2026)
    "image_prices": {
        "google/gemini-3.1-flash-image": 7.69,
        "openai/gpt-image-2": 6.03,
        "google/gemini-3-pro-image": 15.73,
        "google/gemini-2.5-flash-image": 4.32,
        # GPT Image 2.5: на сайте «от 0,53–1,02 ₽» (низкое качество); для качества high — оценка,
        # поправьте по журналу списаний vsellm после первого прогона
        "openai/gpt-image-2.5-sunburst": 12.0,
        "openai/gpt-image-2.5-flare": 9.0,
    },
    "script_model": "anthropic/claude-sonnet-5.5",      # сценарист и тексты карточки
    "check_model": "google/gemini-3.8-flash",           # проверка слайдов
    "check_enabled": True,
    # цены текстовых моделей: руб. за 1 млн токенов (вход, выход)
    "text_prices": {
        "anthropic/claude-sonnet-5.5": [221, 1105],
        "google/gemini-3.8-flash": [83, 414],
        "google/gemini-3.1-pro-preview": [221, 1326],
        "deepseek/deepseek-v4-flash": [21, 42],
        "openai/gpt-6-astra": [1105, 5525],
        "openai/gpt-6.1-sol": [221, 1105],
    },
    "max_attempts": 2,            # попыток на слайд (первая + перегенерации при браке)
    "parallel": 2,                # сколько слайдов рисуется одновременно
    "wb_title_max": 60,
    "ozon_title_max": 200,
    "wb_desc_max": 5000,
    "ozon_desc_max": 6000,
    "port": 8765,
}


def load():
    s = copy.deepcopy(DEFAULTS)
    try:
        with open(PATH, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
            else:
                s[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:      # noqa: BLE001
        print("settings.json не прочитан, беру настройки по умолчанию:", e)
    return s


def save(new):
    s = load()
    for k, v in new.items():
        if k in DEFAULTS:
            s[k] = v
    with open(PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(PATH + ".tmp", PATH)
    return s


def key():
    return getattr(config, "VSELLM_KEY", "") if config else ""


def url():
    return (getattr(config, "VSELLM_URL", "") if config else "") or "https://api.vsellm.ru/v1"


def image_price(s, model):
    return float(s["image_prices"].get(model, 8.0))


def text_price(s, model):
    p = s["text_prices"].get(model) or [221, 1105]
    return float(p[0]), float(p[1])


def profile_models(s, profile):
    """Текстовые модели и качество для профиля: {script, check, quality}."""
    pt = (s.get("profile_text") or {}).get(profile) or {}
    return {"script": pt.get("script") or s["script_model"], "check": pt.get("check") or s["check_model"],
            "quality": pt.get("quality") or ""}


def image_size(model):
    return "896x1200" if model.startswith("google/") else "1024x1536"
