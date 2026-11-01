# -*- coding: utf-8 -*-
"""
Двигатель генератора: сценарий и тексты (текстовая нейросеть), слайды (нейросеть-художник по фото
товара), проверка качества, наложение текста кодом, учёт денег и потолок на серию.
Все данные серии — в папке projects\\<id>\\, состояние — в state.json (его читает страница в браузере).
"""
import base64
import concurrent.futures as cf
import io
import json
import os
import re
import shutil
import threading
import time
import traceback

import requests
from PIL import Image, ImageOps

import render
import settings as S
import styles as ST

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.join(HERE, "projects")
KINDS = ["cover", "problem", "specs", "usage", "trust"]
FILE_NAMES = {"cover": "обложка", "problem": "проблема", "specs": "размеры", "usage": "применение",
              "trust": "комплектация"}

_models_cache = {"t": 0, "ids": None}


class Stop(Exception):
    pass


def money(x):
    return f"{x:.2f} ₽".replace(".", ",")


# =============================================================== vsellm
def client():
    from openai import OpenAI
    # max_retries=0: vsellm берёт деньги и за неудачные попытки — повторяем только сами, с учётом потолка
    return OpenAI(api_key=S.key(), base_url=S.url(), timeout=300, max_retries=0)


def vsellm_models(force=False):
    if not force and _models_cache["ids"] is not None and time.time() - _models_cache["t"] < 3600:
        return _models_cache["ids"]
    try:
        r = requests.get(S.url().rstrip("/") + "/models", headers={"Authorization": "Bearer " + S.key()},
                         timeout=40)
        if r.status_code == 200:
            _models_cache.update(t=time.time(), ids={x.get("id", "") for x in r.json().get("data", [])})
            return _models_cache["ids"]
    except requests.RequestException:
        pass
    return None


def resolve(mid):
    ids = vsellm_models()
    if not ids or not mid:
        return mid
    base = mid[:-len("-preview")] if mid.endswith("-preview") else mid
    for c in (mid, base, base + "-preview"):
        if c in ids:
            return c
    return mid


def err_text(e):
    code = getattr(e, "status_code", None)
    msg = re.sub(r"\s+", " ", str(getattr(e, "message", "") or e))[:200]
    if isinstance(e, (requests.ConnectionError,)) or "Connection" in e.__class__.__name__:
        return "нет связи с vsellm (выключите VPN)"
    return f"HTTP {code}: {msg}" if code else msg


def jpeg_url(img_or_path, side=1024):
    im = Image.open(img_or_path) if isinstance(img_or_path, str) else img_or_path
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((side, side))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


def png_bytes(path, side=1536):
    im = ImageOps.exif_transpose(Image.open(path))
    im.thumbnail((side, side))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def parse_json(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M)
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("нейросеть ответила не в формате JSON")
    return json.loads(txt[a:b + 1])


# =============================================================== проект
class Project:
    def __init__(self, pid):
        self.id = pid
        self.dir = os.path.join(PROJECTS, pid)
        self.lock = threading.RLock()
        self.resume = threading.Event()
        self.thread = None
        with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as f:
            self.st = json.load(f)
        if self.st.get("status") in ("planning", "writing", "generating", "paused_budget"):
            self.st["status"] = "interrupted"     # программу закрыли посреди работы
            self.save()

    # ---------- состояние
    def save(self):
        with self.lock:
            p = os.path.join(self.dir, "state.json")
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.st, f, ensure_ascii=False, indent=1)
            os.replace(p + ".tmp", p)

    def path(self, *a):
        return os.path.join(self.dir, *a)

    def feed(self, text, kind="work", fid=None, cost=None):
        """Лента этапов на странице. kind: work (идёт), done, warn, err. fid — обновить строку."""
        with self.lock:
            f = self.st.setdefault("feed", [])
            if fid:
                for it in f:
                    if it.get("id") == fid:
                        it.update(text=text, kind=kind, t=time.time())
                        if cost is not None:
                            it["cost"] = round(it.get("cost", 0) + cost, 2)
                        self.save()
                        return fid
            fid = fid or f"f{len(f) + 1}"
            f.append({"id": fid, "text": text, "kind": kind, "t": time.time(),
                      **({"cost": round(cost, 2)} if cost else {})})
            self.save()
            return fid

    def set(self, **kw):
        with self.lock:
            self.st.update(kw)
            self.save()

    # ---------- деньги
    def spend(self, what, model, rub):
        with self.lock:
            self.st["spent"] = round(self.st.get("spent", 0) + rub, 2)
            self.st.setdefault("costs", []).append({"t": time.time(), "what": what, "model": model,
                                                    "rub": round(rub, 2)})
            self.save()

    def reserve(self, price, what):
        """Резерв денег под вызов. Не хватает — пауза и вопрос на странице, ждём решения."""
        while True:
            with self.lock:
                if self.st.get("stop_requested"):
                    raise Stop()
                spent, res, lim = self.st.get("spent", 0), self.st.get("reserved", 0), self.st["limit"]
                if spent + res + price <= lim + 1e-9:
                    self.st["reserved"] = round(res + price, 2)
                    self.save()
                    return
                waiting_others = res > 1e-9
                if not waiting_others:
                    if self.st["status"] != "paused_budget":
                        self.st["status_before_pause"] = self.st["status"]
                        self.resume.clear()
                    self.st["budget"] = {"need": round(price, 2), "what": what, "left": round(lim - spent, 2)}
                    self.st["status"] = "paused_budget"
                    self.save()
            if waiting_others:          # параллельные вызовы ещё не списались — подождём их
                time.sleep(0.4)
                continue
            self.resume.wait()
            with self.lock:
                self.st.pop("budget", None)
                if self.st["status"] == "paused_budget":
                    self.st["status"] = self.st.get("status_before_pause") or "generating"
                self.save()

    def settle(self, reserved):
        with self.lock:
            self.st["reserved"] = max(0, round(self.st.get("reserved", 0) - reserved, 2))
            self.save()

    # ---------- запуск фоновой работы
    def run(self, fn, *args):
        if self.thread and self.thread.is_alive():
            raise RuntimeError("Серия уже в работе — дождитесь окончания")
        self.st["stop_requested"] = False

        def wrap():
            try:
                fn(*args)
            except Stop:
                self.feed("Остановлено по вашему решению — готовое сохранено", "warn")
                if self.st.get("slides"):
                    self.finish()
                else:
                    with self.lock:
                        self.st.pop("budget", None)
                    self.set(status="plan_ready" if self.st.get("plan") else "interrupted")
            except Exception as e:       # noqa: BLE001
                traceback.print_exc()
                self.feed("Ошибка: " + err_text(e), "err")
                self.set(status="error", error=err_text(e))
            finally:
                with self.lock:
                    self.st["reserved"] = 0
                    self.save()
        self.thread = threading.Thread(target=wrap, daemon=True)
        self.thread.start()

    def finish(self):
        export(self)
        with self.lock:
            for s in self.st.get("slides", []):
                if not s.get("final") and s.get("status") in ("wait", "work", "check"):
                    s["status"] = "skip"
            self.st.pop("budget", None)
            self.save()
        n_ok = sum(1 for s in self.st.get("slides", []) if s.get("final"))
        self.set(status="done")
        self.feed(f"Готово: {n_ok} слайд(ов), потрачено {self.st.get('spent', 0):.2f} ₽".replace(".", ","),
                  "done")


