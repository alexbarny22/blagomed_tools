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
import math
import re
import time
import uuid

import requests
import urllib3
from PIL import Image, ImageOps

# GigaChat работает с отключённой проверкой сертификата — не засоряем экран предупреждениями
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ------------------------------------------------------------------ подготовка фото
OWN_INNS = set()      # заполняется из config.OWN_INNS ботом
MAX_SIDE = 1600        # длинная сторона: Claude всё равно уменьшает до ~1600, больше — только дольше грузится
QUALITY = 80


def prepare_image(raw: bytes, max_side: int = 0, quality: int = 0) -> bytes:
    """Поворот по EXIF, RGB, уменьшение, лёгкое повышение контраста. Возвращает JPEG."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    k = (max_side or MAX_SIDE) / max(w, h)
    if k < 1:
        img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality or QUALITY, optimize=True)
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
 "records_count": "сколько порядковых номеров записей указано в документе («содержит … порядковых номеров записей»), числом",
 "notes": "что не удалось прочитать или в чём сомневаешься"
}
Правила:
- "n" — номер строки из таблицы документа (графа «№ п/п»), если он виден.
- "name" — переписывай БУКВА В БУКВУ, как напечатано: не исправляй, не сокращай, не дополняй.
- "article" — код товара поставщика из графы «код» / «артикул» (например 00-00000030), если есть.
- Пустые и нулевые поля можно не писать — короче ответ, быстрее работа.
- "sum" — стоимость строки С налогом (итоговая графа), "sum_without_vat" — без налога; если НДС нет — они равны.
- "price" — цена за единицу как в документе.
- "vat_rate": "без НДС", "0%", "5%", "7%", "10%", "20%" или "22%".
- Числа — числами, с точкой: 1234.50. Пробелы между разрядами убирай.
- Количество в ТОРГ-12 и УПД часто пишут с тремя знаками после запятой: «50,000» — это 50, а не 50000.
- Если цифру не разобрать — поставь наиболее вероятное значение и "unclear": true.
- "total" — итог по ВСЕМУ документу: строка «Всего по накладной» / «Всего к оплате». Если документ на
  нескольких страницах, у каждой страницы своё «Итого» — это НЕ итог документа, его не бери.
- Не выдумывай строки, которых нет на фото. Номер строки бывает обрезан краем фото — тогда нумеруй по порядку.
- ИНН поставщика бери из строки «Поставщик» (или «Продавец»), а не из «Грузополучатель», «Плательщик»,
  «Покупатель» и не из печати. ИНН — ровно 10 цифр (организация) или 12 цифр (ИП). ОГРН/ОГРНИП, ОКПО,
  расчётный счёт, БИК — это НЕ ИНН. Перепроверь каждую цифру ИНН.
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


def _post_retry(url, tries=3, **kw):
    """Обрыв связи (SSL EOF, сброс соединения) — повторяем с паузой."""
    for i in range(tries):
        try:
            return requests.post(url, **kw)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            if i == tries - 1:
                raise ModelError(f"обрыв связи с {url.split('/')[2]}: {str(e)[:150]}")
            time.sleep(3 + 5 * i)


def _ask_openai_compatible(m: dict, images: list) -> str:
    content = [{"type": "text", "text": PROMPT}]
    for jpg in images:
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}})
    r = _post_retry(m["base_url"].rstrip("/") + "/chat/completions",
                      headers={"Authorization": "Bearer " + m["api_key"]},
                      json={"model": m["model"], "temperature": 0, "max_tokens": 16000,
                            "messages": [{"role": "user", "content": content}], **m.get("extra", {})},
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
                               "Content-Type": "application/x-www-form-urlencoded",
                               "Accept": "application/json"},
                      data={"scope": m.get("scope", "GIGACHAT_API_PERS")},
                      verify=m.get("verify_ssl", False), timeout=30)
    if r.status_code != 200:
        raise ModelError(f"GigaChat авторизация: HTTP {r.status_code} {r.text[:300]}")
    j = r.json()
    _giga_token.update(value=j["access_token"], until=j.get("expires_at", 0) / 1000 or time.time() + 1500)
    return _giga_token["value"]


def _ask_gigachat(m: dict, images: list) -> str:
    # у новых моделей (GigaChat 3 Ultra) свой адрес — задаётся в config.py полем base_url
    base = m.get("base_url", "https://gigachat.devices.sberbank.ru/api/v1").rstrip("/")
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


def ask_text(m: dict, prompt: str) -> str:
    """Короткий текстовый запрос (без фото) — для подсказок при сопоставлении товаров."""
    r = _post_retry(m["base_url"].rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + m["api_key"]},
                    json={"model": m["model"], "temperature": 0, "max_tokens": 2000,
                          "messages": [{"role": "user", "content": prompt}], **m.get("extra", {})},
                    timeout=120)
    if r.status_code != 200:
        raise ModelError(f"{m['name']}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]


def ask_model(m: dict, images: list, raw_images: list = None) -> dict:
    """raw_images — исходные фото: если связь рвётся на больших файлах, пробуем уменьшенные."""
    ask = _ask_gigachat if m.get("type", "openai") == "gigachat" else _ask_openai_compatible
    try:
        text = ask(m, images)
    except ModelError as e:
        if "обрыв связи" not in str(e) or not raw_images:
            raise
        small = [prepare_image(b, 1200, 75) for b in raw_images]
        text = ask(m, small)
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
    rc = num(d.get("records_count"))
    d["records_count"] = int(rc) if rc and rc == int(rc) and 0 < rc < 1000 else None
    # покупатель и поставщик перепутаны? (ИНН своих организаций — в config.OWN_INNS)
    if d["supplier"]["inn"] in OWN_INNS and d["buyer"].get("inn"):
        d["supplier"], d["buyer"] = d["buyer"], d["supplier"]
        d["supplier"]["inn"] = re.sub(r"\D", "", str(d["supplier"].get("inn") or ""))
    return d


def inn_ok(inn: str) -> bool:
    """Контрольная сумма ИНН: ловит перепутанную или пропущенную цифру."""
    if not re.fullmatch(r"\d{10}|\d{12}", inn or ""):
        return False
    d = [int(c) for c in inn]
    if len(d) == 10:
        return sum(a * b for a, b in zip([2, 4, 10, 3, 5, 9, 4, 6, 8], d)) % 11 % 10 == d[9]
    n11 = sum(a * b for a, b in zip([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)) % 11 % 10
    n12 = sum(a * b for a, b in zip([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)) % 11 % 10
    return n11 == d[10] and n12 == d[11]


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
        elif x["qty"] >= 5000 or (x["unit"].lower().startswith("шт") and x["qty"] != int(x["qty"])):
            bad.add(x["n"])
            issues.append(f"строка {x['n']}: странное количество {x['qty']:g} {x['unit']} — проверьте")
        elif x["unclear"]:
            bad.add(x["n"])
            issues.append(f"строка {x['n']}: нейросеть сомневается в цифрах")
    incomplete = False
    nums = [x["n"] for x in d["lines"]]
    if nums and nums[0] >= 1 and max(nums) - min(nums) + 1 != len(set(nums)):
        missing = sorted(set(range(1, max(nums) + 1)) - set(nums))
        if missing:
            incomplete = True
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
    rc = d.get("records_count")
    if rc and rc != len(d["lines"]):
        incomplete = True
        issues.append(f"в документе {rc} строк(и), а прочитано {len(d['lines'])} — проверьте, все ли страницы")
    inn = d["supplier"].get("inn")
    if not inn:
        issues.append("ИНН поставщика не прочитан")
    elif not inn_ok(inn):
        issues.append(f"ИНН поставщика {inn} прочитан с ошибкой (не сходится контрольная сумма)")
    return {"issues": issues, "bad_lines": bad, "total_ok": total_ok, "inn_bad": not inn_ok(inn or ""),
            "incomplete": incomplete}


def is_good(chk: dict) -> bool:
    """Всё сошлось: строки, итог, ИНН и ни одна строка не потеряна."""
    return bool(chk["total_ok"]) and not chk["bad_lines"] and not chk["inn_bad"] and not chk.get("incomplete")


def score(chk: dict) -> int:
    return (len(chk["issues"]) + (5 if chk["total_ok"] is False else 0) + (3 if chk.get("inn_bad") else 0)
            + (5 if chk.get("incomplete") else 0))


# ------------------------------------------------------------------ ход работы (для красивого показа)
class Steps:
    """Список этапов с состоянием. Бот и тест читают его и рисуют прогресс; сам ничего не выводит."""
    EXPECT = {"prep": 2, "model": 45, "onec": 8}     # ожидаемая длительность этапа, с

    def __init__(self):
        self.items = []         # {kind, title, state: run|ok|warn|err|wait, note, t0, t1}
        self.started = time.time()

    def add(self, kind, title, state="run"):
        self.items.append({"kind": kind, "title": title, "state": state, "note": "",
                           "t0": time.time() if state == "run" else None, "t1": None})
        return self.items[-1]

    def finish(self, it, state="ok", note=""):
        it.update(state=state, note=note, t1=time.time())

    def fraction(self):
        """Доля выполненного 0..1: завершённые этапы + оценка текущего по времени (не больше 95%)."""
        tot = done = 0.0
        for it in self.items:
            w = self.EXPECT.get(it["kind"], 10)
            tot += w
            if it["state"] in ("ok", "warn", "err"):
                done += w
            elif it["state"] == "run" and it["t0"]:
                # плавно растёт и никогда не упирается в 100%, пока этап не закончен
                done += w * min(0.95, 1 - math.exp(-(time.time() - it["t0"]) / w))
        return min(done / tot, 1.0) if tot else 0.0

    def elapsed(self, it=None):
        if it is None:
            return time.time() - self.started
        return (it["t1"] or time.time()) - (it["t0"] or time.time())


# ------------------------------------------------------------------ главный вход
def recognize(raw_images: list, models: list, log=print, start_from=0, steps=None, race=1) -> dict:
    """
    raw_images — список байтов фото. models — config.MODELS.
    race — сколько первых моделей запускать одновременно: берётся первый ответ, у которого всё сошлось
    (ждём самую быструю, а не всех по очереди). Если ни одна не сошлась — остальные модели по очереди.
    Возвращает {'data', 'check', 'model', 'tried': [(имя, кол-во замечаний или ошибка)]}.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    steps = steps or Steps()
    st = steps.add("prep", f"Подготовка фото ({len(raw_images)} шт)")
    images = [prepare_image(b) for b in raw_images]
    steps.finish(st)
    order = [m for m in models[start_from:] + models[:start_from] if m.get("enabled", True)]
    state = {"best": None, "tried": []}

    def run(m, st):
        t0 = time.time()
        try:
            data = ask_model(m, images, raw_images)
        except Exception as e:
            log(f"{m['name']}: ошибка — {e}")
            state["tried"].append((m["name"], f"ошибка: {str(e)[:120]}"))
            if st["state"] == "run":
                steps.finish(st, "err", "нет ответа")
            return None
        chk = check(data)
        good = is_good(chk)
        log(f"{m['name']}: строк {len(data['lines'])}, замечаний {len(chk['issues'])}, {time.time() - t0:.0f} с")
        state["tried"].append((m["name"], len(chk["issues"])))
        if st["state"] == "run":
            steps.finish(st, "ok" if good else "warn",
                         f"строк {len(data['lines'])}, суммы сошлись" if good else f"замечаний {len(chk['issues'])}")
        cand = {"data": data, "check": chk, "model": m["name"]}
        if state["best"] is None or score(chk) < score(state["best"]["check"]):
            state["best"] = cand
        return good

    def done():
        best = state["best"]
        if best is None:
            raise ModelError("Ни одна нейросеть не ответила:\n" +
                             "\n".join(f"• {n}: {r}" for n, r in state["tried"]))
        for it in steps.items:          # кого не дождались — уже не нужны
            if it["state"] == "run":
                steps.finish(it, "skip", "не понадобилась")
        best["tried"] = list(state["tried"])
        return best

    first, rest = order[:max(1, race)], order[max(1, race):]
    if len(first) > 1:
        pool = ThreadPoolExecutor(max_workers=len(first))
        futs = {pool.submit(run, m, steps.add("model", f"{m['name']} читает накладную")): m for m in first}
        try:
            for f in as_completed(futs):
                if f.result():
                    return done()       # первый правильный ответ — дальше не ждём
        finally:
            pool.shutdown(wait=False)
    else:
        for m in first:
            if run(m, steps.add("model", f"{m['name']} читает накладную")):
                return done()
    for m in rest:                      # никто не сошёлся — перепроверяем остальными по очереди
        if run(m, steps.add("model", f"{m['name']} перепроверяет накладную")):
            break
    return done()
