# -*- coding: utf-8 -*-
"""
Аналитика и оптимизация Яндекс Директа для Благомед.

Раз в день (Crontab на Timeweb):
  1. Забирает статистику из API Директа (по дням, фразы, поисковые запросы, площадки РСЯ, устройства).
  2. Забирает настройки кампаний (стратегии, ставки, исключённые площадки, минус-слова).
  3. Считает эффективность и готовит ПРЕДЛОЖЕНИЯ изменений:
       - исключить площадку РСЯ, которая тратит деньги без конверсий;
       - добавить минус-слова из «пустых» поисковых запросов;
       - остановить фразу-«слив»;
       - снизить / поднять ставку (только для ручной стратегии);
       - корректировка ставок на мобильные.
  4. Кладёт дашборд index.html + Excel в OUTPUT_DIR.
Ничего не меняется в Директе без вашей галочки и кнопки «Применить» на странице (apply.php).

Запуск:  python3 direct_report.py          — обычный
         python3 direct_report.py --demo   — с выдуманными данными, без API (посмотреть вид отчёта)
Работает на стандартной библиотеке Python 3.6+ — ничего ставить не нужно.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import secrets
import sys
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from xlsx_writer import write_xlsx  # noqa: E402

VERSION = open(os.path.join(HERE, "version.txt"), encoding="utf-8").read().strip() \
    if os.path.exists(os.path.join(HERE, "version.txt")) else "dev"
LOG = os.path.join(HERE, "direct_report.log")
CACHE = os.path.join(HERE, "data_cache.json")
PROPOSALS = os.path.join(HERE, "proposals.json")
ACTIONS_LOG = os.path.join(HERE, "actions_log.json")
APPLY_SETTINGS = os.path.join(HERE, "apply_settings.json")
AI_PAYLOAD = os.path.join(HERE, "ai_payload.json")
AI_SETTINGS = os.path.join(HERE, "ai_settings.json")
AI_CACHE = os.path.join(HERE, "ai_cache.json")
TZ_SHIFT = dt.timedelta(hours=7)          # Барнаул
DEMO = "--demo" in sys.argv


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def log(msg):
    line = "%s  %s" % ((now_utc() + TZ_SHIFT).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(LOG) > 2_000_000:          # журнал не разрастается
            with open(LOG, encoding="utf-8") as f:
                tail = f.readlines()[-3000:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except Exception:
        pass


# ---------------------------------------------------------------- настройки
def load_config():
    try:
        import config as c
    except ImportError:
        if not DEMO:
            log("Нет файла config.py. Скопируйте config.example.py в config.py и заполните.")
            sys.exit(1)
        c = object()   # демо без config.py — берутся значения по умолчанию
    d = {
        "DIRECT_TOKEN": "", "CLIENT_LOGIN": "", "INCLUDE_VAT": "YES", "SANDBOX": False,
        "EXCLUDE_WORDS": ["прокат", "аренд"], "EXCLUDE_IDS": [], "INCLUDE_ONLY_IDS": [],
        "GOALS": [], "ATTRIBUTION": "AUTO", "TARGET_CPA": 0,
        "DAYS_HISTORY": 90, "ANALYSIS_DAYS": 30, "WASTE_MIN_COST": 300, "ALERT_COST_JUMP": 50,
        "BID_STEP": 20, "MOBILE_MIN": 50,
        "OUTPUT_DIR": "report", "REPORT_USER": "", "REPORT_PASSWORD": "", "KEEP_EXCEL_DAYS": 30,
        "AI_ENABLED": False, "AI_BASE_URL": "", "AI_API_KEY": "", "AI_MODEL": "",
        "AI_AUTH_SCHEME": "Bearer", "AI_MAX_TOKENS": 2000, "AI_TIMEOUT": 120, "AI_CONTEXT_NOTE": "",
    }
    for k in d:
        if hasattr(c, k):
            d[k] = getattr(c, k)
    if not os.path.isabs(d["OUTPUT_DIR"]):
        d["OUTPUT_DIR"] = os.path.join(HERE, d["OUTPUT_DIR"])
    return d


CFG = {}


def api_base():
    return "https://api-sandbox.direct.yandex.com" if CFG.get("SANDBOX") else "https://api.direct.yandex.com"


def _headers(extra=None):
    h = {"Authorization": "Bearer " + CFG["DIRECT_TOKEN"], "Accept-Language": "ru",
         "Content-Type": "application/json; charset=utf-8"}
    if CFG["CLIENT_LOGIN"]:
        h["Client-Login"] = CFG["CLIENT_LOGIN"]
    if extra:
        h.update(extra)
    return h


class DirectError(Exception):
    pass


def _explain(err):
    code = err.get("error_code")
    text = "%s: %s %s" % (code, err.get("error_string", ""), err.get("error_detail", ""))
    hints = {
        53: "токен неверный или устарел — получите новый (README, шаг 1)",
        58: "у приложения нет доступа к API Директа или заявка ещё не одобрена (README, шаг 1)",
        513: "логин не подключён к API: в Директе → Инструменты → API — примите условия",
        152: "закончились баллы API — повторится завтра",
        8800: "у API нет доступа к этому логину — проверьте CLIENT_LOGIN",
    }
    if code in hints:
        text += " → " + hints[code]
    return text


def api(service, method, params):
    body = json.dumps({"method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request("%s/json/v5/%s" % (api_base(), service), data=body, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise DirectError("HTTP %s в %s.%s" % (e.code, service, method))
    if "error" in data:
        raise DirectError(_explain(data["error"]))
    return data["result"]


def report(name, rtype, fields, date_from, date_to, filters=None, with_goals=True):
    """Отчёт Reports API → список словарей. Ждёт, пока Директ его подготовит."""
    params = {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": fields,
        "ReportType": rtype,
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": CFG["INCLUDE_VAT"],
    }
    if filters:
        params["SelectionCriteria"]["Filter"] = filters
    if with_goals and CFG["GOALS"]:
        params["Goals"] = [str(g) for g in CFG["GOALS"]]
        params["AttributionModels"] = [CFG["ATTRIBUTION"]]
    # у отчёта должно быть уникальное имя для каждого набора параметров
    sig = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()[:10]
    params["ReportName"] = "blagomed_%s_%s_%s" % (name, date_to, sig)
    body = json.dumps({"params": params}).encode("utf-8")
    hdr = _headers({"processingMode": "auto", "returnMoneyInMicros": "false",
                    "skipReportHeader": "true", "skipReportSummary": "true"})
    for attempt in range(60):
        req = urllib.request.Request(api_base() + "/json/v5/reports", data=body, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                code = r.status
                text = r.read().decode("utf-8")
                retry = int(r.headers.get("retryIn") or 10)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                raise DirectError(_explain(json.loads(raw)["error"]))
            except (ValueError, KeyError):
                raise DirectError("отчёт %s: HTTP %s %s" % (name, e.code, raw[:200]))
        if code == 200:
            return _parse_tsv(text)
        time.sleep(min(max(retry, 3), 30))     # 201 / 202 — отчёт готовится
    raise DirectError("отчёт %s не подготовился за 15 минут" % name)


def _num(v):
    if v in (None, "", "--"):
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def _parse_tsv(text):
    lines = [x for x in text.split("\n") if x.strip()]
    if not lines:
        return []
    head = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        vals = ln.split("\t")
        r = dict(zip(head, vals))
        # при заданных целях столбцы называются Conversions_<цель>_<модель> — складываем
        r["_conv"] = sum(_num(v) for k, v in r.items() if k.startswith("Conversions"))
        r["_rev"] = sum(_num(v) for k, v in r.items() if k.startswith("Revenue"))
        rows.append(r)
    return rows


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- сбор данных
def collect():
    today = (now_utc() + TZ_SHIFT).date()
    end = today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=CFG["DAYS_HISTORY"] - 1)
    a_start = end - dt.timedelta(days=CFG["ANALYSIS_DAYS"] - 1)
    D = lambda d: d.isoformat()  # noqa
    raw = {"end": D(end), "start": D(start), "a_start": D(a_start), "errors": []}

    log("Кампании…")
    camps = api("campaigns", "get", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Name", "Type", "State", "Status", "ExcludedSites", "NegativeKeywords"],
        "TextCampaignFieldNames": ["BiddingStrategy"],
        "UnifiedCampaignFieldNames": ["BiddingStrategy"],
    }).get("Campaigns", [])
    raw["campaigns"] = camps
    ids = [c["Id"] for c in camps if included(c["Id"], c["Name"])]
    raw["included_ids"] = ids
    if not ids:
        raw["errors"].append("Нет кампаний для анализа (все исключены настройками EXCLUDE_*)")
        return raw
    flt = [{"Field": "CampaignId", "Operator": "IN", "Values": [str(i) for i in ids]}]
    clicks = [{"Field": "Clicks", "Operator": "GREATER_THAN", "Values": ["0"]}]

    jobs = [
        ("daily", "CUSTOM_REPORT", ["Date", "CampaignId", "AdNetworkType", "Impressions", "Clicks", "Cost",
                                     "Conversions", "Revenue"], D(start), flt),
        ("keywords", "CUSTOM_REPORT", ["CampaignId", "AdGroupId", "AdGroupName", "CriterionId", "Criterion",
                                        "CriterionType", "Impressions", "Clicks", "Cost", "Conversions"],
         D(a_start), flt + clicks),
        ("queries", "SEARCH_QUERY_PERFORMANCE_REPORT", ["CampaignId", "AdGroupId", "Query", "Impressions",
                                                         "Clicks", "Cost", "Conversions"], D(a_start), flt + clicks),
        ("placements", "CUSTOM_REPORT", ["CampaignId", "Placement", "Impressions", "Clicks", "Cost",
                                          "Conversions"], D(a_start),
         flt + clicks + [{"Field": "AdNetworkType", "Operator": "EQUALS", "Values": ["AD_NETWORK"]}]),
        ("devices", "CUSTOM_REPORT", ["CampaignId", "Device", "Impressions", "Clicks", "Cost", "Conversions"],
         D(a_start), flt),
    ]
    for name, rtype, fields, frm, f in jobs:
        log("Отчёт %s…" % name)
        try:
            raw[name] = report(name, rtype, fields, frm, D(end), f)
        except Exception as e:
            raw[name] = []
            raw["errors"].append("Отчёт «%s» не получен: %s" % (name, e))
            log("  ошибка: %s" % e)

    log("Ключевые фразы и ставки…")
    raw["kw_meta"] = []
    try:
        for part in chunks(ids, 10):
            offset = 0
            while True:
                res = api("keywords", "get", {
                    "SelectionCriteria": {"CampaignIds": part},
                    "FieldNames": ["Id", "Keyword", "State", "CampaignId", "AdGroupId"],
                    "Page": {"Limit": 10000, "Offset": offset}})
                raw["kw_meta"] += res.get("Keywords", [])
                if "LimitedBy" not in res:
                    break
                offset = res["LimitedBy"]
    except Exception as e:
        raw["errors"].append("Ключевые фразы не получены: %s" % e)
    try:
        bids = {}
        for part in chunks(ids, 10):
            offset = 0
            while True:
                res = api("keywordbids", "get", {
                    "SelectionCriteria": {"CampaignIds": part},
                    "FieldNames": ["KeywordId"], "SearchFieldNames": ["Bid"],
                    "Page": {"Limit": 10000, "Offset": offset}})
                for b in res.get("KeywordBids", []):
                    bids[b["KeywordId"]] = ((b.get("Search") or {}).get("Bid") or 0)
                if "LimitedBy" not in res:
                    break
                offset = res["LimitedBy"]
        for k in raw["kw_meta"]:
            k["Bid"] = bids.get(k["Id"], 0)
    except Exception as e:
        raw["errors"].append("Ставки фраз не получены (предложения по ставкам пропущены): %s" % e)

    log("Корректировки ставок…")
    raw["modifiers"] = []
    try:
        for part in chunks(ids, 10):
            res = api("bidmodifiers", "get", {
                "SelectionCriteria": {"CampaignIds": part, "Levels": ["CAMPAIGN"], "Types": ["MOBILE_ADJUSTMENT"]},
                "FieldNames": ["Id", "CampaignId", "Type"],
                "MobileAdjustmentFieldNames": ["BidModifier"]})
            raw["modifiers"] += res.get("BidModifiers", [])
    except Exception as e:
        raw["errors"].append("Корректировки не получены: %s" % e)
    return raw


def included(cid, name):
    if CFG["INCLUDE_ONLY_IDS"]:
        return int(cid) in [int(x) for x in CFG["INCLUDE_ONLY_IDS"]]
    if int(cid) in [int(x) for x in CFG["EXCLUDE_IDS"]]:
        return False
    low = (name or "").lower()
    return not any(w.lower() in low for w in CFG["EXCLUDE_WORDS"])


# ---------------------------------------------------------------- демо-данные
def demo_collect():
    rnd = random.Random(7)
    today = (now_utc() + TZ_SHIFT).date()
    end = today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=CFG["DAYS_HISTORY"] - 1)
    a_start = end - dt.timedelta(days=CFG["ANALYSIS_DAYS"] - 1)
    manual = {"BiddingStrategy": {"Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                                  "Network": {"BiddingStrategyType": "SERVING_OFF"}}}
    auto = {"BiddingStrategy": {"Search": {"BiddingStrategyType": "PAY_FOR_CONVERSION"},
                                "Network": {"BiddingStrategyType": "NETWORK_DEFAULT"}}}
    camps = [
        {"Id": 101, "Name": "Корсеты и бандажи — Поиск", "Type": "TEXT_CAMPAIGN", "State": "ON", "TextCampaign": manual,
         "ExcludedSites": None, "NegativeKeywords": {"Items": ["бесплатно", "своими руками"]}},
        {"Id": 102, "Name": "Ортопедические стельки — Поиск", "Type": "TEXT_CAMPAIGN", "State": "ON", "TextCampaign": manual,
         "ExcludedSites": None, "NegativeKeywords": None},
        {"Id": 103, "Name": "Ортопедия — ЕПК (поиск + РСЯ)", "Type": "UNIFIED_CAMPAIGN", "State": "ON",
         "UnifiedCampaign": auto, "ExcludedSites": {"Items": ["games.example"]}, "NegativeKeywords": None},
        {"Id": 104, "Name": "Компрессионный трикотаж — РСЯ", "Type": "TEXT_CAMPAIGN", "State": "ON",
         "TextCampaign": auto, "ExcludedSites": None, "NegativeKeywords": None},
        {"Id": 201, "Name": "Прокат кроватей — Поиск", "Type": "TEXT_CAMPAIGN", "State": "ON", "TextCampaign": manual,
         "ExcludedSites": None, "NegativeKeywords": None},
    ]
    ids = [c["Id"] for c in camps if included(c["Id"], c["Name"])]
    prof = {101: (900, 0.045, 32, 0.05), 102: (500, 0.05, 25, 0.06), 103: (4000, 0.012, 18, 0.03),
            104: (3000, 0.008, 11, 0.015)}
    daily = []
    d = start
    while d <= end:
        k = 1 + 0.25 * (d.weekday() < 5)
        for cid in ids:
            imp0, ctr, cpc, cr = prof[cid]
            for net in (["SEARCH"] if cid in (101, 102) else ["SEARCH", "AD_NETWORK"] if cid == 103 else ["AD_NETWORK"]):
                imp = int(imp0 * k * rnd.uniform(.7, 1.3) * (1.4 if net == "AD_NETWORK" else 1))
                cl = int(imp * ctr * rnd.uniform(.7, 1.3))
                cost = round(cl * cpc * rnd.uniform(.85, 1.2), 2)
                if d == end and cid == 104:
                    cost *= 2.4
                conv = sum(1 for _ in range(cl) if rnd.random() < cr)
                daily.append({"Date": d.isoformat(), "CampaignId": str(cid), "AdNetworkType": net,
                              "Impressions": str(imp), "Clicks": str(cl), "Cost": str(cost),
                              "_conv": conv, "_rev": 0})
        d += dt.timedelta(days=1)
    kws = []
    kw_meta = []
    words = {101: ["корсет для спины", "бандаж послеоперационный", "корсет грудопоясничный", "бандаж на коленный сустав",
                   "корсет купить барнаул", "бандаж для беременных"],
             102: ["ортопедические стельки", "стельки при плоскостопии", "стельки детские ортопедические",
                   "стельки купить"],
             103: ["ортопедический салон", "ортопедия барнаул", "ортопедические товары"]}
    kid = 5000
    for cid, ph in words.items():
        if cid not in ids:
            continue
        for p in ph:
            kid += 1
            cl = rnd.randint(20, 140)
            cost = round(cl * rnd.uniform(15, 40), 2)
            conv = 0 if rnd.random() < .3 else rnd.randint(1, 6)
            kws.append({"CampaignId": str(cid), "AdGroupId": str(cid * 10), "AdGroupName": "Группа 1",
                        "CriterionId": str(kid), "Criterion": p, "CriterionType": "KEYWORD",
                        "Impressions": str(cl * 25), "Clicks": str(cl), "Cost": str(cost), "_conv": conv, "_rev": 0})
            kw_meta.append({"Id": kid, "Keyword": p, "State": "ON", "CampaignId": cid, "AdGroupId": cid * 10,
                            "Bid": int(rnd.uniform(15, 45) * 1e6), "ContextBid": 0})
    queries = []
    q_list = ["корсет для спины своими руками выкройка", "бандаж послеоперационный бесплатно по полису",
              "корсет для спины купить барнаул", "бандаж на коленный сустав отзывы врачей", "корсет грудопоясничный цена",
              "стельки при плоскостопии как сделать дома", "стельки ортопедические wildberries", "стельки ортопедические на ozon",
              "ортопедический салон рядом", "ортопедия барнаул ленина", "корсет для собаки", "бандаж для собаки после операции",
              "стельки ортопедические вакансии", "ортопедический матрас", "ортопедический матрас 160х200"]
    for q in q_list:
        cid = 101 if ("корсет" in q or "бандаж" in q) else 102 if "стельки" in q else 103
        cl = rnd.randint(3, 40)
        conv = 0 if any(w in q for w in ("своими", "собак", "вакансии", "матрас", "wildberries", "ozon", "отзывы",
                                         "как сделать")) else rnd.randint(1, 3)
        queries.append({"CampaignId": str(cid), "AdGroupId": str(cid * 10), "Query": q, "Impressions": str(cl * 20),
                        "Clicks": str(cl), "Cost": str(round(cl * rnd.uniform(25, 45), 2)), "_conv": conv, "_rev": 0})
    places = []
    for pl, cl, conv in [("dzen.ru", 220, 4), ("mail.ru", 160, 2), ("com.example.puzzle.game", 410, 0),
                         ("vk.com", 140, 3), ("free-vpn.app", 260, 0), ("weather-widget.example", 95, 0),
                         ("avito.ru", 80, 2), ("solitaire.mobile.app", 180, 0)]:
        for cid in (103, 104):
            if cid in ids:
                c2 = int(cl * rnd.uniform(.5, 1.1))
                places.append({"CampaignId": str(cid), "Placement": pl, "Impressions": str(c2 * (12 if conv else 6)),
                               "Clicks": str(c2), "Cost": str(round(c2 * rnd.uniform(6, 14), 2)),
                               "_conv": conv if cid == 103 else max(0, conv - 1), "_rev": 0})
    devices = []
    for cid in ids:
        for dev, share, crk in (("DESKTOP", .35, 1.0), ("MOBILE", .6, .45 if cid in (101, 104) else .9),
                                ("TABLET", .05, .8)):
            cl = int(prof[cid][0] * prof[cid][1] * 30 * share)
            devices.append({"CampaignId": str(cid), "Device": dev, "Impressions": str(cl * 30), "Clicks": str(cl),
                            "Cost": str(round(cl * prof[cid][2], 2)),
                            "_conv": int(cl * prof[cid][3] * crk), "_rev": 0})
    return {"end": end.isoformat(), "start": start.isoformat(), "a_start": a_start.isoformat(), "errors": [],
            "campaigns": camps, "included_ids": ids, "daily": daily, "keywords": kws, "queries": queries,
            "placements": places, "devices": devices, "kw_meta": kw_meta,
            "modifiers": [{"Id": 9001, "CampaignId": 101, "Type": "MOBILE_ADJUSTMENT",
                           "MobileAdjustment": {"BidModifier": 100}}]}


# ---------------------------------------------------------------- анализ
STOP = set("""и в во на с со по для от до из к ко у о об за не ни как что где это или а но же ли бы
то так при над под без про через ваш ваше наш мой твой его ее их купить цена цены стоимость сколько""".split())


def strategy_of(c):
    sub = c.get("TextCampaign") or c.get("UnifiedCampaign") or {}
    bs = sub.get("BiddingStrategy") or {}
    s = (bs.get("Search") or {}).get("BiddingStrategyType", "")
    n = (bs.get("Network") or {}).get("BiddingStrategyType", "")
    manual = s in ("HIGHEST_POSITION", "IMPRESSIONS_BELOW_SEARCH", "LOWEST_COST", "LOWEST_COST_PREMIUM")
    names = {"HIGHEST_POSITION": "ручная", "SERVING_OFF": "выкл", "NETWORK_DEFAULT": "как на поиске",
             "MAXIMUM_COVERAGE": "ручная", "WB_MAXIMUM_CLICKS": "авто: макс. клики",
             "WB_MAXIMUM_CONVERSION_RATE": "авто: макс. конверсии", "AVERAGE_CPC": "авто: средняя цена клика",
             "AVERAGE_CPA": "авто: средняя цена конверсии", "PAY_FOR_CONVERSION": "авто: оплата за конверсии",
             "AVERAGE_CRR": "авто: доля рекламных расходов", "PAY_FOR_CONVERSION_CRR": "авто: оплата за конверсии (ДРР)",
             "WEEKLY_CLICK_PACKAGE": "авто: пакет кликов", "AVERAGE_ROI": "авто: рентабельность"}
    return manual, "Поиск — %s; Сети — %s" % (names.get(s, s or "—"), names.get(n, n or "—"))


def agg(rows, key):
    out = {}
    for r in rows:
        k = key(r)
        a = out.setdefault(k, {"imp": 0, "clicks": 0, "cost": 0.0, "conv": 0.0, "rev": 0.0})
        a["imp"] += _num(r.get("Impressions"))
        a["clicks"] += _num(r.get("Clicks"))
        a["cost"] += _num(r.get("Cost"))
        a["conv"] += r.get("_conv", 0)
        a["rev"] += r.get("_rev", 0)
    return out


def pid(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def analyze(raw):
    W = float(CFG["WASTE_MIN_COST"])
    camps = {c["Id"]: c for c in raw.get("campaigns", [])}
    inc = set(raw.get("included_ids", []))
    cinfo = {}
    for cid, c in camps.items():
        manual, strat = strategy_of(c)
        cinfo[str(cid)] = {"name": c["Name"], "type": c.get("Type", ""), "state": c.get("State", ""),
                           "manual": manual, "strategy": strat, "included": cid in inc}
    excluded = [c["Name"] for cid, c in camps.items() if cid not in inc]

    a_start = raw["a_start"]
    period = [r for r in raw.get("daily", []) if r["Date"] >= a_start]
    tot = agg(period, lambda r: 1).get(1, {"cost": 0, "conv": 0, "clicks": 0})
    has_conv = tot["conv"] > 0
    target_cpa = float(CFG["TARGET_CPA"]) or (tot["cost"] / tot["conv"] if has_conv else 0)

    props = []
    notes = []
    if not has_conv:
        notes.append("За %d дней в Директе нет ни одной конверсии. Без целей бот не может отличить полезные клики "
                     "от пустых, поэтому предложения по ставкам и остановке фраз не формируются. "
                     "Проверьте, что в кампаниях указан счётчик Метрики и ключевые цели (или заполните GOALS в config.py)."
                     % CFG["ANALYSIS_DAYS"])

    # 1. площадки РСЯ
    site_stats = agg(raw.get("placements", []), lambda r: (r["CampaignId"], r["Placement"]))
    placements_tbl = []
    for (cid, pl), a in sorted(site_stats.items(), key=lambda x: -x[1]["cost"]):
        ctr = a["clicks"] / a["imp"] if a["imp"] else 0
        placements_tbl.append([cid, pl, a["imp"], a["clicks"], a["cost"], a["conv"], ctr])
        c = camps.get(int(cid))
        already = set(((c or {}).get("ExcludedSites") or {}).get("Items") or [])
        if pl in already or not pl or pl.lower() in ("yandex.ru", "ya.ru", "--"):
            continue
        reason = None
        if a["conv"] == 0 and a["cost"] >= W:
            reason = "потрачено %s ₽, конверсий нет" % rub(a["cost"])
        elif a["conv"] == 0 and a["clicks"] >= 20 and ctr >= 0.10:
            reason = "CTR %.0f%% при %d кликах и нуле конверсий — похоже на случайные / ботовые клики" % (ctr * 100, a["clicks"])
        if reason:
            props.append({"id": pid("site", cid, pl), "type": "exclude_site", "campaign_id": int(cid),
                          "title": "Исключить площадку %s" % pl, "object": pl, "reason": reason,
                          "saving": round(a["cost"], 2), "params": {"site": pl}})

    # 2. минус-слова
    kw_words = {}
    for k in raw.get("kw_meta", []):
        for w in words_of(k.get("Keyword", "")):
            kw_words.setdefault(str(k["CampaignId"]), set()).add(w)
    q_by_c = {}
    for q in raw.get("queries", []):
        q_by_c.setdefault(q["CampaignId"], []).append(q)
    queries_tbl = []
    for cid, qs in q_by_c.items():
        good_words = set()
        for q in qs:
            if q["_conv"] > 0:
                good_words |= words_of(q["Query"])
        wstat = {}
        for q in qs:
            queries_tbl.append([cid, q["Query"], _num(q["Impressions"]), _num(q["Clicks"]), _num(q["Cost"]), q["_conv"]])
            if q["_conv"] > 0:
                continue
            for w in words_of(q["Query"]):
                s = wstat.setdefault(w, {"cost": 0.0, "clicks": 0, "queries": []})
                s["cost"] += _num(q["Cost"])
                s["clicks"] += _num(q["Clicks"])
                s["queries"].append(q["Query"])
        c = camps.get(int(cid)) or {}
        have = set(x.lower().lstrip("-!+") for x in ((c.get("NegativeKeywords") or {}).get("Items") or []))
        protect = kw_words.get(cid, set()) | good_words | have | STOP
        ok_word = lambda w: w not in protect and len(w) >= 3  # noqa
        covered = set()
        # а) слово встречается в нескольких «пустых» запросах
        for w, s in sorted(wstat.items(), key=lambda x: -x[1]["cost"]):
            if not ok_word(w) or len(set(s["queries"])) < 2 or s["cost"] < W / 2:
                continue
            covered |= set(s["queries"])
            props.append({"id": pid("neg", cid, w), "type": "add_negative", "campaign_id": int(cid),
                          "title": "Минус-слово «-%s»" % w, "object": w,
                          "reason": "%d запросов без конверсий, %s ₽: %s" % (
                              len(set(s["queries"])), rub(s["cost"]), "; ".join(sorted(set(s["queries"]))[:4])),
                          "saving": round(s["cost"], 2), "params": {"words": [w]}})
        # б) один дорогой «пустой» запрос — его отличительные слова одним предложением
        for q in qs:
            cost = _num(q["Cost"])
            if q["_conv"] > 0 or cost < W or q["Query"] in covered:
                continue
            ws = sorted(w for w in words_of(q["Query"]) if ok_word(w))
            if not ws:
                continue
            props.append({"id": pid("negq", cid, q["Query"]), "type": "add_negative", "campaign_id": int(cid),
                          "title": ("Минус-слова %s" if len(ws) > 1 else "Минус-слово %s") % " ".join("«-%s»" % w for w in ws), "object": q["Query"],
                          "reason": "запрос «%s»: %d кликов, %s ₽, конверсий нет. Не отмечайте, если какое-то из "
                                    "слов встречается в нужных вам запросах" % (q["Query"], _num(q["Clicks"]), rub(cost)),
                          "saving": round(cost, 2), "params": {"words": ws}})

    # 3–4. фразы: остановка и ставки
    kw_stat = agg(raw.get("keywords", []), lambda r: r["CriterionId"])
    kw_name = {r["CriterionId"]: (r["CampaignId"], r["Criterion"], r.get("AdGroupName", "")) for r in raw.get("keywords", [])}
    meta = {str(k["Id"]): k for k in raw.get("kw_meta", [])}
    keywords_tbl = []
    step = float(CFG["BID_STEP"]) / 100
    for kid, a in sorted(kw_stat.items(), key=lambda x: -x[1]["cost"]):
        cid, phrase, grp = kw_name[kid]
        m = meta.get(kid)
        bid = (m or {}).get("Bid") or 0
        cpa = a["cost"] / a["conv"] if a["conv"] else 0
        keywords_tbl.append([cid, grp, phrase, a["imp"], a["clicks"], a["cost"], a["conv"], cpa,
                             bid / 1e6 if bid else None])
        if not has_conv or not m or m.get("State") == "SUSPENDED":
            continue
        manual = cinfo.get(cid, {}).get("manual")
        if a["conv"] == 0 and a["cost"] >= 2 * W and a["clicks"] >= 15:
            props.append({"id": pid("stop", kid), "type": "suspend_keyword", "campaign_id": int(cid),
                          "title": "Остановить фразу «%s»" % phrase, "object": phrase,
                          "reason": "%d кликов, %s ₽, ни одной конверсии" % (a["clicks"], rub(a["cost"])),
                          "saving": round(a["cost"], 2), "params": {"keyword_id": int(kid)}})
        elif manual and bid and a["conv"] >= 1 and target_cpa and cpa > 1.5 * target_cpa:
            new = max(300000, int(bid * (1 - step) / 10000) * 10000)
            props.append({"id": pid("bid", kid, bid, new), "type": "set_bid", "campaign_id": int(cid),
                          "title": "Снизить ставку «%s»: %s → %s ₽" % (phrase, rub(bid / 1e6, 2), rub(new / 1e6, 2)),
                          "object": phrase,
                          "reason": "цена конверсии %s ₽ — в %.1f раза выше целевой %s ₽" % (rub(cpa), cpa / target_cpa, rub(target_cpa)),
                          "saving": round(a["cost"] * step, 2),
                          "params": {"keyword_id": int(kid), "old_bid": bid, "new_bid": new}})
        elif manual and bid and a["conv"] >= 3 and target_cpa and cpa < 0.5 * target_cpa:
            new = int(bid * (1 + step) / 10000) * 10000
            props.append({"id": pid("bid", kid, bid, new), "type": "set_bid", "campaign_id": int(cid),
                          "title": "Поднять ставку «%s»: %s → %s ₽" % (phrase, rub(bid / 1e6, 2), rub(new / 1e6, 2)),
                          "object": phrase,
                          "reason": "%d конверсий по %s ₽ — вдвое дешевле целевой %s ₽, можно забрать больше трафика" % (
                              a["conv"], rub(cpa), rub(target_cpa)),
                          "saving": 0, "params": {"keyword_id": int(kid), "old_bid": bid, "new_bid": new}})

    # 5. мобильные
    dev = agg(raw.get("devices", []), lambda r: (r["CampaignId"], r["Device"]))
    mods = {str(m["CampaignId"]): m for m in raw.get("modifiers", [])}
    devices_tbl = []
    for cid in sorted(set(k[0] for k in dev)):
        mob, desk = dev.get((cid, "MOBILE")), dev.get((cid, "DESKTOP"))
        for name, a in (("Компьютеры", desk), ("Смартфоны", mob), ("Планшеты", dev.get((cid, "TABLET")))):
            if a:
                devices_tbl.append([cid, name, a["imp"], a["clicks"], a["cost"], a["conv"],
                                    a["cost"] / a["conv"] if a["conv"] else 0])
        if not (has_conv and mob and desk and desk["conv"] >= 2 and mob["cost"] >= 2 * W):
            continue
        dcpa = desk["cost"] / desk["conv"]
        mcpa = mob["cost"] / mob["conv"] if mob["conv"] else float("inf")
        if mcpa < 1.5 * dcpa:
            continue
        m = mods.get(cid)
        cur = ((m or {}).get("MobileAdjustment") or {}).get("BidModifier", 100)
        want = int(max(CFG["MOBILE_MIN"], min(90, cur * dcpa / mcpa if mob["conv"] else CFG["MOBILE_MIN"])) / 5) * 5
        if want >= cur:
            continue
        props.append({"id": pid("mob", cid, cur, want), "type": "mobile_adjust", "campaign_id": int(cid),
                      "title": "Ставки на смартфонах: %d%% → %d%%" % (cur, want), "object": "смартфоны",
                      "reason": "конверсия со смартфонов %s, с компьютеров %s ₽" % (
                          ("%s ₽" % rub(mcpa)) if mob["conv"] else "не было ни одной (потрачено %s ₽)" % rub(mob["cost"]), rub(dcpa)),
                      "saving": round(mob["cost"] * (1 - want / cur), 2),
                      "params": {"old": cur, "new": want, "modifier_id": (m or {}).get("Id")}})

    for p in props:
        p["campaign"] = cinfo.get(str(p["campaign_id"]), {}).get("name", str(p["campaign_id"]))
        p["created"] = raw["end"]
    # уже применённые не повторяем
    done = set()
    for a in read_json(ACTIONS_LOG, []):
        if a.get("status") == "ok" and not a.get("undone"):
            done.add(a.get("proposal_id"))
    props = [p for p in props if p["id"] not in done]
    order = {"exclude_site": 0, "add_negative": 1, "suspend_keyword": 2, "set_bid": 3, "mobile_adjust": 4}
    props.sort(key=lambda p: (order[p["type"]], -p["saving"]))

    # компактные данные по дням для дашборда
    daily = []
    for r in raw.get("daily", []):
        daily.append([r["Date"], r["CampaignId"], "S" if r["AdNetworkType"] == "SEARCH" else "N",
                      int(_num(r["Impressions"])), int(_num(r["Clicks"])), round(_num(r["Cost"]), 2),
                      round(r["_conv"], 2), round(r["_rev"], 2)])

    alerts = make_alerts(raw, cinfo)
    return {
        "generated": (now_utc() + TZ_SHIFT).strftime("%d.%m.%Y %H:%M"),
        "version": VERSION, "demo": DEMO, "end": raw["end"], "start": raw["start"], "a_start": a_start,
        "analysis_days": CFG["ANALYSIS_DAYS"], "target_cpa": round(target_cpa, 2), "has_conv": has_conv,
        "campaigns": cinfo, "excluded": excluded, "daily": daily, "proposals": props,
        "alerts": alerts, "notes": notes, "errors": raw.get("errors", []),
        "tables": {"keywords": keywords_tbl, "queries": queries_tbl, "placements": placements_tbl,
                   "devices": devices_tbl},
    }


def words_of(text):
    return set(w for w in re.findall(r"[a-zа-яё0-9]+", (text or "").lower().replace("ё", "е")) if not w.isdigit())


def rub(v, digits=0):
    s = ("{:,.%df}" % digits).format(v).replace(",", " ")
    return s.replace(".", ",")


def make_alerts(raw, cinfo):
    al = []
    end = raw["end"]
    by_day = agg(raw.get("daily", []), lambda r: r["Date"])
    days = sorted(by_day)
    if not days:
        return al
    y = by_day.get(end, {"cost": 0, "conv": 0, "clicks": 0, "imp": 0})
    prev = [by_day[d] for d in days if d < end][-7:]
    if prev:
        avg = sum(p["cost"] for p in prev) / len(prev)
        if avg > 0 and y["cost"] == 0:
            al.append(["critical", "Вчера расход 0 ₽ (обычно ~%s ₽ в день). Возможно, закончились деньги на счёте "
                                   "или кампании остановлены." % rub(avg)])
        elif avg > 0 and abs(y["cost"] - avg) / avg * 100 >= CFG["ALERT_COST_JUMP"]:
            al.append(["warning", "Вчера расход %s ₽ — на %+.0f%% от среднего за 7 дней (%s ₽)." % (
                rub(y["cost"]), (y["cost"] - avg) / avg * 100, rub(avg))])
    # по кампаниям: неделя к неделе
    by_cd = agg(raw.get("daily", []), lambda r: (r["CampaignId"], r["Date"]))
    d_end = dt.datetime.strptime(end, "%Y-%m-%d").date()
    w1 = set((d_end - dt.timedelta(days=i)).isoformat() for i in range(7))
    w0 = set((d_end - dt.timedelta(days=i)).isoformat() for i in range(7, 14))
    for cid, info in cinfo.items():
        if not info["included"]:
            continue
        a1 = {"cost": 0, "conv": 0, "clicks": 0, "imp": 0}
        a0 = dict(a1)
        for (c, d), v in by_cd.items():
            if c != cid:
                continue
            tgt = a1 if d in w1 else a0 if d in w0 else None
            if tgt is not None:
                for k in tgt:
                    tgt[k] += v[k]
        nm = info["name"]
        if a1["cost"] >= 3 * CFG["WASTE_MIN_COST"] and a1["conv"] == 0 and a0["conv"] > 0:
            al.append(["serious", "«%s»: за 7 дней %s ₽ и ни одной конверсии (неделей раньше было %d)." % (
                nm, rub(a1["cost"]), a0["conv"])])
        if a1["conv"] >= 2 and a0["conv"] >= 2:
            c1, c0 = a1["cost"] / a1["conv"], a0["cost"] / a0["conv"]
            if c1 > c0 * 1.4:
                al.append(["warning", "«%s»: цена конверсии выросла с %s до %s ₽ за неделю." % (nm, rub(c0), rub(c1))])
        if a1["imp"] > 500 and a0["imp"] > 500:
            t1, t0 = a1["clicks"] / a1["imp"], a0["clicks"] / a0["imp"]
            if t0 and t1 < t0 * 0.7:
                al.append(["warning", "«%s»: CTR упал с %.2f%% до %.2f%% — проверьте объявления и конкурентов." % (
                    nm, t0 * 100, t1 * 100)])
    return al


# ---------------------------------------------------------------- вывод
def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def write_excel(model, path):
    cn = lambda cid: model["campaigns"].get(str(cid), {}).get("name", cid)  # noqa
    camp = {}
    for d, cid, net, imp, cl, cost, conv, rev in model["daily"]:
        if d < model["a_start"]:
            continue
        a = camp.setdefault(cid, [0, 0, 0.0, 0.0])
        a[0] += imp; a[1] += cl; a[2] += cost; a[3] += conv  # noqa
    rows_c = []
    for cid, (imp, cl, cost, conv) in sorted(camp.items(), key=lambda x: -x[1][2]):
        i = model["campaigns"].get(cid, {})
        rows_c.append([i.get("name", cid), i.get("strategy", ""), imp, cl, (cl / imp if imp else 0),
                       (cost / cl if cl else 0), cost, conv, (conv / cl if cl else 0), (cost / conv if conv else None)])
    by_day = {}
    for d, cid, net, imp, cl, cost, conv, rev in model["daily"]:
        a = by_day.setdefault(d, [0, 0, 0.0, 0.0])
        a[0] += imp; a[1] += cl; a[2] += cost; a[3] += conv  # noqa
    rows_d = [[d, a[0], a[1], a[2], a[3], (a[2] / a[3] if a[3] else None)] for d, a in sorted(by_day.items(), reverse=True)]
    T = model["tables"]
    tp = {"exclude_site": "Исключить площадку", "add_negative": "Минус-слово", "suspend_keyword": "Остановить фразу",
          "set_bid": "Ставка", "mobile_adjust": "Корректировка смартфонов"}
    sheets = [
        ("Предложения", [("Действие", None), ("Кампания", None), ("Что", None), ("Почему", None), ("Экономия, ₽", "money")],
         [[tp[p["type"]], p["campaign"], p["title"], p["reason"], p["saving"]] for p in model["proposals"]]),
        ("Кампании за %d дн" % model["analysis_days"],
         [("Кампания", None), ("Стратегия", None), ("Показы", "int"), ("Клики", "int"), ("CTR", "pct"), ("Цена клика", "money"),
          ("Расход", "money"), ("Конверсии", "int"), ("CR", "pct"), ("Цена конверсии", "money")], rows_c),
        ("По дням", [("Дата", None), ("Показы", "int"), ("Клики", "int"), ("Расход", "money"), ("Конверсии", "int"),
                     ("Цена конверсии", "money")], rows_d),
        ("Фразы", [("Кампания", None), ("Группа", None), ("Фраза", None), ("Показы", "int"), ("Клики", "int"),
                   ("Расход", "money"), ("Конверсии", "int"), ("Цена конверсии", "money"), ("Ставка", "money")],
         [[cn(r[0])] + r[1:7] + [r[7] or None, r[8]] for r in T["keywords"]]),
        ("Поисковые запросы", [("Кампания", None), ("Запрос", None), ("Показы", "int"), ("Клики", "int"),
                               ("Расход", "money"), ("Конверсии", "int")],
         [[cn(r[0])] + r[1:] for r in sorted(T["queries"], key=lambda r: -r[4])]),
        ("Площадки РСЯ", [("Кампания", None), ("Площадка", None), ("Показы", "int"), ("Клики", "int"),
                          ("Расход", "money"), ("Конверсии", "int"), ("CTR", "pct")],
         [[cn(r[0])] + r[1:] for r in T["placements"]]),
        ("Устройства", [("Кампания", None), ("Устройство", None), ("Показы", "int"), ("Клики", "int"),
                        ("Расход", "money"), ("Конверсии", "int"), ("Цена конверсии", "money")],
         [[cn(r[0])] + r[1:6] + [r[6] or None] for r in T["devices"]]),
    ]
    ai = model.get("ai") or {}
    if ai.get("recommendations"):
        prio = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}
        sheets.append((
            "ИИ-рекомендации",
            [("Приоритет", None), ("Категория", None), ("Рекомендация", None), ("Подробно", None),
             ("Ожидаемый эффект", None)],
            [[prio.get(r.get("priority"), r.get("priority")), r.get("category"), r.get("title"),
              r.get("details"), r.get("expected_effect")] for r in ai["recommendations"]]))
    write_xlsx(path, sheets)


def htpasswd_line(user, pwd):
    return "%s:{SHA}%s\n" % (user, base64.b64encode(hashlib.sha1(pwd.encode("utf-8")).digest()).decode())


def publish(model):
    out = CFG["OUTPUT_DIR"]
    os.makedirs(out, exist_ok=True)
    stamp = model["end"]
    xname = "direct_%s.xlsx" % stamp
    try:
        write_excel(model, os.path.join(out, xname))
        # при включённом входе Excel отдаёт гейт index.php (прямой доступ к .xlsx закрыт)
        model["excel"] = ("?xls=" + xname) if (CFG["REPORT_USER"] and CFG["REPORT_PASSWORD"]) else xname
    except Exception as e:
        model["errors"].append("Excel не записан: %s" % e)
        log(traceback.format_exc())
    # старые Excel удаляем
    for f in os.listdir(out):
        if f.startswith("direct_") and f.endswith(".xlsx"):
            p = os.path.join(out, f)
            if time.time() - os.path.getmtime(p) > CFG["KEEP_EXCEL_DAYS"] * 86400:
                os.remove(p)

    # настройки для apply.php — лежат рядом со скриптом, НЕ в публичной папке
    st = read_json(APPLY_SETTINGS, {})
    if not st.get("key"):
        st["key"] = secrets.token_hex(16)
    st.update({"token": CFG["DIRECT_TOKEN"], "client_login": CFG["CLIENT_LOGIN"], "api": api_base(),
               "proposals": PROPOSALS, "log": ACTIONS_LOG, "max_age_days": 4, "demo": DEMO,
               "report_user": CFG["REPORT_USER"],
               "report_pass_hash": hashlib.sha256(CFG["REPORT_PASSWORD"].encode("utf-8")).hexdigest()
               if CFG["REPORT_PASSWORD"] else ""})
    write_json(APPLY_SETTINGS, st)
    try:
        os.chmod(APPLY_SETTINGS, 0o600)
    except Exception:
        pass
    write_json(PROPOSALS, {"created": model["end"], "items": {p["id"]: p for p in model["proposals"]}})

    tpl = open(os.path.join(HERE, "apply_template.php"), encoding="utf-8").read()
    with open(os.path.join(out, "apply.php"), "w", encoding="utf-8") as f:
        f.write(tpl.replace("__SETTINGS_PATH__", APPLY_SETTINGS.replace("\\", "/")))

    # ИИ-советник: настройки для ai.php — рядом со скриптом, НЕ в публичной папке
    ai_tpl = os.path.join(HERE, "ai_template.php")
    if os.path.exists(ai_tpl):
        if CFG.get("AI_ENABLED"):
            ai_st = {"key": st["key"], "base_url": CFG.get("AI_BASE_URL", ""),
                     "api_key": CFG.get("AI_API_KEY", ""), "model": CFG.get("AI_MODEL", ""),
                     "scheme": CFG.get("AI_AUTH_SCHEME") or "Bearer",
                     "max_tokens": int(CFG.get("AI_MAX_TOKENS") or 2000),
                     "timeout": int(CFG.get("AI_TIMEOUT") or 120),
                     "payload": AI_PAYLOAD.replace("\\", "/"), "cache": AI_CACHE.replace("\\", "/")}
            write_json(AI_SETTINGS, ai_st)
            try:
                os.chmod(AI_SETTINGS, 0o600)
            except Exception:
                pass
        with open(os.path.join(out, "ai.php"), "w", encoding="utf-8") as f:
            f.write(open(ai_tpl, encoding="utf-8").read().replace("__SETTINGS_PATH__", AI_SETTINGS.replace("\\", "/")))

    model["apply_key"] = st["key"]
    model["actions"] = read_json(ACTIONS_LOG, [])[-200:]
    html = open(os.path.join(HERE, "dashboard_template.html"), encoding="utf-8").read()
    data = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
    page = html.replace("/*__DATA__*/null", data)

    hta = os.path.join(out, ".htaccess")
    if CFG["REPORT_USER"] and CFG["REPORT_PASSWORD"]:
        # вход через форму на странице (index.php + подписанная cookie на 30 дней)
        gate = open(os.path.join(HERE, "gate_template.php"), encoding="utf-8").read().replace(
            "__SETTINGS_PATH__", APPLY_SETTINGS.replace("\\", "/"))
        with open(os.path.join(out, "index.php"), "w", encoding="utf-8") as f:
            f.write(gate + page)
        with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
            f.write('<!doctype html><meta http-equiv="refresh" content="0; url=index.php">'
                    '<a href="index.php">Перейти к отчёту</a>\n')
        with open(hta, "w", encoding="utf-8") as f:
            f.write('DirectoryIndex index.php index.html\nOptions -Indexes\n'
                    '<FilesMatch "\\.xlsx$">\nRequire all denied\n</FilesMatch>\n'
                    '<FilesMatch "\\.(html|php)$">\nHeader set Cache-Control "no-store"\n</FilesMatch>\n')
    else:
        with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
            f.write(page)
        old_gate = os.path.join(out, "index.php")
        if os.path.exists(old_gate):
            os.remove(old_gate)
        with open(hta, "w", encoding="utf-8") as f:
            f.write("Options -Indexes\n")
    log("Готово: %s (%d предложений)" % (os.path.join(out, "index.html"), len(model["proposals"])))


def main():
    global CFG
    CFG = load_config()
    log("=== Директ-аналитика %s%s ===" % (VERSION, " (ДЕМО)" if DEMO else ""))
    if not DEMO and not CFG["DIRECT_TOKEN"]:
        log("В config.py не заполнен DIRECT_TOKEN.")
        sys.exit(1)
    if not DEMO and not (CFG["REPORT_USER"] and CFG["REPORT_PASSWORD"]) and "public_html" in CFG["OUTPUT_DIR"]:
        log("ВНИМАНИЕ: страница с кнопкой «Применить» лежит в интернете без пароля. "
            "Заполните REPORT_USER и REPORT_PASSWORD в config.py.")
    try:
        raw = demo_collect() if DEMO else collect()
        if not DEMO:
            write_json(CACHE, raw)
    except Exception as e:
        log("Не удалось получить данные: %s" % e)
        raw = read_json(CACHE, None)
        if not raw:
            raise
        raw.setdefault("errors", []).insert(0, "Свежие данные не получены (%s). Показаны данные прошлого запуска." % e)
    model = analyze(raw)
    # ИИ-советник: ночью только готовим тексты сводок (ai_payload.json) —
    # нейросеть вызывается по кнопке в дашборде (ai.php), токены впустую не тратятся
    model["ai"] = None
    try:
        import ai_advisor
        if DEMO:
            model["ai"] = ai_advisor.demo_block(model)
        elif CFG.get("AI_ENABLED") and CFG.get("AI_API_KEY"):
            payload = ai_advisor.build_payload(model, CFG)
            write_json(AI_PAYLOAD, payload)
            model["ai"] = {"available": True, "periods": payload["periods"], "date": payload["date"]}
            log("ИИ-советник: сводки готовы (периоды %s дн), анализ — по кнопке в дашборде"
                % ", ".join(str(p) for p in payload["periods"]))
    except Exception as e:
        log("ИИ-советник пропущен: %s" % e)
    publish(model)


if __name__ == "__main__":
    main()
