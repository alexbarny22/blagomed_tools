# -*- coding: utf-8 -*-
"""
Отрисовка слайдов 900×1200 — конструктор вёрстки.

На картинку от нейросети кодом накладываются заголовок, подзаголовок, преимущества, пункты, бейдж,
размеры, слоган. Вид каждого элемента задаётся «look» (стиль серии + настройки слайда):
  head     — вид заголовка: classic | hero | duo | weight | ribbon | number | bottom
  align    — left | center;  case — sentence | upper;  tracking — разрядка, px
  italic / stroke / shadow — наклон, обводка, тень заголовка;  fill — gradient | solid
  sub      — подзаголовок: plain | ribbon | plate | pill
  photo    — full (во весь слайд) | bands (полосами) | card (в рамке-карточке)
  feats    — преимущества / пункты: column | grid | scatter | cards
  feat_style — filled | outline;  badge — circle | star | arc | corner;  dims — pill | arrows
Все элементы обходят товар (product box от контролёра) и друг друга.
"""
import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
W, H = 900, 1200
PAD = 56

FAMILIES = {
    "Montserrat": {"file": "Montserrat.ttf", "w": {"light": "Light", "regular": "Regular", "medium": "Medium",
                                                    "bold": "Bold", "extrabold": "ExtraBold", "black": "Black"}},
    "Manrope": {"file": "Manrope.ttf", "w": {"light": "Light", "regular": "Regular", "medium": "Medium",
                                              "bold": "Bold", "extrabold": "ExtraBold", "black": "ExtraBold"}},
    "Golos Text": {"file": "GolosText.ttf", "w": {"light": "Regular", "regular": "Regular", "medium": "Medium",
                                                   "bold": "Bold", "extrabold": "ExtraBold", "black": "Black"}},
    "PT Serif": {"file": None, "static": {"regular": "PT_Serif-Web-Regular.ttf", "bold": "PT_Serif-Web-Bold.ttf"}},
}
FALLBACK_BOLD = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
FALLBACK_REG = ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
_cache = {}

DEFAULT_STYLE = {"font": "Montserrat", "ink": "#15232d", "accent": "#2a9d8f", "plate": "#ffffff", "caps": True}
HEADS = ["classic", "hero", "duo", "weight", "ribbon", "number", "bottom"]
LOOK_DEFAULTS = {
    "cover": {"head": "hero", "photo": "full", "feats": "column"},
    "problem": {"head": "classic", "photo": "bands", "feats": "cards"},
    "specs": {"head": "classic", "photo": "bands", "feats": "cards"},
    "usage": {"head": "bottom", "photo": "full", "feats": "cards"},
    "trust": {"head": "classic", "photo": "bands", "feats": "cards"},
}
LOOK_COMMON = {"align": "left", "case": "sentence", "tracking": 0, "italic": False, "stroke": False,
               "shadow": False, "fill": "gradient", "sub": "plain", "feat_style": "filled", "badge": "circle",
               "dims": "pill"}
LOOK_OPTIONS = {   # варианты и подписи для страницы
    "head": [("classic", "Классика"), ("hero", "Главное слово огромно"), ("duo", "Двухцветный (слово акцентом)"),
             ("weight", "Тонкая строка + жирное слово"), ("ribbon", "На цветной ленте"),
             ("number", "Цифра — главная"), ("bottom", "Внизу на затемнении")],
    "align": [("left", "По левому краю"), ("center", "По центру")],
    "case": [("sentence", "Обычными"), ("upper", "ЗАГЛАВНЫМИ")],
    "sub": [("plain", "Обычный с чертой"), ("ribbon", "На белой ленте"), ("plate", "На плашке"),
            ("pill", "На капсуле")],
    "photo": [("full", "Во весь слайд"), ("bands", "Полосами"), ("card", "В рамке-карточке")],
    "feats": [("column", "Колонкой с иконками"), ("cards", "Список карточками"), ("grid", "Сеткой 2×2"),
              ("scatter", "Вразброс")],
    "feat_style": [("filled", "Залитые иконки"), ("outline", "Контурные с рамкой")],
    "badge": [("circle", "Круг"), ("star", "Звезда"), ("arc", "Круг с текстом по дуге"), ("corner", "Лента-уголок")],
    "dims": [("pill", "Подписи в таблетках"), ("arrows", "Стрелки с текстом")],
}
LOOK_BOOL = ("italic", "stroke", "shadow")


def clean_look(look):
    """Оставляет только допустимые значения look."""
    out = {}
    for k, v in (look or {}).items():
        if k in LOOK_OPTIONS and v in [x[0] for x in LOOK_OPTIONS[k]]:
            out[k] = v
        elif k in LOOK_BOOL:
            out[k] = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "да", "yes")
        elif k == "tracking":
            try:
                out[k] = max(0, min(20, int(float(v))))
            except (TypeError, ValueError):
                pass
    return out


SERIES_KEYS = ("align", "case", "tracking", "italic", "stroke", "shadow", "fill", "sub", "feat_style", "badge",
               "dims")


# =================================================================== шрифты и цвета
def font(family, weight, size):
    """weight: True/False (жирный/обычный) или light|regular|medium|bold|extrabold|black."""
    w = {True: "extrabold", False: "medium"}.get(weight, weight) if isinstance(weight, bool) else (weight or "medium")
    size = max(8, int(size))
    key = (family, w, size)
    if key in _cache:
        return _cache[key]
    fam = FAMILIES.get(family) or FAMILIES["Montserrat"]
    f = None
    if fam.get("file"):
        path = os.path.join(FONT_DIR, fam["file"])
        if os.path.exists(path):
            f = ImageFont.truetype(path, size)
            try:
                f.set_variation_by_name(fam["w"].get(w, "Bold"))
            except Exception:       # noqa: BLE001
                pass
    else:
        path = os.path.join(FONT_DIR, fam["static"]["bold" if w in ("bold", "extrabold", "black") else "regular"])
        if os.path.exists(path):
            f = ImageFont.truetype(path, size)
    if f is None:
        for p in (FALLBACK_BOLD if w in ("bold", "extrabold", "black") else FALLBACK_REG):
            if os.path.exists(p):
                f = ImageFont.truetype(p, size)
                break
    f = f or ImageFont.load_default(size=size)
    _cache[key] = f
    return f


def rgb(h, default=(20, 30, 40)):
    try:
        h = str(h).lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:       # noqa: BLE001
        return default


def lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def mix(a, b, k):
    return tuple(int(a[i] * (1 - k) + b[i] * k) for i in range(3))


