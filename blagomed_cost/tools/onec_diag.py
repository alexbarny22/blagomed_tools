#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Живая диагностика источника закупочной цены 1С (этап 5).

Запуск:  python tools/onec_diag.py <путь к config.ini>

Только чтение: COM (V83.COMConnector) и OData (GET). Пароль из config.ini
не выводится (в git не попадает). Результат — таблица 10 артикулов этапа 5
и файл output/onec_diag_*.txt для сверки владельцем.
Фиксированный список артикулов и наименования-подсказки взяты из контрольного
файла за 07.2026 (эталон), секретов нет.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.onec import (  # noqa: E402
    ComSource, ItemStatus, OdataSource, OnecItem, OnecRequest, Params,
    fetch_from_source, merge_odata_guid,
)

OUT_DIR = Path(__file__).resolve().parents[1] / 'output'

ARTICLES = ['232899', '232914', '232956', '232905', '232995', '232996',
            '232946', '232898', '232894', '232886']

NAMES = {
    '232899': 'Подушка-кольцо на сиденье БЛАГО-ППК01, (диам. 41см) Memory',
    '232914': 'Подушка-овал на сиденье БЛАГО-ППК02, (диам. 51х40х7см)',
    '232956': 'Подушка круглая п/прол Благо-ППРН01 (L100)',
    '232905': 'Ремень для фиксации ног на инвалидной коляске',
    '232995': 'Подушка на сиденье п/спину Благо-ППС02 (30*30*8см) черный',
    '232996': 'Подушка на сиденье п/спину Благо-ППС03 (32*34*6см) черный',
    '232946': 'Подушка м/у ног Благо-ППМН101 (для положения на "боку")',
    '232898': 'Подушка м/у ног Благо-ППМН102 (на "боку") без ФИКС голубая',
}


def _row(a):
    return OnecRequest(sku_1c=a, name_1c=NAMES.get(a))


def _odata_table(params) -> list[str]:
    """Сводка по справочнику через OData (без COM): Ref_Key (GUID),
    Description, Артикул. Только GET, N запросов = N артикулов.
    Статус NOT_AVAILABLE означает «карточка найдена, цена в OData не
    публикуется» — это и есть честный результат стандартной публикации."""
    out = ['OData Catalog_Номенклатура: Артикул | Ref_Key | Description '
           '| Статус(цена N/A)']
    try:
        src = OdataSource(params)
        try:
            items = src.fetch([_row(a) for a in ARTICLES])
        finally:
            src.close()
    except Exception as e:  # noqa: BLE001
        out.append(f'ODATA ОШИБКА: {type(e).__name__}: {e}')
        return out
    for a in ARTICLES:
        i = next((x for x in items if x.sku_1c == a), None)
        if i is None:
            out.append(f'{a} | (нет ответа)')
            continue
        out.append(f'{a} | {i.guid_1c or "—"} | {i.name_1c or "—"} | '
                   f'{i.status.value}')
    return out


def main() -> None:
    args = sys.argv[1:]
    cfg = args[0] if args and not args[0].startswith('--') else 'config.ini'
    odata_only = '--odata-only' in args
    com_only = '--com-only' in args
    want_history = '--history' in args
    params = Params.from_file(cfg)
    params.as_of_dates = ()   # срезы на дату не используем (см. worker)
    # Прямое чтение истории регистра — ТОЛЬКО по явному флагу: на боевой базе
    # запрос может быть тяжёлым (зависание сеанса).
    params.history_articles = ('232956', '232905', '232899', '232914') \
        if want_history else ()

    out = ('', [])
    if odata_only:
        out = ('', _odata_table(params))
        _save(out[1])
        print('saved (odata-only)')
        return
    reqs = [_row(a) for a in ARTICLES]

    out = ['Артикул | Наименование 1С (COM) | GUID | Цена (полн. точн.) | '
           'Дата цены | Регистр | Источник | COM | OData | Примечание']
    com: dict[str, OnecItem] = {}
    oda: dict[str, OnecItem] = {}
    com_src: ComSource | None = None
    try:
        com_src = ComSource(params)
        for i in fetch_from_source(com_src, reqs):
            com[i.sku_1c] = i
    except Exception as e:  # noqa: BLE001 - диагностика
        out.append(f'COM ОШИБКА: {type(e).__name__}: {e}')
    if not com_only:
        try:
            for i in fetch_from_source(OdataSource(params), reqs):
                oda[i.sku_1c] = i
        except Exception as e:  # noqa: BLE001
            out.append(f'ODATA ОШИБКА: {type(e).__name__}: {e}')

    for a in ARTICLES:
        c = com.get(a)
        o = oda.get(a)
        if c is None:
            out.append(f'{a} | — | | | | | | ERR | '
                       f'{o.status.value if o else "ERR"} | COM пусто')
            continue
        merged = merge_odata_guid(c, o) if o is not None else c
        out.append(
            f'{a} | {c.name_1c or "—"} | {merged.guid_1c or "—"} | '
            f'{merged.price if merged.price is not None else "—"} | '
            f'{merged.price_date or "—"} | {c.register or "—"} | '
            f'{c.source_status or "—"} | {merged.status.value} | '
            f'{o.status.value if o else "ERR"} | '
            f'Ref_Key(OData)={o.guid_1c or "—"} | {merged.detail}')

    # История цен по артикулам (регистр поставщиков, прямые записи)
    if com_src is not None and com_src._last_data:
        hrows = com_src._last_data.get('history_rows')
        if hrows:
            out.append('')
            out.append('--- История цен регистра «Цены номенклатуры '
                       'поставщиков» (232956/232905/232899/232914) ---')
            cur = None
            for h in hrows:
                if h['sku'] != cur:
                    cur = h['sku']
                    out.append(f'{cur}:')
                out.append(f'  {h["period"]} → {h["price"]}')
        conn = com_src._last_data.get('connect_seconds')
        out.append(f'--- COM: подключение {conn} c, всего '
                   f'{com_src._last_data.get("total_seconds")} c ---')
    _save(out)


def _save(out) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fname = OUT_DIR / f'onec_diag_{datetime.now():%Y%m%d_%H%M%S}.txt'
    fname.write_text('\n'.join(out), encoding='utf-8')
    print('saved', fname)


if __name__ == '__main__':
    main()