# -*- coding: utf-8 -*-
"""Проверка реального переноса данных старого бота (77 товаров).

Запускается только на машине с исходными файлами-память
legacy/wb_catalog.json и legacy/cost_history.json (см. README, «Локальные
файлы»). На чистом клоне без этих файлов весь модуль пропускается —
на его месте работают тесты на фикстурах (tests/test_migration.py).
"""
import json
from pathlib import Path

import pytest

from app.core.db import connect
from app.core.migrate import EXTRA_POROLON, FIXED_COSTS, PPU_WEIGHTS, migrate

BASE = Path(__file__).resolve().parents[1]
LEGACY = BASE / 'legacy'
HAVE_REAL_FILES = (LEGACY / 'wb_catalog.json').exists() and \
    (LEGACY / 'cost_history.json').exists()

pytestmark = pytest.mark.skipif(
    not HAVE_REAL_FILES,
    reason='Файлы-память старого бота отсутствуют (чистый клон): миграция '
           'проверяется на синтетических фикстурах в tests/test_migration.py',
)


def _counts(db_path):
    con = connect(db_path)
    try:
        return {
            'products': con.execute('SELECT COUNT(*) FROM products').fetchone()[0],
            'listings': con.execute('SELECT COUNT(*) FROM mp_listings').fetchone()[0],
            'ppu_weights': con.execute('SELECT COUNT(*) FROM ppu_weights').fetchone()[0],
            'extras': con.execute('SELECT COUNT(*) FROM extras').fetchone()[0],
            'manual_prices': con.execute('SELECT COUNT(*) FROM manual_prices').fetchone()[0],
            'settings': con.execute('SELECT COUNT(*) FROM settings').fetchone()[0],
            'runs': con.execute('SELECT COUNT(*) FROM runs').fetchone()[0],
            'snapshots': con.execute('SELECT COUNT(*) FROM cost_snapshots').fetchone()[0],
        }
    finally:
        con.close()


def test_real_counts_after_migration(tmp_path):
    db = tmp_path / 'cost.db'
    ns = migrate(db)          # реальные файлы legacy/
    c = _counts(db)

    assert c['ppu_weights'] == len(PPU_WEIGHTS) == 13
    assert c['extras'] == len(EXTRA_POROLON) == 5
    assert c['manual_prices'] == len(FIXED_COSTS) == 20
    assert c['settings'] == 3
    assert c['runs'] == 2          # 2026-06 и 2026-08
    assert c['listings'] == 76     # WB-карточки каталога

    history = json.loads((LEGACY / 'cost_history.json').read_text(encoding='utf-8'))
    catalog = json.loads((LEGACY / 'wb_catalog.json').read_text(encoding='utf-8'))
    catalog_sups = {str(i['sup']).strip() for i in catalog['items']}
    history_sups = {k.split('#')[0].strip() for k in history}

    assert c['snapshots'] == len(history) == 81
    # Товары из всех источников: каталог, история, ППУ, доп. расходы, FIXED_COSTS
    extra_sups = set(PPU_WEIGHTS) | set(EXTRA_POROLON) | set(FIXED_COSTS)
    assert c['products'] == len(catalog_sups | history_sups | extra_sups) == 77
    assert ns['orphans'] == len(history_sups - catalog_sups) == 0
    assert ns['products_new'] == len(catalog_sups) == 76


def test_real_rerun_is_idempotent(tmp_path):
    db = tmp_path / 'cost.db'
    migrate(db)
    before = _counts(db)
    second = migrate(db)
    assert _counts(db) == before
    assert second == {'products_new': 0, 'listings': 0, 'ppu_weights': 0,
                      'extras': 0, 'manual_prices': 0, 'settings': 0,
                      'runs': 0, 'snapshots': 0, 'orphans': 0}


def test_real_no_ppu_for_232900_and_232917(tmp_path):
    db = tmp_path / 'cost.db'
    migrate(db)
    con = connect(db)
    try:
        for art in ('232900', '232917'):
            n = con.execute(
                'SELECT COUNT(*) FROM ppu_weights w'
                ' JOIN products p ON p.id = w.product_id WHERE p.sku_1c = ?',
                (art,)).fetchone()[0]
            assert n == 0, art
    finally:
        con.close()


def test_real_no_extras_for_232899_and_232914(tmp_path):
    db = tmp_path / 'cost.db'
    migrate(db)
    con = connect(db)
    try:
        for art in ('232899', '232914'):
            n = con.execute(
                'SELECT COUNT(*) FROM extras e'
                ' JOIN products p ON p.id = e.product_id WHERE p.sku_1c = ?',
                (art,)).fetchone()[0]
            assert n == 0, art
    finally:
        con.close()


def test_real_history_keys_preserved(tmp_path):
    db = tmp_path / 'cost.db'
    migrate(db)
    con = connect(db)
    try:
        keys = {json.loads(r['extras_json'])['legacy_key']
                for r in con.execute(
                'SELECT extras_json FROM cost_snapshots WHERE extras_json IS NOT NULL')}
    finally:
        con.close()
    history = json.loads((LEGACY / 'cost_history.json').read_text(encoding='utf-8'))
    assert keys == set(history)
    assert len(keys) == 81


def test_real_runs_start_on_first_day_of_period(tmp_path):
    db = tmp_path / 'cost.db'
    migrate(db)
    con = connect(db)
    try:
        dates = sorted(r['started_at'] for r in con.execute(
            "SELECT started_at FROM runs WHERE source_1c = 'legacy'"))
    finally:
        con.close()
    assert dates == ['2026-06-01', '2026-08-01']