# =================================================================== иконки
ICONS = {
    "favorite": ("e87d", "сердце"), "check_circle": ("e86c", "галочка"), "verified": ("ef76", "проверено"),
    "shield": ("e9e0", "защита"), "water_drop": ("e798", "капля"), "straighten": ("e41c", "линейка / размер"),
    "open_in_full": ("f1ce", "регулировка размера"), "height": ("ea16", "высота"), "tune": ("e429", "настройка"),
    "schedule": ("e8b5", "время"), "directions_walk": ("e536", "ходьба / движение"), "hiking": ("e50a", "прогулка"),
    "elderly": ("f21a", "пожилой человек"), "accessibility_new": ("e92c", "человек"),
    "self_improvement": ("ea78", "спокойствие"), "healing": ("e3f3", "пластырь / уход"), "spa": ("eb4c", "мягкость"),
    "wash": ("f1b1", "стирка / мыть руки"), "dry": ("f1b3", "сушка"), "sanitizer": ("f21d", "гигиена"),
    "back_hand": ("e764", "рука / удобно держать"), "inventory_2": ("e1a1", "комплект / коробка"),
    "layers": ("e53b", "слои / материал"), "air": ("efd8", "дышит / воздух"), "eco": ("ea35", "натуральное"),
    "compress": ("e94d", "сжатие / фиксация"), "lock": ("e897", "надёжная фиксация"), "bolt": ("ea0b", "быстро"),
    "visibility_off": ("e8f5", "незаметно"), "volunteer_activism": ("ea70", "забота"), "support": ("ef73", "поддержка"),
    "thumb_up": ("e8dc", "нравится"), "star": ("e838", "звезда"), "bedtime": ("ef44", "сон / ночь"),
    "chair": ("efed", "сидеть / кресло"), "fitness_center": ("eb43", "нагрузка / прочность"),
    "local_shipping": ("e558", "доставка"), "sentiment_satisfied": ("e813", "довольный"),
    "zoom_out_map": ("e56b", "растягивается"), "iron": ("e583", "гладить"), "calendar_month": ("ebcc", "каждый день"),
    "brush": ("e3ae", "мягкость / кисть"),
}


def icon_font(size, outline=False):
    p = os.path.join(FONT_DIR, "MaterialIconsOutlined-Regular.otf" if outline else "MaterialIcons-Regular.ttf")
    if not os.path.exists(p):
        p = os.path.join(FONT_DIR, "MaterialIcons-Regular.ttf")
    return ImageFont.truetype(p, int(size)) if os.path.exists(p) else None


def draw_icon(d, name, cx, cy, size, color, outline=False):
    f = icon_font(size, outline)
    if not f:
        return False
    code = ICONS.get(name or "", ICONS["check_circle"])[0]
    d.text((cx, cy), chr(int(code, 16)), font=f, fill=color, anchor="mm")
    return True


def check_icon(d, cx, cy, r, color, ink=(255, 255, 255)):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    w = max(3, r // 4)
    d.line([(cx - r * 0.45, cy + r * 0.02), (cx - r * 0.1, cy + r * 0.38), (cx + r * 0.48, cy - r * 0.32)],
           fill=ink, width=w, joint="curve")


# =================================================================== текст с эффектами
def tlen(text, fnt, tracking=0):
    if not tracking or len(text) < 2:
        return fnt.getlength(text)
    return sum(fnt.getlength(c) for c in text) + tracking * (len(text) - 1)


def wrap(text, fnt, maxw, tracking=0):
    lines = []
    for para in str(text or "").split("\n"):
        cur = ""
        for w in para.split():
            t = (cur + " " + w).strip()
            if tlen(t, fnt, tracking) <= maxw or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
    return lines


def fit_lines(text, family, weight, maxw, maxh, start, smallest, lh=1.1, max_lines=3, tracking=0):
    size = start
    while True:
        f = font(family, weight, size)
        tr = tracking * size / 100 if tracking else 0
        lines = wrap(text, f, maxw, tr)
        ok_w = all(tlen(x, f, tr) <= maxw for x in lines)
        if (ok_w and len(lines) <= max_lines and len(lines) * size * lh <= maxh) or size <= smallest:
            return f, lines, size, tr
        size -= 2


def _mask(size, xy, text, fnt, tracking, stroke=0):
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    x, y = xy
    if not tracking:
        d.text((x, y), text, font=fnt, fill=255, stroke_width=stroke, stroke_fill=255)
        return m
    for c in text:
        d.text((x, y), c, font=fnt, fill=255, stroke_width=stroke, stroke_fill=255)
        x += fnt.getlength(c) + tracking
    return m


def _colorize(mask, fill):
    size = mask.size
    if isinstance(fill, tuple) and fill and fill[0] == "grad":
        _, c1, c2 = fill
        bb = mask.getbbox() or (0, 0, 1, 1)
        n = max(1, bb[3] - bb[1])
        g = Image.new("RGB", (1, n))
        for i in range(n):
            k = i / max(1, n - 1)
            g.putpixel((0, i), mix(c1, c2, k))
        col = Image.new("RGB", size, c1)
        col.paste(g.resize((max(1, bb[2] - bb[0]), n)), (bb[0], bb[1]))
        if bb[3] < size[1]:
            col.paste(c2, (0, bb[3], size[0], size[1]))
    else:
        col = Image.new("RGB", size, fill)
    layer = col.convert("RGBA")
    layer.putalpha(mask)
    return layer


def draw_fx(canvas, xy, text, fnt, fill, fx=None):
    """Текст с эффектами на RGBA-холст: fx = {tracking, stroke: (цвет, толщина) | None, shadow, italic}."""
    fx = fx or {}
    tr = fx.get("tracking", 0)
    size = canvas.size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    st = fx.get("stroke")
    sw = int(st[1]) if st else 0
    if fx.get("shadow"):
        sm = _mask(size, (xy[0] + 4, xy[1] + 6), text, fnt, tr, sw).filter(ImageFilter.GaussianBlur(6))
        sh = Image.new("RGBA", size, (0, 0, 0, 0))
        sh.putalpha(sm.point(lambda v: int(v * 0.45)))
        layer = Image.alpha_composite(layer, sh)
    if st:
        layer = Image.alpha_composite(layer, _colorize(_mask(size, xy, text, fnt, tr, sw), st[0]))
    layer = Image.alpha_composite(layer, _colorize(_mask(size, xy, text, fnt, tr), fill))
    if fx.get("italic"):
        s = 0.2
        yb = xy[1] + fnt.size * 0.8
        layer = layer.transform(size, Image.AFFINE, (1, s, -s * yb, 0, 1, 0), resample=Image.BICUBIC)
    canvas.alpha_composite(layer)


# =================================================================== геометрия
def overlap(a, b):
    """Доля площади прямоугольника a (x0,y0,x1,y1), занятая b."""
    if not a or not b:
        return 0.0
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0) / max(1.0, (a[2] - a[0]) * (a[3] - a[1]))


def zone(im, box):
    box = tuple(int(max(0, min(v, lim))) for v, lim in zip(box, (im.width, im.height, im.width, im.height)))
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        return 200, 0
    st = ImageStat.Stat(im.crop(box).convert("L"))
    return st.mean[0], st.stddev[0]


def rounded_mask(size, r):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=r, fill=255)
    return m


def plate(im, box, color, alpha, radius=24, border=None):
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(box, radius=radius, fill=tuple(color) + (alpha,),
                        outline=(tuple(border[0]) + (255,)) if border else None, width=border[1] if border else 0)
    im.alpha_composite(layer)


