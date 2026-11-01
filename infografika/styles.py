# -*- coding: utf-8 -*-
"""Библиотека стилей: готовые стили под медицину/реабилитацию + сохранённые вами (styles.json)."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "styles.json")

PRESETS = [
    {"name": "Чистая медицина", "typo": {"cover_layout": "hero", "fill": "gradient", "colors": ["#1f8fb4", "#12303f"], "case": "sentence"}, "font": "Manrope", "caps": True, "ink": "#12303f", "accent": "#1f8fb4",
     "plate": "#ffffff",
     "scene_base_en": "clean bright home-care setting, white and pale blue tones, soft diffused daylight, "
                      "minimal tidy interior, trustworthy medical aesthetic",
     "why": "строго и аккуратно: бело-голубые тона, много воздуха"},
    {"name": "Тёплая забота", "typo": {"cover_layout": "classic", "fill": "solid", "colors": ["#d2874a", "#3b2a20"], "case": "sentence"}, "font": "Golos Text", "caps": False, "ink": "#3b2a20", "accent": "#d2874a",
     "plate": "#fffaf3",
     "scene_base_en": "warm cozy home interior, beige cream and light wood tones, soft warm window light, "
                      "calm caring atmosphere, elderly people and their families",
     "why": "тёплый домашний интерьер, мягкий свет, ощущение заботы"},
    {"name": "Премиум", "typo": {"cover_layout": "classic", "fill": "solid", "colors": ["#c9a45c", "#f4efe6"], "case": "sentence"}, "font": "PT Serif", "caps": False, "ink": "#f4efe6", "accent": "#c9a45c",
     "plate": "#1d2226",
     "scene_base_en": "dark elegant background, deep graphite and charcoal tones, soft dramatic spotlight, "
                      "premium studio product photography",
     "why": "тёмный фон, акцентный свет, дорогое ощущение"},
    {"name": "Яркий маркетплейс", "typo": {"cover_layout": "hero", "fill": "gradient", "colors": ["#ff6b2c", "#101820"], "case": "upper"}, "font": "Montserrat", "caps": True, "ink": "#101820", "accent": "#ff6b2c",
     "plate": "#ffffff",
     "scene_base_en": "bright clean background with a tasteful saturated colour gradient, high-key lighting, "
                      "crisp and energetic marketplace look",
     "why": "контрастно и броско, крупный заголовок"},
    {"name": "Минимализм", "typo": {"cover_layout": "classic", "fill": "solid", "colors": ["#1a1a1a", "#1a1a1a"], "case": "sentence"}, "font": "Manrope", "caps": False, "ink": "#1a1a1a", "accent": "#1a1a1a",
     "plate": "#ffffff",
     "scene_base_en": "minimalist light grey seamless background, soft natural shadows, lots of negative space",
     "why": "ничего лишнего: светло-серый фон, товар в центре внимания"},
    {"name": "Природа и спокойствие", "typo": {"cover_layout": "hero", "fill": "gradient", "colors": ["#5c9a6b", "#1f3a2e"], "case": "sentence"}, "font": "Golos Text", "caps": False, "ink": "#1f3a2e",
     "accent": "#5c9a6b", "plate": "#f6fbf6",
     "scene_base_en": "calm natural setting, soft greens and natural light wood, fresh daylight, peaceful mood",
     "why": "спокойные зелёные тона, свежесть, натуральность"},
]


def saved():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def all_styles():
    return [dict(s, builtin=True) for s in PRESETS] + [dict(s, builtin=False) for s in saved()]


def find(name):
    for s in all_styles():
        if s["name"] == name:
            return s
    return None


def save_style(style, name):
    keep = {k: style.get(k) for k in ("font", "caps", "ink", "accent", "plate", "scene_base_en", "why", "typo")}
    keep["name"] = name
    items = [s for s in saved() if s["name"] != name] + [keep]
    with open(PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(PATH + ".tmp", PATH)
    return keep


def delete_style(name):
    items = [s for s in saved() if s["name"] != name]
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
