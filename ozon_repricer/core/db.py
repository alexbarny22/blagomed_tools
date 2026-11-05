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
    "actions_interval_min": "120", # как часто перечитывать список акций Ozon (мин); «Проверить сейчас» — всегда
    # Этап 3 — прибыль и безопасный минимум:
    "tax_pct": "",                  # налог, % от выручки (пусто = 0, ставку вводит пользователь)
    "cost_divider": "1.15",         # при импорте себестоимости из файла: значение уже с наценкой, делится на это
    "min_margin_pct": "10",         # минимальная маржа для кнопки «Рассчитать безопасный минимум», %
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
    stock_fbo REAL, stock_fbs REAL, stock_fbp REAL, stock_total REAL,
    cost REAL,                        -- себестоимость, ₽ за штуку (этап 3)
    acquiring REAL,                   -- эквайринг Ozon, ₽
    comm_sales_pct_fbo REAL, comm_sales_pct_fbs REAL,          -- комиссия Ozon, %
    comm_direct_max_fbo REAL, comm_direct_max_fbs REAL,        -- логистика (max), ₽
    comm_direct_min_fbo REAL, comm_direct_min_fbs REAL,        -- логистика (min), ₽ — для подсказки
    comm_mile_fbo REAL, comm_mile_fbs REAL                     -- последняя миля, ₽
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
CREATE TABLE IF NOT EXISTS product_actions (
    product_id INTEGER NOT NULL,
    action_id INTEGER NOT NULL,
    kind TEXT NOT NULL,             -- participant | candidate
    title TEXT, date_from TEXT, date_to TEXT, status TEXT,
    price REAL, action_price REAL, max_action_price REAL,
    PRIMARY KEY (product_id, action_id, kind)
);
CREATE INDEX IF NOT EXISTS ix_pa ON product_actions(product_id);
CREATE TABLE IF NOT EXISTS auto_add (
    product_id INTEGER NOT NULL,
    action_id INTEGER NOT NULL,
    price_to_auto_add REAL,        -- «Ozon добавит сам по цене X» (/v2/actions/auto-add/products/list)
    updated_at TEXT,
    PRIMARY KEY (product_id, action_id)
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
        # этап 2 (2026.11.17): работа с акциями Ozon
        if "promo_mode" not in cols:
            con.execute("ALTER TABLE rules ADD COLUMN promo_mode INTEGER DEFAULT 0")
            con.execute("ALTER TABLE rules ADD COLUMN promo_entry TEXT DEFAULT 'RECOMMEND'")
            con.execute("ALTER TABLE rules ADD COLUMN promo_deviation_pct REAL DEFAULT 0")
        # Этап 3 (2026.11.24): прибыль и безопасный минимум — себестоимость и расходы Ozon
        pcols3 = [r[1] for r in con.execute("PRAGMA table_info(products)")]
        for col3, ct3 in (("cost", "REAL"), ("acquiring", "REAL"),
                          ("comm_sales_pct_fbo", "REAL"), ("comm_sales_pct_fbs", "REAL"),
                          ("comm_direct_max_fbo", "REAL"), ("comm_direct_max_fbs", "REAL"),
                          ("comm_direct_min_fbo", "REAL"), ("comm_direct_min_fbs", "REAL"),
                          ("comm_mile_fbo", "REAL"), ("comm_mile_fbs", "REAL")):
            if col3 not in pcols3:
                con.execute(f"ALTER TABLE products ADD COLUMN {col3} {ct3}")


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
                 "max_strategy": 0, "control_basis": "", "tags": "",
                 "promo_mode": 0, "promo_entry": "RECOMMEND", "promo_deviation_pct": 0}


