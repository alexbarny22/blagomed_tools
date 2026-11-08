#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Воркер COM-подпроцесса для app/core/onec.py (этап 5). ТОЛЬКО ЧТЕНИЕ.

Запускается родителем (ComSource) отдельным процессом — это даёт реально
работающий таймаут: блокирующий вызов COM выполняется здесь, а родитель может
убить процесс при зависании. Секреты (пароль) принимаются через stdin JSON и
НИКОГДА не печатаются. На stdout — одна строка JSON; прогресс/ошибки — stderr.

Правила цен повторяют старый бот (legacy/blagomed_cost.py):
  1) «Цены номенклатуры поставщиков» (срез последних), вид цены и поставщик
     из payload; варианты фильтров подбираются по фактическим именам измерений;
  2) добор по отсутствующим артикулам из «Цены номенклатуры» (вид цены);
  3) дубли (артикул+наименование) схлопываются по последнему Периоду.
GUID (Ref_Key): попытка v8.GetString(ссылка), затем str(ссылка).
"""
from __future__ import annotations

import json
import sys
import time

QUERY_PRICES = """\
ВЫБРАТЬ
    Ном.Артикул КАК Артикул,
    Ном.Наименование КАК Наименование,
    Цены.Цена КАК Цена,
    Цены.Период КАК Период,
    Ном.Ссылка КАК Ссылка
ИЗ
    РегистрСведений.ЦеныНоменклатуры.СрезПоследних(
        , ВидЦены.Наименование = &ВидЦены) КАК Цены
    ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Номенклатура КАК Ном
        ПО Цены.Номенклатура = Ном.Ссылка
ГДЕ
    Ном.Артикул В(&Артикулы)
    И НЕ Ном.ПометкаУдаления
"""

QUERY_SUPPLIER_PRICES = """\
ВЫБРАТЬ
    Ном.Артикул КАК Артикул,
    Ном.Наименование КАК Наименование,
    Цены.Цена КАК Цена,
    Цены.Период КАК Период,
    Ном.Ссылка КАК Ссылка
ИЗ
    РегистрСведений.ЦеныНоменклатурыПоставщиков.СрезПоследних(
        {SREZ_ARGS}) КАК Цены
    ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Номенклатура КАК Ном
        ПО Цены.Номенклатура = Ном.Ссылка
ГДЕ
    Ном.Артикул В(&Артикулы)
    И НЕ Ном.ПометкаУдаления
