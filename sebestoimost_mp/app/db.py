import sqlite3
import os
from datetime import datetime

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


# ------------------------------------------------------------
# Схема v2: новые колонки и таблица профилей шаблонов
# ------------------------------------------------------------

# products: новые колонки (NULL у *_pct / *_rub = глобальный дефолт из settings)
PRODUCT_COLUMNS_V2 = (
    ('is_active', 'INTEGER NOT NULL DEFAULT 1'),
    ('margin_pct', 'TEXT NULL'),
    ('ppu_rub_per_kg', 'TEXT NULL'),
    ('extras_rub', 'TEXT NULL'),
    ('article_wb', 'TEXT NULL'),
    ('article_ozon', 'TEXT NULL'),
)

# run_items: новые колонки
RUN_ITEM_COLUMNS_V2 = (
    ('prev_cost', 'INTEGER NULL'),
    ('jump', 'INTEGER NOT NULL DEFAULT 0'),
)

TEMPLATE_PROFILES_DDL = """CREATE TABLE IF NOT EXISTS template_profiles (
    id INTEGER PRIMARY KEY,
    marketplace TEXT NOT NULL,
    name TEXT NOT NULL,
    sheet TEXT NOT NULL,
    header_row INTEGER NOT NULL,
    data_row INTEGER NOT NULL,
    col_article_mp TEXT NOT NULL,
    col_article_supplier TEXT,
    col_cost_write TEXT NOT NULL,
    updated_at TEXT,
    UNIQUE(marketplace, name)
)"""

