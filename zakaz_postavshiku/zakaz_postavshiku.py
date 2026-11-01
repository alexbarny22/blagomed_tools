# -*- coding: utf-8 -*-
"""
Заказ поставщику из Excel-отчёта  ->  1С:УТ 11.5 через OData (публикация ut12)

Берёт из файла колонки «Номенклатура» и «Заказ» (и «Склад», если есть),
находит товары в 1С, подставляет цены поставщика, создаёт документ
«Заказ поставщику» на контрагента из config.ini и проводит его.
"""
import configparser
import datetime as dt
import difflib
import os
import re
import sys
import threading
import traceback
import urllib.parse



def _ensure(*mods):
    """Если библиотеки нет — бот сам ставит её через pip и продолжает работу."""
    import importlib
    import subprocess
    pip_names = {"reportlab": "reportlab", "openpyxl": "openpyxl", "requests": "requests", "maxapi": "maxapi"}
    for m in mods:
        try:
            importlib.import_module(m)
        except ImportError:
            print(f"Устанавливаю библиотеку {m}…")
            subprocess.call([sys.executable, "-m", "ensurepip", "--upgrade"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rc = subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                                  pip_names.get(m, m)])
            if rc != 0:
                subprocess.call([sys.executable, "-m", "pip", "install", "--user",
                                 "--disable-pip-version-check", pip_names.get(m, m)])
            importlib.invalidate_caches()
            import site
            try:
                sys.path.append(site.getusersitepackages())
            except Exception:
                pass
            importlib.import_module(m)


_ensure("requests", "openpyxl", "reportlab")
import openpyxl  # noqa: E402
import requests  # noqa: E402

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:  # консольный режим без GUI
    tk = None

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
EMPTY = "00000000-0000-0000-0000-000000000000"
DOC = "Document_ЗаказПоставщику"


# ----------------------------------------------------------------------------- настройки
def load_config():
    cfg = configparser.ConfigParser()
    path = os.path.join(APP_DIR, "config.ini")
    if not os.path.exists(path):
        raise RuntimeError(f"Не найден файл настроек {path}")
    cfg.read(path, encoding="utf-8-sig")
    return cfg


# ----------------------------------------------------------------------------- OData
class ODataError(Exception):
    pass


def q(s):
    """Экранирование строки для $filter."""
    return str(s).replace("'", "''")


class OData:
    def __init__(self, base_url, user, password, timeout=120):
        self.base = base_url.rstrip("/") + "/odata/standard.odata/"
        self.s = requests.Session()
        self.s.auth = (user.encode("utf-8"), password.encode("utf-8"))
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"})
        self.timeout = timeout

    def _url(self, path, params=None):
        url = self.base + urllib.parse.quote(path, safe="/()'=,$:")
        p = {"$format": "json"}
        if params:
            p.update(params)
        qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe=chr(39) + '(),:$')}" for k, v in p.items())
        return url + "?" + qs

    @staticmethod
    def _err(r):
        try:
            j = r.json()
            msg = j.get("odata.error", {}).get("message", {}).get("value") or str(j)
        except Exception:
            msg = r.text[:800]
        if r.status_code == 401:
            msg = "Нет доступа (401): проверьте пользователя и пароль 1С в config.ini. " + msg
        return ODataError(f"HTTP {r.status_code}: {msg}")

    def get(self, path, **params):
        r = self.s.get(self._url(path, params), timeout=self.timeout)
        if r.status_code != 200:
            raise self._err(r)
        j = r.json()
        return j["value"] if isinstance(j, dict) and "value" in j else j

    def post(self, path, data=None, **params):
        r = self.s.post(self._url(path, params), json=data, timeout=self.timeout)
        if r.status_code not in (200, 201, 204):
            raise self._err(r)
        return r.json() if r.content else {}

    def post_document(self, key):
        return self.post(f"{DOC}(guid'{key}')/Post()", PostingModeOperational="false")

    def find_one(self, entity, description, extra=""):
        flt = f"Description eq '{q(description)}' and DeletionMark eq false" + extra
        rows = self.get(entity, **{"$filter": flt})
        return rows[0] if rows else None


# ----------------------------------------------------------------------------- Excel
def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower().replace("ё", "е")


def to_num(v):
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return 0