_projects = {}
_plock = threading.Lock()


def get(pid):
    with _plock:
        if pid not in _projects:
            if not os.path.exists(os.path.join(PROJECTS, pid, "state.json")):
                return None
            _projects[pid] = Project(pid)
        return _projects[pid]


def history():
    out = []
    if not os.path.isdir(PROJECTS):
        return out
    for pid in sorted(os.listdir(PROJECTS), reverse=True):
        try:
            with open(os.path.join(PROJECTS, pid, "state.json"), encoding="utf-8") as f:
                st = json.load(f)
        except Exception:       # noqa: BLE001
            continue
        thumb = next((s.get("final") for s in st.get("slides", []) if s.get("final")), None)
        out.append({"id": pid, "title": st.get("title", pid), "created": st.get("created"),
                    "status": st.get("status"), "spent": st.get("spent", 0), "thumb": thumb,
                    "style": (st.get("plan") or {}).get("style")})
    return out


def create(fields, photos, refs):
    """photos/refs: список (имя, bytes). fields: product_text, wish, covers, style_mode, style_name,
    style_text, profile, limit."""
    title = (re.search(r"Название\s*:\s*(.+)", fields.get("product_text", "")) or [None, ""])[1].strip() \
        or fields.get("product_text", "").strip().split("\n")[0][:60] or "Товар"
    slug = re.sub(r"[^0-9a-zа-яё]+", "-", title.lower()).strip("-")[:40]
    pid = time.strftime("%Y%m%d-%H%M%S") + "-" + slug
    d = os.path.join(PROJECTS, pid)
    os.makedirs(os.path.join(d, "photos"))
    os.makedirs(os.path.join(d, "refs"))
    os.makedirs(os.path.join(d, "work"))
    names = {"photos": [], "refs": []}
    for kind, items in (("photos", photos), ("refs", refs)):
        for i, (name, data) in enumerate(items):
            ext = os.path.splitext(name)[1].lower() or ".jpg"
            fn = f"{i + 1}{ext}"
            with open(os.path.join(d, kind, fn), "wb") as f:
                f.write(data)
            names[kind].append(fn)
    s = S.load()
    st = {
        "id": pid, "title": title, "created": time.time(), "status": "planning",
        "inputs": {
            "product_text": fields.get("product_text", ""), "wish": fields.get("wish", ""),
            "covers": max(1, min(3, int(fields.get("covers") or 1))),
            "style_mode": fields.get("style_mode", "auto"), "style_name": fields.get("style_name", ""),
            "style_text": fields.get("style_text", ""),
            "profile": fields.get("profile") or s["profile"],
        },
        "photos": names["photos"], "refs": names["refs"],
        "limit": float(fields.get("limit") or s["limit_rub"]), "spent": 0, "reserved": 0,
        "costs": [], "feed": [], "plan": None, "slides": [],
    }
    with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    p = get(pid)
    p.run(plan_job, p)
    return p


# =============================================================== текстовая нейросеть
def chat(p, what, content, max_tokens=4000, model=None):
    s = S.load()
    model = resolve(model or s["script_model"])
    pin, pout = S.text_price(s, model)
    est = (12000 * pin + max_tokens * pout) / 1e6          # грубая оценка сверху — для резерва
    p.reserve(est, what)
    cost = 0.0
    try:
        r = client().chat.completions.create(model=model, max_tokens=max_tokens, temperature=0.7,
                                             messages=[{"role": "user", "content": content}])
        u = getattr(r, "usage", None)
        if u:
            cost = (u.prompt_tokens or 0) * pin / 1e6 + (u.completion_tokens or 0) * pout / 1e6
        else:
            cost = est / 2
        return r.choices[0].message.content or "", cost
    finally:
        p.settle(est)
        if cost:
            p.spend(what, model, cost)


