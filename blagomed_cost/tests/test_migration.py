# -*- coding: utf-8 -*-
"""Тесты миграции legacy-данных в SQLite на синтетических фикстурах.

Не требуют локальных файлов-памяти старого бота (legacy/wb_catalog.json,
legacy/cost_history.json): весь прогон на фикстурах tests/fixtures/.
Проверка реального переноса 77 товаров — в tests/test_migration_real.py.
"""
import json
from pathlib import Path

import pytest

from app.core.db import connect, get_schema_version
from app.core.migrate import (
    EXTRA_POROLON, FIXED_COSTS, PPU_WEIGHTS, LegacyDataNotFound, migrate,
)

FIXTURES = Path(__file__).parent / 'fixtures'

CATALOG = json.loads((FIXTURES / 'wb_catalog.json').read_text(encoding='utf-8'))
HISTORY = json.loads((FIXTURES / 'cost_history.json').read_text(encoding='utf-8'))
CATALOG_SUPS = {str(i['sup']).strip() for i in CATALOG['items']}
HISTORY_SUPS = {k.split('#')[0].strip() for k in HISTORY}
# артикулы, которые миграция добавляет независимо от каталога (ППУ, «Поролон», FIXED_COSTS)
EXTRA_SUPS = set(PPU_WEIGHTS) | set(EXTRA_POROLON) | set(FIXED_COSTS)
EXPECT_PRODUCTS = len(CATALOG_SUPS | HISTORY_SUPS | EXTRA_SUPS)


def _migrate(db_path: Path) -> dict:
    return migrate(db_path, legacy_dir=FIXTURES)


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


def test_migration_from_fixtures_computes_expected_counts(tmp_path):
    db = tmp_path / 'cost.db'
    ns = _migrate(db)
    c = _counts(db)

    # Фиксированные наборы данных миграции (не зависят от фикстур)
    assert c['ppu_weights'] == len(PPU_WEIGHTS) == 13
    assert c['extras'] == len(EXTRA_POROLON) == 5
    assert c['manual_prices'] == len(FIXED_COSTS) == 20
    assert c['settings'] == 3
    assert c['runs'] == 2                     # периоды фикстур: 2026_06, 2026_08
    assert c['listings'] == len(CATALOG_SUPS) == 6
    assert c['snapshots'] == len(HISTORY) == 5

    assert c['products'] == EXPECT_PRODUCTS
    # Каталог — только из фикстур; сироты истории — 2000007 и 2000008
    assert ns['products_new'] == len(CATALOG_SUPS) == 6
    assert ns['orphans'] == len(HISTORY_SUPS - CATALOG_SUPS) == 2


def test_rerun_is_idempotent_with_fixtures(tmp_path):
    db = tmp_path / 'cost.db'
    _migrate(db)
    before = _counts(db)
    second = _migrate(db)
    assert _counts(db) == before
    assert second == {'products_new': 0, 'listings': 0, 'ppu_weights': 0,
                      'extras': 0, 'manual_prices': 0, 'settings': 0,
                      'runs': 0, 'snapshots': 0, 'orphans': 0}


def test_no_ppu_for_232900_and_232917(tmp_path):
    db = tmp_path / 'cost.db'
    _migrate(db)
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


def test_no_extras_for_232899_and_232914(tmp_path):
    db = tmp_path / 'cost.db'
    _migrate(db)
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


def test_history_keys_preserved(tmp_path):
    db = tmp_path / 'cost.db'
    _migrate(db)
    con = connect(db)
    try:
        keys = {json.loads(r['extras_json'])['legacy_key']
                for r in con.execute(
                'SELECT extras_json FROM cost_snapshots WHERE extras_json IS NOT NULL')}
    finally:
        con.close()
    assert keys == set(HISTORY)
    assert len(keys) == len(HISTORY) == 5


def test_runs_start_on_first_day_of_period(tmp_path):
    db = tmp_path / 'cost.db'
    _migrate(db)
    con = connect(db)
    try:
        dates = sorted(r['started_at'] for r in con.execute(
            "SELECT started_at FROM runs WHERE source_1c = 'legacy'"))
    finally:
        con.close()
    assert dates == ['2026-06-01', '2026-08-01']


def test_missing_legacy_files_raise_clear_error(tmp_path):
    # Пустой каталог вместо legacy/ — понятная ошибка, БД не создана
    empty = tmp_path / 'legacy'
    empty.mkdir()
    db = tmp_path / 'cost.db'
    with pytest.raises(LegacyDataNotFound, match='wb_catalog.json.*cost_history.json'):
        migrate(db, legacy_dir=empty)
    assert not db.exists()
    assert not (tmp_path / 'data').exists()   # «фиктивный» каталог не создаётся


def test_missing_legacy_files_do_not_touch_existing_db(tmp_path):
    # БД с данными уже существует — неудачная миграция её не меняет
    from app.core.db import init_db
    db = tmp_path / 'cost.db'
    con = init_db(db)
    con.execute("INSERT INTO products (sku_1c, active) VALUES ('2000001', 1)")
    con.commit()
    con.close()
    assert get_schema_version(db) == 2

    empty = tmp_path / 'legacy'
    empty.mkdir()
    with pytest.raises(LegacyDataNotFound):
        migrate(db, legacy_dir=empty)

    # Данные и версия схемы не изменились
    assert get_schema_version(db) == 2
    con = connect(db)
    try:
        assert con.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 1
    finally:
        con.close()