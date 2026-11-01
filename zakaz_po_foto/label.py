# -*- coding: utf-8 -*-
"""
Распознавание этикетки коробки: краткий код товара, количество, маркетплейс.

Формат этикетки:
    МН-01
    24 шт.
    (ВБ)

Алгоритм (проверен на реальных фото, устойчив к произвольному повороту,
темноте, расфокусу и низкому разрешению):
  1. находим на кадре плотный кластер тёмного текста на светлом фоне
     и выпрямляем его по minAreaRect;
  2. прогоняем OCR во всех четырёх ориентациях и двух режимах сегментации,
     выбираем результат по числу найденных полей;
  3. нормализуем латинские двойники кириллицы (Tesseract их путает)
     и сопоставляем код с закрытым списком известных кодов.
"""

import json
import os
import re

import cv2
import numpy as np
import pytesseract
from rapidfuzz import fuzz, process

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CODES_FILE = os.path.join(BASE_DIR, "codes.json")

RE_CODE = re.compile(r"\b([A-ZА-Я]{2,4})\s*[-–—]\s*(\d{1,3})\b")
RE_QTY = re.compile(r"(\d{1,4})\s*шт", re.I)
RE_MP = re.compile(r"\(\s*([A-ZА-Я]{2})\s*\)")

# Tesseract свободно подменяет кириллицу похожей латиницей и наоборот
LAT2CYR = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")

MARKETPLACES = {"ВБ": "ВБ", "ОЗ": "ОЗ", "WB": "ВБ", "OZ": "ОЗ", "ЯМ": "ЯМ"}


# ---------- поиск и выпрямление этикетки ----------

def text_block(img, pad=0.25):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    th = cv2.adaptiveThreshold(
        cv2.GaussianBlur(g, (5, 5), 0), 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 20,
    )
    # оставляем только текст, лежащий на светлом фоне — отсекаем картон и фон
    level, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bright = cv2.dilate(g, np.ones((25, 25), np.uint8)) > max(level * 0.85, 90)
    th = (th * bright).astype(np.uint8)

    k = max(9, int(min(h, w) / 45))
    glue = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((k, k * 3), np.uint8))
    glue = cv2.morphologyEx(glue, cv2.MORPH_CLOSE, np.ones((k * 3, k), np.uint8))

    cnts, _ = cv2.findContours(glue, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < h * w * 0.005:
        return None

    (cx, cy), (rw, rh), ang = cv2.minAreaRect(c)
    rw, rh = rw * (1 + pad), rh * (1 + pad)
    if rw < rh:
        rw, rh = rh, rw
        ang += 90
    M = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)
    rot = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC,
                         borderMode=cv2.BORDER_REPLICATE)
    crop = rot[max(int(cy - rh / 2), 0):int(cy + rh / 2),
               max(int(cx - rw / 2), 0):int(cx + rw / 2)]
    return crop if crop.size else None


def prep(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = g.shape
    s = 1400 / max(h, w)
    if s > 1:
        g = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    g = cv2.bilateralFilter(g, 9, 75, 75)
    _, t = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cv2.copyMakeBorder(t, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)


def _score(t):
    return (3 * bool(RE_CODE.search(t)) + 3 * bool(RE_QTY.search(t))
            + 2 * bool(RE_MP.search(t)))


def _read(img, lang):
    base = prep(img)
    best = (-1, "")
    for k in range(4):
        r = np.ascontiguousarray(np.rot90(base, k))
        for psm in (6, 11):
            t = pytesseract.image_to_string(r, lang=lang, config=f"--psm {psm}")
            s = _score(t) + len(re.findall(r"[A-ZА-Я0-9]", t)) / 100
            if s > best[0]:
                best = (s, t)
    return best[1]


def ocr_label(path, tesseract_path, lang="rus+eng"):
    """Фото -> сырой распознанный текст этикетки."""
    pytesseract.pytesseract.tesseract_cmd = tesseract_path
    src = cv2.imread(path)
    if src is None:
        raise ValueError("Не удалось прочитать изображение")
    text = ""
    crop = text_block(src)
    if crop is not None:
        text = _read(crop, lang)
    if _score(text) < 6:                      # не все поля — пробуем кадр целиком
        alt = _read(src, lang)
        if _score(alt) > _score(text):
            text = alt
    return text


# ---------- разбор ----------

def load_codes():
    if not os.path.exists(CODES_FILE):
        return {}
    with open(CODES_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_code(code, guid, name):
    data = load_codes()
    data[code] = {"guid": guid, "name": name}
    with open(CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)


def norm_code(prefix, number):
    return f"{prefix.upper().translate(LAT2CYR)}-{int(number):02d}"


def resolve_code(raw_code, known, cutoff=80):
    """Приводим распознанный код к известному из справочника кодов."""
    if not known:
        return raw_code, False
    if raw_code in known:
        return raw_code, True
    hit = process.extractOne(raw_code, list(known), scorer=fuzz.ratio)
    if hit and hit[1] >= cutoff:
        return hit[0], True
    return raw_code, False


def parse(text, known_codes=None):
    """
    Сырой текст -> {"code", "qty", "mp", "known", "raw"}.
    Любое поле может быть None, если не распозналось.
    """
    known_codes = known_codes or {}
    res = {"code": None, "qty": None, "mp": None, "known": False,
           "raw": text.strip()}

    m = RE_CODE.search(text)
    if m:
        code = norm_code(m.group(1), m.group(2))
        code, known = resolve_code(code, known_codes)
        res["code"], res["known"] = code, known

    q = RE_QTY.search(text)
    if q:
        res["qty"] = int(q.group(1))

    p = RE_MP.search(text)
    if p:
        tag = p.group(1).upper().translate(LAT2CYR)
        hit = process.extractOne(tag, list(MARKETPLACES), scorer=fuzz.ratio)
        if hit and hit[1] >= 50:
            res["mp"] = MARKETPLACES[hit[0]]
    return res


def recognize(path, tesseract_path, lang="rus+eng"):
    return parse(ocr_label(path, tesseract_path, lang), load_codes())


# ---------- автономная проверка: python label.py фото1.jpg фото2.jpg ----------

if __name__ == "__main__":
    import configparser
    import sys

    _cfg = configparser.ConfigParser()
    _cfg.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")
    _tess = _cfg["ocr"]["tesseract"]
    _lang = _cfg["ocr"]["lang"]

    if len(sys.argv) < 2:
        print("Использование: python label.py путь_к_фото.jpg [ещё_фото.jpg ...]")
        raise SystemExit(1)

    for p in sys.argv[1:]:
        try:
            r = recognize(p, _tess, _lang)
            print(f"{os.path.basename(p):40s} код={r['code']} "
                  f"кол-во={r['qty']} МП={r['mp']} "
                  f"{'(код известен)' if r['known'] else '(код новый)'}")
            if not r["code"]:
                print("    сырой текст:", repr(r["raw"][:120]))
        except Exception as e:
            print(f"{os.path.basename(p):40s} ОШИБКА: {e}")
