"""Тесты миграций схемы БД (v2 -> v3: uploads.marketplace 'ozon' -> 'ozon_prices')."""

import sqlite3

from app.db import init_db


def _make_v2_db(path):
    """Минимальная БД v2 с одной загрузкой эталона Ozon под старым ключом."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("""CREATE TABLE uploads (
            id INTEGER PRIMARY KEY, marketplace TEXT NOT NULL, filename TEXT NOT NULL,
            stored_path TEXT NOT NULL, uploaded_at TEXT NOT NULL
        )""")
        conn.execute(
            "INSERT INTO uploads (marketplace, filename, stored_path, uploaded_at) "
            "VALUES ('ozon', 'ozon.xlsx', 'data/uploads/ozon.xlsx', '2026-01-01')"
        )
        conn.execute('PRAGMA user_version = 2')
        conn.commit()
    finally:
        conn.close()


def test_migrate_v2_ozon_uploads_to_ozon_prices(tmp_path):
    path = str(tmp_path / 'old.db')
    _make_v2_db(path)

    init_db(path)

    conn = sqlite3.connect(path)
    try:
        rows = [r[0] for r in conn.execute("SELECT marketplace FROM uploads").fetchall()]
        assert rows == ['ozon_prices']
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 3
    finally:
        conn.close()