PLAN_TASK = """Ты — арт-директор и маркетолог карточек товаров Wildberries и Ozon в категории медицинских
и реабилитационных товаров (магазин «Благомед»). Подготовь серию из 5 слайдов инфографики для карточки.

ДАННЫЕ ТОВАРА ОТ ПРОДАВЦА (могут быть сырыми: описание из карточки, характеристики, отзывы):
---
{product}
---
{style_block}
Ниже — фото настоящего товара{refs_note}.

ПРАВИЛА
• Слайд 1 (cover): заголовок — консервативный поисковый запрос, как покупатель ищет этот товар (2–6 слов,
  близко к семантическому ядру). Подзаголовок — эмоциональный или функциональный крючок до 60 символов.
• Слайд 2 (problem): проблема покупателя → решение. Короткий заголовок (до 35 символов), 3 пункта до 45 символов.
  Опирайся на реальные боли из отзывов и описания, если они есть.
• Слайд 3 (specs): характеристики и размеры. Заголовок до 30 символов, 2–3 пункта (материал, регулировка и т.п.).
  dims — только размеры, которые реально есть в данных (не выдумывай!): axis "x" — ширина, "y" — высота.
• Слайд 4 (usage): товар в применении. Заголовок до 30 символов, подзаголовок до 60 символов.
• Слайд 5 (trust): комплектация / уход / почему нам доверять. Заголовок до 30 символов, 3–4 пункта до 40 символов.
• Без медицинских обещаний: нельзя «лечит», «избавит от боли», «№1», «рекомендовано врачами».
  Нельзя упоминать конкурентов и другие маркетплейсы. Только факты из данных и с фото.
• scene_en — по-английски, что изобразить на фоне/в сцене (люди, окружение, свет), без текста на картинке.
  Деликатно: интимные зоны и медицинские изделия на теле — аккуратно, через одежду, без натурализма.
• Ключевые запросы — как реально ищут на WB/Ozon: main — 3–5 главных высокочастотных по смыслу,
  extra — 5–10 уточняющих, synonyms — 3–8 синонимов и разговорных вариантов.
• ЕДИНЫЙ СТИЛЬ СЕРИИ (обязательно!): все 5 слайдов — ОДНА среда/фон, палитра и свет (style.scene_base_en);
  scene_en каждого слайда — только вариация внутри этой среды (другой ракурс, человек, деталь), без смены места
  (нельзя: обложка у воды, второй в интерьере, четвёртый в парке). Одна вёрстка заголовков на всех слайдах
  (общий style.look), фото у всех во весь слайд, заголовок сверху. footer — либо у всех 5 слайдов, либо ни у одного.
• ВЁРСТКА. Каждый слайд собирается конструктором; выбери вид элементов (style.look — для всей серии,
  slide.look — особенности слайда). Варианты:
  head (вид заголовка): classic (заголовок + подзаголовок) | hero (одно главное слово огромно + уточнение) |
    duo (двухцветный: accent_word выделено акцентом) | weight (тонкая строка hero_rest + огромное жирное hero_word) |
    ribbon (заголовок белым на цветной ленте) | number (цифра огромно + headline мелко; берётся из badge) |
    bottom (внизу на затемнённом фото)
  align: left | center;  case: sentence | upper;  tracking: разрядка 0–15;  italic, stroke (обводка), shadow: true/false
  sub (подзаголовок): plain | ribbon (на белой ленте во всю ширину) | plate | pill (на полупрозрачной капсуле)
  photo: full (фото во весь слайд, текст поверх) | bands (заголовок, фото, пункты — полосами) | card (фото в белой рамке)
  feats (преимущества/пункты): column (колонкой с иконками) | cards (список) | grid (2×2) | scatter (вразброс)
  feat_style: filled | outline (контурные иконки в рамках);  badge: circle | star | arc (текст по дуге) | corner
  dims (размеры): pill | arrows (простые стрелки с подписью)
  По умолчанию: обложка — hero, остальные — classic; photo full у всех.
  {typo_hint}
• ОБЛОЖКА: hero_word — 1 слово крупно (тип товара), hero_rest — уточнение 2–5 слов (вместе = поисковый запрос).
  features — 2–4 преимущества: icon, title (1–2 слова), text (2–4 слова, можно пусто).
  footer — слоган внизу (до 50 символов) или пусто.
• badge (на любом слайде, обычно обложка или размеры) — ТОЛЬКО настоящая цифра/факт из данных, иначе null:
  {{"icon": "...", "value": "2500", "unit": "мл", "top": "текст по верхней дуге для arc", "bottom": "по нижней"}}.
  Для badge star можно короткую фразу в value («объём больше»), если это правда.
• tag — метка-табличка на слайде (например «РАЗМЕРЫ» на слайде 3) или пусто.
• icons (слайды problem и trust) — иконка к каждому пункту.
  Список иконок: {icons}
• Типографика style.typo: fill gradient | solid; colors — 2 цвета градиента заголовка (сверху вниз).
• cover_variants — {n_variants} дополнительных варианта обложки для A/B: заголовок тот же, другой подзаголовок
  и немного другая сцена. Если 0 — пустой список.

Ответь ТОЛЬКО JSON без пояснений:
{{
 "product_name": "короткое название товара",
 "audience": "кто покупает, 1 фраза",
 "product_en": "точное описание внешнего вида товара на английском по фото: форма, цвет, материал, детали",
 "keywords": {{"main": [], "extra": [], "synonyms": []}},
 "style": {{"name": "название стиля", "why": "1 фраза почему", "font": "Montserrat|Manrope|Golos Text|PT Serif",
           "caps": true, "ink": "#цвет текста", "accent": "#акцентный цвет", "plate": "#цвет плашек",
           "scene_base_en": "общая атмосфера серии по-английски: палитра, свет, тип фона, настроение",
           "badge_color": "#цвет бейджа",
           "typo": {{"fill": "gradient", "colors": ["#цвет1", "#цвет2"]}},
           "look": {{"align": "left", "case": "sentence", "tracking": 0, "italic": false, "stroke": false,
                     "shadow": false, "sub": "plain", "feat_style": "filled", "badge": "circle", "dims": "pill"}}}},
 "slides": [
  {{"kind": "cover", "look": {{"head": "hero", "photo": "full", "feats": "column"}}, "headline": "", "subtitle": "",
    "hero_word": "", "hero_rest": "", "accent_word": "", "features": [{{"icon": "", "title": "", "text": ""}}],
    "badge": null, "footer": "", "scene_en": ""}},
  {{"kind": "problem", "look": {{"head": "classic", "feats": "grid"}}, "headline": "", "subtitle": "",
    "bullets": ["", "", ""], "icons": ["", "", ""], "scene_en": ""}},
  {{"kind": "specs", "look": {{"head": "classic"}}, "headline": "", "subtitle": "", "bullets": ["", ""],
    "dims": [{{"value": "18 см", "axis": "x"}}], "badge": null, "tag": "", "scene_en": ""}},
  {{"kind": "usage", "look": {{"head": "classic"}}, "headline": "", "subtitle": "", "footer": "", "scene_en": ""}},
  {{"kind": "trust", "look": {{"head": "classic", "feats": "grid"}}, "headline": "",
    "bullets": ["", "", ""], "icons": ["", "", ""], "footer": "", "scene_en": ""}}
 ],
 "cover_variants": [{{"subtitle": "", "scene_en": ""}}]
}}"""

