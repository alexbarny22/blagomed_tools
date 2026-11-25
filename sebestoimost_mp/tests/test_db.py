"""Тесты миграций схемы БД (v2 -> v3 -> v4).

v3 -> v4: профили Ozon читают артикул поставщика, товары-сироты по SKU удалены.
"""

import sqlite3

from app.db import TEMPLATE_PROFILES_DDL, init_db

PRODUCTS_DDL = """CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    article_supplier TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    onec_guid TEXT,
    manual_price INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    margin_pct TEXT NULL,
    ppu_rub_per_kg TEXT NULL,
    extras_rub TEXT NULL,
    article_wb TEXT NULL,
    article_ozon TEXT NULL
)"""


def _make_old_db(path, version, ozon_key='ozon_prices', products=()):
    """БД версии v2/v3: uploads + products + профили Ozon со старыми колонками."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("""CREATE TABLE uploads (
            id INTEGER PRIMARY KEY, marketplace TEXT NOT NULL, filename TEXT NOT NULL,
            stored_path TEXT NOT NULL, uploaded_at TEXT NOT NULL
        )""")
        conn.execute(
            "INSERT INTO uploads (marketplace, filename, stored_path, uploaded_at) "
            "VALUES (?, 'ozon.xlsx', 'data/uploads/ozon.xlsx', '2026-01-01')",
            (ozon_key,),
        )
        conn.execute(PRODUCTS_DDL)
        conn.execute(TEMPLATE_PROFILES_DDL)
        conn.execute(
            "INSERT INTO template_profiles (marketplace, name, sheet, header_row, "
            "data_row, col_article_mp, col_article_supplier, col_cost_write, "
            "updated_at) VALUES ('ozon', 'prices', 'Товары и цены', 2, 5, "
            "'A', NULL, 'BD', '2026-01-01 00:00:00')"
        )
        conn.execute(
            "INSERT INTO template_profiles (marketplace, name, sheet, header_row, "
            "data_row, col_article_mp, col_article_supplier, col_cost_write, "
            "updated_at) VALUES ('ozon', 'finance', 'Sheet1', 1, 2, "
            "'A', NULL, 'B', '2026-01-01 00:00:00')"
        )
        for article_supplier, article_ozon in products:
            conn.execute(
                "INSERT INTO products (article_supplier, created_at, article_ozon) "
                "VALUES (?, '2026-01-01', ?)",
                (article_supplier, article_ozon),
            )
        conn.execute(f'PRAGMA user_version = {version}')
        conn.commit()
    finally:
        conn.close()


def _make_v2_db(path):
    """Минимальная БД v2 с одной загрузкой эталона Ozon под старым ключом."""
    _make_old_db(path, 2, ozon_key='ozon')


def test_migrate_v2_ozon_uploads_to_ozon_prices(tmp_path):
    path = str(tmp_path / 'old.db')
    _make_v2_db(path)

    init_db(path)

    conn = sqlite3.connect(path)
    try:
        rows = [r[0] for r in conn.execute("SELECT marketplace FROM uploads").fetchall()]
        assert rows == ['ozon_prices']
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 4
    finally:
        conn.close()


def test_migrate_v3_to_v4_ozon_profiles_and_orphans(tmp_path):
    path = str(tmp_path / 'v3.db')
    _make_old_db(
        path, 3,
        products=(
            # сироты: заведены по SKU Ozon (9-10 знаков) — удаляются
            ('761235340', '761235340'),
            ('7612353401', '7612353401'),
            # товары по артикулу поставщика (5-6 знаков) — остаются
            ('232922', '232922'),
            ('232923', '232923'),
            # 7 знаков: LENGTH-фильтр защищает, не удаляем
            ('1234567', '1234567'),
            # артикул МП не совпадает с поставщиком — не сирота
            ('232924', '761235341'),
            # article_ozon пустой — не сирота
            ('232925', ''),
        ),
    )

    init_db(path)

    conn = sqlite3.connect(path)
    try:
        profiles = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT name, col_article_mp, col_article_supplier "
                "FROM template_profiles WHERE marketplace = 'ozon'"
            ).fetchall()
        }
        articles = [r[0] for r in conn.execute(
            "SELECT article_supplier FROM products ORDER BY article_supplier"
        ).fetchall()]
        version = conn.execute('PRAGMA user_version').fetchone()[0]
    finally:
        conn.close()

    assert profiles['prices'] == ('B', 'A')
    assert profiles['finance'] == ('A', 'A')
    assert articles == ['1234567', '232922', '232923', '232924', '232925']
    assert version == 4
