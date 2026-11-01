# -*- coding: utf-8 -*-
"""
Распознавание накладной (УПД / ТОРГ-12 / счёт / накладная) по одной или нескольким фотографиям.

Нейросеть выбирается в config.py (список MODELS). Поддерживаются:
  * любой OpenAI-совместимый агрегатор (vsellm, VseGPT, ProxyAPI, OpenRouter …) — Claude, GPT, Gemini и др.;
  * GigaChat (Сбер).
Если первая модель ошиблась в суммах — автоматически пробуется следующая, берётся лучший результат.
"""
import base64
import io
import json
import re
import time
import uuid

import requests
from PIL import Image, ImageOps

# ------------------------------------------------------------------ подготовка фото
MAX_SIDE = 2400        # длинная сторона после уменьшения — хватает для мелкого шрифта


def prepare_image(raw: bytes) -> bytes:
    """Поворот по EXIF, RGB, уменьшение, лёгкое повышение контраста. Возвращает JPEG."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    k = MAX_SIDE / max(w, h)
    if k < 1:
        img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


# ------------------------------------------------------------------ запрос к нейросети
PROMPT = """На фотографиях — страницы ОДНОГО бумажного документа поставщика о поставке товара
(УПД, ТОРГ-12, товарная накладная или счёт). Фото могут быть плохого качества, под углом, в любом
порядке; одна и та же строка таблицы может попасть на два фото — не дублируй её.

