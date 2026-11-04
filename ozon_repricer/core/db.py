"""База SQLite: товары, правила, история цен, журнал изменений, события, настройки."""
import os
import sqlite3
import threading
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "repricer.db")
_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "client_id": "",
    "api_key": "",
    "interval_min": "30",          # как часто проверять цены
    "dry_run": "1",                # 1 = только показывать, что изменил бы
    "auto_enabled": "1",           # фоновые проверки включены
    "cooldown_min": "60",          # пауза после изменения цены товара
    "max_step_pct": "25",          # максимальный шаг изменения за раз, %
    "max_changes_day": "8",        # не больше изменений одного товара в сутки
    "push_min_to_ozon": "1",       # передавать наш минимум в поле «минимальная цена» Ozon
    "control_basis": "seller",     # seller = цена со скидками продавца; buyer = расчётная цена покупателя
    "visibility": "ALL",
    "history_days": "365",
    "strict_overshoot_pct": "100", # на сколько % можно превысить максимум при стратегии «строгое удержание»
    "banner_minmax_seen": "0",     # видел ли пользователь плашку о новом смысле мин/макс («Понятно»)
    "migration_dryrun": "0",       # 1 = при обновлении базы принудительно включена тренировка (проверьте мин/макс)
}

# Ключи Ozon НЕ вписываются в код: они вводятся в интерфейсе (вкладка «Настройки»)
# и хранятся только в локальной базе data/repricer.db (public-репозиторий).
PRESET_KEYS = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    offer_id TEXT, sku INTEGER, name TEXT,
    price REAL, old_price REAL, seller_price REAL, ozon_min_price REAL, buyer_est REAL,
    color_index TEXT, idx_ozon_min REAL, idx_ozon_val REAL, idx_ext_min REAL, idx_ext_val REAL,
    actions TEXT, active INTEGER DEFAULT 1, updated_at TEXT,
    stock_fbo REAL, stock_fbs REAL, stock_fbp REAL, stock_total REAL
);
CREATE TABLE IF NOT EXISTS rules (
    product_id INTEGER PRIMARY KEY,
    mode TEXT DEFAULT 'watch',      -- watch | guard | hold
    min_price REAL, max_price REAL, target REAL,
    tolerance REAL DEFAULT 2,       -- допуск для удержания, %
    max_strategy INTEGER DEFAULT 0, -- 0 = не превышать максимум; 1 = строгое удержание (можно выше максимума)
    control_basis TEXT DEFAULT '',  -- '' = как в настройках | seller | buyer — тип контроля для товара
    tags TEXT DEFAULT '',           -- теги, через запятую
    note TEXT
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER, ts TEXT,
    price REAL, old_price REAL, seller_price REAL, buyer_est REAL, ozon_min_price REAL, color_index TEXT
);
CREATE INDEX IF NOT EXISTS ix_hist ON price_history(product_id, ts);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, product_id INTEGER, offer_id TEXT,
    controlled_before REAL, price_before REAL, price_after REAL, old_price_after REAL, min_after REAL,
    reason TEXT, source TEXT, dry_run INTEGER, ok INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS ix_changes ON changes(product_id, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init():
    with _lock, connect() as con:
        con.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
        # ключи Ozon задаются в интерфейсе; если база старая и ключей нет — заполняются из PRESET_KEYS
        for k, v in PRESET_KEYS.items():
            con.execute("UPDATE settings SET value=? WHERE key=? AND (value IS NULL OR value='')", (v, k))
        cols = [r[1] for r in con.execute("PRAGMA table_info(rules)")]
        if "locked" not in cols:  # обновление старой базы: отметка «Под MPSpace»
            con.execute("ALTER TABLE rules ADD COLUMN locked INTEGER DEFAULT 0")
        # этап 1 (2026.11.10): стратегия при максимуме, тип контроля товара, теги
        if "max_strategy" not in cols:
            con.execute("ALTER TABLE rules ADD COLUMN max_strategy INTEGER DEFAULT 0")
            con.execute("ALTER TABLE rules ADD COLUMN control_basis TEXT DEFAULT ''")
            con.execute("ALTER TABLE rules ADD COLUMN tags TEXT DEFAULT ''")
            # смысл мин/макс изменился: если есть активные правила guard/hold — принудительно
            # включаем тренировку, чтобы старые границы не двигали цены на первом запуске
            n_active = con.execute("SELECT COUNT(*) FROM rules WHERE mode IN ('guard','hold')").fetchone()[0]
            if n_active:
                con.execute("INSERT INTO settings(key, value) VALUES('dry_run','1') "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
                con.execute("INSERT INTO settings(key, value) VALUES('migration_dryrun','1') "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        pcols = [r[1] for r in con.execute("PRAGMA table_info(products)")]
        for col in ("stock_fbo", "stock_fbs", "stock_fbp", "stock_total"):
            if col not in pcols:
                con.execute(f"ALTER TABLE products ADD COLUMN {col} REAL")
        # плашка о новом смысле мин/макс: событие пишем один раз, при обновлении базы с правилами
        if "max_strategy" not in cols:
            n_rules = con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
            if n_rules:
                con.execute("INSERT INTO events(ts, level, message) VALUES(?, 'warn', ?)",
                            (now(), "Обновление: смысл полей «Мин»/«Макс» изменился — теперь это границы "
                                    "вашей цены селлера (цены, которую отправляем в Ozon), а не контролируемой "
                                    "цены. Цель удержания задаётся отдельно. Проверьте значения на вкладке "
                                    "«Товары», плашка об этом видна над таблицей."))
            n_active = con.execute("SELECT COUNT(*) FROM rules WHERE mode IN ('guard','hold')").fetchone()[0]
            if n_active:
                con.execute("INSERT INTO events(ts, level, message) VALUES(?, 'warn', ?)",
                            (now(), "Режим тренировки включён после обновления: смысл полей «Мин»/«Макс» изменился "
                                    "(теперь это границы вашей цены селлера). Проверьте значения мин/макс на "
                                    "вкладке «Товары» и включите боевой режим в «Настройках», когда убедитесь."))
        if "hidden" not in cols:  # скрытые из таблиц товары
            con.execute("ALTER TABLE rules ADD COLUMN hidden INTEGER DEFAULT 0")
        pcols = [r[1] for r in con.execute("PRAGMA table_info(products)")]
        if "image" not in pcols:  # фото товара
            con.execute("ALTER TABLE products ADD COLUMN image TEXT")


def q(sql, args=(), one=False):
    with _lock, connect() as con:
        cur = con.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    with _lock, connect() as con:
        cur = con.execute(sql, args)
        return cur.lastrowid


def get_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key, value FROM settings"):
        s[r["key"]] = r["value"]
    return s


def set_setting(key, value):
    ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))


def log(level, message):
    ex("INSERT INTO events(ts, level, message) VALUES(?, ?, ?)", (now(), level, message))


def get_rule(pid):
    r = q("SELECT * FROM rules WHERE product_id=?", (pid,), one=True)
    return r or {"product_id": pid, "mode": "watch", "min_price": None, "max_price": None,
                 "target": None, "tolerance": 2, "note": None, "locked": 0, "hidden": 0,
                 "max_strategy": 0, "control_basis": "", "tags": ""}


def save_rule(pid, mode="watch", min_price=None, max_price=None, target=None, tolerance=2, note=None,
              max_strategy=0, control_basis="", tags=""):
    ex("""INSERT INTO rules(product_id, mode, min_price, max_price, target, tolerance, note,
             max_strategy, control_basis, tags)
          VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(product_id) DO UPDATE SET mode=excluded.mode, min_price=excluded.min_price,
          max_price=excluded.max_price, target=excluded.target, tolerance=excluded.tolerance,
          note=excluded.note, max_strategy=excluded.max_strategy,
          control_basis=excluded.control_basis, tags=excluded.tags""",
       (pid, mode, min_price, max_price, target, tolerance, note, max_strategy, control_basis, tags))


def set_locked(pid, locked: bool):
    """Отметка «Под MPSpace»: товар только наблюдается, цены не меняются никогда."""
    ex("INSERT INTO rules(product_id, mode, locked) VALUES(?, 'watch', ?) "
       "ON CONFLICT(product_id) DO UPDATE SET locked=excluded.locked", (pid, int(bool(locked))))
    if locked:
        ex("UPDATE rules SET mode='watch' WHERE product_id=?", (pid,))


def set_hidden(pid, hidden: bool):
    """Скрыть товар из таблиц и графиков. Только отображение — правила продолжают работать."""
    ex("INSERT INTO rules(product_id, mode, hidden) VALUES(?, 'watch', ?) "
       "ON CONFLICT(product_id) DO UPDATE SET hidden=excluded.hidden", (pid, int(bool(hidden))))


def is_locked(pid) -> bool:
    r = q("SELECT locked FROM rules WHERE product_id=?", (pid,), one=True)
    return bool(r and r["locked"])


def changes_since(pid, minutes, include_min_only=False):
    """Сколько реальных изменений цены товара было за последние N минут."""
    since = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT COUNT(*) AS n FROM changes WHERE product_id=? AND ts>=? AND ok=1 AND dry_run=0"
    if not include_min_only:
        sql += " AND source != 'мин. цена'"
    return q(sql, (pid, since), one=True)["n"]


def last_change(pid):
    return q("SELECT * FROM changes WHERE product_id=? AND ok=1 AND dry_run=0 AND source != 'мин. цена' "
             "ORDER BY id DESC LIMIT 1", (pid,), one=True)


def cleanup(history_days: int):
    border = (datetime.now() - timedelta(days=history_days)).strftime("%Y-%m-%d %H:%M:%S")
    ex("DELETE FROM price_history WHERE ts < ?", (border,))
    ex("DELETE FROM events WHERE ts < ?", (border,))
