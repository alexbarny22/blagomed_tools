#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Контролируемое подтверждение GUID 1С (этап 6, раздел «Запись GUID в SQLite»).

Подтверждение относится к ТОЧНОЙ паре product_id + guid_1c (никакой массовой
записи по совпадению артикула). Перед записью каждую пару можно просмотреть
в режиме dry-run; apply-confirmed сохраняет только пары со статусом 'ok' и
пишет записи аудита в одной транзакции.

  python tools/guid_apply.py --db data/cost.db --pairs pairs.json --mode dry-run
  python tools/guid_apply.py --db data/cost.db --pairs pairs.json --mode apply --user owner

Формат pairs.json: [{"product_id": 12, "guid_1c": "aabb...", "comment": "..."}]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import connect  # noqa: E402
from app.core.guid import apply_guid_updates, plan_guid_updates  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description='Подтверждение GUID товаров 1С')
    ap.add_argument('--db', default='data/cost.db')
    ap.add_argument('--pairs', required=True, help='JSON-файл со списком пар')
    ap.add_argument('--mode', required=True, choices=['dry-run', 'apply'],
                    help='dry-run — только просмотр; apply — запись подтверждённых')
    ap.add_argument('--user', default='cli')
    args = ap.parse_args()

    pairs = json.loads(Path(args.pairs).read_text(encoding='utf-8'))
    items = [(p['product_id'], p['guid_1c']) for p in pairs]
    con = connect(args.db)
    try:
        plans = plan_guid_updates(con, items)
        for p in plans:
            print(f"[{p.status}] product_id={p.product_id} "
                  f"guid={p.guid_1c!r} old={p.old_guid_1c!r} {p.reason}")
        if args.mode == 'apply':
            messages, applied = apply_guid_updates(
                con, items, user=args.user)
            print(f'Применено подтверждённых пар: {applied}')
            for m in messages:
                print(m)
        else:
            ok = sum(1 for p in plans if p.status == 'ok')
            print(f'БД не изменена. Подтверждённых пар (ok): {ok}')
    finally:
        con.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())