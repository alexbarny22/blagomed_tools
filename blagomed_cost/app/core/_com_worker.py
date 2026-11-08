#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Воркер COM-подпроцесса для app/core/onec.py (этап 5). ТОЛЬКО ЧТЕНИЕ.

Запускается родителем (ComSource) отдельным процессом — это даёт реально
работающий таймаут: блокирующий вызов COM выполняется здесь, а родитель может
убить процесс при зависании. Секреты (пароль) принимаются через stdin JSON и
НИКОГДА не печатаются. На stdout — одна строка JSON; прогресс/ошибки — stderr.

Правила цен повторяют старый бот (legacy/blagomed_cost.py), сохраняя ПОРЯДОК
источников КАК В СТАРОМ БОТЕ (экономическая логика не меняется):
  1) «Цены номенклатуры поставщиков» (срез последних): сначала точный вид цены
     И поставщик, затем вид цены + владелец-поставщик, затем только вид цены,
     затем срез без условий (reserve; помечается source_status='any' — цена НЕ
     объявляется проверенной автоматически);
  2) добор по отсутствующим артикулам из «Цены номенклатуры» (вид цены из
     payload) — source_status='fallback_register';
  3) дубли (артикул+наименование) с разными датами схлопываются по последнему
     Периоду (как в отчёте «Условия закупок»); РАЗНЫЕ цены одной позиции
     (характеристики/поставщики/виды цен) НЕ схлопываются — resolver вернёт
     AMBIGUOUS, а не «первого попавшегося».

