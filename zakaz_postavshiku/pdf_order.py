# -*- coding: utf-8 -*-
"""Печатная форма «Заказ поставщику» в PDF — по данным документа из 1С (OData)."""
import datetime as dt
import os
import re
import shutil

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

EMPTY = "00000000-0000-0000-0000-000000000000"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря"]

# ----------------------------------------------------------------------------- шрифт с кириллицей
_FONT = None


def font():
    global _FONT
    if _FONT:
        return _FONT
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    pairs = [(os.path.join(win, "arial.ttf"), os.path.join(win, "arialbd.ttf")),
             (os.path.join(win, "calibri.ttf"), os.path.join(win, "calibrib.ttf")),
             (os.path.join(win, "tahoma.ttf"), os.path.join(win, "tahomabd.ttf")),
             ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")]
    for reg, bold in pairs:
        if os.path.exists(reg):
            pdfmetrics.registerFont(TTFont("F", reg))
            pdfmetrics.registerFont(TTFont("FB", bold if os.path.exists(bold) else reg))
            _FONT = ("F", "FB")
            return _FONT
    raise RuntimeError("Не найден шрифт с кириллицей (Arial) в папке Windows\\Fonts")


# ----------------------------------------------------------------------------- числа
def money(v):
    s = f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    return s


def qty_fmt(v):
    v = float(v)
    return f"{v:g}".replace(".", ",")


_UNITS = [["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"],
          ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
          "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUND = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def _plural(n, forms):
    n = abs(n) % 100
    if 10 < n < 20:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


def _triad(n, fem):
    w = [_HUND[n // 100]]
    t = n % 100
    if 10 <= t < 20:
        w.append(_TEENS[t - 10])
    else:
        w += [_TENS[t // 10], _UNITS[1 if fem else 0][t % 10]]
    return [x for x in w if x]


def words(amount):
    """509480.5 -> 'Пятьсот девять тысяч четыреста восемьдесят рублей 50 копеек'"""
    rub = int(round(amount * 100)) // 100
    kop = int(round(amount * 100)) % 100
    if rub == 0:
        parts = ["ноль"]
    else:
        parts = []
        groups = [(10 ** 9, False, ("миллиард", "миллиарда", "миллиардов")),
                  (10 ** 6, False, ("миллион", "миллиона", "миллионов")),
                  (10 ** 3, True, ("тысяча", "тысячи", "тысяч"))]
        n = rub
        for base, fem, forms in groups:
            g = n // base
            if g:
                parts += _triad(g, fem) + [_plural(g, forms)]
            n %= base
        parts += _triad(n, False)
    s = " ".join(parts) + " " + _plural(rub, ("рубль", "рубля", "рублей")) + \
        f" {kop:02d} " + _plural(kop, ("копейка", "копейки", "копеек"))
    return s[0].upper() + s[1:]


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip()


# ----------------------------------------------------------------------------- данные из 1С
class Lookup:
    """Кэш чтения справочников по ссылке; при ошибке доступа — пусто."""

    def __init__(self, od):
        self.od = od
        self.cache = {}

    def get(self, entity, key, select=None):
        if not key or key == EMPTY:
            return {}
        ck = (entity, key)
        if ck not in self.cache:
            try:
                params = {"$select": select} if select else {}
                self.cache[ck] = self.od.get(f"{entity}(guid'{key}')", **params) or {}
            except Exception:
                try:  # без $select — вдруг какого-то поля нет
                    self.cache[ck] = self.od.get(f"{entity}(guid'{key}')") or {}
                except Exception:
                    self.cache[ck] = {}
        return self.cache[ck]


def party(rec):
    name = (rec.get("НаименованиеПолное") or rec.get("Description") or "").strip()
    bits = [name]
    if rec.get("ИНН"):
        bits.append(f"ИНН {rec['ИНН']}")
    if rec.get("КПП"):
        bits.append(f"КПП {rec['КПП']}")
    return ", ".join(b for b in bits if b)


def make_pdf(od, doc_key, out_dir, copy_dir=""):
    """Читает документ из 1С и сохраняет PDF. Возвращает путь к файлу."""
    F, FB = font()
    d = od.get(f"Document_ЗаказПоставщику(guid'{doc_key}')")
    lk = Lookup(od)
    number = str(d.get("Number", "")).strip()
    date = dt.datetime.strptime(d["Date"][:19], "%Y-%m-%dT%H:%M:%S")

    org = lk.get("Catalog_Организации", d.get("Организация_Key"))
    ka = lk.get("Catalog_Контрагенты", d.get("Контрагент_Key"))
    sklad = lk.get("Catalog_Склады", d.get("Склад_Key"), "Description")
    resp = lk.get("Catalog_Пользователи", d.get("Ответственный_Key"), "Description")

    rows, has_art = [], False
    for ln in sorted(d.get("Товары", []) or [], key=lambda x: int(x.get("LineNumber", 0))):
        if ln.get("Отменено"):
            continue
        it = lk.get("Catalog_Номенклатура", ln.get("Номенклатура_Key"))
        unit = ""
        pk = ln.get("Упаковка_Key")
        if pk and pk != EMPTY:
            unit = lk.get("Catalog_УпаковкиЕдиницыИзмерения", pk, "Description").get("Description", "")
            q = ln.get("КоличествоУпаковок", ln.get("Количество", 0))
        else:
            q = ln.get("Количество", 0)
            unit = lk.get("Catalog_УпаковкиЕдиницыИзмерения", it.get("ЕдиницаИзмерения_Key"),
                          "Description").get("Description", "")
        art = (it.get("Артикул") or "").strip()
        has_art = has_art or bool(art)
        rows.append(dict(name=it.get("Description", "?"), art=art, qty=q, unit=unit or "шт",
                         price=ln.get("Цена", 0), sum=ln.get("СуммаСНДС") or ln.get("Сумма", 0),
                         nds=ln.get("СуммаНДС", 0)))
    total = sum(float(r["sum"]) for r in rows)
    nds = sum(float(r["nds"]) for r in rows)

    receipt = ""
    for k in ("ДатаПоступления", "ЖелаемаяДатаПоступления"):
        v = d.get(k)
        if v and not v.startswith("0001"):
            receipt = dt.datetime.strptime(v[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
            break

    # ---- оформление
    st = ParagraphStyle("n", fontName=F, fontSize=9, leading=11)
    stb = ParagraphStyle("b", parent=st, fontName=FB)
    title = ParagraphStyle("t", fontName=FB, fontSize=14, leading=18)
    small = ParagraphStyle("s", parent=st, fontSize=8, leading=10)

    os.makedirs(out_dir, exist_ok=True)
    fname = safe_name(f"Заказ поставщику {number} от {date:%d.%m.%Y}.pdf")
    path = os.path.join(out_dir, fname)
    pdf = SimpleDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"Заказ поставщику № {number}", author=org.get("Description", ""))
    W = A4[0] - 27 * mm
    el = [Paragraph(f"Заказ поставщику № {number} от {date.day} {MONTHS[date.month - 1]} {date.year} г.", title),
          Spacer(1, 2 * mm)]
    line = Table([[""]], colWidths=[W], rowHeights=[1])
    line.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, -1), 1.5, colors.black)]))
    el += [line, Spacer(1, 2 * mm)]

    head = [["Поставщик:", Paragraph(party(ka) or "—", stb)],
            ["Заказчик:", Paragraph(party(org) or "—", stb)]]
    if sklad.get("Description"):
        head.append(["Склад:", Paragraph(sklad["Description"], st)])
    if receipt:
        head.append(["Дата поступления:", Paragraph(receipt, st)])
    th = Table(head, colWidths=[36 * mm, W - 36 * mm])
    th.setStyle(TableStyle([("FONT", (0, 0), (0, -1), F, 9), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    el += [th, Spacer(1, 4 * mm)]

    if has_art:
        hdr = ["№", "Артикул", "Товар", "Кол-во", "Ед.", "Цена", "Сумма"]
        cw = [8 * mm, 24 * mm, None, 17 * mm, 11 * mm, 20 * mm, 24 * mm]
    else:
        hdr = ["№", "Товар", "Кол-во", "Ед.", "Цена", "Сумма"]
        cw = [8 * mm, None, 17 * mm, 11 * mm, 20 * mm, 24 * mm]
    cw[cw.index(None)] = W - sum(c for c in cw if c)
    data = [[Paragraph(h, stb) for h in hdr]]
    for i, r in enumerate(rows, 1):
        row = [str(i)] + ([Paragraph(r["art"], small)] if has_art else []) + \
              [Paragraph(r["name"], st), qty_fmt(r["qty"]), r["unit"], money(r["price"]), money(r["sum"])]
        data.append(row)
    t = Table(data, colWidths=cw, repeatRows=1)
    n = len(hdr)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), F, 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BOX", (0, 0), (-1, -1), 1.2, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("ALIGN", (n - 4, 1), (n - 4, -1), "RIGHT"),
        ("ALIGN", (n - 3, 1), (n - 3, -1), "CENTER"),
        ("ALIGN", (n - 2, 1), (-1, -1), "RIGHT"),
    ]))
    el += [t, Spacer(1, 2 * mm)]

    tot = [["Итого:", money(total)],
           ["В том числе НДС:" if nds else "Без налога (НДС)", money(nds) if nds else "—"],
           ["Всего к оплате:", money(total)]]
    tt = Table(tot, colWidths=[W - 30 * mm, 30 * mm])
    tt.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), FB, 9), ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1)]))
    el += [tt, Spacer(1, 3 * mm),
           Paragraph(f"Всего наименований {len(rows)}, на сумму {money(total)} руб.", st),
           Paragraph(words(total), stb), Spacer(1, 3 * mm)]
    if d.get("Комментарий") and "бот" not in d["Комментарий"].lower():
        el += [Paragraph("Комментарий: " + d["Комментарий"], st), Spacer(1, 3 * mm)]
    el += [line, Spacer(1, 8 * mm)]
    who = (resp.get("Description") or "").strip()
    sig = Table([["Заказчик", "", f"/ {who} /" if who else "/                         /"]],
                colWidths=[25 * mm, 55 * mm, 70 * mm])
    sig.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), F, 9), ("LINEBELOW", (1, 0), (1, 0), 0.5, colors.black),
                             ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    el.append(sig)
    pdf.build(el)

    if copy_dir:
        try:
            os.makedirs(copy_dir, exist_ok=True)
            shutil.copy2(path, os.path.join(copy_dir, fname))
        except Exception as e:
            raise RuntimeError(f"PDF сохранён в {path}, но не скопирован в {copy_dir}: {e}")
    return path


def find_order(od, number):
    """Ищет заказ поставщику по номеру (можно без нулей/префикса: 31 -> 00ЦБ-000031). Возвращает Ref_Key."""
    number = number.strip()
    rows = od.get("Document_ЗаказПоставщику", **{"$filter": f"Number eq '{number}' and DeletionMark eq false",
                                                   "$select": "Ref_Key,Number,Date"})
    if not rows and number.isdigit():
        since = (dt.date.today() - dt.timedelta(days=400)).strftime("%Y-%m-%dT00:00:00")
        allr = od.get("Document_ЗаказПоставщику", **{"$filter": f"Date ge datetime'{since}' and DeletionMark eq false",
                                                       "$select": "Ref_Key,Number,Date"})
        rows = [r for r in allr if re.sub(r"\D", "", r["Number"]).lstrip("0") == number.lstrip("0")]
    if not rows:
        raise RuntimeError(f"Заказ поставщику № {number} не найден")
    rows.sort(key=lambda r: r["Date"], reverse=True)
    return rows[0]["Ref_Key"]