def read_excel(path):
    """Возвращает список {name, qty, sklad} — только строки с заказом > 0."""
    wb = openpyxl.load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        hdr_row = col_name = col_qty = col_sklad = None
        for r in range(1, min(ws.max_row, 50) + 1):
            vals = {norm(ws.cell(r, c).value): c for c in range(1, ws.max_column + 1) if ws.cell(r, c).value}
            if "номенклатура" in vals and "заказ" in vals:
                hdr_row, col_name, col_qty = r, vals["номенклатура"], vals["заказ"]
                col_sklad = vals.get("склад")
                break
        if not hdr_row:
            continue
        items = {}
        for r in range(hdr_row + 1, ws.max_row + 1):
            name = ws.cell(r, col_name).value
            if not name or norm(name) in ("итого", "итог"):
                continue
            qty = to_num(ws.cell(r, col_qty).value)
            if qty <= 0:
                continue
            sklad = str(ws.cell(r, col_sklad).value or "").strip() if col_sklad else ""
            key = (norm(name), sklad)
            if key in items:
                items[key]["qty"] += qty
            else:
                items[key] = {"name": str(name).strip(), "qty": qty, "sklad": sklad, "row": r}
        return list(items.values())
    raise RuntimeError("В файле не найдена строка с заголовками «Номенклатура» и «Заказ»")


# ----------------------------------------------------------------------------- логика 1С
HEADER_DROP = {"Ref_Key", "DataVersion", "Number", "Date", "Posted", "DeletionMark",
               "Комментарий", "СуммаДокумента", "Автор_Key", "Согласован"}
HEADER_DROP_PARTS = ("Основание", "Закрыт", "ЗаказКлиента", "ПоДаннымПоставщика", "ДатаСогласования")
LINE_DROP_PARTS = ("Заказ", "Основание", "Отмен", "Скидк", "Причина", "КодСтроки", "LineNumber",
                   "Сертификат", "СуммаЗалог")
LINE_DROP_IF_OTHER_ITEM = ("Характеристика", "Упаковка", "Назначение", "Серия")


def add_months(d, n):
    import calendar
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def yes(v):
    return str(v).strip().lower() not in ("нет", "no", "false", "0", "н", "-")


