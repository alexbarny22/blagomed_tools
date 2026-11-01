# -*- coding: utf-8 -*-
"""
1С:УТ 11.5 через OData (публикация ut12): поставщик по ИНН, номенклатура, сопоставления,
создание и проведение документа «Приобретение товаров и услуг».
"""
import datetime as dt
import json
import os
import re
import time
import urllib.parse

import requests
from rapidfuzz import fuzz, process

HERE = os.path.dirname(os.path.abspath(__file__))
EMPTY = "00000000-0000-0000-0000-000000000000"
DOC = "Document_ПриобретениеТоваровУслуг"
CACHE_FILE = os.path.join(HERE, "nomenclature.json")
MAP_FILE = os.path.join(HERE, "mapping.json")


class ODataError(Exception):
    pass


def q(s):
    return str(s).replace("'", "''")


def odt(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def parse_odt(s):
    try:
        return dt.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def norm(s):
    s = str(s).lower().replace("ё", "е")
    s = re.sub(r"[\"'«»“”.,;:()\[\]/\\_\-–—№#*+]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


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
            msg = "Нет доступа (401): проверьте логин и пароль 1С в config.py. " + msg
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


# ------------------------------------------------------------------ сопоставления (запоминаются)
def load_map():
    try:
        with open(MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_map(mp):
    tmp = MAP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mp, f, ensure_ascii=False, indent=1)
    os.replace(tmp, MAP_FILE)


def remember(inn, line, key, name):
    """Запомнить: «так называется у поставщика» -> наша позиция."""
    mp = load_map()
    sup = mp.setdefault(inn or "без_инн", {})
    sup["n:" + norm(line["name"])] = {"key": key, "name": name}
    if line.get("article"):
        sup["a:" + norm(line["article"])] = {"key": key, "name": name}
    save_map(mp)


def forget(inn, line):
    mp = load_map()
    sup = mp.get(inn or "без_инн", {})
    sup.pop("n:" + norm(line["name"]), None)
    if line.get("article"):
        sup.pop("a:" + norm(line["article"]), None)
    save_map(mp)


# ------------------------------------------------------------------ основной класс
HEADER_DROP = {"Ref_Key", "DataVersion", "Number", "Date", "Posted", "DeletionMark", "Комментарий",
               "СуммаДокумента", "НомерВходящегоДокумента", "ДатаВходящегоДокумента"}
HEADER_DROP_PARTS = ("Основание", "ЗаказПоставщику", "Распоряжение", "ПоДаннымПоставщика", "СчетФактура",
                     "ДатаПлатежа", "Согласован")
LINE_DROP_PARTS = ("Заказ", "Основание", "КодСтроки", "LineNumber", "Сертификат", "Серия", "Характеристика",
                   "Упаковка", "НоменклатураПартнера", "НоменклатураПоставщика", "Назначение", "Распоряжение",
                   "ИдентификаторСтроки", "Расхожден", "СуммаЗалог", "НомерГТД", "СтранаПроисхождения",
                   "Маркиров", "Прослеживаем", "КодТНВЭД", "Количество")


class OneC:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.od = OData(cfg.ODATA_URL, cfg.ONEC_USER, cfg.ONEC_PASSWORD, getattr(cfg, "ODATA_TIMEOUT", 120))
        self.items = []          # [{key, name, art}]
        self._choices = {}
        self._by_key = {}
        self._refs = {}

    # ---------- номенклатура (кэш в файле, как в move_bot)
    def load_items(self, force=False):
        days = getattr(self.cfg, "CACHE_DAYS", 7)
        if not force and os.path.exists(CACHE_FILE) and time.time() - os.path.getmtime(CACHE_FILE) < days * 86400:
            with open(CACHE_FILE, encoding="utf-8") as f:
                self.items = json.load(f)
        else:
            self.log("Загружаю номенклатуру из 1С…")
            rows = self.od.get("Catalog_Номенклатура", **{
                "$filter": "IsFolder eq false and DeletionMark eq false",
                "$select": "Ref_Key,Description,Артикул"})
            skip = [s.lower() for s in getattr(self.cfg, "EXCLUDE_NAME_PARTS", ["(МГ)"]) if s]
            self.items = [{"key": r["Ref_Key"], "name": r["Description"], "art": (r.get("Артикул") or "").strip()}
                          for r in rows if not any(s in r["Description"].lower() for s in skip)]
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False)
            self.log(f"  позиций: {len(self.items)}")
        self._choices = {i: norm(it["name"]) for i, it in enumerate(self.items)}
        self._by_key = {it["key"]: it for it in self.items}
        return len(self.items)

    def search(self, text, limit=5):
        """Похожие позиции нашей номенклатуры: [(item, score)]."""
        if not self._choices:
            self.load_items()
        res = process.extract(norm(text), self._choices, scorer=fuzz.WRatio, limit=limit)
        return [(self.items[i], round(sc)) for _, sc, i in res]

    def match_line(self, inn, line):
        """
        -> {'key','name1c','how','cands'}; how: 'запомнено' | 'точно' | '' (нужен выбор).
        Автоматически принимаем только запомненное или полное совпадение названия.
        """
        sup = load_map().get(inn or "без_инн", {})
        hit = sup.get("n:" + norm(line["name"]))
        if not hit and line.get("article"):
            hit = sup.get("a:" + norm(line["article"]))
        if hit and hit["key"] in self._by_key:
            return {"key": hit["key"], "name1c": self._by_key[hit["key"]]["name"], "how": "запомнено", "cands": []}
        nn = norm(line["name"])
        exact = [it for it in self.items if norm(it["name"]) == nn]
        if len(exact) == 1:
            return {"key": exact[0]["key"], "name1c": exact[0]["name"], "how": "точно", "cands": []}
        cands = self.search(line["name"], 4)
        return {"key": None, "name1c": "", "how": "", "cands": cands}

    # ---------- поставщик
    def find_supplier(self, inn, name=""):
        flt = "DeletionMark eq false"
        if inn:
            rows = self.od.get("Catalog_Контрагенты", **{"$filter": f"ИНН eq '{q(inn)}' and {flt}",
                                                          "$select": "Ref_Key,Description,ИНН,Партнер_Key"})
            if rows:
                return rows[0]
        # запасной вариант — по названию (без «ООО», кавычек)
        core = re.sub(r"(?i)\b(ооо|оао|зао|пао|ао|ип|индивидуальный предприниматель)\b|[\"'«»]", " ", name or "")
        core = max(core.split(), key=len, default="")
        if len(core) >= 4:
            for v in dict.fromkeys([core, core.upper(), core.title()]):
                rows = self.od.get("Catalog_Контрагенты", **{
                    "$filter": f"substringof('{q(v)}', Description) and {flt}",
                    "$select": "Ref_Key,Description,ИНН,Партнер_Key"})
                if len(rows) == 1:
                    return rows[0]
        return None

    def find_duplicate(self, ka_key, number):
        if not number:
            return None
        try:
            rows = self.od.get(DOC, **{
                "$filter": f"Контрагент_Key eq guid'{ka_key}' and НомерВходящегоДокумента eq '{q(number)}' "
                           f"and DeletionMark eq false",
                "$select": "Ref_Key,Number,Date,Posted"})
        except ODataError:
            return None
        return rows[0] if rows else None

    # ---------- шаблон шапки: последнее поступление от этого поставщика (или любое)
    def _last_doc(self, flt):
        try:
            rows = self.od.get(DOC, **{"$filter": flt, "$orderby": "Date desc", "$top": "1"})
            return rows[0] if rows else None
        except ODataError as e:
            if "404" in str(e) and "Приобретение" in str(e):
                raise
            heads = self.od.get(DOC, **{"$filter": flt, "$select": "Ref_Key,Date"})
            if not heads:
                return None
            k = max(heads, key=lambda r: r.get("Date", ""))["Ref_Key"]
            return self.od.get(f"{DOC}(guid'{k}')")

    def _ref(self, entity, name):
        if not name:
            return None
        k = (entity, name)
        if k not in self._refs:
            rows = self.od.get(entity, **{"$filter": f"substringof('{q(name)}', Description) and DeletionMark eq false",
                                          "$select": "Ref_Key,Description"})
            exact = [r for r in rows if r["Description"].strip().lower() == name.strip().lower()]
            self._refs[k] = (exact or rows or [None])[0]
        return self._refs[k]

    def template(self, ka):
        flt = "DeletionMark eq false and Posted eq true"
        try:
            tpl = self._last_doc(f"Контрагент_Key eq guid'{ka['Ref_Key']}' and {flt}")
        except ODataError as e:
            raise ODataError("Документ «Приобретение товаров и услуг» не включён в OData — запустите "
                             "setup_odata_prihod.bat (один раз). " + str(e))
        own = tpl is not None
        if not own:
            tpl = self._last_doc(flt)
        if tpl is None:
            raise ODataError("В 1С нет ни одного проведённого поступления — не с чего взять настройки шапки. "
                             "Создайте одно поступление вручную, дальше бот будет брать его за образец.")
        return tpl, own

    # ---------- сборка и проведение
    def build(self, ka, rec, lines, tpl, own_tpl):
        """
        rec — распознанный документ; lines — [{line, key}] (только сопоставленные).
        Суммы берутся из документа поставщика (что заплатили), цена = сумма / количество.
        """
        cfg = self.cfg
        doc = {}
        for k, v in tpl.items():
            if k in HEADER_DROP or "@" in k or isinstance(v, list):
                continue
            if any(p in k for p in HEADER_DROP_PARTS):
                continue
            doc[k] = v
        if not own_tpl:      # образец от другого поставщика — его соглашение/договор не подходят
            for k in ("Соглашение_Key", "Договор_Key", "ВидЦеныПоставщика_Key", "БанковскийСчетКонтрагента_Key",
                      "КонтактноеЛицо_Key"):
                if k in doc:
                    doc[k] = EMPTY
            if ka.get("Партнер_Key") not in (None, EMPTY):
                try:
                    sg = self.od.get("Catalog_СоглашенияСПоставщиками", **{
                        "$filter": f"Партнер_Key eq guid'{ka['Партнер_Key']}' and DeletionMark eq false",
                        "$select": "Ref_Key"})
                    if sg:
                        doc["Соглашение_Key"] = sg[0]["Ref_Key"]
                except ODataError:
                    pass
            try:
                dg = self.od.get("Catalog_ДоговорыКонтрагентов", **{
                    "$filter": f"Контрагент_Key eq guid'{ka['Ref_Key']}' and DeletionMark eq false",
                    "$select": "Ref_Key"})
                if dg:
                    doc["Договор_Key"] = dg[0]["Ref_Key"]
            except ODataError:
                pass

        now = dt.datetime.now().replace(microsecond=0)
        doc_date = now
        rdate = None
        try:
            rdate = dt.datetime.strptime(str(rec.get("date", ""))[:10], "%d.%m.%Y")
        except ValueError:
            pass
        if getattr(cfg, "DATE_MODE", "today") == "document" and rdate and rdate <= now:
            doc_date = rdate.replace(hour=12)
        doc["Date"] = odt(doc_date)
        doc["Контрагент_Key"] = ka["Ref_Key"]
        if ka.get("Партнер_Key") not in (None, EMPTY):
            doc["Партнер_Key"] = ka["Партнер_Key"]
        org = self._ref("Catalog_Организации", getattr(cfg, "ORGANIZATION", ""))
        if org:
            doc["Организация_Key"] = org["Ref_Key"]
        sklad = self._ref("Catalog_Склады", getattr(cfg, "WAREHOUSE", ""))
        if sklad:
            doc["Склад_Key"] = sklad["Ref_Key"]
        doc["НомерВходящегоДокумента"] = str(rec.get("number") or "")[:30]
        if rdate:
            doc["ДатаВходящегоДокумента"] = odt(rdate)
        doc["Комментарий"] = (f"Создан ботом по фото: {rec.get('doc_type', '')} № {rec.get('number', '')} "
                              f"от {rec.get('date', '')}").strip()

        incl = bool(doc.get("ЦенаВключаетНДС", True))
        pattern = (tpl.get("Товары") or [None])[0]
        track_vat = bool(pattern and to_num(pattern.get("СуммаНДС")) > 0)

        out, total = [], 0.0
        for n, it in enumerate(lines, 1):
            ln = {}
            if pattern:
                for k, v in pattern.items():
                    if "@" in k or any(p in k for p in LINE_DROP_PARTS):
                        continue
                    if k in ("Цена", "Сумма", "СуммаНДС", "СуммаСНДС", "СуммаСкидки", "ПроцентСкидки",
                             "СуммаРучнойСкидки", "ПроцентРучнойСкидки", "СуммаАвтоматическойСкидки",
                             "ПроцентАвтоматическойСкидки"):
                        continue
                    ln[k] = v
            src = it["line"]
            qty = float(src["qty"])
            with_vat = round(float(src["sum"]), 2)
            vat = round(float(src.get("vat_sum") or 0), 2) if track_vat else 0.0
            summa = with_vat if incl else round(with_vat - vat, 2)
            ln.update({
                "LineNumber": str(n),
                "Номенклатура_Key": it["key"],
                "Характеристика_Key": EMPTY,
                "Упаковка_Key": EMPTY,
                "Количество": qty,
                "КоличествоУпаковок": qty,
                "Цена": round(summa / qty, 2) if qty else 0,
                "Сумма": summa,
                "СуммаНДС": vat,
                "СуммаСНДС": with_vat,
            })
            if doc.get("Склад_Key"):
                ln["Склад_Key"] = doc["Склад_Key"]
            out.append(ln)
            total += with_vat
        doc["Товары"] = out
        doc["СуммаДокумента"] = round(total, 2)

        stages = tpl.get("ЭтапыГрафикаОплаты") or []
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
                        s[k] = odt(doc_date.replace(hour=0, minute=0, second=0))
                    elif ("Зачет" in k or "Зачёт" in k or "Залог" in k) and isinstance(s[k], (int, float)):
                        s[k] = 0
                new.append(s)
            doc["ЭтапыГрафикаОплаты"] = new
        return doc

    def create(self, doc, post=True):
        created = self.od.post(DOC, doc)
        key, number = created.get("Ref_Key"), str(created.get("Number", "")).strip()
        posted, err = False, ""
        if post:
            try:
                self.od.post(f"{DOC}(guid'{key}')/Post()", PostingModeOperational="false")
                posted = True
            except ODataError as e:
                err = str(e)
        return {"key": key, "number": number, "posted": posted, "error": err}