# (marketplace, name, sheet, header_row, data_row,
#  col_article_mp, col_article_supplier, col_cost_write)
# sheet для 'wb' — шаблон имени листа, {YYYY-MM} подставляется на дату расчёта
# Ozon: A = «Артикул» (артикул ПОСТАВЩИКА), B = «SKU» (артикул Ozon).
# В файле себестоимостей Ozon SKU нет — единственный ключ артикул поставщика.
TEMPLATE_PROFILES_SEEDS = (
    ('wb', 'wb', 'Себестоимость за {YYYY-MM}', 2, 3, 'A', 'B', 'E'),
    ('ozon', 'prices', 'Товары и цены', 2, 5, 'B', 'A', 'BD'),
    ('ozon', 'finance', 'Sheet1', 1, 2, 'A', 'A', 'B'),
)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _table_columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def _seed_template_profiles(conn):
    """Идемпотентно вставляет 3 профиля шаблонов."""
    for (marketplace, name, sheet, header_row, data_row,
         col_article_mp, col_article_supplier, col_cost_write) in TEMPLATE_PROFILES_SEEDS:
        conn.execute(
            """INSERT OR IGNORE INTO template_profiles
               (marketplace, name, sheet, header_row, data_row,
                col_article_mp, col_article_supplier, col_cost_write, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (marketplace, name, sheet, header_row, data_row,
             col_article_mp, col_article_supplier, col_cost_write, _now()),
        )


def _create_schema(conn):
    """Полная схема для нового файла БД (актуальная версия — v4)."""
    conn.execute("""CREATE TABLE products (
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
    )""")
    conn.execute("""CREATE TABLE settings (
        id INTEGER PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL, valid_from TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE ppu_weights (
        id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
        weight_g INTEGER NOT NULL, valid_from TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE extra_kinds (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL)""")
    conn.execute("""CREATE TABLE extras (
        id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
        kind_id INTEGER NOT NULL REFERENCES extra_kinds(id), rub TEXT NOT NULL,
        valid_from TEXT NOT NULL, valid_to TEXT
    )""")
    conn.execute("""CREATE TABLE manual_prices (
        id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
        price TEXT NOT NULL, valid_from TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE uploads (
        id INTEGER PRIMARY KEY, marketplace TEXT NOT NULL, filename TEXT NOT NULL,
        stored_path TEXT NOT NULL, uploaded_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE runs (
        id INTEGER PRIMARY KEY, calc_date TEXT NOT NULL, created_at TEXT NOT NULL,
        total INTEGER DEFAULT 0, ok INTEGER DEFAULT 0, problems INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE run_items (
        id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
        marketplace TEXT NOT NULL, row_num INTEGER, article_mp TEXT, article_supplier TEXT,
        product_id INTEGER, price TEXT, price_source TEXT, ppu_rub TEXT, extras_rub TEXT,
        margin_pct TEXT, cost INTEGER, status TEXT NOT NULL, message TEXT DEFAULT '',
        prev_cost INTEGER NULL, jump INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute(TEMPLATE_PROFILES_DDL)
    conn.execute('INSERT INTO settings (key, value, valid_from) VALUES (?, ?, ?)',
                 ('ppu_rub_per_kg', '405', '2026-01-01'))
    conn.execute('INSERT INTO settings (key, value, valid_from) VALUES (?, ?, ?)',
                 ('margin_pct', '15', '2026-01-01'))
    _seed_template_profiles(conn)
    conn.execute('PRAGMA user_version = 4')


def _migrate_v1_to_v2(conn):
    """v1 -> v2. Данные не трогаем: только ALTER/CREATE + сиды профилей."""
    existing_products = _table_columns(conn, 'products')
    for name, decl in PRODUCT_COLUMNS_V2:
        if name not in existing_products:
            conn.execute(f'ALTER TABLE products ADD COLUMN {name} {decl}')

    existing_items = _table_columns(conn, 'run_items')
    for name, decl in RUN_ITEM_COLUMNS_V2:
        if name not in existing_items:
            conn.execute(f'ALTER TABLE run_items ADD COLUMN {name} {decl}')

    conn.execute(TEMPLATE_PROFILES_DDL)
    _seed_template_profiles(conn)
    conn.execute('PRAGMA user_version = 2')


def _migrate_v2_to_v3(conn):
    """v2 -> v3: старые загрузки эталона Ozon ('ozon') переименовываются
    в 'ozon_prices', как их ищет код v3."""
    conn.execute(
        "UPDATE uploads SET marketplace = 'ozon_prices' "
        "WHERE marketplace = 'ozon'"
    )
    conn.execute('PRAGMA user_version = 3')


def _migrate_v3_to_v4(conn):
    """v3 -> v4: профили Ozon читают артикул поставщика + чистка сирот.

    В реальных файлах Ozon «Артикул» (колонка A «Товаров и цен») — артикул
    ПОСТАВЩИКА, «SKU» (колонка B) — артикул Ozon. Профиль v3 читал только
    SKU, поэтому товары заводились по артикулу Ozon и не мэтчились с 1С.
    """
    conn.execute(
        "UPDATE template_profiles SET col_article_mp = 'B', "
        "col_article_supplier = 'A' "
        "WHERE marketplace = 'ozon' AND name = 'prices'"
    )
    # В файле себестоимостей Ozon SKU нет: единственный ключ — артикул
    # поставщика, поэтому он же остаётся «артикулом МП» (дублирование
    # допустимо: мэтчинг идёт по поставщику в первую очередь).
    conn.execute(
        "UPDATE template_profiles SET col_article_mp = 'A', "
        "col_article_supplier = 'A' "
        "WHERE marketplace = 'ozon' AND name = 'finance'"
    )
    # Сироты — товары, заведённые по SKU Ozon (9-10 знаков) вместо артикула
    # поставщика. LENGTH-фильтр обязателен: артикулы поставщика (5-6 знаков),
    # заведённые из файла себестоимостей, не удаляем.
    conn.execute(
        "DELETE FROM products WHERE article_ozon IS NOT NULL "
        "AND article_ozon <> '' AND article_supplier = article_ozon "
        "AND LENGTH(article_supplier) > 7"
    )
    conn.execute('PRAGMA user_version = 4')


def init_db(db_path: str):
    """Инициализирует БД, если файла нет. Если есть — мигрирует до v4.

    Существующие данные не перезаписываются.
    """
    if not os.path.exists(db_path):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = connect(db_path)
        try:
            _create_schema(conn)
            conn.commit()
        finally:
            conn.close()
        return

    # Файл есть: миграции цепочкой v1 -> v2 -> v3 -> v4
    conn = connect(db_path)
    try:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version < 2:
            _migrate_v1_to_v2(conn)
            conn.commit()
            version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version < 3:
            _migrate_v2_to_v3(conn)
            conn.commit()
            version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version < 4:
            _migrate_v3_to_v4(conn)
            conn.commit()
    finally:
        conn.close()
