# -*- coding: utf-8 -*-
"""
ИИ-советник для аналитики Яндекс Директа.

Отправляет компактную сводку статистики в нейросеть через OpenAI-совместимый
API (chat/completions) и возвращает экспертный разбор с рекомендациями.

Поддерживаемые провайдеры (задаются в config.py):
  - YandexGPT:  AI_BASE_URL = "https://llm.api.cloud.yandex.net/v1",
                AI_AUTH_SCHEME = "Api-Key",
                AI_MODEL = "gpt://<folder_id>/yandexgpt-lite/latest"
  - Kimi (Moonshot): AI_BASE_URL = "https://api.moonshot.ai/v1",
                AI_AUTH_SCHEME = "Bearer", AI_MODEL = "kimi-k2-0905-preview"
  - DeepSeek:   AI_BASE_URL = "https://api.deepseek.com/v1",
                AI_AUTH_SCHEME = "Bearer", AI_MODEL = "deepseek-chat"
  - OpenAI:     AI_BASE_URL = "https://api.openai.com/v1",
                AI_AUTH_SCHEME = "Bearer", AI_MODEL = "gpt-4o-mini"

Работает на стандартной библиотеке Python 3.6+.
Любой сбой (нет сети, нет ключа, плохой ответ) не роняет основной отчёт:
возвращается {"error": "..."} и дашборд просто показывает причину.
"""
import json
import re
import urllib.error
import urllib.request

# ------------------------------------------------------------------ промпт
SYSTEM_PROMPT = """Ты — старший эксперт по контекстной рекламе в Яндекс Директе (уровень ведущего медиабаера агентства, 10+ лет практики, медицинская тематика и e-commerce).

Тебе дают сводку статистики аккаунта Яндекс Директа. Проанализируй её как эксперт, а не как автомат:
- ищи неочевидные закономерности (дни недели, устройства, качество трафика, площадки, поиск vs РСЯ, сезонность спроса);
- оценивай экономику: цена конверсии против целевой, доля бесполезного расхода, куда перелить бюджет;
- оценивай настройки кампаний: стратегии, минус-слова, ставки, корректировки;
- отделяй шум от сигнала: маленькая выборка — не вывод, скажи прямо, когда данных мало;
- ничего не выдумывай: опирайся только на цифры из сводки. Если каких-то данных нет — скажи, что посмотреть в интерфейсе Директа.

Рекомендации давай конкретные и выполнимые: что именно сделать в каком разделе Директа, с ожидаемым эффектом. Приоритизируй по соотношению эффект/риск. Сначала деньги (остановка слива), потом усиление того, что работает, потом гигиена аккаунта.

Ответ строго одним JSON-объектом без markdown и пояснений вокруг:
{
  "summary": "3-6 предложений: общая оценка аккаунта, главная проблема и главная возможность",
  "recommendations": [
    {
      "title": "короткий заголовок действия",
      "details": "2-5 предложений: что сделать, где в интерфейсе Директа, почему — с цифрами из сводки",
      "priority": "high|medium|low",
      "category": "бюджет|ставки|минус-слова|площадки|стратегия|объявления|структура|цели|другое",
      "expected_effect": "ожидаемый эффект (руб/проценты/качество трафика)"
    }
  ],
  "watchlist": ["за чем следить на этой неделе — короткие пункты"]
}
До 8 рекомендаций, только значимые. Если данных для вывода недостаточно — так и напиши в summary и дай 1-3 рекомендации по сбору данных."""

# ------------------------------------------------------------------ сводка
def _rub(v):
    return ("%s" % round(v, 2)).rstrip("0").rstrip(".")

def _cpa(cost, conv):
    return round(cost / conv, 2) if conv else None