Для КАЖДОЙ строки результата сохраняются фактические: регистр, вид цены,
поставщик, дата, цена и статус достоверности источника (source_status).
GUID (Ref_Key): попытка v8.GetString(ссылка), затем str(ссылка).
Полная точность цены (Decimal) сохраняется — в JSON пишется str(price),
без float() и округлений.
"""
from __future__ import annotations

import json
import sys
import time


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


def _com_date_literal(iso: str) -> str:
    """Строка ДАТАВРЕМЯ(год,мес,день) для запроса 1С по ISO-дате."""
    y, m, d = str(iso)[:10].split('-')
    return f'ДАТАВРЕМЯ({int(y)},{int(m)},{int(d)})'


def _sig(row):
    """Существенные данные строки для обнаружения неоднозначности."""
    return (str(row[2]), str(row[3]), str(row[6]) if len(row) > 6 else '',
            str(row[5]) if len(row) > 5 else '')


def _dedupe_latest(rows):
    """Схлопывает дубли (артикул+наименование) только по последнему Периоду.

    РАЗНЫЕ существенные данные (цена/период/поставщик/вид цены) по одной
    позиции — НЕ схлопываются: они остаются отдельными строками, и resolver
    вернёт AMBIGUOUS, а не «первого попавшегося». Однозначные дубли (одинаковая
    позиция и даты) схлопываются по самой поздней дате — как в старом боте.
    """
    best, order = {}, []
    for art, name, price, period, guid, *rest in rows:
        key = (art, name)
        if key not in best:
            best[key] = []
            order.append(key)
        best[key].append((art, name, price, period, guid, *rest))
    out = []
    for key in order:
        group = best[key]
        sigs = {_sig(r) for r in group}
        if len(sigs) > 1:
            # конфликт цен одной позиции — отдаём ВСЕ записи, решает resolver
            out.extend(group)
            continue
        latest = max(group, key=lambda r: str(r[3] or '0000-00-00'))
        out.append(latest)
    return out
def _refs_from_guids(v8, guids):
    """Массив ссылок-заглушек по GUID (лучшая попытка, без записи)."""
    refs = None
    if not guids:
        return refs
    try:
        arr = v8.NewObject('Array')
        for g in guids:
            gs = str(g).strip()
            if not gs:
                continue
            uuid = v8.NewObject('УникальныйИдентификатор', gs)
            ref = v8.Справочники.Номенклатура.ПолучитьСсылку(uuid)
            arr.Add(ref)
        refs = arr
    except Exception as e:
        sys.stderr.write(f'GUID-ссылки не собраны: {_com_err_text(e)[:100]}\n')
        refs = None
    return refs


def _supplier_query(where_arts, srez_args, vid_dim, sup_dim):
    extra = ''
    if vid_dim:
        extra += f', Цены.{vid_dim}.Наименование КАК ВидЦены'
    if sup_dim:
        extra += f', Цены.{sup_dim}.Наименование КАК Поставщик'
    return (
        'ВЫБРАТЬ\n'
        '    Ном.Артикул КАК Артикул,\n'
        '    Ном.Наименование КАК Наименование,\n'
        '    Цены.Цена КАК Цена,\n'
        '    Цены.Период КАК Период,\n'
        '    Ном.Ссылка КАК Ссылка'
        f'{extra}\n'
        'ИЗ\n'
        '    РегистрСведений.ЦеныНоменклатурыПоставщиков.СрезПоследних('
        f'{srez_args}) КАК Цены\n'
        '    ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Номенклатура КАК Ном\n'
        '        ПО Цены.Номенклатура = Ном.Ссылка\n'
        f'ГДЕ\n    {where_arts}\n'
        '    И НЕ Ном.ПометкаУдаления\n'
    )


def main() -> int:
    req = json.loads(sys.stdin.readline() or '{}')
    articles = [a for a in (req.get('articles') or []) if a]
    guids = [g for g in (req.get('guids') or []) if g]
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

    refs = _refs_from_guids(v8, guids)

    arr = v8.NewObject('Array')
    for a in articles:
        arr.Add(a)

    # Фильтр по артикулам и/или GUID (артикул мог измениться — GUID первичен).
    conds = []
    if articles:
        conds.append('Ном.Артикул В(&Артикулы)')
    if refs is not None:
        conds.append('Ном.Ссылка В(&Ссылки)')
    where_arts = ' ИЛИ '.join(conds) if conds else '(ИСТИНА)'

    def run_query(text, params=None, refs_=None):
        q = v8.NewObject('Query')
        q.Text = text
        if articles:
            q.SetParameter('Артикулы', arr)
        if refs_ is not None:
            q.SetParameter('Ссылки', refs_)
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
            vid = sup = ''
            try:
                vid = str(sel.ВидЦены).strip()
            except Exception:
                pass
            try:
                sup = str(sel.Поставщик).strip()
            except Exception:
                pass
            if art and price is not None:
                rows.append((art, name, price, period, guid, vid, sup))
        return rows
# 1) Регистр «Цены номенклатуры поставщиков» — структура и попытки среза
    cols = []
    try:
        qs = v8.NewObject('Query')
        qs.Text = ('ВЫБРАТЬ ПЕРВЫЕ 1 * ИЗ '
                   'РегистрСведений.ЦеныНоменклатурыПоставщиков')
        res = qs.Execute()
        kolonki = res.Колонки
        for i in range(kolonki.Количество()):
            cols.append(str(kolonki.Получить(i).Имя))
    except Exception as e:
        sys.stderr.write(f'структура регистра поставщиков: '
                         f'{_com_err_text(e)[:120]}\n')

    vid_dim = next((c for c in cols if 'видцен' in c.lower()), None)
    sup_dim = next((c for c in cols if 'поставщик' in c.lower()
                    and 'видцен' not in c.lower()), None)

    # Порядок попыток — КАК В СТАРОМ БОТЕ (от точного к общему):
    #   1) вид цены И поставщик;  2) вид цены + владелец-поставщик;
    #   3) только вид цены;       4) срез без условий (source_status='any').
    attempts = []
    if vid_dim and sup_dim:
        attempts.append((
            'exact',
            f', {vid_dim}.Наименование = &ВидЦены '
            f'И {sup_dim}.Наименование ПОДОБНО &Поставщик',
            {'ВидЦены': price_type, 'Поставщик': f'%{supplier}%'}))
    if vid_dim:
        attempts.append((
            'exact',
            f', {vid_dim}.Наименование = &ВидЦены '
            f'И {vid_dim}.Владелец.Наименование ПОДОБНО &Поставщик',
            {'ВидЦены': price_type, 'Поставщик': f'%{supplier}%'}))
        attempts.append((
            'vid_only',
            f', {vid_dim}.Наименование = &ВидЦены',
            {'ВидЦены': price_type}))
    attempts.append(('any', '', {}))

    src_status = ''
    rows = []
    for label, srez_args, params in attempts:
        text_with = _supplier_query(where_arts, srez_args, vid_dim, sup_dim)
        text_plain = _supplier_query(where_arts, srez_args, None, None)
        err = None
        for text in (text_with, text_plain):
            try:
                rows = run_query(text, params, refs_=refs)
                err = None
                break
            except Exception as e:
                err = e
        if err is not None:
            sys.stderr.write(f'вариант фильтра «{label}»: '
                             f'{_com_err_text(err)[:120]}\n')
            continue
        if rows:
            src_status = label
            sys.stderr.write(f'цены поставщиков ({label}): {len(rows)} строк\n')
            break
    rows = _dedupe_latest(rows)
    supplier_used = src_status == 'exact'

    # 2) Добор из «Цены номенклатуры» для отсутствующих артикулов.
    #    ЦЕНА ИЗ ЭТОГО РЕГИСТРА НЕ СЧИТАЕТСЯ ПРОВЕРЕННОЙ: доборный регистр без
    #    поставщика, source_status='fallback_register' (как в старом боте —
    #    «СВЕРЬ эти цены, регистр может быть устаревшим»).
    found = {r[0] for r in rows}
    missing = [a for a in articles if a not in found]
    fallback_rows = []
    if missing:
        cat_text = (
            'ВЫБРАТЬ\n'
            '    Ном.Артикул КАК Артикул,\n'
            '    Ном.Наименование КАК Наименование,\n'
            '    Цены.Цена КАК Цена,\n'
            '    Цены.Период КАК Период,\n'
            '    Ном.Ссылка КАК Ссылка\n'
            'ИЗ\n'
            '    РегистрСведений.ЦеныНоменклатуры.СрезПоследних('
            ', ВидЦены.Наименование = &ВидЦены) КАК Цены\n'
            '    ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Номенклатура КАК Ном\n'
            '        ПО Цены.Номенклатура = Ном.Ссылка\n'
            f'ГДЕ\n    {where_arts}\n'
            '    И НЕ Ном.ПометкаУдаления\n')
        try:
            rows2 = _dedupe_latest(run_query(cat_text,
                                             {'ВидЦены': price_type},
                                             refs_=refs))
            fallback_rows = [r for r in rows2 if r[0] in missing]
            if fallback_rows:
                sys.stderr.write(f'добор «Цены номенклатуры»: '
                                 f'{len(fallback_rows)} артикулов\n')
        except Exception as e:
            sys.stderr.write(f'добор недоступен: {_com_err_text(e)[:120]}\n')

    notes = {}
    for a in articles:
        if a not in found and a not in {r[0] for r in fallback_rows}:
            notes[a] = 'цены нет ни в одном из регистров цен'

    # История цен (для проверки гипотезы 232956/232905) — прямое чтение
    # записей регистра поставщиков (срез виртуальной таблицы с датой на
    # боевой базе зависает — тяжёлый запрос НЕ повторяем).
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

    # Кандидаты: фактический регистр/вид/поставщик/дата/цена + статус источника.
    candidates = []
    for art, name, price, period, guid, vid, sup in rows:
        candidates.append({
            'sku': art, 'name': name, 'guid': guid or None,
            'price': str(price), 'price_date': period,
            'price_type': vid or price_type,
            'supplier': sup or (supplier if supplier_used else None),
            'register': 'ЦеныНоменклатурыПоставщиков',
            'source_status': src_status or 'any',
            'deletion_mark': False,
        })
    for art, name, price, period, guid, vid, sup in fallback_rows:
        candidates.append({
            'sku': art, 'name': name, 'guid': guid or None,
            'price': str(price), 'price_date': period,
            'price_type': vid or price_type,
            'supplier': None,
            'register': 'ЦеныНоменклатуры',
            'source_status': 'fallback_register',
            'deletion_mark': False,
        })

    answers = {
        'candidates': candidates,
        'notes': notes,
        'prices_by_date': {},
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