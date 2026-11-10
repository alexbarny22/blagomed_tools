#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перенос стартовых данных из legacy-бота в SQLite (этап 3).

Переносится:
  legacy/wb_catalog.json  → products (sku_1c) + mp_listings (WB-карточки)
  ППУ из CLINE_PROMPT                    → ppu_weights (232900/232917 не переносим)
  FIXED_COSTS старого бота               → products.manual_price + manual_prices
  EXTRA старого бота                     → extra_kinds + extras («Поролон»)
  settings: ppu_rub_per_kg=405, margin_pct=15, alert_pct=10
  legacy/cost_history.json               → runs (по периодам) + cost_snapshots
                                           с price_source='legacy'

Гарантии:
- идемпотентность — повторный запуск не создаёт дублей (естественные ключи,
  история проверяется по (run периода, legacy-ключу из extras_json));
- перед применением схемы бэкап БД делает db.apply_migrations;
- сироты (артикулы из истории, которых нет в каталоге) импортируются как товары
  без карточек и попадают в список «Проверить» (счётчик orphans).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.core.db import init_db

BASE = Path(__file__).resolve().parents[2]
LEGACY = BASE / 'legacy'


class LegacyDataNotFound(FileNotFoundError):
    """Файлы-память старого бота (wb_catalog.json, cost_history.json) не найдены."""


def _require_legacy_files(legacy_dir: Path) -> None:
    """Проверяет файлы-память ДО создания/изменения БД: при отсутствии данных
    миграция безопасно останавливается — БД не создаётся, а существующая
    не изменяется (нет риска потери данных и «фиктивного» каталога)."""
    missing = [str(p) for p in (legacy_dir / 'wb_catalog.json',
                                legacy_dir / 'cost_history.json')
               if not p.exists()]
    if missing:
        raise LegacyDataNotFound(
            'Файлы-память старого бота не найдены: ' + ', '.join(missing) + '. '
            'Миграция остановлена до создания/изменения БД. Скопируйте '
            'wb_catalog.json и cost_history.json в папку legacy/ из папки '
            'работающего старого бота (см. README, раздел «Локальные файлы»).')


# Вес ППУ, г. Переносим ТОЛЬКО эти (232900 и 232917 — не ППУ-группа).
PPU_WEIGHTS = {
    '232946': 270, '232898': 270, '232918': 270, '232987': 385, '232995': 365,
    '232996': 325, '333001': 365, '333002': 365, '232906': 465, '232902': 445,
    '232899': 465, '232914': 465, '232894': 210,
}

# Доп. расход «Поролон» (у 232899 и 232914 доп. расходов НЕТ).
EXTRA_POROLON = {
    '232956': 110, '232916': 196, '232896': 176, '232957': 300, '232971': 332,
}

# Фиксированная себестоимость другого поставщика → manual_prices.
FIXED_COSTS = {
    '232924': 190, '232983': 743, '232991': 636, '232943': 743, '232923': 120,
    '232948': 110, '232951': 1100, '232942': 110, '232955': 449, '232936': 170,
    '232965': 743, '232934': 190, '232897': 410, '232937': 1520, '232935': 110,
    '333003': 652, '232954': 921, '232925': 1100, '232800': 759, '232962': 330,
}

VALID_FROM = '2026-07-01'  # старт импорта — период контрольного файла 07.2026
INITIAL_SETTINGS = (
    ('ppu_rub_per_kg', 405.0),
    ('margin_pct', 15.0),          # проценты: 15 = 15%, не 0.15
    ('alert_pct', 10.0),
)


def _parse_period(value) -> str:
    """'2026_08' → '2026-08-01'; иначе VALID_FROM."""
    s = str(value or '').strip()
    try:
        return datetime.strptime(s, '%Y_%m').date().isoformat()
    except ValueError:
        return VALID_FROM


def _get_or_create_product(con, by_sku: dict, sup: str) -> tuple[int, bool]:
    """Возвращает (product_id, created) — создаёт товар, если артикул ещё не известен."""
    sup = sup.strip()
    pid = by_sku.get(sup)
    if pid is None:
        cur = con.execute(
            'INSERT INTO products (sku_1c, active, added_at) VALUES (?, 1, ?)',
            (sup, VALID_FROM))
        pid = cur.lastrowid
        by_sku[sup] = pid
        return pid, True
    return pid, False


