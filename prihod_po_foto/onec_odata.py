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
ORDER_DOC = "Document_ЗаказПоставщику"
CACHE_FILE = os.path.join(HERE, "nomenclature.json")


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


# ------------------------------------------------------------------ сопоставления — таблица Excel (mapping.py)
import mapping  # noqa: E402


def remember(supplier, inn, line, item):
    """Запомнить: «так называется у поставщика» -> наш товар (строка в Сопоставления.xlsx)."""
    mapping.add(supplier, inn, line, item["name"], item.get("code", ""))


def forget(inn, line):
    mapping.remove(inn, line)


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
        fresh = None
        if not force and os.path.exists(CACHE_FILE) and time.time() - os.path.getmtime(CACHE_FILE) < days * 86400:
            with open(CACHE_FILE, encoding="utf-8") as f:
                fresh = json.load(f)
            if not fresh or "code" not in fresh[0]:          # старый кэш без кодов — перезагрузить
                fresh = None
        if fresh is not None:
            self.items = fresh
        else:
            self.log("Загружаю номенклатуру из 1С…")
            rows = self.od.get("Catalog_Номенклатура", **{
                "$filter": "IsFolder eq false and DeletionMark eq false",
                "$select": "Ref_Key,Description,Артикул,Code"})
            skip = [s.lower() for s in getattr(self.cfg, "EXCLUDE_NAME_PARTS", ["(МГ)"]) if s]
            self.items = [{"key": r["Ref_Key"], "name": r["Description"], "art": (r.get("Артикул") or "").strip(),
                           "code": (r.get("Code") or "").strip()}
                          for r in rows if not any(s in r["Description"].lower() for s in skip)]
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False)
            self.log(f"  позиций: {len(self.items)}")
        self._choices = {i: norm(it["name"]) for i, it in enumerate(self.items)}
        self._by_key = {it["key"]: it for it in self.items}
        self._by_code = {norm(it["code"]): it for it in self.items if it.get("code")}
        self._by_name = {}
        for it in self.items:
            self._by_name.setdefault(norm(it["name"]), []).append(it)
        return len(self.items)

    def resolve(self, our_name, our_code=""):
        """Наш товар по строке таблицы: по коду 1С, иначе по точному названию."""
        if our_code and norm(our_code) in self._by_code:
            return self._by_code[norm(our_code)]
        hits = self._by_name.get(norm(our_name), [])
        return hits[0] if len(hits) == 1 else None

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
        row = mapping.lookup(inn, line)
        if row:
            it = self.resolve(row.get("Наш товар в 1С"), row.get("Код в 1С"))
            if it:
                return {"key": it["key"], "name1c": it["name"], "how": "по таблице", "cands": []}
            self.log(f"В таблице сопоставлений товар «{row.get('Наш товар в 1С')}» не найден в 1С — спрошу заново")
        nn = norm(line["name"])
        exact = [it for it in self.items if norm(it["name"]) == nn]
        if len(exact) == 1:
            return {"key": exact[0]["key"], "name1c": exact[0]["name"], "how": "точно", "cands": []}
        cands = self.search(line["name"], 4)
        return {"key": None, "name1c": "", "how": "", "cands": cands}

    def ai_suggest(self, lines, model, ask):
        """
        Подсказки нейросети для несопоставленных строк, когда названия не похожи по буквам.
        lines: [{'line', 'cands'}]; ask(model, prompt) -> текст. Ставит выбранный вариант первым с пометкой 🤖.
        """
        if not lines:
            return
        blocks, pools = [], []
        for k, L in enumerate(lines, 1):
            pool = [it for it, _ in self.search(L["line"]["name"], 25)]
            pools.append(pool)
            opts = "\n".join(f"  {j}. {it['name']}" for j, it in enumerate(pool, 1))
            blocks.append(f"{k}) У поставщика: «{L['line']['name']}»\n{opts}")
        prompt = ("Сопоставь товары из накладной поставщика с нашим справочником. Названия могут сильно "
                  "отличаться (другие слова, сокращения, порядок). Для каждого пункта выбери номер подходящего "
                  "варианта из его списка или 0, если подходящего нет. Ответ — только JSON вида "
                  '{"1": 3, "2": 0}.\n\n' + "\n\n".join(blocks))
        try:
            txt = ask(model, prompt)
            ans = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
        except Exception as e:
            self.log(f"Подсказки нейросети не получены: {e}")
            return
        for k, L in enumerate(lines, 1):
            try:
                j = int(ans.get(str(k), 0))
            except (TypeError, ValueError):
                continue
            if 1 <= j <= len(pools[k - 1]):
                pick = pools[k - 1][j - 1]
                rest = [(it, sc) for it, sc in L["cands"] if it["key"] != pick["key"]]
                L["cands"] = [(dict(pick, ai=True), 100)] + rest[:3]

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
    # ---------- заказы поставщику: куда разложить поступление
    @staticmethod
    def _order_ref_field(row):
        """Поле строки поступления со ссылкой на заказ (ЗаказПоставщику_Key или составное ЗаказПоставщику)."""
        for k in row:
            if "ЗаказПоставщику" in k and not k.endswith("_Type") and "@" not in k:
                return k
        return None

    def orders_context(self, ka, tpl=None):
        """
        Открытые заказы поставщику и сколько по ним ещё не поступило.
        Не поступило = заказано − уже оприходовано проведёнными поступлениями со ссылкой на этот заказ.
        """
        od = self.od
        t0 = time.time()
        flt = f"Контрагент_Key eq guid'{ka['Ref_Key']}' and Posted eq true and DeletionMark eq false"
        sel = "Ref_Key,Number,Date,Статус,ЦенаВключаетНДС,Товары"
        orders = None
        for extra in (" and Статус ne 'Закрыт'", ""):      # сначала просим 1С отдать только незакрытые
            try:
                orders = od.get(ORDER_DOC, **{"$filter": flt + extra, "$select": sel})
                break
            except ODataError as e:
                self.log(f"  заказы: запрос без фильтра по статусу ({str(e)[:80]})")
        orders = [o for o in orders or [] if "закрыт" not in str(o.get("Статус", "")).lower()]
        orders.sort(key=lambda r: r.get("Date", ""))
        self.log(f"  открытых заказов: {len(orders)} ({time.time() - t0:.1f} с)")
        if not orders:
            return None
        okeys = {o["Ref_Key"] for o in orders}
        since = min(o["Date"] for o in orders)[:19]
        t1 = time.time()
        recs = None
        # 0) самый быстрый путь: строки поступлений (табличная часть «Товары») с отбором по заказу,
        #    по короткому запросу на каждый открытый заказ; потом — какие из этих поступлений проведены
        try:
            lines_by_doc = {}
            for k in okeys:
                for ln in od.get(DOC + "_Товары", **{"$filter": f"ЗаказПоставщику_Key eq guid'{k}'"}):
                    lines_by_doc.setdefault(ln["Ref_Key"], []).append(ln)
            alive = set()
            ids = list(lines_by_doc)
            for i in range(0, len(ids), 20):
                cond = " or ".join(f"Ref_Key eq guid'{x}'" for x in ids[i:i + 20])
                for r in od.get(DOC, **{"$filter": f"({cond}) and Posted eq true and DeletionMark eq false",
                                        "$select": "Ref_Key"}):
                    alive.add(r["Ref_Key"])
            recs = [{"Ref_Key": k, "Товары": v} for k, v in lines_by_doc.items() if k in alive]
        except ODataError as e:
            self.log(f"  строки поступлений по заказам: не вышло ({str(e)[:80]})")
            recs = None
        # 1) поступления, где в строках есть ссылка на эти заказы (1С отбирает сама)
        if recs is None and len(okeys) <= 40:
            cond = " or ".join(f"t/ЗаказПоставщику_Key eq guid'{k}'" for k in okeys)
            try:
                recs = od.get(DOC, **{"$filter": f"{flt} and Товары/any(t: {cond})", "$select": "Ref_Key,Date,Товары"})
            except ODataError as e:
                self.log(f"  поступления по заказам: отбор в 1С не сработал ({str(e)[:80]}), беру за период")
        # 2) запасной путь — все поступления поставщика с даты самого старого открытого заказа
        if recs is None:
            try:
                recs = od.get(DOC, **{"$filter": f"{flt} and Date ge datetime'{since}'", "$select": "Ref_Key,Date,Товары"})
            except ODataError:
                recs = [r for r in od.get(DOC, **{"$filter": flt}) if r.get("Date", "") >= since]
        self.log(f"  поступлений по ним: {len(recs)} ({time.time() - t1:.1f} с)")
        # как в этой базе строка поступления ссылается на заказ — узнаём по прошлым поступлениям/образцу
        sample = [ln for r in recs + ([tpl] if tpl else []) for ln in (r.get("Товары") or [])]
        field = next((self._order_ref_field(ln) for ln in sample if self._order_ref_field(ln)), "ЗаказПоставщику_Key")
        got_code, got_item = {}, {}
        for r in recs:
            for ln in r.get("Товары") or []:
                ok = ln.get(field)
                if ok not in okeys:
                    continue
                q_ = to_num(ln.get("Количество"))
                code = ln.get("КодСтроки")
                if code:
                    got_code[(ok, code)] = got_code.get((ok, code), 0) + q_
                got_item[(ok, ln.get("Номенклатура_Key"))] = got_item.get((ok, ln.get("Номенклатура_Key")), 0) + q_
        # совпадают ли коды строк поступлений с кодами строк заказов (тогда связь построчная)
        order_codes = {(o["Ref_Key"], ln.get("КодСтроки")) for o in orders for ln in o.get("Товары") or []}
        use_code = bool(got_code) and all(k in order_codes for k in got_code)
        rem = []
        for o in orders:
            left_item = {}
            for ln in o.get("Товары") or []:
                if ln.get("Отменено"):
                    continue
                item = ln.get("Номенклатура_Key")
                qty = to_num(ln.get("Количество"))
                if use_code:
                    qty -= got_code.get((o["Ref_Key"], ln.get("КодСтроки")), 0)
                else:              # поступило по товару — списываем со строк заказа по порядку
                    k = (o["Ref_Key"], item)
                    left_item.setdefault(k, got_item.get(k, 0))
                    take = min(qty, left_item[k])
                    left_item[k] -= take
                    qty -= take
                if qty > 0.0001:
                    # цена в заказе за единицу — для сверки с накладной (с НДС, если цены в заказе без него)
                    price = to_num(ln.get("Цена"))
                    if not o.get("ЦенаВключаетНДС", True) and to_num(ln.get("Количество")):
                        price = round(to_num(ln.get("СуммаСНДС") or ln.get("Сумма")) / to_num(ln.get("Количество")), 2)
                    rem.append({"order": o["Ref_Key"], "number": str(o.get("Number", "")).strip(),
                                "date": o.get("Date", "")[:10], "item": item, "code": ln.get("КодСтроки"),
                                "left": qty, "price": price})
        return {"orders": orders, "rem": rem, "field": field, "use_code": use_code}

    @staticmethod
    def allocate(ctx, lines):
        """
        Раскладывает строки по заказам, от старых к новым. -> [[(rem, кол-во), ...] на каждую строку].
        Товар, которого нет в открытых заказах (или сверх заказанного), привязывается к открытому заказу,
        куда легла большая часть этой поставки (если такого нет — к самому свежему); у такой части
        rem["extra"] = True. rem = None только если открытых заказов нет совсем.
        """
        left = [dict(r) for r in (ctx or {}).get("rem", [])]
        out = []
        for it in lines:
            need = float(it["line"]["qty"])
            parts = []
            for r in left:
                if need <= 0.0001:
                    break
                if r["item"] != it["key"] or r["left"] <= 0.0001:
                    continue
                take = min(need, r["left"])
                r["left"] -= take
                need -= take
                parts.append((r, take))
            if need > 0.0001 or not parts:
                parts.append((None, need if parts else float(it["line"]["qty"])))
            out.append(parts)
        # остаток без заказа — в «основной» заказ этой поставки
        orders = (ctx or {}).get("orders") or []
        if orders:
            weight = {}
            for parts in out:
                for r, q in parts:
                    if r:
                        weight[r["order"]] = weight.get(r["order"], 0) + q
            main = max(weight, key=weight.get) if weight else orders[-1]["Ref_Key"]
            o = next(x for x in orders if x["Ref_Key"] == main)
            home = {"order": main, "number": str(o.get("Number", "")).strip(), "date": o.get("Date", "")[:10],
                    "item": None, "code": None, "left": 0, "extra": True}
            out = [[(r if r else home, q) for r, q in parts] for parts in out]
        return out

    def build(self, ka, rec, lines, tpl, own_tpl, orders=None):
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
        tlines = tpl.get("Товары") or []
        pattern = next((ln for ln in tlines if self._order_ref_field(ln) and
                        ln.get(self._order_ref_field(ln)) not in (None, EMPTY)), tlines[0] if tlines else None)
        alloc = self.allocate(orders, lines) if orders else [[(None, float(it["line"]["qty"]))] for it in lines]
        ofield = (orders or {}).get("field", "ЗаказПоставщику_Key")
        obase = ofield[:-4] if ofield.endswith("_Key") else ofield
        track_vat = bool(pattern and to_num(pattern.get("СуммаНДС")) > 0)

        out, total, used_orders = [], 0.0, set()
        n = 0
        for it, parts in zip(lines, alloc):
          src = it["line"]
          full_qty = float(src["qty"]) or 1.0
          full_sum = round(float(src["sum"]), 2)
          full_vat = round(float(src.get("vat_sum") or 0), 2)
          done_sum = done_vat = 0.0
          for pi, (r, part_qty) in enumerate(parts):
            n += 1
            last = pi == len(parts) - 1
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
            qty = part_qty
            # сумма делится пропорционально количеству; последней части — остаток, чтобы копейки сошлись
            with_vat = round(full_sum - done_sum, 2) if last else round(full_sum * qty / full_qty, 2)
            vat_all = round(full_vat - done_vat, 2) if last else round(full_vat * qty / full_qty, 2)
            done_sum += with_vat
            done_vat += vat_all
            vat = vat_all if track_vat else 0.0
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
            if orders:
                ln[ofield] = r["order"] if r else EMPTY
                if pattern and f"{obase}_Type" in pattern:
                    ln[f"{obase}_Type"] = pattern[f"{obase}_Type"]
                if r and orders.get("use_code") and r.get("code"):
                    ln["КодСтроки"] = r["code"]
                if r:
                    used_orders.add(r["order"])
            out.append(ln)
            total += with_vat
        doc["Товары"] = out
        doc["СуммаДокумента"] = round(total, 2)
        if orders:      # шапка: один заказ — ссылка на него, несколько — пусто (заказы в строках)
            hfield = self._order_ref_field({k: v for k, v in tpl.items() if not isinstance(v, list)}) or ofield
            hbase = hfield[:-4] if hfield.endswith("_Key") else hfield
            doc[hfield] = next(iter(used_orders)) if len(used_orders) == 1 else EMPTY
            if f"{hbase}_Type" in tpl:
                doc[f"{hbase}_Type"] = tpl[f"{hbase}_Type"]

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