STYLE_AUTO = "СТИЛЬ: подбери сам под товар и аудиторию."
TYPO_REFS = ("ВЁРСТКУ ОПРЕДЕЛИ ПО ОБРАЗЦАМ конкурентов и повтори их приёмы как можно точнее: вид заголовка, "
             "выравнивание, регистр, разрядку, курсив, обводку, тень, подзаголовок (лента/капсула/плашка), режим фото "
             "(рамка-карточка?), вид и раскладку преимуществ (контурные/залитые, колонка/сетка/вразброс), форму бейджа "
             "(звезда, текст по дуге), слоган внизу, метку. Цвета — по образцам.")
TYPO_AUTO = "Вёрстку выбери сам — так, чтобы карточка выглядела как сильный продающий слайд маркетплейса."
STYLE_REFS = ("СТИЛЬ: продавцу нравятся слайды конкурентов (они приложены последними). Перейми их стиль — палитру, "
              "тип фона, свет, настроение, характер шрифта и композиции (font и цвета подбери максимально близко). "
              "Не копируй их товары, тексты и логотипы.{wish}")
STYLE_TEXT = "СТИЛЬ: продавец описал словами: «{text}». Подбери палитру, шрифт и атмосферу по этому описанию."
STYLE_FIXED = ("СТИЛЬ ЗАДАН, используй его: «{name}» — {why}. Атмосфера: {scene}. Шрифт {font}, цвета "
               "ink {ink}, accent {accent}, plate {plate}.")


def plan_job(p):
    inp = p.st["inputs"]
    p.set(status="planning")
    f1 = p.feed("Изучаю товар" + (" и слайды конкурентов" if p.st["refs"] and inp["style_mode"] == "refs" else ""))
    mode = inp["style_mode"]
    fixed = ST.find(inp["style_name"]) if mode == "preset" else None
    if mode == "refs" and p.st["refs"]:
        style_block = STYLE_REFS.format(wish=f" Пожелание продавца: «{inp['wish']}»." if inp["wish"] else "")
    elif mode == "text" and inp.get("style_text"):
        style_block = STYLE_TEXT.format(text=inp["style_text"])
    elif fixed:
        style_block = STYLE_FIXED.format(name=fixed["name"], why=fixed.get("why", ""), scene=fixed["scene_base_en"],
                                         font=fixed["font"], ink=fixed["ink"], accent=fixed["accent"],
                                         plate=fixed["plate"])
    else:
        style_block = STYLE_AUTO
    if inp["wish"] and mode != "refs":
        style_block += f"\nПожелание продавца: «{inp['wish']}»."
    use_refs = mode == "refs" and p.st["refs"]
    icons = ", ".join(f"{k} ({v[1]})" for k, v in render.ICONS.items())
    typo_hint = TYPO_REFS if (mode == "refs" and p.st["refs"]) else (
        "Типографика задана стилем — заполни typo как в нём." if fixed else TYPO_AUTO)
    content = [{"type": "text", "text": PLAN_TASK.format(
        product=inp["product_text"] or "(продавец не дал описание — опиши товар по фото)",
        style_block=style_block, n_variants=inp["covers"] - 1, icons=icons, typo_hint=typo_hint,
        refs_note=", затем слайды конкурентов — образцы стиля" if use_refs else "")}]
    for fn in p.st["photos"][:3]:
        content.append({"type": "image_url", "image_url": {"url": jpeg_url(p.path("photos", fn))}})
    if use_refs:
        for fn in p.st["refs"][:3]:
            content.append({"type": "text", "text": "Слайд конкурента (образец стиля):"})
            content.append({"type": "image_url", "image_url": {"url": jpeg_url(p.path("refs", fn))}})
    pm = S.profile_models(S.load(), inp["profile"])
    txt, cost = chat(p, "сценарий", content, max_tokens=6000, model=pm["script"])
    plan = parse_json(txt)
    plan = normalize_plan(plan, inp["covers"], fixed)
    p.set(plan=plan)
    p.feed(f"Сценарий готов: стиль «{plan['style'].get('name', '')}», ключей {sum(len(v) for v in plan['keywords'].values())}",
           "done", fid=f1, cost=cost)
    texts_step(p)
    p.set(status="plan_ready")
    p.feed("Проверьте ключи, сценарий и тексты — затем «Запустить генерацию»", "done")


