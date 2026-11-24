import sqlite3
import os
from flask import g

def get_db(app=None):
    if 'db' not in g:
        db_path = app.config['DB_PATH']
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 5000')
        g.db = conn
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 5000')
    return conn

def init_db(db_path: str):
    """Инициализирует БД, если файла нет. Если есть — ничего не перезаписывает."""
    if os.path.exists(db_path):
        conn = connect(db_path)
        conn.close()
        return
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = connect(db_path)
    conn.execute('PRAGMA user_version = 1')
    conn.execute("""CREATE TABLE products (
        id INTEGER PRIMARY KEY,
        article_supplier TEXT UNIQUE NOT NULL,
        name TEXT DEFAULT '',
        onec_guid TEXT,
        manual_price INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE settings (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        valid_from TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE ppu_weights (
        id INTEGER PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id),
        weight_g INTEGER NOT NULL,
        valid_from TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE extra_kinds (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL
    )""")
    conn.execute("""CREATE TABLE extras (
        id INTEGER PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id),
        kind_id INTEGER NOT NULL REFERENCES extra_kinds(id),
        rub TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_to TEXT
    )""")
    conn.execute("""CREATE TABLE manual_prices (
        id INTEGER PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id),
        price TEXT NOT NULL,
        valid_from TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE uploads (
        id INTEGER PRIMARY KEY,
        marketplace TEXT NOT NULL,
        filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        uploaded_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        calc_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        total INTEGER DEFAULT 0,
        ok INTEGER DEFAULT 0,
        problems INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE run_items (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES runs(id),
        marketplace TEXT NOT NULL,
        row_num INTEGER,
        article_mp TEXT,
        article_supplier TEXT,
        product_id INTEGER,
        price TEXT,
        price_source TEXT,
        ppu_rub TEXT,
        extras_rub TEXT,
        margin_pct TEXT,
        cost INTEGER,
        status TEXT NOT NULL,
        message TEXT DEFAULT ''
    )""")
    conn.execute('INSERT INTO settings (key, value, valid_from) VALUES (?, ?, ?)',
                 ('ppu_rub_per_kg', '405', '2026-01-01'))
    conn.execute('INSERT INTO settings (key, value, valid_from) VALUES (?, ?, ?)',
                 ('margin_pct', '15', '2026-01-01'))
    conn.commit()
    conn.close()
