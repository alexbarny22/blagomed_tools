-- 001_init.sql — схема БД «Себестоимость МП»
-- Применяется один раз; версия схемы = PRAGMA user_version = 1

-- Товар верхнего уровня (соответствует «Артикулу поставщика» в отчётах и 1С).
CREATE TABLE IF NOT EXISTS products (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guid_1c      TEXT UNIQUE,              -- GUID из 1С (Ref_Key); заполняется на этапе 5
    sku_1c       TEXT,                     -- артикул 1С (ключ legacy), NULL до сопоставления
    name_1c      TEXT,                     -- наименование из 1С
    manual_price INTEGER,                  -- признак «ручная закупочная цена» (FIXED_COSTS)
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    added_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_products_sku_1c ON products (sku_1c);

-- Карточки МП: «1 товар → N карточек» (у части артикулов по две карточки WB).
CREATE TABLE IF NOT EXISTS mp_listings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products (id),
    marketplace TEXT NOT NULL CHECK (marketplace IN ('WB', 'OZON')),
    art         TEXT NOT NULL,             -- артикул на МП («Артикул WB» / «Артикул Ozon»)
    size        TEXT,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE (marketplace, art)
);
CREATE INDEX IF NOT EXISTS idx_mp_listings_product ON mp_listings (product_id);

-- ── Вес ППУ (история с датами) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ppu_weights (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products (id),
    weight_g   INTEGER NOT NULL CHECK (weight_g >= 0),
    valid_from TEXT NOT NULL,
    changed_by TEXT,
    UNIQUE (product_id, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_ppu_weights_product ON ppu_weights (product_id);
CREATE INDEX IF NOT EXISTS idx_ppu_weights_valid   ON ppu_weights (valid_from);

-- ── Справочник статей доп. расходов ───────────────────────────────────
CREATE TABLE IF NOT EXISTS extra_kinds (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

-- ── Доп. расходы по товару (история) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS extras (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products (id),
    kind_id    INTEGER NOT NULL REFERENCES extra_kinds (id),
    rub        REAL NOT NULL CHECK (rub >= 0),
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    changed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_extras_product ON extras (product_id);

-- ── Параметры расчёта (история значений) ──────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT    NOT NULL,           -- ppu_rub_per_kg | margin_pct | alert_pct
    value      REAL    NOT NULL,
    valid_from TEXT    NOT NULL,
    changed_by TEXT,
    UNIQUE (key, valid_from)
);

-- ── Ручная закупочная цена (история) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS manual_prices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products (id),
    price      REAL    NOT NULL CHECK (price >= 0),
    valid_from TEXT    NOT NULL,
    changed_by TEXT,
    UNIQUE (product_id, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_manual_prices_product_af ON manual_prices (product_id);

-- ── Запуски расчёта ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'error')),
    source_1c   TEXT,                      -- COM | OData | legacy | manual
    user        TEXT,
    error       TEXT,
    wb_file     TEXT,
    ozon_file   TEXT
);

-- ── Раскладка себестоимости по позициям (воспроизводимость) ───────────
CREATE TABLE IF NOT EXISTS cost_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs (id),
    product_id  INTEGER NOT NULL REFERENCES products (id),
    price_1c    REAL,
    price_source TEXT NOT NULL DEFAULT '1c' CHECK (price_source IN ('1c', 'manual', 'legacy')),
    ppu_weight_g INTEGER NOT NULL DEFAULT 0,
    ppu_rate    REAL NOT NULL DEFAULT 405,
    ppu_rub     REAL NOT NULL DEFAULT 0,
    extras_rub  REAL NOT NULL DEFAULT 0,
    extras_json TEXT,
    margin_pct  REAL NOT NULL DEFAULT 15,
    cost        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_run     ON cost_snapshots (run_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_product ON cost_snapshots (product_id);

-- ── Аудит изменений ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    user      TEXT,
    entity    TEXT NOT NULL,               -- products | ppu_weights | extras | settings | ...
    entity_id INTEGER,
    old       TEXT,                        -- JSON
    new       TEXT                         -- JSON
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log (entity, entity_id);

PRAGMA user_version = 1;