Извлеки данные и верни ТОЛЬКО JSON без пояснений и без ```:
{
 "doc_type": "УПД | ТОРГ-12 | Накладная | Счет | Другое",
 "number": "номер документа",
 "date": "ДД.ММ.ГГГГ",
 "supplier": {"name": "продавец/поставщик", "inn": "ИНН цифрами", "kpp": ""},
 "buyer": {"name": "покупатель", "inn": ""},
 "lines": [
   {"n": 1, "name": "наименование полностью, как в документе", "article": "артикул/код если есть",
    "unit": "шт", "qty": 1, "price": 0.0, "sum_without_vat": 0.0, "vat_rate": "без НДС",
    "vat_sum": 0.0, "sum": 0.0, "unclear": false}
 ],
 "total": {"sum_without_vat": 0.0, "vat": 0.0, "sum": 0.0},
 "notes": "что не удалось прочитать или в чём сомневаешься"
}
Правила:
- "n" — номер строки из таблицы документа (графа «№ п/п»), если он виден.
- "sum" — стоимость строки С налогом (итоговая графа), "sum_without_vat" — без налога; если НДС нет — они равны.
- "price" — цена за единицу как в документе.
- "vat_rate": "без НДС", "0%", "5%", "7%", "10%", "20%" или "22%".
- Числа — числами, с точкой: 1234.50. Пробелы между разрядами убирай.
- Если цифру не разобрать — поставь наиболее вероятное значение и "unclear": true.
- Не выдумывай строки, которых нет на фото. ИНН поставщика ищи в шапке (продавец / грузоотправитель).
"""


class ModelError(Exception):
    pass


def _parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < 0:
        raise ModelError("Нейросеть не вернула JSON: " + text[:300])
    try:
        return json.loads(t[a:b + 1])
    except json.JSONDecodeError as e:
        raise ModelError(f"Испорченный JSON от нейросети ({e}): " + text[:300])


def _ask_openai_compatible(m: dict, images: list) -> str:
    content = [{"type": "text", "text": PROMPT}]
    for jpg in images:
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}})
    r = requests.post(m["base_url"].rstrip("/") + "/chat/completions",
                      headers={"Authorization": "Bearer " + m["api_key"]},
                      json={"model": m["model"], "temperature": 0, "max_tokens": 16000,
                            "messages": [{"role": "user", "content": content}]},
                      timeout=m.get("timeout", 240))
    if r.status_code != 200:
        raise ModelError(f"{m['name']}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


_giga_token = {"value": "", "until": 0}


def _giga_auth(m: dict) -> str:
    if _giga_token["value"] and time.time() < _giga_token["until"] - 60:
        return _giga_token["value"]
    r = requests.post("https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                      headers={"Authorization": "Basic " + m["api_key"], "RqUID": str(uuid.uuid4()),
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"scope": m.get("scope", "GIGACHAT_API_PERS")},
                      verify=m.get("verify_ssl", False), timeout=30)
    if r.status_code != 200:
        raise ModelError(f"GigaChat авторизация: HTTP {r.status_code} {r.text[:300]}")
    j = r.json()
    _giga_token.update(value=j["access_token"], until=j.get("expires_at", 0) / 1000 or time.time() + 1500)
    return _giga_token["value"]


def _ask_gigachat(m: dict, images: list) -> str:
    base = "https://gigachat.devices.sberbank.ru/api/v1"
    hdr = {"Authorization": "Bearer " + _giga_auth(m)}
    ver = m.get("verify_ssl", False)
    msgs = []
    # GigaChat принимает одно изображение на сообщение — каждое фото отдельным сообщением
    for i, jpg in enumerate(images, 1):
        up = requests.post(base + "/files", headers=hdr, verify=ver, timeout=60,
                           files={"file": (f"page{i}.jpg", jpg, "image/jpeg")}, data={"purpose": "general"})
        if up.status_code != 200:
            raise ModelError(f"GigaChat загрузка фото: HTTP {up.status_code} {up.text[:300]}")
        msgs.append({"role": "user", "content": f"Фото {i} из {len(images)}.", "attachments": [up.json()["id"]]})
    msgs.append({"role": "user", "content": PROMPT})
    r = requests.post(base + "/chat/completions", headers=hdr, verify=ver, timeout=m.get("timeout", 240),
                      json={"model": m["model"], "temperature": 0.01, "max_tokens": 16000, "messages": msgs})
    if r.status_code != 200:
        raise ModelError(f"GigaChat: HTTP {r.status_code} {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def ask_model(m: dict, images: list) -> dict:
    if m.get("type", "openai") == "gigachat":
        text = _ask_gigachat(m, images)
    else:
        text = _ask_openai_compatible(m, images)
    return normalize(_parse_json(text))


# ------------------------------------------------------------------ числа и самопроверка
def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    if s.count(".") > 1:                       # 1.234.50 -> 1234.50
        head, tail = s.rsplit(".", 1)
        s = head.replace(".", "") + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def vat_percent(rate) -> float:
    m = re.search(r"(\d+)", str(rate or ""))
    return float(m.group(1)) if m else 0.0


def normalize(d: dict) -> dict:
    d.setdefault("supplier", {})
    d.setdefault("buyer", {})
    d["supplier"]["inn"] = re.sub(r"\D", "", str(d["supplier"].get("inn") or ""))
    lines = []
    for i, ln in enumerate(d.get("lines") or [], 1):
        x = {
            "n": int(num(ln.get("n")) or i),
            "name": str(ln.get("name") or "").strip(),
            "article": str(ln.get("article") or "").strip(),
            "unit": str(ln.get("unit") or "").strip(),
            "qty": num(ln.get("qty")) or 0.0,
            "price": num(ln.get("price")) or 0.0,
            "sum_without_vat": num(ln.get("sum_without_vat")),
            "vat_rate": str(ln.get("vat_rate") or "без НДС"),
            "vat_sum": num(ln.get("vat_sum")) or 0.0,
            "sum": num(ln.get("sum")),
            "unclear": bool(ln.get("unclear")),
        }
        if not x["name"]:
            continue
        if x["sum"] is None:
            base = x["sum_without_vat"] if x["sum_without_vat"] is not None else x["qty"] * x["price"]
            x["sum"] = round(base + x["vat_sum"], 2)
        if x["sum_without_vat"] is None:
            x["sum_without_vat"] = round(x["sum"] - x["vat_sum"], 2)
        lines.append(x)
    # дубли строк с двух соседних фото (одинаковый номер и содержимое)
    seen, uniq = set(), []
    for x in lines:
        k = (x["n"], x["name"].lower()[:40], round(x["qty"], 3), round(x["sum"], 2))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    uniq.sort(key=lambda x: x["n"])
    d["lines"] = uniq
    t = d.get("total") or {}
    d["total"] = {"sum_without_vat": num(t.get("sum_without_vat")), "vat": num(t.get("vat")),
                  "sum": num(t.get("sum"))}
    return d


def _close(a, b, rel=0.005, abs_=1.0):
    return a is not None and b is not None and abs(a - b) <= max(abs_, abs(b) * rel)


def line_ok(x) -> bool:
    """Количество × цена должно совпасть с суммой без НДС или с суммой (цена бывает с НДС)."""
    q, p = x["qty"], x["price"]
    if q <= 0:
        return False
    calc = q * p
    k = 1 + vat_percent(x["vat_rate"]) / 100
    return _close(calc, x["sum_without_vat"]) or _close(calc, x["sum"]) or _close(calc * k, x["sum"])


def check(d: dict) -> dict:
    """Самопроверка. Возвращает {'issues': [...], 'bad_lines': {n,...}, 'total_ok': bool|None}."""
    issues, bad = [], set()
    if not d["lines"]:
        issues.append("не найдено ни одной строки товара")
    for x in d["lines"]:
        if not line_ok(x):
            bad.add(x["n"])
            issues.append(f"строка {x['n']}: {x['qty']:g} × {x['price']:.2f} ≠ {x['sum']:.2f}")
        elif x["unclear"]:
            bad.add(x["n"])
            issues.append(f"строка {x['n']}: нейросеть сомневается в цифрах")
    nums = [x["n"] for x in d["lines"]]
    if nums and nums[0] >= 1 and max(nums) - min(nums) + 1 != len(set(nums)):
        missing = sorted(set(range(1, max(nums) + 1)) - set(nums))
        if missing:
            issues.append("пропущены строки № " + ", ".join(map(str, missing[:15])) +
                          " — возможно, не хватает фото")
    total_ok = None
    tot = d["total"].get("sum")
    if tot:
        s = round(sum(x["sum"] for x in d["lines"]), 2)
        total_ok = _close(s, tot, 0.001, 1.0)
        if not total_ok:
            issues.append(f"сумма строк {s:,.2f} ≠ итогу документа {tot:,.2f}".replace(",", " "))
    else:
        issues.append("итог документа не прочитан — сверить не с чем")
    if not d["supplier"].get("inn"):
        issues.append("ИНН поставщика не прочитан")
    return {"issues": issues, "bad_lines": bad, "total_ok": total_ok}


def score(chk: dict) -> int:
    return len(chk["issues"]) + (5 if chk["total_ok"] is False else 0)


# ------------------------------------------------------------------ главный вход
def recognize(raw_images: list, models: list, log=print, start_from=0) -> dict:
    """
    raw_images — список байтов фото. models — config.MODELS.
    Пробует модели по очереди, пока результат не сойдётся по суммам. Возвращает
    {'data', 'check', 'model', 'tried': [(имя, кол-во замечаний или ошибка)]}.
    """
    images = [prepare_image(b) for b in raw_images]
    best, tried = None, []
    order = models[start_from:] + models[:start_from]
    for m in order:
        if not m.get("enabled", True):
            continue
        t0 = time.time()
        try:
            data = ask_model(m, images)
        except Exception as e:
            log(f"{m['name']}: ошибка — {e}")
            tried.append((m["name"], f"ошибка: {str(e)[:120]}"))
            continue
        chk = check(data)
        log(f"{m['name']}: строк {len(data['lines'])}, замечаний {len(chk['issues'])}, {time.time() - t0:.0f} с")
        tried.append((m["name"], len(chk["issues"])))
        cand = {"data": data, "check": chk, "model": m["name"]}
        if best is None or score(chk) < score(best["check"]):
            best = cand
        if chk["total_ok"] and not chk["bad_lines"]:
            break      # всё сошлось — дальше не тратим деньги
    if best is None:
        raise ModelError("Ни одна нейросеть не ответила:\n" + "\n".join(f"• {n}: {r}" for n, r in tried))
    best["tried"] = tried
    return best