"""

QUERY_SUPPLIER_STRUCTURE = """\
ВЫБРАТЬ ПЕРВЫЕ 1 *
ИЗ РегистрСведений.ЦеныНоменклатурыПоставщиков
"""


def _com_err_text(exc) -> str:
    """Понятный текст ошибки 1С из COM-исключения."""
    try:
        info = exc.excepinfo
        if info and info[2]:
            return str(info[2]).strip()
    except Exception:
        pass
    return str(exc)


def _guid_of(v8, ref):
    """GUID ссылки: перебор надёжных способов COM, отбрасываем мусор.

    Формат GUID 1С: 8-4-4-4-12 шестнадцатеричных цифр (с дефисами или без).
    Если получить не удалось — возвращаем '' (родитель пометит как
    «не извлечён», не выдумывая значения).
    """
    import re
    guid_re = re.compile(
        r'^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?'
        r'[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$')
    attempts = (
        lambda: ref.УникальныйИдентификатор().Строкой(),
        lambda: ref.УникальныйИдентификатор().Строка(),
        lambda: ref.УникальныйИдентификатор().ToString(),
        lambda: str(ref.УникальныйИдентификатор()),
        lambda: type(ref.УникальныйИдентификатор()).__name__ and
                 str(ref.УникальныйИдентификатор().Строкой()),
        lambda: v8.String(ref),
        lambda: v8.GetString(ref),
        lambda: str(ref),
    )
    for fn in attempts:
        try:
            g = str(fn()).strip()
        except Exception:
            continue
        if guid_re.match(g):
            return g
    return ''

# Прокидываемое определение _com_date_literal выше, затем _dedupe_latest.


def _com_date_literal(iso: str) -> str:
    """Строка ДАТАВРЕМЯ(год,мес,день) для запроса 1С по ISO-дате."""
    y, m, d = str(iso)[:10].split('-')
    return f'ДАТАВРЕМЯ({int(y)},{int(m)},{int(d)})'


def _dedupe_latest(rows):
    """Схлопывает дубли (артикул+наименование) по последнему Периоду."""
    best, order = {}, []
    for art, name, price, period, guid in rows:
        key = (art, name)
        if key not in best:
            best[key] = (price, period, guid)
            order.append(key)
        else:
            old_price, old_period, _ = best[key]
            try:
                newer = (old_period is None or
                         (period is not None and period > old_period))
            except Exception:
                newer = False
            if newer:
                best[key] = (price, period, guid)
    return [(a, n, *best[(a, n)]) for a, n in order]


def main() -> int:
    req = json.loads(sys.stdin.readline() or '{}')
    articles = req.get('articles') or []
    server = req['server']
    base = req['base']
    user = req['user']
    password = req.get('password')
    price_type = req.get('price_type', 'Закупочная')
    supplier = req.get('supplier', 'ГАВРЮШКИН')
    t_connect = float(req.get('timeout_connect', 300))
    t_total = t_connect + float(req.get('timeout_read', 120))

    import win32com.client
    import pythoncom
    pythoncom.CoInitialize()
    t0 = time.monotonic()
    connector = win32com.client.Dispatch('V83.COMConnector')
    conn_str = (f'Srvr="{server}";Ref="{base}";Usr="{user}";'
                f'Pwd="{password}";')
    v8 = connector.Connect(conn_str)
    t_conn = time.monotonic() - t0
    sys.stderr.write(f'connect {t_conn:.1f}s\n')
    if t_conn > t_connect:
        sys.stderr.write(f'превышен таймаут подключения {t_connect:.0f}s\n')
        return 3

    arr = v8.NewObject('Array')
    for a in articles:
        arr.Add(a)

    def run_query(text, params=None):
        q = v8.NewObject('Query')
        q.Text = text
        q.SetParameter('Артикулы', arr)
        for k, val in (params or {}).items():
            q.SetParameter(k, val)
        sel = q.Execute().Select()
        rows = []
        while sel.Next():
            art = str(sel.Артикул).strip() if sel.Артикул else ''
            name = str(sel.Наименование).strip() if sel.Наименование else ''
            price = sel.Цена
            period = str(sel.Период)[:10] if sel.Период else None
            guid = _guid_of(v8, sel.Ссылка)
            if art and price is not None:
                rows.append((art, name, price, period, guid))
        return rows

    # 1) Регистр «Цены номенклатуры поставщиков» — главный источник
    rows, cols = [], []
    try:
        qs = v8.NewObject('Query')
        qs.Text = QUERY_SUPPLIER_STRUCTURE
        res = qs.Execute()
        kolonki = res.Колонки
        for i in range(kolonki.Количество()):
            cols.append(str(kolonki.Получить(i).Имя))
    except Exception as e:
        sys.stderr.write(f'структура регистра поставщиков: '
                         f'{_com_err_text(e)[:120]}\n')

    attempts = []
    if cols:
        vid = next((c for c in cols if 'видцен' in c.lower()), None)
        sup = next((c for c in cols if 'поставщик' in c.lower()
                    and 'видцен' not in c.lower()), None)
        if vid and sup:
            attempts.append((f'{vid}.Наименование = &ВидЦены '
                             f'И {sup}.Наименование ПОДОБНО &Поставщик',
                             {'ВидЦены': price_type,
                              'Поставщик': f'%{supplier}%'}))
        if vid:
            attempts.append((f'{vid}.Наименование = &ВидЦены',
                             {'ВидЦены': price_type}))
    attempts.append(('без условий', {}))
    for srez, params in attempts:
        try:
            rows = run_query(QUERY_SUPPLIER_PRICES.replace('{SREZ_ARGS}', srez),
                             params)
        except Exception as e:
            sys.stderr.write(f'вариант фильтра «{srez or "(пусто)"}»: '
                             f'{_com_err_text(e)[:120]}\n')
            continue
        if rows:
            sys.stderr.write(f'цены поставщиков ({srez or "срез"}): '
                             f'{len(rows)} строк\n')
            break
    rows = _dedupe_latest(rows)

    # 2) Добор из «Цены номенклатуры» для отсутствующих
    found = {r[0] for r in rows}
    missing = [a for a in articles if a not in found]
    if missing:
        try:
            rows2 = _dedupe_latest(run_query(QUERY_PRICES,
                                             {'ВидЦены': price_type}))
            add = [r for r in rows2 if r[0] in missing]
            if add:
                sys.stderr.write(f'добор «Цены номенклатуры»: '
                                 f'{len(add)} артикулов\n')
                rows.extend(add)
                found |= {r[0] for r in add}
        except Exception as e:
            sys.stderr.write(f'добор недоступен: {_com_err_text(e)[:120]}\n')

    notes = {}
    for a in articles:
        if a not in found:
            notes[a] = 'цены нет ни в одном из регистров цен'

    # История цен (для проверки гипотезы 232956/232905) — прямое чтение
    # записей регистра поставщиков с границей по периоду и списком артикулов
    # (срез виртуальной таблицы с параметром-датой на боевой базе зависает).
    prices_by_date = {}
    history_rows = []
    hist_arts = req.get('history_articles') or []
    if hist_arts:
        h_arr = v8.NewObject('Array')
        for a in hist_arts:
            h_arr.Add(a)
        qh = v8.NewObject('Query')
        qh.Text = (
            'ВЫБРАТЬ\n'
            '    Ном.Артикул КАК Артикул,\n'
            '    Цены.Период КАК Период,\n'
            '    Цены.Цена КАК Цена\n'
            'ИЗ\n'
            '    РегистрСведений.ЦеныНоменклатурыПоставщиков КАК Цены\n'
            '    ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Номенклатура КАК Ном\n'
            '        ПО Цены.Номенклатура = Ном.Ссылка\n'
            'ГДЕ\n'
            '    Ном.Артикул В (&Артикулы)\n'
            '    И Цены.Период >= ДАТАВРЕМЯ(2026,6,1)\n'
            'УПОРЯДОЧИТЬ ПО\n'
            '    Цены.Период УБЫВ\n')
        qh.SetParameter('Артикулы', h_arr)
        try:
            selh = qh.Execute().Select()
            while selh.Next():
                history_rows.append({
                    'sku': str(selh.Артикул).strip(),
                    'period': str(selh.Период)[:10],
                    'price': str(selh.Цена),
                })
        except Exception as e:
            sys.stderr.write(f'история регистра: {_com_err_text(e)[:150]}\n')
        sys.stderr.write(f'строк истории: {len(history_rows)}\n')

    candidates = []
    for art, name, price, period, guid in rows:
        candidates.append({
            'sku': art, 'name': name, 'guid': guid or None,
            'price': str(price), 'price_date': period,
            'price_type': price_type, 'supplier': supplier,
            'deletion_mark': False,
        })
    answers = {
        'candidates': candidates,
        'notes': notes,
        'prices_by_date': prices_by_date,
        'history_rows': history_rows,
        'connect_seconds': round(t_conn, 2),
        'total_seconds': round(time.monotonic() - t0, 2),
        'source': 'com',
    }
    # ensure_ascii=True: JSON в чистом ASCII — без зависимости от кодировки
    # консоли/pipe (родитель читает utf-8 надёжно).
    sys.stdout.write(json.dumps(answers, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    try:
        rc = main()
    except Exception as e:
        import traceback
        sys.stderr.write(f'COM-WORKER ERROR {type(e).__name__}: '
                         f'{_com_err_text(e)[:300]}\n')
        sys.stderr.write(traceback.format_exc()[-800:])
        rc = 2
    sys.exit(rc)