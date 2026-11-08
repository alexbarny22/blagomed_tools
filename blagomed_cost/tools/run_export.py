#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI запуска расчёта себестоимости WB/OZON (этап 6).

Примеры:
  python tools/run_export.py --wb input\\Благомед_Себестоимость_11.2026_ВБ.xlsx
      --ozon input\\OZON_Себестоимость_11.2026.xlsx --date 2026-11-01
      --db data/cost.db --legacy-dir legacy --out output --user cli [--config config.ini]
  python tools/run_export.py ... --dry-run        (сухой прогон: temp-БД, черновики, без записи в рабочую БД)
  python tools/run_export.py ... --no-1c          (без подключения к 1С)

Правила: исходные файлы не изменяются (SHA-256 до/после); пишется только
целевая колонка себестоимости; при нерешённых позициях публикуются только
черновики + отчёт исключений. Одно COM-подключение на пакет (ComSource).
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import connect, init_db  # noqa: E402
from app.core.export import run_exports  # noqa: E402
from app.core.migrate import LegacyDataNotFound, migrate  # noqa: E402


def _fetch_raw(config_path: str):
    """Возвращает (source_1c, fetch_prices), создавая 1С-источник по config.ini."""
    from app.core.onec import ComSource, Params, OdataSource, fetch_from_source
    params = Params.from_file(config_path)
    mode = getattr(params, '_mode', 'com')
    if mode == 'odata':
        source = OdataSource(params)
    else:
        source = ComSource(params)

    def fetch(reqs):
        return fetch_from_source(source, list(reqs))

    return ('com' if mode == 'com' else 'odata'), fetch


def _db_for(args) -> Path:
    """Выбирает БД: при dry-run — временная (рабочая не трогается)."""
    if args.dry_run:
        return Path(tempfile.mkdtemp(prefix='cost_dry_')) / 'cost_dry.db'
    return Path(args.db)


def main() -> int:
    ap = argparse.ArgumentParser(description='Расчёт себестоимости WB/OZON')
    ap.add_argument('--wb', required=True, help='исходный отчёт WB (xlsx)')
    ap.add_argument('--ozon', required=True, help='исходный отчёт OZON (xlsx)')
    ap.add_argument('--date', required=True, help='дата расчёта YYYY-MM-DD')
    ap.add_argument('--db', default='data/cost.db', help='рабочая БД')
    ap.add_argument('--legacy-dir', default=None, help='папка файлов-памяти legacy')
    ap.add_argument('--out', default='output', help='каталог результатов')
    ap.add_argument('--config', default=None, help='config.ini 1С (для цен)')
    ap.add_argument('--user', default='cli', help='инициатор запуска')
    ap.add_argument('--mode', default='strict', choices=['strict', 'force'])
    ap.add_argument('--salt', default='', help='salt для нового самостоятельного расчёта')
    ap.add_argument('--no-1c', action='store_true', help='не подключаться к 1С')
    ap.add_argument('--dry-run', action='store_true',
                    help='сухой прогон: temp-БД, черновики, рабочая БД не пишется')
    args = ap.parse_args()

    db = _db_for(args)
    if db.exists():
        con = connect(db)
    else:
        if args.dry_run:
            init_db(db)          # схема для сухого прогона
            try:
                migrate(db, legacy_dir=Path(args.legacy_dir)
                        if args.legacy_dir else None)
            except LegacyDataNotFound:
                pass             # без legacy-каталога — пустая схема
            con = connect(db)
        else:
            con = init_db(db)    # создаёт/приводит схему и бэкапит при изменении
            try:
                migrate(db, legacy_dir=Path(args.legacy_dir)
                        if args.legacy_dir else None)
            except LegacyDataNotFound as e:
                print(f'WARN: {e}', file=sys.stderr)

    source_1c = 'none'
    fetch = None
    if not args.no_1c and not args.dry_run and args.config:
        source_1c, fetch = _fetch_raw(args.config)

    try:
        report = run_exports(
            con=con, calc_date=args.date, wb_path=args.wb, ozon_path=args.ozon,
            out_dir=args.out, fetch_prices=fetch, source_1c=source_1c,
            user=args.user, mode=args.mode, salt=args.salt,
            persist=not args.dry_run)
        print(json.dumps({
            'run': report.run_id, 'status': report.status,
            'drafts': report.drafts,
            'wb_file': report.wb_path, 'ozon_file': report.ozon_path,
            'report': report.report_path,
            'summary': report.summary, 'hashes': report.hashes,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == '__main__':
    raise SystemExit(main())