def odt(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def day(d):
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_odt(s):
    try:
        return dt.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


class OrderBuilder:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        c = cfg["1C"]
        self.od = OData(c["url"], c["user"], c.get("password", ""), int(c.get("timeout", "120")))
        self.ctx = None

    # --- поиск контрагента, шаблона и справочников
    def prepare(self):
        o = self.cfg["ЗАКАЗ"]
        od = self.od
        self.log("Подключаюсь к 1С и ищу поставщика…")
        search = o.get("поставщик", "ГАВРЮШКИН").strip()
        ka = None
        for variant in dict.fromkeys([search, search.upper(), search.title()]):
            rows = od.get("Catalog_Контрагенты", **{
                "$filter": f"substringof('{q(variant)}', Description) and DeletionMark eq false",
                "$select": "Ref_Key,Description,Партнер_Key"})
            if rows:
                ka = rows[0]
                if len(rows) > 1:
                    self.log("  Найдено несколько контрагентов, беру первый: " + "; ".join(r["Description"] for r in rows))
                break
        if not ka:
            raise RuntimeError(f"Контрагент «{search}» не найден в 1С")
        self.log(f"  Поставщик: {ka['Description']}")

        # последние заказы этого поставщика — шаблон шапки и история цен
        orders = []
        try:
            orders = od.get(DOC, **{"$filter": f"Контрагент_Key eq guid'{ka['Ref_Key']}' and DeletionMark eq false",
                                    "$orderby": "Date desc", "$top": "30"})
        except ODataError as e:
            if "ЗаказПоставщику" in str(e) and ("404" in str(e) or "не найден" in str(e).lower()):
                raise RuntimeError("Документ «Заказ поставщику» не включён в состав OData.\n"
                                   "См. README, раздел «Если пишет: не включён в состав OData».")
            self.log(f"  Не удалось отсортировать заказы на сервере ({e}), пробую без сортировки…")
            orders = od.get(DOC, **{"$filter": f"Контрагент_Key eq guid'{ka['Ref_Key']}' and DeletionMark eq false"})
            orders.sort(key=lambda d: d.get("Date", ""), reverse=True)
            orders = orders[:30]
        template = orders[0] if orders else None
        if template:
            self.log(f"  Шаблон шапки: заказ № {template.get('Number', '').strip()} от {template.get('Date', '')[:10]}")
        else:
            self.log("  Прошлых заказов поставщику нет — заполню шапку по настройкам и соглашению")

        history = {}  # номенклатура -> (дата, строка)
        for d in orders:
            for ln in d.get("Товары", []) or []:
                k = ln.get("Номенклатура_Key")
                if k and k not in history and to_num(ln.get("Цена")) > 0:
                    history[k] = (d.get("Date", ""), ln)

        refs = {}
        for key, entity in (("организация", "Catalog_Организации"), ("соглашение", "Catalog_СоглашенияСПоставщиками"),
                            ("договор", "Catalog_ДоговорыКонтрагентов"), ("склад", "Catalog_Склады")):
            name = o.get(key, "").strip()
            if not name:
                continue
            try:
                row = od.find_one(entity, name)
            except ODataError as e:
                self.log(f"  {entity}: {e}")
                row = None
            if row:
                refs[key] = row
            else:
                self.log(f"  ВНИМАНИЕ: «{name}» не найдено в {entity} — возьму из прошлого заказа")

        # виды цен поставщика (для регистра цен)
        price_kinds = set()
        if ka.get("Партнер_Key") and ka["Партнер_Key"] != EMPTY:
            try:
                price_kinds = {r["Ref_Key"] for r in od.get("Catalog_ВидыЦенПоставщиков", **{
                    "$filter": f"Owner_Key eq guid'{ka['Партнер_Key']}'", "$select": "Ref_Key"})}
            except ODataError:
                pass
        if refs.get("соглашение") and refs["соглашение"].get("ВидЦеныПоставщика_Key") not in (None, EMPTY):
            price_kinds.add(refs["соглашение"]["ВидЦеныПоставщика_Key"])

        self.ctx = dict(ka=ka, template=template, history=history, refs=refs, price_kinds=price_kinds,
                        sklad_cache={}, price_reg_ok=True)
        return self.ctx

    def _sklad(self, name):
        c = self.ctx["sklad_cache"]
        if name not in c:
            try:
                c[name] = self.od.find_one("Catalog_Склады", name)
            except ODataError:
                c[name] = None
        return c[name]

    def _find_item(self, name):
        rows = self.od.get("Catalog_Номенклатура", **{
            "$filter": f"Description eq '{q(name)}' and IsFolder eq false",
            "$select": "Ref_Key,Description,DeletionMark"})
        live = [r for r in rows if not r.get("DeletionMark")]
        if live:
            return live[0], "точно" if len(live) == 1 else f"точно (дублей в 1С: {len(live)})"
        return None, None

    def _all_items(self):
        if "all_items" not in self.ctx:
            self.log("  Загружаю справочник номенклатуры для поиска похожих наименований…")
            rows = self.od.get("Catalog_Номенклатура", **{
                "$filter": "IsFolder eq false and DeletionMark eq false", "$select": "Ref_Key,Description"})
            self.ctx["all_items"] = rows
        return self.ctx["all_items"]

    def _price(self, item_key):
        """Цена: регистр цен поставщика -> последний заказ поставщику."""
        if self.ctx["price_reg_ok"]:
            try:
                rows = self.od.get("InformationRegister_ЦеныНоменклатурыПоставщиков/SliceLast()", **{
                    "$filter": f"Номенклатура_Key eq guid'{item_key}'"})
                kinds = self.ctx["price_kinds"]
                rows = [r for r in rows if to_num(r.get("Цена")) > 0 and
                        (not kinds or r.get("ВидЦеныПоставщика_Key") in kinds or
                         r.get("Партнер_Key") == self.ctx["ka"].get("Партнер_Key"))]
                if rows:
                    return to_num(rows[0]["Цена"]), "цена поставщика"
            except ODataError as e:
                self.ctx["price_reg_ok"] = False
                self.log(f"  Регистр цен поставщиков недоступен по OData ({str(e)[:120]}) — беру цены из прошлых заказов")
        h = self.ctx["history"].get(item_key)
        if h:
            return to_num(h[1].get("Цена")), f"из заказа от {h[0][:10]}"
        return 0.0, "нет цены"

    def match(self, items):
        """Сопоставление строк файла с 1С. Дополняет items полями key, name1c, how, price, price_src."""
        for it in items:
            it.update(key=None, name1c="", how="не найдено", price=0.0, price_src="")
            row, how = self._find_item(it["name"])
            if not row:
                cands = self._all_items()
                names = {norm(r["Description"]): r for r in cands}
                if norm(it["name"]) in names:
                    row, how = names[norm(it["name"])], "точно (пробелы/регистр)"
                else:
                    best = difflib.get_close_matches(norm(it["name"]), list(names), n=1, cutoff=0.9)
                    if best:
                        row, how = names[best[0]], "ПОХОЖЕЕ — проверьте"
            if row:
                it["key"], it["name1c"], it["how"] = row["Ref_Key"], row["Description"], how
                it["price"], it["price_src"] = self._price(row["Ref_Key"])
            self.log(f"  {it['name'][:60]:<60} -> {it['how']}; цена {it['price']:.2f} ({it['price_src']})")
        return items

    # --- сборка документа
    def build(self, items, doc_date, sklad_name=None):
        o = self.cfg["ЗАКАЗ"]
        ctx = self.ctx
        ka, tpl, refs = ctx["ka"], ctx["template"], ctx["refs"]
        doc = {}
        tpl_date = parse_odt(tpl["Date"]) if tpl else None
        if tpl:
            for k, v in tpl.items():
                if k in HEADER_DROP or "@" in k or isinstance(v, list):
                    continue
                if any(p in k for p in HEADER_DROP_PARTS):
                    continue
                doc[k] = v
            # сдвигаем даты шапки (желаемая дата поступления и т.п.)
            for k, v in list(doc.items()):
                if "Дата" in k and isinstance(v, str):
                    d = parse_odt(v)
                    if d and d.year > 1:
                        doc[k] = odt(day(max(doc_date, d + (doc_date - tpl_date)) if tpl_date else doc_date))
        else:
            sog = refs.get("соглашение") or {}
            for k in ("Валюта_Key", "ЦенаВключаетНДС", "НалогообложениеНДС", "Организация_Key", "Склад_Key",
                      "ФормаОплаты", "ПорядокРасчетов"):
                if k in sog:
                    doc[k] = sog[k]
            doc.setdefault("ХозяйственнаяОперация", "ЗакупкаУПоставщика")
            doc["ЖелаемаяДатаПоступления"] = odt(doc_date)

        doc["Date"] = odt(doc_date)
        doc["Партнер_Key"] = ka.get("Партнер_Key") or doc.get("Партнер_Key", EMPTY)
        doc["Контрагент_Key"] = ka["Ref_Key"]
        if refs.get("организация"):
            doc["Организация_Key"] = refs["организация"]["Ref_Key"]
        if refs.get("соглашение"):
            doc["Соглашение_Key"] = refs["соглашение"]["Ref_Key"]
        if refs.get("договор"):
            doc["Договор_Key"] = refs["договор"]["Ref_Key"]
        sklad = self._sklad(sklad_name) if sklad_name else None
        if sklad:
            doc["Склад_Key"] = sklad["Ref_Key"]
        elif refs.get("склад"):
            doc["Склад_Key"] = refs["склад"]["Ref_Key"]
        doc["Статус"] = o.get("статус", "Согласован").strip() or "Согласован"
        doc["Комментарий"] = o.get("комментарий", "Создан из файла {file}").replace("{file}", self.cfg.get("_run", "file", fallback=""))

        price_incl_vat = bool(doc.get("ЦенаВключаетНДС"))
        # дата поступления: пустая дата 1С (0001-01-01) не годится — регистр «Товары к поступлению» её не принимает
        def valid(v):
            d = parse_odt(v) if isinstance(v, str) else None
            return v if d and d.year > 1 else None
        try:
            months = int(o.get("срок_поступления_мес", "1").strip() or 1)
        except ValueError:
            months = 1
        receipt_date = odt(add_months(day(doc_date), months))
        doc["ЖелаемаяДатаПоступления"] = receipt_date
        if "ДатаПоступления" in doc:
            doc["ДатаПоступления"] = receipt_date
        default_pattern = (tpl.get("Товары") or [None])[0] if tpl else None

        lines, total = [], 0.0
        for n, it in enumerate([i for i in items if i.get("key")], 1):
            h = ctx["history"].get(it["key"])
            pattern = h[1] if h else default_pattern
            same = bool(h)
            ln = {}
            if pattern:
                for k, v in pattern.items():
                    if "@" in k or any(p in k for p in LINE_DROP_PARTS):
                        continue
                    if not same and any(p in k for p in LINE_DROP_IF_OTHER_ITEM):
                        continue
                    ln[k] = v
            # ставка НДС из образца
            rate = 0.0
            if pattern:
                ps, pn = to_num(pattern.get("Сумма")), to_num(pattern.get("СуммаНДС"))
                base = (ps - pn) if price_incl_vat else ps
                rate = round(pn / base, 4) if base else 0.0
            qty = it["qty"]
            price = round(float(it["price"]), 2)
            summa = round(qty * price, 2)
            nds = round(summa * rate / (1 + rate), 2) if price_incl_vat else round(summa * rate, 2)
            ln.update({
                "LineNumber": str(n), "КодСтроки": n,
                "Номенклатура_Key": it["key"],
                "Количество": qty, "КоличествоУпаковок": qty,
                "Цена": price, "Сумма": summa, "СуммаНДС": nds,
                "СуммаСНДС": summa if price_incl_vat else round(summa + nds, 2),
                "ДатаПоступления": receipt_date,
            })
            if not same:
                ln["Характеристика_Key"] = EMPTY
                ln["Упаковка_Key"] = EMPTY
            if "Склад_Key" in ln or doc.get("Склад_Key"):
                ln["Склад_Key"] = doc.get("Склад_Key", EMPTY)
            ln["Отменено"] = False
            lines.append(ln)
            total += ln["СуммаСНДС"]
        doc["Товары"] = lines
        doc["СуммаДокумента"] = round(total, 2)

        # этапы оплаты — по образцу прошлого заказа, суммы пересчитаны
        stages = (tpl or {}).get("ЭтапыГрафикаОплаты") or []
        if stages:
            new, left = [], round(total, 2)
            for i, st in enumerate(stages, 1):
                s = {k: v for k, v in st.items() if "@" not in k}
                s["LineNumber"] = str(i)
                pct = to_num(st.get("ПроцентПлатежа")) or (100.0 if len(stages) == 1 else 0)
                amt = left if i == len(stages) else round(total * pct / 100, 2)
                left = round(left - amt, 2)
                s["СуммаПлатежа"] = amt
                for k in list(s):
                    if "Дата" in k and isinstance(s[k], str):
                        d = parse_odt(s[k])
                        if d and d.year > 1 and tpl_date:
                            s[k] = odt(day(max(doc_date, d + (doc_date - tpl_date))))
                        elif k == "ДатаПлатежа":
                            s[k] = odt(day(doc_date))
                for k in list(s):
                    if "Зачет" in k or "Зачёт" in k or "СуммаЗалог" in k:
                        s[k] = 0 if isinstance(s[k], (int, float)) else s[k]
                new.append(s)
            doc["ЭтапыГрафикаОплаты"] = new
        return doc

    def save_pdf(self, key, open_file=True):
        """PDF печатной формы в папку бота (и копия, например, на Яндекс Диск)."""
        import pdf_order
        p = self.cfg["PDF"] if self.cfg.has_section("PDF") else {}
        out = (p.get("папка", "") or "Заказы").strip()
        if not os.path.isabs(out):
            out = os.path.join(APP_DIR, out)
        copy_dir = (p.get("копия", "") or "").strip()
        path = pdf_order.make_pdf(self.od, key, out, copy_dir)
        self.log(f"  PDF: {path}" + (f"\n  Копия: {copy_dir}" if copy_dir else ""))
        if open_file and yes(p.get("открывать", "да")) and hasattr(os, "startfile"):
            os.startfile(path)
        return path

    def create_and_post(self, items, doc_date, post=True):
        groups = {}
        for it in items:
            if it.get("key"):
                groups.setdefault(it.get("sklad") or "", []).append(it)
        results = []
        for sklad_name, grp in groups.items():
            doc = self.build(grp, doc_date, sklad_name or None)
            self.log(f"Создаю заказ поставщику{(' (склад ' + sklad_name + ')') if sklad_name else ''}: "
                     f"{len(grp)} строк, сумма {doc['СуммаДокумента']:.2f}")
            created = self.od.post(DOC, doc)
            key, num = created.get("Ref_Key"), str(created.get("Number", "")).strip()
            self.log(f"  Записан: № {num}")
            posted, err = False, ""
            if post:
                try:
                    self.od.post_document(key)
                    posted = True
                    self.log(f"  Проведён: № {num}")
                except ODataError as e:
                    err = str(e)
                    self.log(f"  НЕ ПРОВЕДЁН: {err}\n  Документ остался записанным — откройте его в 1С и проведите вручную.")
            pdf = ""
            try:
                pdf = self.save_pdf(key)
            except Exception as e:
                self.log(f"  PDF не создан: {e}")
            results.append(dict(number=num, key=key, posted=posted, error=err, sum=doc["СуммаДокумента"],
                                sklad=sklad_name, pdf=pdf))
        return results


# ----------------------------------------------------------------------------- окно
class App:
    def __init__(self, root):
        self.root = root
        root.title("Заказ поставщику из Excel → 1С")
        root.geometry("1150x700")
        self.items = []
        self.builder = None
        self.file = ""

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="1. Выбрать файл…", command=self.pick).pack(side="left")
        self.lbl_file = ttk.Label(top, text="файл не выбран", width=50)
        self.lbl_file.pack(side="left", padx=8)
        ttk.Label(top, text="Дата заказа:").pack(side="left")
        self.date_var = tk.StringVar(value=dt.date.today().strftime("%d.%m.%Y"))
        ttk.Entry(top, textvariable=self.date_var, width=11).pack(side="left", padx=4)
        self.btn_check = ttk.Button(top, text="2. Проверить в 1С", command=self.check, state="disabled")
        self.btn_check.pack(side="left", padx=6)
        self.btn_go = ttk.Button(top, text="3. Создать и провести", command=self.go, state="disabled")
        self.btn_go.pack(side="left", padx=6)

        bar2 = ttk.Frame(root, padding=(8, 0, 8, 6))
        bar2.pack(fill="x")
        ttk.Label(bar2, text="PDF уже созданного заказа, №:").pack(side="left")
        self.num_var = tk.StringVar()
        ent = ttk.Entry(bar2, textvariable=self.num_var, width=16)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda _: self.pdf_by_number())
        ttk.Button(bar2, text="Сохранить PDF", command=self.pdf_by_number).pack(side="left", padx=4)
        ttk.Button(bar2, text="Открыть папку с PDF", command=self.open_pdf_dir).pack(side="left", padx=12)
        ttk.Button(bar2, text="Отправить поставщику в МАКС", command=self.send_max).pack(side="left", padx=4)
        self.last_pdf = ""
        self.last_number = ""

        cols = ("n", "name", "name1c", "how", "qty", "price", "sum", "src")
        heads = ("№", "Номенклатура из файла", "Номенклатура в 1С", "Найдено", "Кол-во", "Цена", "Сумма", "Откуда цена")
        widths = (35, 300, 300, 140, 60, 80, 90, 120)
        mid = ttk.Frame(root, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=16)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("name", "name1c", "how", "src") else "e")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.tag_configure("bad", background="#ffd6d6")
        self.tree.tag_configure("warn", background="#fff2c2")
        self.tree.bind("<Double-1>", self.edit_cell)

        self.lbl_total = ttk.Label(root, text="Двойной щелчок по цене или количеству — изменить", padding=(8, 2))
        self.lbl_total.pack(fill="x")
        self.logbox = tk.Text(root, height=11, font=("Consolas", 9))
        self.logbox.pack(fill="both", padx=8, pady=(0, 8))

    def log(self, msg):
        self.root.after(0, lambda: (self.logbox.insert("end", msg + "\n"), self.logbox.see("end")))

    def busy(self, on):
        st = "disabled" if on else "normal"
        self.btn_check.configure(state=st if self.items else "disabled")
        self.btn_go.configure(state=st if (self.items and self.builder and self.builder.ctx) else "disabled")

    def pick(self):
        p = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx *.xlsm"), ("Все файлы", "*.*")])
        if not p:
            return
        try:
            self.items = read_excel(p)
        except Exception as e:
            messagebox.showerror("Ошибка файла", str(e))
            return
        self.file = p
        self.builder = None
        self.lbl_file.configure(text=os.path.basename(p))
        self.log(f"Файл: {p}\nСтрок с заказом: {len(self.items)}, всего штук: {sum(i['qty'] for i in self.items):g}")
        for i in self.items:
            i.update(key=None, name1c="", how="", price=0.0, price_src="")
        self.refresh()
        self.busy(False)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        total = 0
        for n, it in enumerate(self.items, 1):
            s = round(it["qty"] * it.get("price", 0), 2)
            if it.get("key"):
                total += s
            tag = ()
            if it.get("how") == "не найдено":
                tag = ("bad",)
            elif it.get("key") and ("ПОХОЖ" in it["how"] or it.get("price", 0) <= 0):
                tag = ("warn",)
            self.tree.insert("", "end", iid=str(n - 1), tags=tag, values=(
                n, it["name"], it.get("name1c", ""), it.get("how", ""), f"{it['qty']:g}",
                f"{it.get('price', 0):.2f}", f"{s:.2f}", it.get("price_src", "")))
        found = sum(1 for i in self.items if i.get("key"))
        self.lbl_total.configure(text=f"Строк: {len(self.items)}, найдено в 1С: {found}, сумма: {total:,.2f} ₽"
                                      "   |   Двойной щелчок по цене или количеству — изменить".replace(",", " "))

    def edit_cell(self, ev):
        row, col = self.tree.identify_row(ev.y), self.tree.identify_column(ev.x)
        if not row or col not in ("#5", "#6"):
            return
        x, y, w, h = self.tree.bbox(row, col)
        it = self.items[int(row)]
        field = "qty" if col == "#5" else "price"
        e = ttk.Entry(self.tree)
        e.insert(0, f"{it[field]:g}")
        e.place(x=x, y=y, width=w, height=h)
        e.focus_set()
        e.select_range(0, "end")

        def done(_=None):
            v = to_num(e.get())
            e.destroy()
            if v >= 0:
                it[field] = v
                if field == "price":
                    it["price_src"] = "вручную"
                self.refresh()
        e.bind("<Return>", done)
        e.bind("<FocusOut>", done)
        e.bind("<Escape>", lambda _: e.destroy())

    def pdf_dir(self):
        try:
            cfg = load_config()
            out = (cfg.get("PDF", "папка", fallback="") or "Заказы").strip()
        except Exception:
            out = "Заказы"
        return out if os.path.isabs(out) else os.path.join(APP_DIR, out)

    def open_pdf_dir(self):
        d = self.pdf_dir()
        os.makedirs(d, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(d)

    def pdf_by_number(self):
        num = self.num_var.get().strip()
        if not num:
            messagebox.showinfo("PDF", "Введите номер заказа, например 00ЦБ-000031 или просто 31")
            return

        def job():
            import pdf_order
            if not (self.builder and self.builder.cfg):
                self.builder = OrderBuilder(load_config(), self.log)
            self.log(f"Ищу заказ поставщику № {num}…")
            key = pdf_order.find_order(self.builder.od, num)
            self.last_pdf = self.builder.save_pdf(key)
            m = re.search(r"Заказ поставщику (.+?) от ", os.path.basename(self.last_pdf))
            self.last_number = m.group(1) if m else num
        self.run_bg(job)

    # ---------------- МАКС (вручную из своего аккаунта: файл в буфер обмена -> Ctrl+V в чате)
    def max_text(self, number):
        try:
            cfg = load_config()
            t = cfg.get("MAX", "текст", fallback="") or "Заказ № {number}"
        except Exception:
            t = "Заказ № {number}"
        return t.replace("{number}", number)

    @staticmethod
    def copy_file_to_clipboard(path):
        import subprocess
        lit = path.replace("'", "''")
        rc = subprocess.call(["powershell", "-NoProfile", "-STA", "-Command", f"Set-Clipboard -LiteralPath '{lit}'"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if rc != 0:
            raise RuntimeError("Не удалось положить файл в буфер обмена")

    def open_max_app(self):
        """Открывает МАКС, если найдёт программу (путь можно указать в config.ini: [MAX] программа =)."""
        try:
            cfg = load_config()
            prog = (cfg.get("MAX", "программа", fallback="") or "").strip()
        except Exception:
            prog = ""
        la = os.environ.get("LOCALAPPDATA", "")
        cands = [prog] + [os.path.join(la, *p) for p in (("Programs", "MAX", "MAX.exe"), ("MAX", "MAX.exe"),
                                                           ("Programs", "max", "MAX.exe"), ("Programs", "Max", "Max.exe"))]
        cands += [os.path.join(os.environ.get("ProgramFiles", ""), "MAX", "MAX.exe")]
        for c in cands:
            if c and os.path.exists(c) and hasattr(os, "startfile"):
                os.startfile(c)
                return True
        return False

    def send_max(self):
        num = self.num_var.get().strip()
        if not num and not self.last_pdf:
            messagebox.showinfo("МАКС", "Сначала создайте заказ или впишите номер заказа в поле слева")
            return

        def job():
            if num:
                import pdf_order
                if not (self.builder and self.builder.cfg):
                    self.builder = OrderBuilder(load_config(), self.log)
                key = pdf_order.find_order(self.builder.od, num)
                path = self.builder.save_pdf(key, open_file=False)
                m = re.search(r"Заказ поставщику (.+?) от ", os.path.basename(path))
                number = m.group(1) if m else num
            else:
                path, number = self.last_pdf, self.last_number
            self.copy_file_to_clipboard(path)
            self.log(f"Файл заказа № {number} в буфере обмена — вставьте в чат МАКС (Ctrl+V)")
            self.root.after(0, lambda: self.max_steps(path, number))
        self.run_bg(job)

    def max_steps(self, path, number):
        text = self.max_text(number)
        self.open_max_app()
        messagebox.showinfo("Отправка в МАКС — шаг 1 из 2",
                            "Файл заказа уже скопирован.\n\n"
                            "1. Откройте в МАКС переписку с поставщиком.\n"
                            "2. Нажмите Ctrl+V — файл вставится — и отправьте.\n\n"
                            f"Потом нажмите «ОК» здесь — скопирую текст «{text}».")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        messagebox.showinfo("Отправка в МАКС — шаг 2 из 2",
                            f"Текст «{text}» скопирован.\n\nВ том же чате нажмите Ctrl+V и Enter.")
        self.log(f"  Текст «{text}» скопирован")


    def doc_date(self):
        d = dt.datetime.strptime(self.date_var.get().strip(), "%d.%m.%Y")
        now = dt.datetime.now()
        return d.replace(hour=now.hour, minute=now.minute, second=now.second)

    def run_bg(self, fn):
        self.busy(True)

        def wrap():
            try:
                fn()
            except Exception as e:
                err = str(e)
                self.log("ОШИБКА: " + err)
                self.log(traceback.format_exc())
                self.root.after(0, lambda m=err: messagebox.showerror("Ошибка", m))
            finally:
                self.root.after(0, lambda: self.busy(False))
        threading.Thread(target=wrap, daemon=True).start()

    def check(self):
        def job():
            cfg = load_config()
            self.builder = OrderBuilder(cfg, self.log)
            self.builder.prepare()
            self.log("Сопоставляю товары…")
            self.builder.match(self.items)
            self.root.after(0, self.refresh)
            bad = [i for i in self.items if not i.get("key")]
            self.log("Готово. " + (f"Не найдено в 1С: {len(bad)} — они НЕ попадут в заказ." if bad else "Все товары найдены."))
        self.run_bg(job)

    def go(self):
        try:
            d = self.doc_date()
        except ValueError:
            messagebox.showerror("Дата", "Дата в формате ДД.ММ.ГГГГ")
            return
        ok = [i for i in self.items if i.get("key")]
        if not ok:
            messagebox.showwarning("Нет строк", "Нет найденных в 1С товаров")
            return
        zero = [i for i in ok if i.get("price", 0) <= 0]
        msg = f"Создать и провести заказ поставщику от {d:%d.%m.%Y}?\nСтрок: {len(ok)}"
        if zero:
            msg += f"\n\nВНИМАНИЕ: у {len(zero)} строк цена 0."
        miss = len(self.items) - len(ok)
        if miss:
            msg += f"\nНе найдено в 1С и будет пропущено: {miss}"
        if not messagebox.askyesno("Подтверждение", msg):
            return

        def job():
            cfg = self.builder.cfg
            if not cfg.has_section("_run"):
                cfg.add_section("_run")
            cfg.set("_run", "file", os.path.basename(self.file))
            res = self.builder.create_and_post(ok, d, post=yes(cfg["ЗАКАЗ"].get("проводить", "да")))
            for r in res:
                if r.get("pdf"):
                    self.last_pdf, self.last_number = r["pdf"], r["number"]
            txt = "\n".join(f"№ {r['number']} на {r['sum']:,.2f} ₽ — " + ("проведён" if r["posted"] else "записан, НЕ проведён")
                            + (f"\nPDF: {r['pdf']}" if r.get("pdf") else "")
                            for r in res).replace(",", " ")
            self.root.after(0, lambda: messagebox.showinfo("Готово", txt))
        self.run_bg(job)


# ----------------------------------------------------------------------------- консоль
def console(path, date_str=None, post=True):
    cfg = load_config()
    if not cfg.has_section("_run"):
        cfg.add_section("_run")
    cfg.set("_run", "file", os.path.basename(path))
    items = read_excel(path)
    print(f"Строк с заказом: {len(items)}")
    b = OrderBuilder(cfg)
    b.prepare()
    b.match(items)
    d = dt.datetime.strptime(date_str, "%d.%m.%Y") if date_str else dt.datetime.now()
    for r in b.create_and_post(items, d, post=post):
        print(r)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        console(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        root = tk.Tk()
        App(root)
        root.mainloop()