def normalize_plan(plan, covers, fixed=None):
    by_kind = {s.get("kind"): s for s in plan.get("slides", []) if isinstance(s, dict)}
    slides = []
    for k in KINDS:
        s = by_kind.get(k, {})
        extra = {}
        if k == "cover":
            feats = [{"icon": str(f.get("icon", "")).strip() if str(f.get("icon", "")).strip() in render.ICONS else "check_circle",
                      "title": str(f.get("title", "")).strip(), "text": str(f.get("text", "")).strip()}
                     for f in (s.get("features") or []) if isinstance(f, dict) and str(f.get("title", "")).strip()][:3]
            b = s.get("badge") if isinstance(s.get("badge"), dict) else None
            badge = {"icon": b.get("icon") if b.get("icon") in render.ICONS else "",
                     "value": str(b.get("value", "")).strip(), "unit": str(b.get("unit", "")).strip(),
                     "top": str(b.get("top", "") or "").strip(), "bottom": str(b.get("bottom", "") or "").strip()} \
                if b and str(b.get("value", "")).strip() else None
            extra = {"hero_word": str(s.get("hero_word", "")).strip(), "hero_rest": str(s.get("hero_rest", "")).strip(),
                     "features": feats, "badge": badge}
        if k in ("problem", "trust"):
            extra["icons"] = [str(x) if str(x) in render.ICONS else "check_circle" for x in (s.get("icons") or [])]
        extra["look"] = render.clean_look(s.get("look") if isinstance(s.get("look"), dict) else {})
        for fld in ("footer", "tag", "accent_word"):
            extra[fld] = str(s.get(fld) or "").strip()
        if k != "cover":
            b = s.get("badge") if isinstance(s.get("badge"), dict) else None
            extra["badge"] = {"icon": b.get("icon") if b.get("icon") in render.ICONS else "",
                              "value": str(b.get("value", "")).strip(), "unit": str(b.get("unit", "")).strip(),
                              "top": str(b.get("top", "") or "").strip(), "bottom": str(b.get("bottom", "") or "").strip()} \
                if b and str(b.get("value", "")).strip() else None
        slides.append({**extra, "kind": k, "headline": str(s.get("headline", "")).strip(),
                       "subtitle": str(s.get("subtitle", "") or "").strip(),
                       "bullets": [str(b).strip() for b in (s.get("bullets") or []) if str(b).strip()],
                       "dims": [d for d in (s.get("dims") or []) if isinstance(d, dict) and str(d.get("value", "")).strip()],
                       "scene_en": str(s.get("scene_en", "")).strip()})
    # единый слоган: если он есть у большинства слайдов — у всех (недостающим — слоган обложки), иначе ни у кого
    feet = [x["footer"] for x in slides if x.get("footer")]
    if len(feet) >= 3:
        for x in slides:
            x["footer"] = x.get("footer") or feet[0]
    else:
        for x in slides:
            x["footer"] = ""
    # единая раскладка: фото во весь слайд у всех, если сценарий не задал общую иначе
    for x in slides:
        (x.get("look") or {}).pop("photo", None)
    plan["slides"] = slides
    cv = [v for v in (plan.get("cover_variants") or []) if isinstance(v, dict)][:covers - 1]
    while len(cv) < covers - 1:
        cv.append({"subtitle": slides[0]["subtitle"], "scene_en": slides[0]["scene_en"]})
    plan["cover_variants"] = cv
    kw = plan.get("keywords") or {}
    plan["keywords"] = {k: [str(x).strip() for x in (kw.get(k) or []) if str(x).strip()]
                        for k in ("main", "extra", "synonyms")}
    style = {**render.DEFAULT_STYLE, **(plan.get("style") or {})}
    if fixed:
        style.update({k: fixed[k] for k in ("name", "font", "caps", "ink", "accent", "plate", "scene_base_en", "why", "typo")
                      if k in fixed})
    ty = style.get("typo") if isinstance(style.get("typo"), dict) else {}
    cols = [c for c in (ty.get("colors") or []) if isinstance(c, str) and c.startswith("#")][:2]
    style["typo"] = {"fill": "solid" if ty.get("fill") == "solid" else "gradient",
                     "colors": cols if len(cols) == 2 else [style.get("accent", "#1f8fb4"), style.get("ink", "#12303f")]}
    lk = dict(fixed.get("look") or {}) if fixed else {}
    if ty.get("cover_layout") == "classic":
        lk.setdefault("_cover_classic", True)
    lk.update(style.get("look") if isinstance(style.get("look"), dict) else {})
    style["look"] = {k: v for k, v in render.clean_look(lk).items() if k in render.SERIES_KEYS}
    if lk.get("_cover_classic") and not (plan["slides"][0].get("look") or {}).get("head"):
        plan["slides"][0].setdefault("look", {})["head"] = "classic"
    if not str(style.get("badge_color", "")).startswith("#"):
        style["badge_color"] = style.get("accent")
    if style.get("font") not in render.FAMILIES:
        style["font"] = "Montserrat"
    plan["style"] = style
    plan.setdefault("texts", {})
    return plan


TEXTS_TASK = """Ты — SEO-копирайтер карточек Wildberries и Ozon (медицинские и реабилитационные товары).
Напиши тексты карточки для товара. Тексты для WB и Ozon должны быть РАЗНЫМИ (не копией друг друга).

ДАННЫЕ ТОВАРА:
---
{product}
---
Что обещают слайды серии (тексты должны им соответствовать и не противоречить):
{claims}

КЛЮЧЕВЫЕ ЗАПРОСЫ (утверждены продавцом):
главные: {main}
уточняющие: {extra}
синонимы: {syn}

ПРАВИЛА
WB:
• wb_title — до {wb_title_max} символов, главный запрос в начале, без бренда в начале, без кавычек, эмодзи, капса.
• wb_description — {wb_from}–{wb_to} символов, без HTML и эмодзи, абзацы через пустую строку. Главный запрос —
  в первых 200 символах. Остальные запросы вписывай естественно, без переспама (каждый 1–2 раза).
  Rich-контент на WB не индексируется, поэтому все ключи — в тексте.
Ozon:
• ozon_title — до {ozon_title_max} символов по формуле Ozon: тип товара + бренд (если есть) + модель + ключевые
  характеристики (размер, материал, назначение).
• ozon_annotation — поле «Аннотация» (описание), {oz_from}–{oz_to} символов, без HTML и эмодзи, абзацы. Другая
  структура и формулировки, чем у WB.
• ozon_hashtags — 10–20 хештегов вида #слово_слово (строчными, без пробелов) по запросам.
Общее: только факты из данных. Без медицинских обещаний («лечит», «избавит», «№1», «рекомендовано врачами»),
без упоминания конкурентов и других маркетплейсов, без контактов продавца.

Ответь ТОЛЬКО JSON:
{{"wb_title": "", "wb_description": "", "ozon_title": "", "ozon_annotation": "", "ozon_hashtags": []}}"""