def soft_shadow(im, box, radius=36, strength=70, blur=18, dy=10):
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((box[0], box[1] + dy, box[2], box[3] + dy), radius=radius,
                                            fill=(0, 0, 0, strength))
    im.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def fade_mask(size, n, top=True, bottom=True, sides=False):
    w, h = size

    def ramp(length, a, b):
        g = Image.new("L", (1, length))
        for i in range(length):
            k = 1.0
            if a:
                k = min(k, i / n)
            if b:
                k = min(k, (length - 1 - i) / n)
            g.putpixel((0, i), int(255 * max(0.0, min(1.0, k))))
        return g
    m = ramp(h, top, bottom).resize((w, h))
    if sides:
        m = ImageChops.darker(m, ramp(w, True, True).rotate(90, expand=True).resize((w, h)))
    return m


def gradient_rect(im, box, color, a_top, a_bottom):
    x0, y0, x1, y1 = [int(v) for v in box]
    n = max(1, y1 - y0)
    g = Image.new("L", (1, n))
    for i in range(n):
        k = i / max(1, n - 1)
        g.putpixel((0, i), int(a_top + (a_bottom - a_top) * k ** 1.3))
    layer = Image.new("RGBA", (x1 - x0, n), tuple(color) + (0,))
    layer.putalpha(g.resize((x1 - x0, n)))
    im.alpha_composite(layer, (x0, y0))


# =================================================================== фото
def place_photo(src, w, h, box, bgc):
    """Кадрирует картинку под область w×h так, чтобы товар (box в долях 0..1) был целиком и по центру.
    Возвращает (картинка, рамка товара в пикселях области или None)."""
    src = src.convert("RGB")
    iw, ih = src.size
    tr = w / h
    cw, ch = (ih * tr, ih) if iw / ih > tr else (iw, iw / tr)
    cx, cy = iw / 2, ih / 2
    fits = True
    bpx = None
    if box:
        b = [max(0.0, min(1.0, float(v))) for v in box]
        bpx = (b[0] * iw, b[1] * ih, b[2] * iw, b[3] * ih)
        cx, cy = (bpx[0] + bpx[2]) / 2, (bpx[1] + bpx[3]) / 2
        pw, ph = (bpx[2] - bpx[0]) * 1.1, (bpx[3] - bpx[1]) * 1.1
        fits = pw <= cw and ph <= ch
    if fits:
        x0 = max(0, min(iw - cw, cx - cw / 2))
        y0 = max(0, min(ih - ch, cy - ch / 2))
        rw, rh = cw, ch
        img = src.crop((int(x0), int(y0), int(x0 + cw), int(y0 + ch))).resize((w, h), Image.LANCZOS)
    else:      # товар крупнее кадра — кадр вокруг товара, недостающие края — размытое продолжение
        rw = max(pw, ph * tr)
        rh = rw / tr
        x0, y0 = cx - rw / 2, cy - rh / 2
        pl, pt = int(max(0, -x0)), int(max(0, -y0))
        pr, pb = int(max(0, x0 + rw - iw)), int(max(0, y0 + rh - ih))
        ext = ImageOps.fit(src, (iw + pl + pr, ih + pt + pb), Image.LANCZOS).filter(ImageFilter.GaussianBlur(30))
        ext.paste(src, (pl, pt), fade_mask(src.size, 40, top=pt > 0, bottom=pb > 0, sides=pl > 0 or pr > 0))
        img = ext.crop((int(x0 + pl), int(y0 + pt), int(x0 + pl + rw), int(y0 + pt + rh))).resize((w, h), Image.LANCZOS)
    pbox = None
    if bpx:
        s = w / rw
        pbox = ((bpx[0] - x0) * s, (bpx[1] - y0) * s, (bpx[2] - x0) * s, (bpx[3] - y0) * s)
    return img, pbox


def fit_3x4(im):
    return place_photo(im, W, H, None, (240, 240, 240))[0]


def aspect_ok(im):
    return abs(im.width / im.height - W / H) / (W / H) < 0.1


def product_bbox(im):
    """Товар на ровном светлом фоне: всё, что заметно отличается от цвета краёв (для слайда размеров)."""
    small = im.convert("RGB").resize((180, 240))
    px = small.load()
    border = [px[x, y] for x in range(180) for y in (0, 1, 238, 239)] + \
             [px[x, y] for y in range(240) for x in (0, 1, 178, 179)]
    border.sort(key=lum)
    bg = border[len(border) // 2]
    xs, ys = [], []
    for y in range(240):
        for x in range(180):
            c = px[x, y]
            if abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]) > 60:
                xs.append(x)
                ys.append(y)
    if len(xs) < 200:
        return None
    xs.sort()
    ys.sort()
    k = len(xs) // 100
    x0, x1, y0, y1 = xs[k], xs[-1 - k], ys[k], ys[-1 - k]
    if (x1 - x0) * (y1 - y0) > 0.8 * 180 * 240:
        return None
    sx, sy = im.width / 180, im.height / 240
    return x0 * sx, y0 * sy, x1 * sx, y1 * sy


# =================================================================== look
def resolve_look(kind, slide, style):
    ty = style.get("typo") or {}
    look = dict(LOOK_COMMON)
    look.update(LOOK_DEFAULTS.get(kind, {}))
    if kind == "cover" and ty.get("cover_layout") == "classic":
        look["head"] = "classic"
    if ty.get("fill") in ("gradient", "solid"):
        look["fill"] = ty["fill"]
    for k, v in (style.get("look") or {}).items():
        if k in SERIES_KEYS:
            look[k] = v
    if kind == "cover" and ty.get("case"):
        look["case"] = ty["case"]
    for k, v in (slide.get("look") or {}).items():
        if v not in (None, ""):
            look[k] = v
    if look["head"] not in HEADS:
        look["head"] = "classic"
    return look


def colors(style):
    ty = style.get("typo") or {}
    ink = rgb(style.get("ink"), (18, 48, 63))
    accent = rgb(style.get("accent"), (31, 143, 180))
    plate_c = rgb(style.get("plate"), (255, 255, 255))
    cols = ty.get("colors") or [style.get("accent"), style.get("ink")]
    g1, g2 = rgb(cols[0], accent), rgb(cols[-1], ink)
    badge = rgb(style.get("badge_color") or style.get("accent"), accent)
    return ink, accent, plate_c, g1, g2, badge