def migrate(db_path: Path, legacy_dir: Path | None = None, *,
            confirm: bool = False) -> dict:
    """Применяет схему и наполняет базу стартовыми данными. Возвращает счётчики.

    legacy_dir — папка файлов-памяти старого бота (по умолчанию BASE/legacy).
    Если wb_catalog.json / cost_history.json отсутствуют, выбрасывается
    LegacyDataNotFound, а база данных не создаётся и не изменяется.
    confirm=True — как в db.init_db: требуется, если схема применяется к уже
    существующей БД (иначе db.MigrationConfirmationRequired).
    """
    legacy_dir = Path(legacy_dir) if legacy_dir is not None else LEGACY
    _require_legacy_files(legacy_dir)

    ns = {'products_new': 0, 'listings': 0, 'ppu_weights': 0, 'extras': 0,
          'manual_prices': 0, 'settings': 0, 'runs': 0, 'snapshots': 0,
          'orphans': 0}
    con = init_db(db_path, confirm=confirm)
    try:
        items = json.loads((legacy_dir / 'wb_catalog.json').read_text(encoding='utf-8'))['items']
        history = json.loads((legacy_dir / 'cost_history.json').read_text(encoding='utf-8'))

        by_sku = {str(r['sku_1c']).strip(): r['id']
                  for r in con.execute('SELECT id, sku_1c FROM products')
                  if r['sku_1c']}

        with con:
            # 1) Каталог → products + WB-карточки в mp_listings
            for it in items:
                sup = str(it['sup']).strip()
                wb = str(it['wb']).strip()
                pid, created = _get_or_create_product(con, by_sku, sup)
                if created:
                    ns['products_new'] += 1
                if wb and con.execute(
                        'SELECT 1 FROM mp_listings WHERE marketplace = ? AND art = ?',
                        ('WB', wb)).fetchone() is None:
                    con.execute(
                        'INSERT INTO mp_listings (product_id, marketplace, art, size, active)'
                        ' VALUES (?, ?, ?, ?, 1)',
                        (pid, 'WB', wb, str(it.get('size') or '').strip() or None))
                    ns['listings'] += 1

            # 2) Вес ППУ
            for sup, weight in sorted(PPU_WEIGHTS.items()):
                pid, _ = _get_or_create_product(con, by_sku, sup)
                if con.execute(
                        'SELECT 1 FROM ppu_weights WHERE product_id = ? AND valid_from = ?',
                        (pid, VALID_FROM)).fetchone() is None:
                    con.execute(
                        'INSERT INTO ppu_weights (product_id, weight_g, valid_from)'
                        ' VALUES (?, ?, ?)', (pid, int(weight), VALID_FROM))
                    ns['ppu_weights'] += 1

            # 3) Доп. расход «Поролон»
            con.execute("INSERT OR IGNORE INTO extra_kinds (name) VALUES ('Поролон')")
            kind_id = con.execute(
                "SELECT id FROM extra_kinds WHERE name = 'Поролон'").fetchone()['id']
            for sup, rub in sorted(EXTRA_POROLON.items()):
                pid, _ = _get_or_create_product(con, by_sku, sup)
                if con.execute(
                        'SELECT 1 FROM extras WHERE product_id = ? AND kind_id = ?'
                        ' AND valid_from = ?',
                        (pid, kind_id, VALID_FROM)).fetchone() is None:
                    con.execute(
                        'INSERT INTO extras (product_id, kind_id, rub, valid_from)'
                        ' VALUES (?, ?, ?, ?)', (pid, kind_id, float(rub), VALID_FROM))
                    ns['extras'] += 1

            # 4) Ручные закупочные цены (FIXED_COSTS)
            for sup, price in sorted(FIXED_COSTS.items()):
                pid, _ = _get_or_create_product(con, by_sku, sup)
                con.execute('UPDATE products SET manual_price = 1 WHERE id = ?', (pid,))
                if con.execute(
                        'SELECT 1 FROM manual_prices WHERE product_id = ? AND valid_from = ?',
                        (pid, VALID_FROM)).fetchone() is None:
                    con.execute(
                        'INSERT INTO manual_prices (product_id, price, valid_from)'
                        ' VALUES (?, ?, ?)', (pid, float(price), VALID_FROM))
                    ns['manual_prices'] += 1

            # 5) Параметры расчёта (на VALID_FROM)
            for key, value in INITIAL_SETTINGS:
                if con.execute(
                        'SELECT 1 FROM settings WHERE key = ? AND valid_from = ?',
                        (key, VALID_FROM)).fetchone() is None:
                    con.execute(
                        'INSERT INTO settings (key, value, valid_from) VALUES (?, ?, ?)',
                        (key, value, VALID_FROM))
                    ns['settings'] += 1

            # 6) История цен → один legacy-run на период, снимки по записям
            by_run = {r['started_at']: r['id'] for r in con.execute(
                "SELECT id, started_at FROM runs"
                " WHERE source_1c = 'legacy' AND user = 'migrate_legacy'")}
            by_period: dict[str, dict] = {}
            for key, rec in history.items():
                by_period.setdefault(str(rec.get('period')), {})[key] = rec

            for period, recs in sorted(by_period.items()):
                pdate = _parse_period(period)
                run_id = by_run.get(pdate)
                if run_id is None:
                    cur = con.execute(
                        "INSERT INTO runs (started_at, finished_at, status, source_1c,"
                        " user, wb_file, ozon_file)"
                        " VALUES (?, ?, 'done', 'legacy', 'migrate_legacy', ?, '')",
                        (pdate, pdate, 'cost_history.json'))
                    run_id = cur.lastrowid
                    by_run[pdate] = run_id
                    ns['runs'] += 1
                done = set()
                for r in con.execute(
                        'SELECT extras_json FROM cost_snapshots WHERE run_id = ?'
                        ' AND extras_json IS NOT NULL', (run_id,)):
                    try:
                        done.add(json.loads(r['extras_json'])['legacy_key'])
                    except (KeyError, ValueError, TypeError):
                        pass
                for key, rec in sorted(recs.items()):
                    if key in done:
                        continue
                    sup = key.split('#')[0].strip()
                    pid, created = _get_or_create_product(con, by_sku, sup)
                    if created:
                        ns['orphans'] += 1
                    con.execute(
                        "INSERT INTO cost_snapshots (run_id, product_id, price_1c,"
                        " price_source, ppu_rate, margin_pct, cost, extras_json)"
                        " VALUES (?, ?, NULL, 'legacy', 405, 15, ?, ?)",
                        (run_id, pid, int(rec['cost']),
                         json.dumps({'legacy_key': key, 'period': period},
                                    ensure_ascii=False)))
                    ns['snapshots'] += 1

        con.commit()
        return ns
    finally:
        con.close()


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Перенос legacy-данных в SQLite')
    ap.add_argument('--db', default=str(BASE / 'data' / 'cost.db'),
                    help='путь к файлу БД (по умолчанию data/cost.db)')
    args = ap.parse_args()
    print(json.dumps(migrate(Path(args.db)), ensure_ascii=False, indent=2))