def texts_step(p):
    s = S.load()
    plan = p.st["plan"]
    kw = plan["keywords"]
    claims = "\n".join(
        f"- {sl['headline']}" + (f": {sl['subtitle']}" if sl.get("subtitle") else "")
        + (" — " + "; ".join(sl["bullets"]) if sl.get("bullets") else "") for sl in plan["slides"])
    task = TEXTS_TASK.format(
        product=p.st["inputs"]["product_text"] or plan.get("product_name", ""), claims=claims,
        main=", ".join(kw["main"]) or "—", extra=", ".join(kw["extra"]) or "—", syn=", ".join(kw["synonyms"]) or "—",
        wb_title_max=s["wb_title_max"], ozon_title_max=s["ozon_title_max"],
        wb_from=min(1500, s["wb_desc_max"] // 2), wb_to=min(3500, s["wb_desc_max"]),
        oz_from=min(1500, s["ozon_desc_max"] // 2), oz_to=min(4000, s["ozon_desc_max"]))
    f = p.feed("Пишу тексты карточки для WB и Ozon")
    pm = S.profile_models(s, p.st["inputs"]["profile"])
    txt, cost = chat(p, "тексты WB/Ozon", [{"type": "text", "text": task}], max_tokens=7000, model=pm["script"])
    t = parse_json(txt)
    tags = t.get("ozon_hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    texts = {"wb_title": str(t.get("wb_title", "")).strip(), "wb_description": str(t.get("wb_description", "")).strip(),
             "ozon_title": str(t.get("ozon_title", "")).strip(),
             "ozon_annotation": str(t.get("ozon_annotation", "")).strip(),
             "ozon_hashtags": " ".join("#" + str(x).lstrip("#").strip().replace(" ", "_") for x in tags if str(x).strip())}
    with p.lock:
        p.st["plan"]["texts"] = texts
        p.save()
    p.feed("Тексты WB и Ozon готовы", "done", fid=f, cost=cost)


def rewrite_texts_job(p):
    p.set(status="writing")
    texts_step(p)
    p.set(status="plan_ready")


# =============================================================== картинки
STRICT = ("STRICT: no text, no letters, no numbers, no logos, no watermarks, no labels, no frames, no collage, "
          "no split screen — one single photograph. Photorealistic commercial product photography, "
          "professional lighting, high detail, vertical 3:4.")
PRODUCT_REF = ("PRODUCT: use the attached photos as the exact reference of the product ({desc}). Keep its shape, "
               "proportions, colors, materials, straps, seams, buckles and every detail unchanged. "
               "Do not add or remove parts, do not change the color.")


SERIES_REF = ("SERIES STYLE REFERENCE: the LAST attached image is the cover of the same product card series. Match its "
              "colour palette, lighting, colour grading and background environment closely (same place and backdrop "
              "family, same mood) so all slides look like one series. Do NOT copy its composition, people or any text. "
              "The product must stay identical to the product photos.")


def build_prompt(plan, slide, wish="", style_ref=False):
    style = plan["style"]
    kind = slide["kind"]
    base = style.get("scene_base_en", "")
    scene = f"{base}. {slide.get('scene_en', '')}"
    if kind == "specs":
        scene = (f"Product-only shot, front view, on a simple seamless backdrop in the series palette ({base}). "
                 + (slide.get("scene_en") or ""))
    zone = render.composition_en(kind, slide, style)
    parts = [PRODUCT_REF.format(desc=plan.get("product_en", "the product")),
             "SERIES LOOK (identical for every slide of the series): " + base,
             "SCENE OF THIS SLIDE: " + scene, "COMPOSITION: " + zone, STRICT]
    if style_ref:
        parts.insert(1, SERIES_REF)
    if wish:
        parts.append("CHANGES REQUESTED BY THE SELLER (most important): " + wish)
    return "\n".join(parts)


def cover_ref(p):
    """Картинка обложки этой серии — образец стиля для остальных слайдов."""
    for sl in p.st.get("slides", []):
        if sl.get("kind") == "cover" and sl.get("bg") and os.path.exists(p.path(sl["bg"])):
            return png_bytes(p.path(sl["bg"]), 1024)
    return None


def call_image(model, prompt, refs, size, quality=""):
    cli = client()
    files = [(f"product{i + 1}.png", b, "image/png") for i, b in enumerate(refs)]
    kw = {"quality": quality} if quality and model.startswith("openai/") else {}
    try:
        resp = cli.images.edit(model=model, image=files, prompt=prompt, size=size, n=1, **kw)
    except Exception as e:      # noqa: BLE001
        if kw and getattr(e, "status_code", None) in (400, 422) and "quality" in str(e).lower():
            resp = cli.images.edit(model=model, image=files, prompt=prompt, size=size, n=1)   # без «качества»
        else:
            raise
    d = resp.data[0] if getattr(resp, "data", None) else None
    if d is None:
        raise RuntimeError("модель не вернула картинку")
    if getattr(d, "b64_json", None):
        return Image.open(io.BytesIO(base64.b64decode(d.b64_json)))
    if getattr(d, "url", None):
        r = requests.get(d.url, timeout=120)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content))
    raise RuntimeError("в ответе нет картинки")


CHECK_TASK = """Ты — строгий контролёр качества инфографики. Первые изображения — фото НАСТОЯЩЕГО товара,
последнее — сгенерированная картинка для карточки. Оцени последнюю.
Ответь ТОЛЬКО JSON: {"product_match": 0-10, "has_text": true/false, "defects": "кратко по-русски, что не так (или пусто)",
 "product_box": [x0, y0, x1, y1]}
product_box: рамка, в которую целиком помещается товар на ПОСЛЕДНЕЙ картинке (вместе с ремнями, пряжками и т.п.),
в долях от ширины и высоты картинки: 0 — левый/верхний край, 1 — правый/нижний.
product_match: насколько товар на картинке совпадает с настоящим (форма, цвет, детали, пропорции); 10 — идеально.
has_text: есть ли на картинке буквы, надписи, цифры, логотипы.
defects: искажения рук/лиц, лишние детали товара, неестественность."""


def check_image(p, img, ref_paths, label):
    s = S.load()
    content = [{"type": "text", "text": CHECK_TASK}]
    for path in ref_paths[:2]:
        content.append({"type": "image_url", "image_url": {"url": jpeg_url(path, 768)}})
    content.append({"type": "image_url", "image_url": {"url": jpeg_url(img, 768)}})
    try:
        pm = S.profile_models(s, p.st["inputs"]["profile"])
        txt, cost = chat(p, f"проверка: {label}", content, max_tokens=400, model=pm["check"])
        r = parse_json(txt)
        box = r.get("product_box")
        try:
            box = [max(0.0, min(1.0, float(v))) for v in box][:4]
            if len(box) != 4 or box[2] - box[0] < 0.03 or box[3] - box[1] < 0.03:
                box = None
        except (TypeError, ValueError):
            box = None
        return int(r.get("product_match", 10)), bool(r.get("has_text")), str(r.get("defects", "")), cost, box
    except Stop:      # картинка уже оплачена — сохраняем её без проверки, остальное остановится дальше
        return 10, False, "", 0.0, None
    except Exception as e:      # noqa: BLE001
        return 10, False, f"проверка не сработала: {err_text(e)}", 0.0, None


def build_slides(plan, covers, profile_models):
    """Список слайдов серии: варианты обложки (A/B/C) + слайды 2–5."""
    out = []
    base = plan["slides"]
    letters = "ABC"
    for i in range(covers):
        sl = dict(base[0])
        if i > 0:
            v = plan["cover_variants"][i - 1]
            sl["subtitle"] = v.get("subtitle") or sl["subtitle"]
            sl["scene_en"] = v.get("scene_en") or sl["scene_en"]
        sl.update(key=f"1{letters[i]}" if covers > 1 else "1", title="Обложка" + (f" {letters[i]}" if covers > 1 else ""),
                  model=profile_models[i % len(profile_models)])
        out.append(sl)
    for n, sl in enumerate(base[1:], start=2):
        out.append(dict(sl, key=str(n), title=render.KIND_NAMES[sl["kind"]], model=profile_models[0]))
    for sl in out:
        sl.update(status="wait", attempts=0, cost=0, note="", bg=None, final=None, ver=0)
    return out


def generate_job(p, only_key=None, wish=""):
    s = S.load()
    st = p.st
    p.set(status="generating")
    if only_key is None:
        models = [resolve(m) for m in s["profiles"].get(st["inputs"]["profile"], s["profiles"]["Баланс"])]
        st["slides"] = build_slides(st["plan"], st["inputs"]["covers"], models)
        p.save()
        fstart = p.feed(f"Рисую {len(st['slides'])} слайд(ов) — профиль «{st['inputs']['profile']}»")
        keys = [x["key"] for x in st["slides"]]
    else:
        keys = [only_key]
    refs = [png_bytes(p.path("photos", fn)) for fn in st["photos"][:4]]
    ref_paths = [p.path("photos", fn) for fn in st["photos"]]
    run_series(p, keys, refs, ref_paths, wish if only_key else "")
    if only_key is None:
        p.feed(f"Нарисовано слайдов: {len(keys)}", "done", fid=fstart)
    p.finish()


def generate_missing_job(p):
    """Дорисовать слайды, которых нет (после закрытия программы, остановки по потолку или ошибки)."""
    st = p.st
    if not st.get("slides"):
        return generate_job(p)
    missing = [x["key"] for x in st["slides"] if not x.get("final")]
    p.set(status="generating")
    fstart = p.feed(f"Дорисовываю недостающие слайды: {len(missing)}")
    refs = [png_bytes(p.path("photos", fn)) for fn in st["photos"][:4]]
    ref_paths = [p.path("photos", fn) for fn in st["photos"]]
    run_series(p, missing, refs, ref_paths)
    p.feed(f"Дорисовано: {len(missing)}", "done", fid=fstart)
    p.finish()


def run_series(p, keys, refs, ref_paths, wish=""):
    """Сначала обложка (она задаёт стиль серии), затем остальные слайды — с обложкой как образцом стиля."""
    par = max(1, int(S.load()["parallel"]))
    covers = [k for k in keys if k.startswith("1")]
    rest = [k for k in keys if not k.startswith("1")]
    for group in (covers, rest):
        if not group:
            continue
        ref = cover_ref(p) if group is rest else None
        with cf.ThreadPoolExecutor(max_workers=par) as ex:
            for fu in [ex.submit(make_slide, p, k, refs, ref_paths, wish, ref) for k in group]:
                fu.result()


def slide_by_key(p, key):
    for sl in p.st["slides"]:
        if sl["key"] == key:
            return sl
    raise KeyError(key)


def make_slide(p, key, refs, ref_paths, wish="", style_ref=None):
    s = S.load()
    sl = slide_by_key(p, key)
    if style_ref and sl.get("kind") != "cover":
        refs = list(refs) + [style_ref]
    else:
        style_ref = None
    plan = p.st["plan"]
    prompt = build_prompt(plan, sl, wish, bool(style_ref))
    models = [sl["model"]] + [resolve(m) for m in s["profiles"].get(p.st["inputs"]["profile"], [])
                              if resolve(m) != sl["model"]]
    max_att = max(1, int(s["max_attempts"]))
    fid = None
    with p.lock:
        sl.update(status="wait", note="")
        p.save()
    best = None          # (score, img, model)
    last_error = ""
    tried = 0
    for att in range(max_att):
        # повтор при браке — другой нейросетью из профиля (если она есть): у них разные слабые места
        model = models[att % len(models)]
        price = S.image_price(s, model)
        p.reserve(price, f"{sl['title']}: картинка")
        with p.lock:
            sl["status"] = "work"
            p.save()
        fid = p.feed(f"{sl['title']}: рисую…" if att == 0 else f"{sl['title']}: перерисовываю ({model.split('/')[-1]})…",
                     fid=fid)
        img, error = None, ""
        try:
            img = call_image(model, prompt, refs, S.image_size(model),
                             S.profile_models(s, p.st["inputs"]["profile"])["quality"])
        except Stop:
            raise
        except Exception as e:      # noqa: BLE001
            error = err_text(e)
        finally:
            p.settle(price)
        # vsellm списывает и за неудачные попытки — учитываем всегда
        p.spend(f"{sl['title']}: картинка", model, price)
        tried += 1
        with p.lock:
            sl["cost"] = round(sl["cost"] + price, 2)
            sl["attempts"] += 1
            p.save()
        if img is None:
            last_error = f"{model.split('/')[-1]}: {error}"
            p.feed(f"{sl['title']}: ошибка {model.split('/')[-1]} — {error}", "warn", fid=fid, cost=price)
            if "VPN" in error or "HTTP 401" in error or "HTTP 402" in error or "HTTP 403" in error:
                break
            continue
        img = img.convert("RGB")
        score, has_text, defects, box = 10, False, "", None
        if s["check_enabled"]:
            with p.lock:
                sl["status"] = "check"
                p.save()
            p.feed(f"{sl['title']}: проверяю качество…", fid=fid, cost=price)
            score, has_text, defects, ccost, box = check_image(p, img, ref_paths, sl["title"])
            with p.lock:
                sl["cost"] = round(sl["cost"] + ccost, 2)
                p.save()
            p.feed(f"{sl['title']}: проверка — совпадение товара {score}/10", fid=fid, cost=ccost)
        else:
            p.feed(f"{sl['title']}: нарисовано", fid=fid, cost=price)
        quality = score - (4 if has_text else 0)
        if best is None or quality > best[0]:
            best = (quality, img, model, defects, box)
        if score >= 7 and not has_text:
            break
        if att < max_att - 1:
            why = (defects or ("на картинке есть надписи" if has_text else "товар не похож"))[:120]
            p.feed(f"{sl['title']}: брак ({why}) — перерисовываю", "warn", fid=fid)
    if best is None:
        with p.lock:
            sl.update(status="fail", note=("Ошибка — " + last_error[:160] + ". " if last_error else "")
                      + "Нажмите «Переделать».")
            p.save()
        p.feed(f"{sl['title']}: не получилось" + (f" ({last_error[:120]})" if last_error else ""), "err", fid=fid)
        return
    quality, img, model, defects, box = best
    with p.lock:
        sl["ver"] += 1
        bg = f"work/bg_{key}_v{sl['ver']}.png"
        img.save(p.path(bg))
        sl.update(bg=bg, used_model=model, box=box)
        if quality < 7:
            sl["note"] = "проверка сомневается: " + (defects or "товар может отличаться") + ". Посмотрите сами."
        p.save()
    rerender(p, key)
    p.feed(f"{sl['title']}: готово · {model.split('/')[-1]}",
           "done" if quality >= 7 else "warn", fid=fid)


def rerender(p, key):
    """Наложение текста на сохранённую картинку — бесплатно, без нейросети."""
    sl = slide_by_key(p, key)
    if not sl.get("bg"):
        return
    with Image.open(p.path(sl["bg"])) as bg:
        out = render.render(sl["kind"], bg, sl, p.st["plan"]["style"])
    with p.lock:
        name = f"work/slide_{key}_v{sl['ver']}_{int(time.time() * 1000) % 100000}.png"
        out.save(p.path(name))
        old = sl.get("final")
        sl.update(final=name, status="done")
        p.save()
    if old and old != name and os.path.exists(p.path(old)):
        try:
            os.remove(p.path(old))
        except OSError:
            pass


def update_slide_text(p, key, data):
    with p.lock:
        sl = slide_by_key(p, key)
        for k in ("headline", "subtitle"):
            if k in data:
                sl[k] = str(data[k])
        if "bullets" in data:
            sl["bullets"] = [str(b).strip() for b in data["bullets"] if str(b).strip()]
        if "dims" in data:
            sl["dims"] = [d for d in data["dims"] if str(d.get("value", "")).strip()]
        for k in ("hero_word", "hero_rest"):
            if k in data:
                sl[k] = str(data[k])
        if "features" in data:
            sl["features"] = [f for f in data["features"] if str(f.get("title", "")).strip()][:3]
        if "badge" in data:
            b = data["badge"] or {}
            sl["badge"] = b if str(b.get("value", "")).strip() else None
        if "icons" in data:
            sl["icons"] = data["icons"]
        for k in ("footer", "tag", "accent_word"):
            if k in data:
                sl[k] = str(data[k] or "").strip()
        if "look" in data:
            sl["look"] = render.clean_look(data["look"])
        p.save()
    rerender(p, key)
    export(p)


def regen_slide_job(p, key, wish):
    refs = [png_bytes(p.path("photos", fn)) for fn in p.st["photos"][:4]]
    ref_paths = [p.path("photos", fn) for fn in p.st["photos"]]
    p.set(status="generating")
    make_slide(p, key, refs, ref_paths, wish, cover_ref(p))
    export(p)
    p.set(status="done")


def update_style(p, style):
    with p.lock:
        st_ = p.st["plan"]["style"]
        st_.update({k: v for k, v in style.items() if k in ("font", "caps", "ink", "accent", "plate", "typo",
                                                            "badge_color")})
        if "look" in style:
            st_["look"] = {k: v for k, v in render.clean_look(style["look"]).items() if k in render.SERIES_KEYS}
            if "photo" in st_["look"]:      # единая раскладка для всей серии — снимаем отличия отдельных слайдов
                for x in list(p.st["plan"].get("slides", [])) + list(p.st.get("slides", [])):
                    (x.get("look") or {}).pop("photo", None)
        p.save()
    for sl in p.st.get("slides", []):
        if sl.get("bg"):
            rerender(p, sl["key"])
    export(p)


# =============================================================== выгрузка
def export(p):
    """Папка «готово»: слайды по порядку + тексты карточки."""
    out = p.path("готово")
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    for sl in p.st.get("slides", []):
        if sl.get("final") and os.path.exists(p.path(sl["final"])):
            shutil.copy(p.path(sl["final"]), os.path.join(out, f"{sl['key']}_{FILE_NAMES[sl['kind']]}.png"))
    t = (p.st.get("plan") or {}).get("texts") or {}
    kw = (p.st.get("plan") or {}).get("keywords") or {}
    if t:
        body = (f"=== WILDBERRIES ===\n\nНазвание:\n{t.get('wb_title', '')}\n\nОписание:\n{t.get('wb_description', '')}\n\n"
                f"=== OZON ===\n\nНазвание:\n{t.get('ozon_title', '')}\n\nАннотация:\n{t.get('ozon_annotation', '')}\n\n"
                f"#Хештеги:\n{t.get('ozon_hashtags', '')}\n\n"
                f"=== КЛЮЧЕВЫЕ ЗАПРОСЫ ===\n" + "\n".join(kw.get("main", []) + kw.get("extra", []) + kw.get("synonyms", [])))
        with open(os.path.join(out, "тексты.txt"), "w", encoding="utf-8-sig") as f:
            f.write(body)
    return out