# =================================================================== блок заголовка
def head_block(kind, look, style, slide, on_dark):
    """Готовит заголовок: возвращает (высота, функция рисования draw(im, y))."""
    fam = style.get("font", "Montserrat")
    ink, accent, plate_c, g1, g2, _ = colors(style)
    head = look["head"]
    upper = look.get("case") == "upper"
    trk = float(look.get("tracking") or 0)
    center = look.get("align") == "center"
    maxw = W - 2 * PAD
    light_txt = (255, 255, 255)
    t_ink = light_txt if on_dark else (ink if lum(ink) < 140 else (18, 30, 40))
    fill = ("grad", g1, g2) if look.get("fill") == "gradient" and not on_dark else t_ink
    stroke = ((255, 255, 255) if lum(t_ink) < 128 else (20, 30, 40), 4) if look.get("stroke") else None
    fx = {"tracking": 0, "stroke": stroke, "shadow": bool(look.get("shadow")), "italic": bool(look.get("italic"))}
    sub_c = mix(t_ink, (128, 128, 128), 0.25) if not on_dark else (235, 240, 243)
    up = (lambda s: s.upper()) if upper else (lambda s: s)
    parts = []          # (высота, рисовалка(im, y))

    def line_x(width):
        return (W - width) / 2 if center else PAD

    def text_lines(lines, f, size, tr, lh, colr, fx_):
        h = int(len(lines) * size * lh)

        def draw(im, y):
            for ln in lines:
                draw_fx(im, (line_x(tlen(ln, f, tr)), y), ln, f, colr, dict(fx_, tracking=tr))
                y += int(size * lh)
        return h, draw

    if head == "hero":
        word = up((slide.get("hero_word") or (slide.get("headline", "").split(" ") or [""])[0]).strip())
        size = 250
        while size > 80:
            fw = font(fam, "black", size)
            tr = trk * size / 100
            if tlen(word, fw, tr) <= maxw:
                break
            size -= 6
        bb = fw.getbbox(word)
        hgt = bb[3] - bb[1]

        def draw_word(im, y, word=word, fw=fw, tr=tr, bb=bb):
            draw_fx(im, (line_x(tlen(word, fw, tr)) - 2, y - bb[1]), word, fw, fill, dict(fx, tracking=tr))
        parts.append((hgt + 14, draw_word))
        rest = (slide.get("hero_rest") or "").strip()
        if rest:
            rf, rl, rs, rtr = fit_lines(up(rest) if upper else rest, fam, "extrabold", maxw, 130, 54, 32, 1.12, 2,
                                        trk / 2)
            rc = g1 if not on_dark else light_txt
            parts.append(text_lines(rl, rf, rs, rtr, 1.12, rc, dict(fx, stroke=None, shadow=False)))
    elif head == "weight":
        rest = (slide.get("hero_rest") or "").strip()
        word = up((slide.get("hero_word") or slide.get("headline", "")).strip())
        if rest:
            rf, rl, rs, rtr = fit_lines(rest, fam, "light", maxw, 120, 58, 34, 1.1, 2, trk)
            parts.append(text_lines(rl, rf, rs, rtr, 1.1, t_ink, dict(fx, stroke=None)))
        wf, wl, ws, wtr = fit_lines(word, fam, "black", maxw, 260, 150, 56, 1.0, 2, trk)
        parts.append(text_lines(wl, wf, ws, wtr, 1.02, fill, fx))
    elif head == "number":
        b = slide.get("number") or slide.get("badge") or {}
        val = f"{b.get('value', '')} {b.get('unit', '')}".strip() or slide.get("headline", "")
        nf, nl, ns, ntr = fit_lines(val, fam, "black", maxw, 230, 190, 70, 1.0, 1, trk)
        parts.append(text_lines(nl, nf, ns, ntr, 1.04, fill, fx))
        hf, hl, hs, htr = fit_lines(up(slide.get("headline", "")), fam, "extrabold", maxw, 130, 50, 32, 1.12, 2, trk / 2)
        parts.append(text_lines(hl, hf, hs, htr, 1.12, t_ink, dict(fx, stroke=None, shadow=False)))
    elif head == "ribbon":
        hf, hl, hs, htr = fit_lines(up(slide.get("headline", "")), fam, "black", maxw - 20, 220, 66, 38, 1.1, 3, trk)
        bh = int(len(hl) * hs * 1.1) + 44

        def draw_rib(im, y, hl=hl, hf=hf, hs=hs, htr=htr, bh=bh):
            d = ImageDraw.Draw(im)
            d.rectangle((0, y, W, y + bh), fill=accent)
            yy = y + 20
            for ln in hl:
                draw_fx(im, (line_x(tlen(ln, hf, htr)), yy), ln, hf, (255, 255, 255),
                        {"tracking": htr, "italic": fx["italic"], "shadow": fx["shadow"]})
                yy += int(hs * 1.1)
        parts.append((bh, draw_rib))
    else:       # classic / duo / bottom
        txt = up(slide.get("headline", ""))
        start = 150 if len(txt) <= 10 else 120 if len(txt) <= 16 else (84 if upper else 78)
        hf, hl, hs, htr = fit_lines(txt, fam, "black" if upper else "extrabold", maxw, 270, start, 40,
                                    1.08, 3, trk)
        if head == "duo":
            acc_words = {w.lower().strip(".,!?«»") for w in str(slide.get("accent_word") or "").split()} or \
                        {txt.split()[-1].lower()} if txt.split() else set()
            h = int(len(hl) * hs * 1.1)

            def draw_duo(im, y, hl=hl, hf=hf, hs=hs, htr=htr, acc_words=acc_words):
                for ln in hl:
                    x = line_x(tlen(ln, hf, htr))
                    for wd in ln.split(" "):
                        c = (g1 if not on_dark else (255, 220, 120)) if wd.lower().strip(".,!?«»") in acc_words else t_ink
                        draw_fx(im, (x, y), wd, hf, c, dict(fx, tracking=htr))
                        x += tlen(wd + " ", hf, htr)
                    y += int(hs * 1.1)
            parts.append((h, draw_duo))
        else:
            parts.append(text_lines(hl, hf, hs, htr, 1.1, fill, fx))
    # подзаголовок
    sub = (slide.get("subtitle") or "").strip() if head not in ("hero", "number") else ""
    if sub:
        mode = look.get("sub", "plain")
        sf, sl, ss, _ = fit_lines(sub, fam, "bold" if mode in ("ribbon",) else "medium", maxw - 40, 130, 38, 26, 1.22, 3)
        sh = int(len(sl) * ss * 1.22)
        if mode == "ribbon":
            bh = sh + 34

            def draw_sub(im, y, sl=sl, sf=sf, ss=ss, bh=bh):
                ImageDraw.Draw(im).rectangle((0, y, W, y + bh), fill=(255, 255, 255) if not on_dark else plate_c)
                yy = y + 16
                for ln in sl:
                    draw_fx(im, (line_x(sf.getlength(ln)), yy), ln, sf, accent if lum(accent) < 200 else ink,
                            {"italic": fx["italic"]})
                    yy += int(ss * 1.22)
            parts.append((bh + 6, draw_sub))
        elif mode in ("pill", "plate"):
            bh = sh + 28

            def draw_sub(im, y, sl=sl, sf=sf, ss=ss, bh=bh, mode=mode):
                tw = max(sf.getlength(x) for x in sl)
                if mode == "pill":
                    x0, x1 = PAD - 10, W - PAD + 10
                    plate(im, (x0, y, x1, y + bh), mix(ink, (0, 0, 0), 0.2) if lum(ink) < 128 else (40, 40, 40),
                          170, radius=bh // 2)
                    tc = (255, 255, 255)
                else:
                    x0 = line_x(tw) - 22
                    x1 = x0 + tw + 44
                    plate(im, (x0, y, x1, y + bh), plate_c, 235, radius=16)
                    tc = ink if lum(plate_c) > 128 else (255, 255, 255)
                d = ImageDraw.Draw(im)
                yy = y + 14
                for ln in sl:
                    lw = sf.getlength(ln)
                    xx = (x0 + x1 - lw) / 2 if mode == "pill" else line_x(tw)
                    d.text((xx, yy), ln, font=sf, fill=tc)
                    yy += int(ss * 1.22)
            parts.append((bh + 8, draw_sub))
        else:
            bar = 8 + 22

            def draw_sub(im, y, sl=sl, sf=sf, ss=ss):
                d = ImageDraw.Draw(im)
                if not center:
                    d.rounded_rectangle((PAD, y, PAD + 96, y + 8), radius=4, fill=accent)
                yy = y + bar
                for ln in sl:
                    d.text((line_x(sf.getlength(ln)), yy), ln, font=sf, fill=sub_c)
                    yy += int(ss * 1.22)
            parts.append((sh + bar, draw_sub))
    gap = 18
    total = sum(h for h, _ in parts) + gap * (len(parts) - 1)

    def draw_all(im, y):
        for h, fn in parts:
            fn(im, y)
            y += h + gap
    return total, draw_all


# =================================================================== преимущества / пункты
def feat_items(slide, kind):
    if kind == "cover":
        return [f for f in (slide.get("features") or []) if str(f.get("title", "")).strip()][:4]
    icons = slide.get("icons") or []
    return [{"icon": icons[i] if i < len(icons) else None, "title": b, "text": ""}
            for i, b in enumerate([b for b in (slide.get("bullets") or []) if str(b).strip()][:4])]


def feat_layout(items, look, style, region_w, kind):
    """Размеры карточек. Возвращает список (w, h, рисовалка(im, x, y)) и способ раскладки."""
    fam = style.get("font", "Montserrat")
    ink, accent, plate_c, *_ = colors(style)
    outline = look.get("feat_style") == "outline"
    mode = look.get("feats", "cards")
    if mode == "cards" or kind != "cover" and mode == "column":
        mode = "cards"
    out = []
    # общий размер шрифта для всех карточек — по самому длинному слову
    words = [w for it in items for w in str(it.get("title", "")).split()]
    longest = max(words, key=len) if words else ""
    if mode == "column":
        cw0, tile0, base = min(region_w, 400), 84, 33
    elif mode == "cards":
        cw0, tile0, base = W - 2 * PAD, 40, 33
    else:
        cw0, tile0, base = (min(region_w, W - 2 * PAD) - 18) // 2, 64, 29
    avail = cw0 - tile0 - (46 if mode in ("grid", "scatter") else 22 if mode == "column" else 82)
    common = base
    while common > 20 and font(fam, "extrabold", common).getlength(longest) > avail:
        common -= 1
    for it in items:
        title, text, icon = str(it.get("title", "")), str(it.get("text", "")), it.get("icon")
        if mode == "cards":
            cw = W - 2 * PAD
            tile = 40
            f = font(fam, "extrabold", 33)
            tl = wrap(title + (" — " + text if text else ""), f, cw - 22 - tile - 40)[:3]
            ch = max(tile, len(tl) * 39) + 36
        elif mode == "column":
            cw = min(region_w, 400)
            tile = 84
            f = font(fam, "extrabold", 33)
            f = font(fam, "extrabold", common)
            tl = wrap(title, f, cw - tile - 22)[:2]
            ch = max(tile, len(tl) * 40 + (len(wrap(text, font(fam, "regular", 29), cw - tile - 22)[:2]) * 36 if text else 0))
        else:       # grid / scatter
            cw = (min(region_w, W - 2 * PAD) - 18) // 2
            tile = 64
            fsz = 29
            longest = max(title.split(), key=len) if title.split() else ""
            while fsz > 20 and font(fam, "extrabold", fsz).getlength(longest) > cw - tile - 46:
                fsz -= 1
            f = font(fam, "extrabold", fsz)
            tl = wrap(title, f, cw - tile - 46)[:3]
            ch = max(tile + 20, len(tl) * 35 + (len(wrap(text, font(fam, "regular", 25), cw - tile - 46)[:2]) * 31 if text else 0) + 34)

        def draw(im, x, y, title=title, text=text, icon=icon, cw=cw, ch=ch, tile=tile, f=f, tl=tl, mode=mode):
            d = ImageDraw.Draw(im)
            dark_plate = lum(plate_c) < 128
            if mode == "column":
                mean, std = zone(im, (x, y, x + cw, y + ch))
                fink = (255, 255, 255) if mean < 120 else (ink if lum(ink) < 128 else (18, 30, 40))
                if outline:
                    plate(im, (x - 12, y - 12, x + cw + 12, y + ch + 12), plate_c, 230, 18,
                          border=(ink if not dark_plate else (255, 255, 255), 2))
                    fink = ink if not dark_plate else (255, 255, 255)
                elif std > 42:
                    plate(im, (x - 14, y - 14, x + cw + 14, y + ch + 14), plate_c, 205, 18)
                    fink = ink if not dark_plate else (255, 255, 255)
                d = ImageDraw.Draw(im)
                if outline:
                    draw_icon(d, icon, x + tile // 2, y + tile // 2, 60, fink, outline=True)
                else:
                    d.rounded_rectangle((x, y, x + tile, y + tile), radius=20, fill=accent)
                    draw_icon(d, icon, x + tile // 2, y + tile // 2, 54, (255, 255, 255))
                ty = y + 2
                for ln in tl:
                    d.text((x + tile + 22, ty), ln, font=f, fill=fink)
                    ty += 40
                if text:
                    fs = font(fam, "regular", 29)
                    for ln in wrap(text, fs, cw - tile - 22)[:2]:
                        d.text((x + tile + 22, ty), ln, font=fs, fill=mix(fink, (128, 128, 128), 0.3))
                        ty += 36
                return
            card = plate_c if not dark_plate else mix(plate_c, (255, 255, 255), 0.1)
            cink = ink if lum(card) > 128 else (255, 255, 255)
            if lum(cink) > 128 and lum(card) > 128:
                cink = (18, 30, 40)
            plate(im, (x, y, x + cw, y + ch), card, 238, 20 if mode != "cards" else 22,
                  border=(cink, 2) if outline else None)
            d = ImageDraw.Draw(im)
            if mode == "cards":
                cx, cy, r = x + 22 + 20, y + ch // 2, 20
                if icon and icon in ICONS and icon_font(10):
                    if outline:
                        draw_icon(d, icon, cx, cy, 40, cink, outline=True)
                    else:
                        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=accent)
                        draw_icon(d, icon, cx, cy, 25, (255, 255, 255))
                else:
                    check_icon(d, cx, cy, r, accent)
                ty = y + (ch - len(tl) * 39) // 2 - 2
                for ln in tl:
                    d.text((x + 22 + 40 + 20, ty), ln, font=f, fill=cink)
                    ty += 39
                return
            ix, iy = x + 18 + tile // 2, y + ch // 2
            if outline:
                draw_icon(d, icon, ix, iy, 58, cink, outline=True)
            else:
                d.rounded_rectangle((ix - tile // 2, iy - tile // 2, ix + tile // 2, iy + tile // 2), radius=16, fill=accent)
                draw_icon(d, icon, ix, iy, 42, (255, 255, 255))
            fs = font(fam, "regular", min(25, f.size - 3))
            sl = wrap(text, fs, cw - tile - 46)[:2] if text else []
            th = len(tl) * 35 + len(sl) * 31
            ty = y + (ch - th) // 2 - 2
            for ln in tl:
                d.text((x + tile + 32, ty), ln, font=f, fill=cink)
                ty += 35
            for ln in sl:
                d.text((x + tile + 32, ty), ln, font=fs, fill=mix(cink, (128, 128, 128), 0.35))
                ty += 31
        out.append((cw, ch, draw))
    return out, mode


def feat_block_size(cards, mode, gap=14):
    if not cards:
        return 0, 0
    if mode in ("cards", "column"):
        return max(c[0] for c in cards), sum(c[1] for c in cards) + gap * (len(cards) - 1) + (30 if mode == "column" else 0) * (len(cards) - 1)
    rows = grid_rows(cards, mode)
    hh = sum(max(c[1] for c in r) for r in rows) + gap * (len(rows) - 1)
    ww = max(sum(c[0] for c in r) + 18 * (len(r) - 1) for r in rows)
    return ww, hh


def grid_rows(cards, mode):
    if mode == "scatter" and len(cards) >= 3:
        if len(cards) == 3:
            return [cards[:1], cards[1:3]]
        return [cards[:1], cards[1:3], cards[3:]]
    return [cards[i:i + 2] for i in range(0, len(cards), 2)]


def draw_feats(im, cards, mode, x0, y0, width, gap=14):
    if mode in ("cards", "column"):
        y = y0
        for cw, ch, fn in cards:
            fn(im, x0, y)
            y += ch + gap + (30 if mode == "column" else 0)
        return
    y = y0
    for i, row in enumerate(grid_rows(cards, mode)):
        rw = sum(c[0] for c in row) + 18 * (len(row) - 1)
        if mode == "scatter" and len(row) == 1:
            x = x0 + (width - rw) * (0.75 if i % 2 == 0 else 0.25)
        else:
            x = x0 + (width - rw) / 2
        for cw, ch, fn in row:
            fn(im, int(x), y)
            x += cw + 18
        y += max(c[1] for c in row) + gap


# =================================================================== бейдж
def badge_size(look):
    return {"circle": 118, "star": 122, "arc": 112, "corner": 0}.get(look.get("badge", "circle"), 118)


def arc_text(im, text, cx, cy, radius, fnt, color, top=True):
    """Текст по дуге окружности (сверху — по часовой, снизу — против)."""
    if not text:
        return
    widths = [fnt.getlength(c) for c in text]
    total = sum(widths) + 4 * (len(text) - 1)
    ang_total = total / radius
    a = -math.pi / 2 - ang_total / 2 if top else math.pi / 2 + ang_total / 2
    for c, w in zip(text, widths):
        mid = a + (w / 2) / radius * (1 if top else -1)
        x, y = cx + radius * math.cos(mid), cy + radius * math.sin(mid)
        g = Image.new("RGBA", (int(fnt.size * 1.6), int(fnt.size * 1.6)), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((g.width / 2, g.height / 2), c, font=fnt, fill=color, anchor="mm")
        rot = -math.degrees(mid + math.pi / 2) if top else -math.degrees(mid - math.pi / 2)
        g = g.rotate(rot, resample=Image.BICUBIC, expand=False)
        im.alpha_composite(g, (int(x - g.width / 2), int(y - g.height / 2)))
        a += (w + 4) / radius * (1 if top else -1)


def draw_badge(im, look, style, badge, cx, cy):
    fam = style.get("font", "Montserrat")
    ink, accent, plate_c, g1, g2, bcol = colors(style)
    kind = look.get("badge", "circle")
    val, unit = str(badge.get("value", "")).strip(), str(badge.get("unit", "")).strip()
    d = ImageDraw.Draw(im)
    white = (255, 255, 255)
    if kind == "star":
        r1, r2, n = 122, 104, 22
        pts = []
        for i in range(n * 2):
            rr = r1 if i % 2 == 0 else r2
            a = math.pi * i / n
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        soft = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(soft).polygon([(x + 4, y + 8) for x, y in pts], fill=(0, 0, 0, 70))
        im.alpha_composite(soft.filter(ImageFilter.GaussianBlur(8)))
        d = ImageDraw.Draw(im)
        d.polygon(pts, fill=bcol)
        txt = (val + (" " + unit if unit else "")).strip()
        f, lines, s, _ = fit_lines(txt, fam, "extrabold", 170, 150, 46, 22, 1.08, 3)
        y = cy - len(lines) * s * 1.08 / 2
        for ln in lines:
            d.text((cx, y + s * 0.54), ln, font=f, fill=white, anchor="mm")
            y += s * 1.08
    elif kind == "arc":
        r = 112
        soft_shadow(im, (cx - r, cy - r, cx + r, cy + r), radius=r, strength=60, blur=10, dy=6)
        d = ImageDraw.Draw(im)
        ring = ink if lum(ink) < 128 else (40, 40, 40)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=white, outline=ring, width=5)
        top = str(badge.get("top") or "").upper()
        bottom = str(badge.get("bottom") or unit).upper()
        af = font(fam, "extrabold", 26)
        arc_text(im, top, cx, cy, r - 30, af, ring, top=True)
        arc_text(im, bottom, cx, cy, r - 30, af, ring, top=False)
        vs = 88
        while vs > 30 and font(fam, "black", vs).getlength(val) > r * 1.05:
            vs -= 4
        ImageDraw.Draw(im).text((cx, cy), val, font=font(fam, "black", vs), fill=ring, anchor="mm")
    elif kind == "corner":
        txt = (val + (" " + unit if unit else "")).strip().upper()
        f = font(fam, "black", 34)
        tw = f.getlength(txt) + 120
        band = Image.new("RGBA", (int(tw), 64), bcol + (255,))
        ImageDraw.Draw(band).text((band.width / 2, 32), txt, font=f, fill=white, anchor="mm")
        band = band.rotate(-45 if cx > W / 2 else 45, expand=True, resample=Image.BICUBIC)
        im.alpha_composite(band, (int(cx - band.width / 2), int(cy - band.height / 2)))
    else:       # circle
        r = 112
        d.ellipse((cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 8), fill=mix(bcol, (0, 0, 0), 0.25))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=bcol)
        has_icon = bool(badge.get("icon")) and draw_icon(d, badge.get("icon"), cx, cy - 58, 46, white)
        vs = 66
        while vs > 28 and font(fam, "black", vs).getlength(val) > r * 1.7:
            vs -= 4
        y0 = cy + (8 if has_icon else -10)
        d.text((cx, y0), val, font=font(fam, "black", vs), fill=white, anchor="mm")
        if unit:
            d.text((cx, y0 + vs * 0.72), unit, font=font(fam, "bold", 32), fill=white, anchor="mm")


# =================================================================== размеры
def arrow(d, a, b, color, width=4, head=16, both=True):
    d.line([a, b], fill=color, width=width)
    ends = ((a, b), (b, a)) if both else ((b, a),)
    for p, q in ends:
        ang = math.atan2(q[1] - p[1], q[0] - p[0])
        for s in (0.5, -0.5):
            d.line([p, (p[0] + head * math.cos(ang + s), p[1] + head * math.sin(ang + s))], fill=color, width=width)


def draw_dims(im, style, look, dims, bb, area_bottom):
    dims = [x for x in (dims or []) if str(x.get("value", "")).strip()]
    if not dims or not bb:
        return
    fam = style.get("font", "Montserrat")
    ink, accent, *_ = colors(style)
    color = ink if lum(ink) < 160 else (21, 35, 45)
    x0, y0, x1, y1 = [int(v) for v in bb]
    pills = look.get("dims", "pill") != "arrows"
    horiz = [x for x in dims if x.get("axis", "x") == "x"][:1]
    vert = [x for x in dims if x.get("axis") == "y"][:1]
    d = ImageDraw.Draw(im)
    f = font(fam, "bold" if pills else "extrabold", 32)

    def label(text, cx, cy, rotate=False):
        if pills:
            tw = f.getlength(text)
            ImageDraw.Draw(im).rounded_rectangle((cx - tw / 2 - 16, cy - 25, cx + tw / 2 + 16, cy + 25), radius=25,
                                                 fill=accent)
            ImageDraw.Draw(im).text((cx, cy), text, font=f, fill=(255, 255, 255), anchor="mm")
            return
        g = Image.new("RGBA", (int(f.getlength(text) + 20), 50), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((g.width / 2, 25), text, font=f, fill=color, anchor="mm")
        if rotate:
            g = g.rotate(90, expand=True, resample=Image.BICUBIC)
        im.alpha_composite(g, (int(cx - g.width / 2), int(cy - g.height / 2)))
    if horiz:
        y = int(min(y1 + 40, area_bottom - 40))
        if pills:
            for xx in (x0, x1):
                d.line([(xx, y - 18), (xx, y + 18)], fill=color, width=3)
        arrow(d, (x0 + 4, y), (x1 - 4, y), color)
        label(horiz[0]["value"], (x0 + x1) / 2, y + (42 if pills else 36))
    if vert:
        x = min(x1 + 44, im.width - PAD - 10)
        if x - x1 < 24:
            x = max(x0 - 44, PAD + 10)
        if pills:
            for yy in (y0, y1):
                d.line([(x - 18, yy), (x + 18, yy)], fill=color, width=3)
        arrow(d, (x, y0 + 4), (x, y1 - 4), color)
        lx = x + (70 if x > x1 else -70) if pills else x + (34 if x > x1 else -34)
        lx = max(PAD + 40, min(im.width - PAD - 30, lx))
        label(vert[0]["value"], lx, (y0 + y1) / 2, rotate=not pills)


# =================================================================== композиция для нейросети
def composition_en(kind, slide, style):
    look = resolve_look(kind, slide, style)
    if kind == "specs":
        return ("Plain seamless very light studio background, no props. The product alone, front view, centered, "
                "fully visible, occupying about 55% of the frame with generous empty space on all sides "
                "(dimension lines will be drawn around it).")
    if look["photo"] in ("bands", "card"):
        return ("The product (and the situation) centered and fully visible, nothing cut off, with some breathing "
                "room around it — the picture will be cropped to a band.")
    parts = []
    if look["head"] == "bottom":
        parts.append("Keep the bottom 30% of the frame darker and calm (a caption goes there); the product is clearly "
                     "visible in the upper and middle part.")
    else:
        big = look["head"] in ("hero", "weight", "number")
        parts.append(f"Keep the upper {34 if big else 30}% of the frame calm and empty (soft plain background) "
                     "for a headline.")
    has_feats = kind == "cover" and any(str(f.get("title", "")).strip() for f in slide.get("features") or [])
    if has_feats:
        side = "right" if look.get("feats") in ("column", None) else "left"
        other = "left" if side == "right" else "right"
        parts.append(f"Place the whole product in the {side} half and lower two thirds of the frame; keep the {other} "
                     f"40% of the frame below the headline calm and uncluttered (feature cards go there).")
    elif look["head"] != "bottom":
        parts.append("The whole product sits in the lower two thirds, fully visible.")
    return " ".join(parts)


ZONES_EN = {k: "" for k in LOOK_DEFAULTS}      # совместимость со старым кодом
ZONE_HERO_EN = ""
KIND_NAMES = {"cover": "Обложка", "problem": "Проблема и решение", "specs": "Характеристики и размеры",
              "usage": "Как использовать", "trust": "Комплектация и уход"}


# =================================================================== главная функция
def render(kind, bg, slide, style):
    """bg — картинка нейросети; slide — тексты слайда (+ box — где товар, доли 0..1, + look); style — стиль серии."""
    style = {**DEFAULT_STYLE, **(style or {})}
    look = resolve_look(kind, slide, style)
    ink, accent, plate_c, g1, g2, bcol = colors(style)
    box = slide.get("box")
    footer = str(slide.get("footer") or "").strip()
    foot_h = 104 if footer else 0
    items = feat_items(slide, kind)
    has_badge = bool(str((slide.get("badge") or {}).get("value", "")).strip()) and look["head"] != "number"
    photo_mode = look["photo"]
    if kind == "specs":
        photo_mode = "card" if photo_mode == "card" else "bands"
    band_bg = mix(plate_c, accent, 0.08)

    # --- 1. решаем раскладку
    head_at_bottom = look["head"] == "bottom"
    if photo_mode == "full":
        full, pbox = place_photo(bg, W, H, box, band_bg)
        tmp = full.convert("RGBA")
        top_mean, _ = zone(tmp, (0, 0, W, 420)) if not head_at_bottom else zone(tmp, (0, H - 420, W, H))
        on_dark = top_mean < 125 or head_at_bottom
        hh, hdraw = head_block(kind, look, style, slide, on_dark)
        if not head_at_bottom:
            head_y0, head_y1 = 44, 44 + hh
            clash = pbox and pbox[1] < head_y1 + 10 and overlap((PAD, head_y0, W - PAD, head_y1), pbox) > 0.06
        else:
            head_y1 = H - foot_h - 52
            head_y0 = head_y1 - hh
            clash = pbox and pbox[3] > head_y0 + 30 and overlap((PAD, head_y0, W - PAD, head_y1), pbox) > 0.15
        if clash:
            photo_mode = "bands"
    if photo_mode in ("bands", "card"):
        on_dark = lum(band_bg) < 128 if photo_mode == "bands" else lum(band_bg) < 128
        hh, hdraw = head_block(kind, look, style, slide, on_dark)

    # --- 2. пункты / преимущества (для полос и рамки — своя нижняя полоса)
    region_w = W - 2 * PAD
    if photo_mode == "full" and look.get("feats") in ("grid", "scatter") and box:
        region_w = int(W * 0.64)
    cards, fmode = feat_layout(items, look, style, region_w, kind) if items else ([], None)
    if items and photo_mode in ("bands", "card") and fmode == "column":
        look = dict(look, feats="grid")
        cards, fmode = feat_layout(items, look, style, W - 2 * PAD, kind)
    fw, fh = feat_block_size(cards, fmode) if cards else (0, 0)

    # --- 3. фон и фото
    if photo_mode == "full":
        im = full.convert("RGBA")
        if head_at_bottom:
            gradient_rect(im, (0, int(head_y0 - 160), W, H), (8, 14, 18), 0, 225)
    else:
        top_h = 0 if head_at_bottom else hh + 44 + 40
        bot_h = (fh + 34 + 44 if cards else 0) + foot_h + ((hh + 44 + 40) if head_at_bottom else 0)
        if H - top_h - bot_h < 430 and cards:      # мало места под фото — пункты компактнее
            look2 = dict(look, feats="grid") if fmode == "cards" and len(cards) >= 3 else look
            cards, fmode = feat_layout(items, look2, style, W - 2 * PAD, kind)
            fw, fh = feat_block_size(cards, fmode)
            bot_h = (fh + 34 + 44) + foot_h + ((hh + 84) if head_at_bottom else 0)
        p_top, p_bot = top_h, H - bot_h
        im = Image.new("RGBA", (W, H), band_bg + (255,))
        if photo_mode == "card":
            ImageDraw.Draw(im).rectangle((0, 0, W, H), fill=mix(plate_c, accent, 0.18))
            gradient_rect(im, (0, 0, W, H), mix(plate_c, accent, 0.55), 0, 255)
            cx0, cy0, cx1, cy1 = PAD - 14, p_top + 6, W - PAD + 14, p_bot - 10
            soft_shadow(im, (cx0, cy0, cx1, cy1), radius=40)
            ImageDraw.Draw(im).rounded_rectangle((cx0, cy0, cx1, cy1), radius=40, fill=(255, 255, 255))
            iw_, ih_ = cx1 - cx0 - 36, cy1 - cy0 - 36
            ph_img, pb = place_photo(bg, iw_, ih_, box, (255, 255, 255))
            im.paste(ph_img, (cx0 + 18, cy0 + 18), rounded_mask((iw_, ih_), 28))
            ox, oy, pw_, phh = cx0 + 18, cy0 + 18, iw_, ih_
        else:
            ph_img, pb = place_photo(bg, W, p_bot - p_top, box, band_bg)
            im.paste(ph_img, (0, p_top), fade_mask(ph_img.size, 46, top=p_top > 0, bottom=p_bot < H))
            ox, oy, pw_, phh = 0, p_top, W, p_bot - p_top
        pbox = (pb[0] + ox, pb[1] + oy, pb[2] + ox, pb[3] + oy) if pb else None
        if kind == "specs":
            area = im.crop((ox, oy, ox + pw_, oy + phh)).convert("RGB")
            bb = product_bbox(area)
            bb = (bb[0] + ox, bb[1] + oy, bb[2] + ox, bb[3] + oy) if bb else pbox
            draw_dims(im, style, look, slide.get("dims"), bb, oy + phh)
            pbox = bb or pbox
        head_y0 = 44 if not head_at_bottom else p_bot + 44
        if head_at_bottom and cards:
            head_y0 = p_bot + 44

    # --- 4. заголовок
    hdraw(im, head_y0)
    occupied = [(0, head_y0 - 10, W, head_y0 + hh + 10)]
    if pbox:
        occupied.append(pbox)

    # --- 5. преимущества / пункты
    if cards:
        if photo_mode == "full":
            cands = []
            col_w = fw
            top_free = (head_y0 + hh + 60) if not head_at_bottom else 60
            bottom_lim = (H - foot_h - 40) if not head_at_bottom else (head_y0 - 40)
            xs = [PAD, W - PAD - col_w] if fmode in ("column",) else (
                [W - PAD - region_w, PAD] if region_w < W - 2 * PAD else [PAD])
            for x in xs:
                for y in (top_free, max(top_free, int(H * 0.42)), bottom_lim - fh):
                    if y >= top_free - 1 and y + fh <= bottom_lim + 1:
                        cands.append((x, int(y)))
            if not cands:
                cands = [(PAD, max(top_free, bottom_lim - fh))]
            area_w = col_w if fmode == "column" else region_w

            def bad(p):
                r = (p[0] - 14, p[1] - 14, p[0] + area_w + 14, p[1] + fh + 14)
                return overlap(r, pbox) if pbox else 0.0
            fx_, fy_ = min(cands, key=bad)
            draw_feats(im, cards, fmode, fx_, fy_, area_w)
            occupied.append((fx_ - 14, fy_ - 14, fx_ + area_w + 14, fy_ + fh + 14))
        else:
            fy_ = H - foot_h - 44 - fh
            draw_feats(im, cards, fmode, PAD, fy_, W - 2 * PAD)
            occupied.append((PAD, fy_, W - PAD, fy_ + fh))

    # --- 6. бейдж
    if has_badge:
        r = badge_size(look)
        if look.get("badge") == "corner":
            spot = (W - 110, 110) if look.get("align") != "center" else (W - 110, H - foot_h - 110)
            draw_badge(im, look, style, slide["badge"], *spot)
        else:
            hb = head_y0 + hh
            spots = [(W - PAD - r + 12, hb + r + 24), (W - PAD - r + 12, H - foot_h - r - 40),
                     (PAD + r - 12, H - foot_h - r - 40), (PAD + r - 12, hb + r + 24),
                     (W - PAD - r + 12, int(H * 0.55)), (W // 2, H - foot_h - r - 40)]

            def badness(p):
                rr = (p[0] - r, p[1] - r, p[0] + r, p[1] + r)
                if rr[1] < 0 or rr[3] > H - foot_h:
                    return 9.0
                return sum(overlap(rr, o) * (0.6 if o is pbox else 1.0) for o in occupied)
            cx, cy = min(spots, key=badness)
            draw_badge(im, look, style, slide["badge"], cx, cy)

    # --- 7. метка (например «РАЗМЕРЫ»)
    tag = str(slide.get("tag") or "").strip()
    if tag:
        fam = style.get("font", "Montserrat")
        f = font(fam, "black", 38)
        tw = f.getlength(tag.upper())
        bx1, by1 = W - PAD, H - foot_h - 40 - (fh + 20 if cards and photo_mode != "full" else 0)
        bx0, by0 = bx1 - tw - 70, by1 - 86
        soft_shadow(im, (bx0, by0, bx1, by1), radius=22, strength=50, blur=10, dy=5)
        ImageDraw.Draw(im).rounded_rectangle((bx0, by0, bx1, by1), radius=22, fill=(255, 255, 255))
        draw_fx(im, (bx0 + 35, by0 + 20), tag.upper(), f, ink if lum(ink) < 128 else (18, 30, 40),
                {"italic": bool(look.get("italic"))})

    # --- 8. слоган внизу
    if footer:
        fam = style.get("font", "Montserrat")
        fc = ink if lum(ink) < 110 else mix(accent, (0, 0, 0), 0.35)
        ImageDraw.Draw(im).rectangle((0, H - foot_h, W, H), fill=fc)
        f, lines, s, _ = fit_lines(footer, fam, "extrabold", W - 2 * PAD + 20, foot_h - 24, 36, 22, 1.12, 2)
        y = H - foot_h + (foot_h - len(lines) * s * 1.12) / 2
        d = ImageDraw.Draw(im)
        for ln in lines:
            d.text((W / 2, y + s * 0.56), ln, font=f, fill=(255, 255, 255), anchor="mm")
            y += s * 1.12
    return im.convert("RGB")