def build_summary(model, cfg):
    """Компактная текстовая сводка модели для нейросети (~4-6 КБ)."""
    L = []
    a_days = model.get("analysis_days", 30)
    L.append("Период анализа: %s … %s (история с %s). Конверсии считаются по целям: %s." % (
        model.get("a_start"), model.get("end"), model.get("start"),
        ", ".join(str(g) for g in cfg.get("GOALS") or []) or "ключевые цели кампаний"))
    if model.get("target_cpa"):
        L.append("Целевая цена конверсии: %s ₽." % _rub(model["target_cpa"]))
    else:
        L.append("Целевая цена конверсии не задана.")

    # итоги и динамика по дням (последние 14 дней)
    a_start = model.get("a_start", "")
    by_day = {}
    for d, cid, net, imp, cl, cost, conv, rev in model.get("daily", []):
        a = by_day.setdefault(d, [0, 0, 0.0, 0.0])
        a[0] += imp; a[1] += cl; a[2] += cost; a[3] += conv
    days = sorted(by_day)
    last14 = days[-14:]
    L.append("Динамика по дням (дата: показы, клики, расход ₽, конверсии):")
    for d in last14:
        a = by_day[d]
        L.append("  %s: %d пок., %d кл., %s ₽, %s конв." % (d, a[0], a[1], _rub(a[2]), _rub(a[3])))

    # кампании за период анализа
    per = {}
    for d, cid, net, imp, cl, cost, conv, rev in model.get("daily", []):
        if d < a_start:
            continue
        a = per.setdefault(cid, {"imp": 0, "cl": 0, "cost": 0.0, "conv": 0.0, "nets": {}})
        a["imp"] += imp; a["cl"] += cl; a["cost"] += cost; a["conv"] += conv
        n = a["nets"].setdefault(net, {"cl": 0, "cost": 0.0, "conv": 0.0})
        n["cl"] += cl; n["cost"] += cost; n["conv"] += conv
    L.append("Кампании за %d дн:" % a_days)
    for cid, i in model.get("campaigns", {}).items():
        a = per.get(cid, {"imp": 0, "cl": 0, "cost": 0.0, "conv": 0.0, "nets": {}})
        ctr = a["cl"] / a["imp"] * 100 if a["imp"] else 0
        L.append("  «%s» (id %s, %s, состояние %s, стратегия: %s): показы %d, клики %d (CTR %.2f%%), "
                 "расход %s ₽, конверсии %s, цена конверсии %s." % (
                     i.get("name"), cid, i.get("type", ""), i.get("state", ""), i.get("strategy", ""),
                     a["imp"], a["cl"], ctr, _rub(a["cost"]), _rub(a["conv"]),
                     ("%s ₽" % _rub(_cpa(a["cost"], a["conv"]))) if a["conv"] else "нет конверсий"))
        for net, n in a["nets"].items():
            L.append("    %s: %d кл., %s ₽, %s конв." % (
                "Поиск" if net == "S" else "Сети(РСЯ)", n["cl"], _rub(n["cost"]), _rub(n["conv"])))
    if model.get("excluded"):
        L.append("Исключены настройками бота (не анализируются): %s." % ", ".join(model["excluded"][:10]))

    # топ-таблицы
    def top(tbl, n, cols):
        L.append(cols)
        for r in tbl[:n]:
            L.append("  " + " | ".join(str(x) for x in r))
    T = model.get("tables", {})
    cname = lambda cid: model.get("campaigns", {}).get(str(cid), {}).get("name", cid)  # noqa
    if T.get("keywords"):
        top(sorted(T["keywords"], key=lambda r: -r[5])[:15],
            15, "Топ фраз по расходу (кампания | группа | фраза | показы | клики | расход ₽ | конв. | цена конв. ₽ | ставка ₽):")
    if T.get("queries"):
        q0 = [r for r in sorted(T["queries"], key=lambda r: -r[4]) if r[5] == 0][:15]
        top(q0, 15, "Топ поисковых запросов БЕЗ конверсий (кампания | запрос | показы | клики | расход ₽ | конв.):")
        q1 = [r for r in sorted(T["queries"], key=lambda r: -r[5]) if r[5] > 0][:10]
        if q1:
            top(q1, 10, "Запросы С конверсиями (топ-10, тот же формат):")
    if T.get("placements"):
        top(sorted(T["placements"], key=lambda r: -r[4])[:12],
            12, "Площадки РСЯ по расходу (кампания | площадка | показы | клики | расход ₽ | конв. | CTR):")
    if T.get("devices"):
        top(T["devices"][:9], 9, "Устройства (кампания | устройство | показы | клики | расход ₽ | конв. | цена конв. ₽):")

    # что уже нашли правила
    if model.get("alerts"):
        L.append("Алерты бота: " + " / ".join(t for _, t in model["alerts"][:6]))
    if model.get("proposals"):
        L.append("Уже сформированные правилами предложения (дублировать не нужно, можно оценить):")
        for p in model["proposals"][:12]:
            L.append("  - %s (%s): %s" % (p.get("title"), p.get("campaign"), p.get("reason")))
    note = (cfg.get("AI_CONTEXT_NOTE") or "").strip()
    if note:
        L.append("Контекст от владельца аккаунта: " + note)
    return "\n".join(L)

