# -*- coding: utf-8 -*-
"""Источники закупочных цен для веб-запуска.

Три режима (выбор пользователя на странице «Расчёт»):
- '1c'       — существующий read-only COM-источник (app.core.onec);
- 'snapshot' — цены из последнего успешного снимка расчёта; всегда помечаются
               как исторические (не свежие), дата и номер запуска видны в UI;
- 'none'     — 1С не используется; ручные цены работают, остальные строки
               получают NEEDS_CONFIRM, а не подставленную чужую цену.

Пароль 1С никогда не попадает в сообщения/логи: Params инкапсулирует его.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.core import onec
from app.core.onec import ItemStatus, OnecItem

PRICE_MODES = {
    '1c': '1С (COM, только чтение) — если подключение настроено',
    'snapshot': 'Исторические цены последнего успешного расчёта (не свежие)',
    'none': 'Без 1С: только ручные цены',
}


def _read_ini_section(path: Path, section: str) -> dict:
    import configparser
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding='utf-8')
    return dict(cfg[section]) if cfg.has_section(section) else {}


def onec_status(cfg) -> dict:
    """Диагностика подключения без сетевых вызовов и без секретов."""
    ini = Path(cfg.config_ini)
    status = {'config': str(ini), 'exists': ini.exists(),
              'configured': False, 'mode': 'не задан', 'server': '',
              'base': '', 'price_type': onec.DEFAULT_PRICE_TYPE,
              'supplier': onec.DEFAULT_SUPPLIER, 'message': ''}
    if not ini.exists():
        status['message'] = (
            'config.ini не найден: 1С не подключена. Приложение работает с '
            'ручными ценами и ранее сохранёнными снимками; старые цены за '
            'свежие не выдаются.')
        return status
    sec = _read_ini_section(ini, '1C')
    prices = _read_ini_section(ini, 'Цены')
    status['mode'] = (sec.get('mode') or 'com').strip().lower()
    status['server'] = (sec.get('server') or '').strip()
    status['base'] = (sec.get('base') or '').strip()
    status['price_type'] = (prices.get('вид_цены') or
                            onec.DEFAULT_PRICE_TYPE).strip()
    status['supplier'] = (prices.get('поставщик') or
                          onec.DEFAULT_SUPPLIER).strip()
    status['configured'] = bool(status['server'] and status['base'])
    if status['configured']:
        status['message'] = (
            f"1С настроена: {status['server']}/{status['base']}, "
            f"вид цены «{status['price_type']}», поставщик "
            f"«{status['supplier']}». Подключение выполняется только при "
            "запуске расчёта.")
    else:
        status['message'] = (
            'В config.ini не заполнены server/base ([1C]): подключение к 1С '
            'недоступно. Доступны ручные цены и снимки прошлых расчётов.')
    return status


def _fallback_items(requests, status: ItemStatus, detail: str
                    ) -> list[OnecItem]:
    return [OnecItem(sku_1c=r.sku_1c, name_1c=r.name_1c,
                     guid_1c=r.guid_1c, source='1c', status=status,
                     detail=detail) for r in requests]


def today() -> str:
    return date.today().isoformat()
def _snapshot_fetch(con):
    """Возвращает (fetch, message) по последнему успешному снимку."""
    from app.web import store
    run = store.last_done_run(con)
    if run is None:
        def no_snapshot(requests):
            return _fallback_items(
                requests, ItemStatus.NEEDS_CONFIRM,
                'нет сохранённого успешного расчёта: исторические цены '
                'недоступны; укажите ручную цену')
        return no_snapshot, ('Снимков успешных расчётов нет: цены не '
                             'подставляются, строки без ручной цены останутся '
                             'нерешёнными.')
    rows = con.execute(
        'SELECT s.product_id, s.price_1c_exact, s.price_1c, p.sku_1c,'
        ' p.guid_1c FROM cost_snapshots s JOIN products p'
        ' ON p.id = s.product_id WHERE s.run_id = ?', (run['id'],)
    ).fetchall()
    by_sku: dict[str, dict] = {}
    for r in rows:
        raw = r['price_1c_exact'] if r['price_1c_exact'] is not None \
            else r['price_1c']
        try:
            price = Decimal(str(raw)) if raw is not None else None
        except Exception:                              # noqa: BLE001
            price = None
        if r['sku_1c']:
            by_sku[str(r['sku_1c'])] = {'price': price,
                                        'guid': r['guid_1c']}
    snap_date = str(run['started_at'])[:10]

    def fetch(requests):
        items = []
        for req in requests:
            rec = by_sku.get(str(req.sku_1c or ''))
            if rec is None:
                items.append(OnecItem(
                    sku_1c=req.sku_1c, name_1c=req.name_1c,
                    guid_1c=req.guid_1c, source='snapshot',
                    status=ItemStatus.NO_PRICE,
                    detail=f'в снимке расчёта №{run["id"]} нет цены этого '
                           'товара; укажите ручную цену'))
                continue
            if rec['price'] is None:
                items.append(OnecItem(
                    sku_1c=req.sku_1c, name_1c=req.name_1c,
                    guid_1c=rec['guid'] or req.guid_1c, source='snapshot',
                    status=ItemStatus.NO_PRICE,
                    detail='в снимке нет закупочной цены (товар был на ручной '
                           'цене или без цены)'))
                continue
            items.append(OnecItem(
                sku_1c=req.sku_1c, name_1c=req.name_1c,
                guid_1c=rec['guid'] or req.guid_1c,
                price=rec['price'], price_date=snap_date,
                price_type='историческая (снимок расчёта)',
                supplier='снимок расчёта',
                register=f'cost_snapshots#{run["id"]}',
                source_status='snapshot', source='snapshot',
                status=ItemStatus.OK,
                detail=f'цена из снимка расчёта №{run["id"]} от {snap_date}; '
                       'это историческая, а не свежая цена 1С'))
        return items

    message = (f'Используются исторические цены из снимка расчёта №{run["id"]} '
               f'от {snap_date}. Это НЕ свежие цены 1С; их актуальность '
               f'ограничена датой снимка.')
    return fetch, message


def make_fetch_prices(cfg, con, mode: str):
    """Возвращает (fetch_prices | None, сообщение, метка источника)."""
    mode = (mode or 'none').strip().lower()
    if mode not in PRICE_MODES:
        raise ValueError(f'Неизвестный режим цен: {mode!r}')
    if mode == 'none':
        return (None,
                '1С в этом запуске не используется. Товары без ручной цены '
                'останутся в статусе NEEDS_CONFIRM — чужая цена не '
                'подставляется.', 'none')
    if mode == 'snapshot':
        fetch, message = _snapshot_fetch(con)
        return fetch, message, 'snapshot'
    status = onec_status(cfg)
    if not status['configured']:
        def not_configured(requests):
            return _fallback_items(
                requests, ItemStatus.CONNECT_ERROR,
                'подключение к 1С не настроено (config.ini [1C] server/base); '
                'цена не запрашивалась')
        return not_configured, status['message'], '1c-unconfigured'

    def fetch_1c(requests):
        reqs = list(requests)
        try:
            params = onec.Params.from_file(cfg.config_ini)
        except onec.OnecError as e:
            return _fallback_items(reqs, ItemStatus.CONNECT_ERROR, str(e))
        source = onec.ComSource(params)
        try:
            return onec.fetch_from_source(source, reqs)
        except onec.OnecError as e:
            return _fallback_items(reqs, onec._status_from_error(e), str(e))

    message = ('Цены запрошены из 1С (COM, только чтение). Подключение может '
               'занять до нескольких минут; при ошибке расчёт продолжится, '
               'старые цены за свежие не выдаются.')
    return fetch_1c, message, '1c'