def save_rule(pid, mode="watch", min_price=None, max_price=None, target=None, tolerance=2, note=None,
              max_strategy=0, control_basis="", tags="",
              promo_mode=0, promo_entry="RECOMMEND", promo_deviation_pct=0):
    ex("""INSERT INTO rules(product_id, mode, min_price, max_price, target, tolerance, note,
             max_strategy, control_basis, tags, promo_mode, promo_entry, promo_deviation_pct)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(product_id) DO UPDATE SET mode=excluded.mode, min_price=excluded.min_price,
          max_price=excluded.max_price, target=excluded.target, tolerance=excluded.tolerance,
          note=excluded.note, max_strategy=excluded.max_strategy,
          control_basis=excluded.control_basis, tags=excluded.tags,
          promo_mode=excluded.promo_mode, promo_entry=excluded.promo_entry,
          promo_deviation_pct=excluded.promo_deviation_pct""",
       (pid, mode, min_price, max_price, target, tolerance, note, max_strategy, control_basis, tags,
        promo_mode, promo_entry, promo_deviation_pct))


def save_actions(rows, gone_aids=(), reset_pa=(), reset_aa=(), auto_add_rows=()):
    """Обновление данных акций Ozon (вызывается из sync).

    rows            — новые строки product_actions только УСПЕШНО прочитанных акций;
    gone_aids       — акции, которых больше нет в /v1/actions или у которых истёк date_end:
                      их строки удаляются полностью;
    reset_pa        — акции, прочитанные целиком: их старые product_actions удаляются
                      перед вставкой (товары, вышедшие из акции, исчезают);
    reset_aa        — акции, у которых автодобавление прочитано целиком: то же для auto_add;
    auto_add_rows   — (product_id, action_id, price_to_auto_add, updated_at).

    Данные акций, которые прочитать не удалось, НЕ трогаются: прежние строки сохраняются.
    """
    with _lock, connect() as con:
        if gone_aids:
            marks = ",".join("?" * len(gone_aids))
            con.execute(f"DELETE FROM product_actions WHERE action_id IN ({marks})", tuple(gone_aids))
            con.execute(f"DELETE FROM auto_add WHERE action_id IN ({marks})", tuple(gone_aids))
        if reset_pa:
            marks = ",".join("?" * len(reset_pa))
            con.execute(f"DELETE FROM product_actions WHERE action_id IN ({marks})", tuple(reset_pa))
        if reset_aa:
            marks = ",".join("?" * len(reset_aa))
            con.execute(f"DELETE FROM auto_add WHERE action_id IN ({marks})", tuple(reset_aa))
        con.executemany("""INSERT INTO product_actions(product_id, action_id, kind, title, date_from,
                          date_to, status, price, action_price, max_action_price)
                          VALUES(?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(product_id, action_id, kind) DO UPDATE SET title=excluded.title,
                          date_from=excluded.date_from, date_to=excluded.date_to, status=excluded.status,
                          price=excluded.price, action_price=excluded.action_price,
                          max_action_price=excluded.max_action_price""", rows)
        if auto_add_rows:
            con.executemany("""INSERT INTO auto_add(product_id, action_id, price_to_auto_add, updated_at)
                               VALUES(?,?,?,?)
                               ON CONFLICT(product_id, action_id) DO UPDATE SET
                               price_to_auto_add=excluded.price_to_auto_add, updated_at=excluded.updated_at""",
                            auto_add_rows)


def get_product_actions(pid):
    return q("SELECT * FROM product_actions WHERE product_id=? ORDER BY date_from, action_id", (pid,))


def get_auto_add(pid):
    """Что Ozon добавит в акции автоматически для товара (с названием акции из product_actions)."""
    return q("""SELECT a.action_id, a.price_to_auto_add, a.updated_at,
                       COALESCE(p.title, '') AS title, p.kind
                FROM auto_add a
                LEFT JOIN product_actions p ON p.product_id=a.product_id
                    AND p.action_id=a.action_id AND p.kind='candidate'
                WHERE a.product_id=? ORDER BY a.action_id""", (pid,))


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


def set_min(pid, value):
    """Обновить только минимум правила товара, не трогая остальные поля правила."""
    ex("INSERT INTO rules(product_id, min_price) VALUES(?, ?) "
       "ON CONFLICT(product_id) DO UPDATE SET min_price=excluded.min_price", (pid, value))


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