# ------------------------------------------------------------------ вызов API
def _chat(base, key, scheme, model_name, messages, max_tokens, timeout):
    body = json.dumps({
        "model": model_name,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body, headers={
        "Authorization": "%s %s" % (scheme, key),
        "Content-Type": "application/json; charset=utf-8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise RuntimeError("HTTP %s от API нейросети: %s" % (e.code, detail))
    except Exception as e:
        raise RuntimeError("Нет связи с API нейросети: %s" % e)
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError("неожиданный ответ API: %s" % json.dumps(data, ensure_ascii=False)[:300])

def _parse(text):
    """Достаём JSON из ответа; если не вышло — отдаём текст как summary."""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {"summary": text, "recommendations": [], "watchlist": []}

def ask(model, cfg):
    """Главная точка входа. Возвращает блок для дашборда или {"error": ...}."""
    key = (cfg.get("AI_API_KEY") or "").strip()
    if not key:
        return {"error": "не заполнен AI_API_KEY в config.py"}
    base = (cfg.get("AI_BASE_URL") or "").strip()
    name = (cfg.get("AI_MODEL") or "").strip()
    if not base or not name:
        return {"error": "заполните AI_BASE_URL и AI_MODEL в config.py"}
    summary = build_summary(model, cfg)
    try:
        text = _chat(base, key, (cfg.get("AI_AUTH_SCHEME") or "Bearer").strip(), name,
                     [{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": summary}],
                     int(cfg.get("AI_MAX_TOKENS") or 2000), int(cfg.get("AI_TIMEOUT") or 120))
        out = _parse(text)
        out["provider"] = name
        out["tokens_sent"] = len(summary) // 3
        recs = []
        for r in out.get("recommendations") or []:
            if isinstance(r, dict) and r.get("title"):
                recs.append({"title": str(r.get("title", ""))[:200],
                             "details": str(r.get("details", ""))[:2000],
                             "priority": str(r.get("priority", "medium")).lower(),
                             "category": str(r.get("category", "другое"))[:40],
                             "expected_effect": str(r.get("expected_effect", ""))[:300]})
        out["recommendations"] = recs[:8]
        out["watchlist"] = [str(w)[:200] for w in (out.get("watchlist") or [])][:6]
        out["summary"] = str(out.get("summary", ""))[:3000]
        return out
    except Exception as e:
        return {"error": str(e)}

# ------------------------------------------------------------------ демо
def demo_block(model):
    """Пример блока для демо-режима — без обращения к сети."""
    return {
        "provider": "демо-пример",
        "summary": "Аккаунт в целом здоров: конверсии есть, но до 40% расхода уходит в площадки РСЯ и "
                   "запросы без единой конверсии. Главная проблема — мусорный трафик из мобильных приложений; "
                   "главная возможность — перелив бюджета в поисковые фразы с ценой конверсии вдвое ниже целевой.",
        "recommendations": [
            {"title": "Исключить игровые приложения из РСЯ",
             "details": "com.example.puzzle.game и solitaire.mobile.app съели ~30% недельного бюджета при нуле "
                        "конверсий и CTR выше 10% — типичные случайные тапы. Добавьте их в исключённые площадки "
                        "на уровне кампании ЕПК (Настройки → Спец. настройки → Исключённые площадки).",
             "priority": "high", "category": "площадки",
             "expected_effect": "экономия ~2 500 ₽/нед без потери конверсий"},
            {"title": "Перелить 15% бюджета из РСЯ в поиск «Корсеты и бандажи»",
             "details": "В поиске кампании 101 цена конверсии вдвое ниже целевой, при этом доля показов падает. "
                        "Поднимите недельный бюджет кампании на 15% за счёт РСЯ-кампании 104.",
             "priority": "high", "category": "бюджет",
             "expected_effect": "+3-5 конверсий/нед при том же бюджете"},
            {"title": "Добавить минус-слова по информационным запросам",
             "details": "«своими руками», «выкройка», «как сделать дома» дают ~8% кликов без конверсий. "
                        "Пользователь ищет инструкцию, а не товар. Добавьте минус-слова на кампанию.",
             "priority": "medium", "category": "минус-слова",
             "expected_effect": "рост CR на 0,3-0,5 п.п."},
        ],
        "watchlist": ["цена конверсии со смартфонов — растёт 2 недели подряд",
                      "CTR кампании 102 упал ниже 4% — обновить заголовки"],
        "demo": True,
    }
