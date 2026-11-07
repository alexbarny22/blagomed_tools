#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI-обёртка миграции стартовых данных: python tools/migrate_legacy.py [--db путь]."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.migrate import migrate  # noqa: E402

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Перенос legacy-данных в SQLite')
    default_db = Path(__file__).resolve().parents[1] / 'data' / 'cost.db'
    ap.add_argument('--db', default=str(default_db), help='путь к файлу БД')
    args = ap.parse_args()
    print(migrate(Path(args.db)))