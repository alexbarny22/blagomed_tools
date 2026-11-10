# -*- coding: utf-8 -*-
"""Слой данных веб-интерфейса: только параметризованный SQL и сервисные
операции поверх существующих таблиц/миграций. Расчёт — только app.core.calc
(вторая формула не создаётся)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.core import calc, db as core_db

ISSUE_STATUSES = ('AMBIGUOUS', 'NO_MATCH', 'NOT_FOUND', 'NEEDS_CONFIRM',
                  'NO_PRICE', 'ERROR', 'PPU_UNCONFIRMED')
STATUS_LABELS = {
    'OK': 'рассчитано',
    'AMBIGUOUS': 'неоднозначное сопоставление',
    'NO_MATCH': 'нет товара в каталоге',
    'NOT_FOUND': 'товар не найден в 1С',
    'NEEDS_CONFIRM': 'требуется подтверждение',
    'NO_PRICE': 'нет закупочной цены',
    'ERROR': 'ошибка источника/расчёта',
    'PPU_UNCONFIRMED': 'вес ППУ не утверждён',
}
PRICE_SOURCE_LABELS = {
    'manual': 'ручная цена',
    'snapshot': 'исторический снимок (не свежая цена)',
    'none': 'нет данных',
}


class WebStoreError(RuntimeError):
    """Ошибка операции веб-слоя с понятным пользователю текстом."""


def today_iso() -> str:
    return date.today().isoformat()


def parse_iso_date(value, *, field: str = 'дата', default: str | None = None
                   ) -> str:
    raw = (str(value).strip() if value is not None else '') or (default or '')
    if not raw:
        raise WebStoreError(f'{field}: укажите дату в формате ГГГГ-ММ-ДД')
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise WebStoreError(
            f'{field}: {value!r} — ожидается дата ГГГГ-ММ-ДД') from None


def parse_money(value, *, field: str, allow_empty: bool = False):
    raw = (str(value).strip() if value is not None else '')
    if not raw:
        if allow_empty:
            return None
        raise WebStoreError(f'{field}: укажите сумму')
    raw = raw.replace(' ', '').replace(',', '.')
    try:
        d = Decimal(raw)
    except InvalidOperation:
        raise WebStoreError(f'{field}: {value!r} — не число') from None
    if not d.is_finite() or d < 0:
        raise WebStoreError(f'{field}: сумма должна быть неотрицательной')
    if d > Decimal('10000000'):
        raise WebStoreError(f'{field}: слишком большая сумма')
    return d


def parse_weight(value, *, field: str = 'вес ППУ, г'):
    raw = (str(value).strip() if value is not None else '')
    if not raw:
        raise WebStoreError(f'{field}: укажите целое число граммов '
                            '(0 — допустимый вес)')
    try:
        w = int(raw)
    except ValueError:
        raise WebStoreError(f'{field}: {value!r} — ожидается целое число') \
            from None
    if not (0 <= w <= 10000):
        raise WebStoreError(f'{field}: допустимо 0..10000 г')
    return w


# ── Подключение и состояние БД ──────────────────────────────────────────────


def open_db(cfg) -> sqlite3.Connection:
    if not Path(cfg.db_path).exists():
        raise WebStoreError(
            f'База данных не найдена: {cfg.db_path}. Создайте её в разделе '
            '«База данных».')
    return core_db.connect(cfg.db_path)


def db_info(cfg) -> dict:
    path = Path(cfg.db_path)
    info = {'path': str(path), 'exists': path.exists(), 'integrity': None,
            'user_version': None, 'counts': {}, 'size': None,
            'error': None}
    if not path.exists():
        return info
    info['size'] = path.stat().st_size
    try:
        con = core_db.connect(path)
        try:
            info['integrity'] = con.execute('PRAGMA integrity_check'
                                            ).fetchone()[0]
            info['user_version'] = con.execute('PRAGMA user_version'
                                               ).fetchone()[0]
            for t in ('products', 'mp_listings', 'ppu_weights', 'extras',
                      'manual_prices', 'runs', 'cost_snapshots'):
                info['counts'][t] = con.execute(
                    f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        info['error'] = str(e)
    return info


def create_db(cfg) -> None:
    path = Path(cfg.db_path)
    if path.exists():
        raise WebStoreError(
            f'База уже существует: {path}. Существующая база не '
            'перезаписывается.')
    path.parent.mkdir(parents=True, exist_ok=True)
    con = core_db.init_db(path)
    try:
        # Новая БД должна быть сразу пригодна к расчёту: те же стартовые
        # параметры, что и при переносе legacy (405 / 15% / 10%).
        from app.core.migrate import INITIAL_SETTINGS, VALID_FROM
        with con:
            for key, value in INITIAL_SETTINGS:
                con.execute(
                    'INSERT OR IGNORE INTO settings'
                    ' (key, value, valid_from, changed_by)'
                    ' VALUES (?, ?, ?, ?)',
                    (key, float(value), VALID_FROM, 'setup'))
        ok = con.execute('PRAGMA integrity_check').fetchone()[0]
        if ok != 'ok':
            raise WebStoreError(f'Проверка целостности новой БД: {ok}')
    finally:
        con.close()


def migrate_legacy(cfg) -> dict:
    """Перенос данных старого бота (существующая штатная функция)."""
    legacy = Path(cfg.base_dir) / 'legacy'
    for name in ('wb_catalog.json', 'cost_history.json'):
        if not (legacy / name).exists():
            raise WebStoreError(
                f'Нет файла {legacy / name}. Скопируйте файлы-память старого '
                'бота в папку legacy/.')
    from app.core.migrate import migrate
    return migrate(Path(cfg.db_path), legacy_dir=legacy)


def audit(con, user: str, entity: str, entity_id, old, new) -> None:
    con.execute(
        'INSERT INTO audit_log (user, entity, entity_id, old, new)'
        ' VALUES (?, ?, ?, ?, ?)',
        (user, entity, entity_id,
         json.dumps(old, ensure_ascii=False) if old is not None else None,
         json.dumps(new, ensure_ascii=False) if new is not None else None))
# ── Товары и цены ──────────────────────────────────────────────────────────


def last_done_run(con):
    return con.execute(
        "SELECT id, started_at, finished_at, wb_file, ozon_file"
        " FROM runs WHERE status = 'done' AND source_1c IS NOT NULL"
        " AND source_1c <> 'legacy' ORDER BY id DESC LIMIT 1").fetchone()


def latest_snapshot_map(con) -> tuple[object, dict]:
    run = last_done_run(con)
    if run is None:
        return None, {}
    rows = con.execute(
        'SELECT product_id, price_1c_exact, price_1c, ppu_weight_g,'
        '       cost, price_source FROM cost_snapshots WHERE run_id = ?',
        (run['id'],)).fetchall()
    out = {}
    for r in rows:
        raw = r['price_1c_exact'] if r['price_1c_exact'] is not None \
            else r['price_1c']
        try:
            price = Decimal(str(raw)) if raw is not None else None
        except InvalidOperation:
            price = None
        out[int(r['product_id'])] = {
            'price': price,
            'cost': int(r['cost']) if r['cost'] is not None else None,
            'price_source': r['price_source']}
    return run, out


def manual_price_as_of(con, product_id: int, on_date: str):
    return con.execute(
        'SELECT price, valid_from FROM manual_prices WHERE product_id = ?'
        ' AND valid_from <= ? ORDER BY valid_from DESC, id DESC LIMIT 1',
        (product_id, on_date)).fetchone()


def weight_as_of(con, product_id: int, on_date: str):
    return con.execute(
        'SELECT weight_g, valid_from FROM ppu_weights WHERE product_id = ?'
        ' AND valid_from <= ? ORDER BY valid_from DESC, id DESC LIMIT 1',
        (product_id, on_date)).fetchone()


def extras_as_of(con, product_id: int, on_date: str):
    try:
        return list(calc.get_extras(con, product_id, on_date))
    except calc.CalcError as e:
        raise WebStoreError(str(e)) from None


def get_product(con, product_id: int):
    return con.execute(
        'SELECT id, sku_1c, name_1c, guid_1c, manual_price, active, added_at'
        ' FROM products WHERE id = ?', (product_id,)).fetchone()


def product_history(con, product_id: int) -> dict:
    return {
        'manual_prices': con.execute(
            'SELECT price, valid_from, changed_by FROM manual_prices'
            ' WHERE product_id = ? ORDER BY valid_from DESC, id DESC',
            (product_id,)).fetchall(),
        'ppu_weights': con.execute(
            'SELECT weight_g, valid_from, changed_by FROM ppu_weights'
            ' WHERE product_id = ? ORDER BY valid_from DESC, id DESC',
            (product_id,)).fetchall(),
        'extras': con.execute(
            'SELECT e.id, k.name AS kind, e.rub, e.valid_from, e.valid_to,'
            '       e.changed_by FROM extras e JOIN extra_kinds k'
            ' ON k.id = e.kind_id WHERE e.product_id = ?'
            ' ORDER BY e.valid_from DESC, e.id DESC', (product_id,)).fetchall(),
        'audit': con.execute(
            'SELECT at, user, entity, old, new FROM audit_log'
            ' WHERE entity_id = ? ORDER BY id DESC LIMIT 50',
            (product_id,)).fetchall(),
    }
def product_view(con, cfg, product, on_date: str, snap_run=None,
                 snap_map: dict | None = None) -> dict:
    """Цена/источник, ППУ, доп. расходы и себестоимость товара на дату.

    Приоритет цены: ручная цена (manual_price=1) → последний успешный снимок
    расчёта (помечается историческим) → нет данных. Цена 1С «на лету» здесь
    не запрашивается, чтобы не выдавать старые данные за свежие.
    """
    pid = int(product['id'])
    snap_map = snap_map or {}
    manual = int(product['manual_price'] or 0) == 1
    price = None
    price_source = 'none'
    price_date = None
    note = ''
    manual_row = manual_price_as_of(con, pid, on_date) if manual else None
    if manual:
        if manual_row is not None:
            price = Decimal(str(manual_row['price']))
            price_source = 'manual'
            price_date = manual_row['valid_from']
            note = f'Ручная цена с {price_date}'
        else:
            note = 'Ручной режим включён, но цены на дату нет'
    elif snap_map.get(pid) and snap_map[pid]['price'] is not None:
        price = snap_map[pid]['price']
        price_source = 'snapshot'
        price_date = (snap_run['started_at'][:10] if snap_run is not None
                      else None)
        note = (f'Цена из снимка расчёта №{snap_run["id"]} от {price_date} — '
                'историческая, не свежая цена 1С')
    weight_row = weight_as_of(con, pid, on_date)
    extras = []
    extras_error = ''
    try:
        extras = extras_as_of(con, pid, on_date)
    except WebStoreError as e:
        extras_error = str(e)
    cost = None
    cost_error = ''
    status = 'OK'
    if price is None:
        status = 'NO_PRICE'
        cost_error = ('Нет подтверждённой закупочной цены: укажите ручную цену '
                      'или выполните расчёт с доступным источником 1С.')
    elif extras_error:
        status = 'ERROR'
        cost_error = extras_error
    else:
        try:
            res = calc.calc_product(con, product_id=pid, price=price,
                                    calc_date=on_date)
            cost = int(res.cost)
        except calc.CalcError as e:
            status = 'ERROR'
            cost_error = str(e)
    return {
        'id': pid, 'sku': product['sku_1c'], 'name': product['name_1c'],
        'guid': (product['guid_1c'] or '').strip(), 'manual': manual,
        'price': price, 'price_source': price_source,
        'price_source_label': PRICE_SOURCE_LABELS.get(price_source,
                                                      price_source),
        'price_date': price_date, 'price_note': note,
        'weight_g': int(weight_row['weight_g']) if weight_row else None,
        'weight_from': weight_row['valid_from'] if weight_row else None,
        'extras': [{'kind': e.kind, 'rub': e.rub} for e in extras],
        'extras_sum': sum((e.rub for e in extras), Decimal('0')),
        'extras_error': extras_error, 'cost': cost, 'status': status,
        'status_label': STATUS_LABELS.get(status, status),
        'cost_error': cost_error, 'on_date': on_date,
        'snap_run_id': snap_run['id'] if snap_run is not None else None,
    }


def list_products(con, cfg, q: str = '', status: str = '',
                  on_date: str | None = None) -> list[dict]:
    on_date = on_date or today_iso()
    params, where = [], ['1 = 1']
    if q:
        where.append('(p.sku_1c LIKE ? OR p.name_1c LIKE ?)')
        like = f'%{q}%'
        params += [like, like]
    if status == 'manual':
        where.append('p.manual_price = 1')
    elif status == 'no_price':
        where.append('p.manual_price = 0')
    rows = con.execute(
        'SELECT p.id, p.sku_1c, p.name_1c, p.guid_1c, p.manual_price,'
        '       p.active FROM products p WHERE ' + ' AND '.join(where) +
        ' ORDER BY p.sku_1c, p.id', params).fetchall()
    snap_run, snap_map = latest_snapshot_map(con)
    views = [product_view(con, cfg, r, on_date, snap_run, snap_map)
             for r in rows]
    if status in ('ok', 'problem'):
        views = [v for v in views if (v['status'] == 'OK') == (status == 'ok')]
    return views
def set_manual_price(con, product_id: int, price, valid_from: str,
                     user: str = 'web') -> None:
    """price=None — вернуть источник «1С/снимок» (manual_price=0)."""
    row = con.execute('SELECT manual_price, sku_1c FROM products'
                      ' WHERE id = ?', (product_id,)).fetchone()
    if row is None:
        raise WebStoreError('Товар не найден')
    with con:
        if price is None:
            con.execute('UPDATE products SET manual_price = 0 WHERE id = ?',
                        (product_id,))
            audit(con, user, 'products', product_id,
                  {'manual_price': row['manual_price']},
                  {'manual_price': 0, 'reason': 'источник цены — 1С/снимок'})
            return
        d = price if isinstance(price, Decimal) else Decimal(str(price))
        con.execute(
            'INSERT INTO manual_prices (product_id, price, valid_from,'
            ' changed_by) VALUES (?, ?, ?, ?)'
            ' ON CONFLICT(product_id, valid_from) DO UPDATE SET'
            ' price = excluded.price, changed_by = excluded.changed_by',
            (product_id, float(d), valid_from, user))
        con.execute('UPDATE products SET manual_price = 1 WHERE id = ?',
                    (product_id,))
        audit(con, user, 'manual_prices', product_id,
              {'manual_price': row['manual_price']},
              {'price': str(d), 'valid_from': valid_from})


def set_ppu_weight(con, product_id: int, weight_g: int, valid_from: str,
                   user: str = 'web') -> None:
    if get_product(con, product_id) is None:
        raise WebStoreError('Товар не найден')
    with con:
        con.execute(
            'INSERT INTO ppu_weights (product_id, weight_g, valid_from,'
            ' changed_by) VALUES (?, ?, ?, ?)'
            ' ON CONFLICT(product_id, valid_from) DO UPDATE SET'
            ' weight_g = excluded.weight_g, changed_by = excluded.changed_by',
            (product_id, int(weight_g), valid_from, user))
        audit(con, user, 'ppu_weights', product_id, None,
              {'weight_g': int(weight_g), 'valid_from': valid_from})


def get_or_create_kind(con, name: str) -> int:
    name = (name or '').strip()
    if not name:
        raise WebStoreError('Название статьи доп. расхода пустое')
    if len(name) > 100:
        raise WebStoreError('Название статьи длиннее 100 символов')
    row = con.execute('SELECT id FROM extra_kinds WHERE name = ?',
                      (name,)).fetchone()
    if row:
        return int(row['id'])
    cur = con.execute('INSERT INTO extra_kinds (name) VALUES (?)', (name,))
    return int(cur.lastrowid)


def add_extra(con, product_id: int, kind: str, rub, valid_from: str,
              valid_to: str | None = None, user: str = 'web') -> int:
    if get_product(con, product_id) is None:
        raise WebStoreError('Товар не найден')
    d = rub if isinstance(rub, Decimal) else Decimal(str(rub))
    if valid_to and valid_to <= valid_from:
        raise WebStoreError('Дата окончания должна быть позже даты начала')
    with con:
        kind_id = get_or_create_kind(con, kind)
        row = con.execute(
            'SELECT id FROM extras WHERE product_id = ? AND kind_id = ?'
            ' AND valid_from = ?', (product_id, kind_id, valid_from)
        ).fetchone()
        if row:
            con.execute('UPDATE extras SET rub = ?, valid_to = ?,'
                        ' changed_by = ? WHERE id = ?',
                        (float(d), valid_to, user, row['id']))
            extra_id = int(row['id'])
            old = {'rub': 'прежняя запись', 'valid_from': valid_from}
        else:
            cur = con.execute(
                'INSERT INTO extras (product_id, kind_id, rub, valid_from,'
                ' valid_to, changed_by) VALUES (?, ?, ?, ?, ?, ?)',
                (product_id, kind_id, float(d), valid_from, valid_to, user))
            extra_id = int(cur.lastrowid)
            old = None
        audit(con, user, 'extras', product_id, old,
              {'kind': kind, 'rub': str(d), 'valid_from': valid_from,
               'valid_to': valid_to})
    return extra_id


def close_extra(con, extra_id: int, valid_to: str, user: str = 'web') -> None:
    row = con.execute('SELECT id, product_id, valid_from FROM extras'
                      ' WHERE id = ?', (extra_id,)).fetchone()
    if row is None:
        raise WebStoreError('Статья доп. расхода не найдена')
    if valid_to <= row['valid_from']:
        raise WebStoreError('Дата окончания должна быть позже даты начала')
    with con:
        con.execute('UPDATE extras SET valid_to = ?, changed_by = ?'
                    ' WHERE id = ?', (valid_to, user, extra_id))
        audit(con, user, 'extras', row['product_id'], None,
              {'closed_extra_id': extra_id, 'valid_to': valid_to})
# ── История запусков и файлы ────────────────────────────────────────────────


def save_run_meta(cfg, run_id: int, data: dict) -> None:
    path = Path(cfg.history_dir) / f'run_{int(run_id)}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                    encoding='utf-8')


def load_run_meta(cfg, run_id: int) -> dict:
    path = Path(cfg.history_dir) / f'run_{int(run_id)}.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def list_runs(con, cfg, limit: int = 100) -> list[dict]:
    rows = con.execute(
        'SELECT r.id, r.started_at, r.finished_at, r.status, r.source_1c,'
        ' r.user, r.wb_file, r.ozon_file, r.error,'
        ' (SELECT COUNT(*) FROM cost_snapshots s WHERE s.run_id = r.id)'
        ' AS snapshots FROM runs r ORDER BY r.id DESC LIMIT ?',
        (limit,)).fetchall()
    return [{'row': r, 'meta': load_run_meta(cfg, r['id'])} for r in rows]


def get_run(con, run_id: int):
    return con.execute(
        'SELECT id, started_at, finished_at, status, source_1c, user,'
        ' wb_file, ozon_file, error FROM runs WHERE id = ?',
        (run_id,)).fetchone()


def last_uploads(cfg) -> dict:
    result = {'wb': None, 'ozon': None}
    for mp in result:
        files = sorted(Path(cfg.uploads).glob(f'{mp}_*.xlsx'),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            result[mp] = files[0]
    return result


SETTINGS_KEYS = {'ppu_rub_per_kg': (Decimal('0'), Decimal('100000')),
                 'margin_pct': (Decimal('0'), Decimal('1000'))}


def set_setting(con, key: str, value, valid_from: str,
                user: str = 'web') -> None:
    """Новая запись параметра расчёта с даты (история settings)."""
    if key not in SETTINGS_KEYS:
        raise WebStoreError(f'Неизвестный параметр расчёта: {key!r}')
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    low, high = SETTINGS_KEYS[key]
    if not d.is_finite() or not (low <= d <= high):
        raise WebStoreError(
            f'Значение {key} вне допустимого диапазона {low}..{high}')
    old = con.execute(
        'SELECT value FROM settings WHERE key = ? AND valid_from <= ?'
        ' ORDER BY valid_from DESC, id DESC LIMIT 1',
        (key, valid_from)).fetchone()
    with con:
        con.execute(
            'INSERT INTO settings (key, value, valid_from, changed_by)'
            ' VALUES (?, ?, ?, ?)'
            ' ON CONFLICT(key, valid_from) DO UPDATE SET'
            ' value = excluded.value, changed_by = excluded.changed_by',
            (key, float(d), valid_from, user))
        audit(con, user, 'settings', None,
              {'key': key, 'value': old['value'] if old else None},
              {'key': key, 'value': str(d), 'valid_from': valid_from})


def settings_as_of(con, on_date: str) -> dict:
    try:
        return dict(calc.get_settings(con, on_date))
    except calc.CalcError as e:
        raise WebStoreError(str(e)) from None


def pending_migrations(cfg) -> dict:
    from app.core import db as core_db
    applied = (core_db.get_schema_version(cfg.db_path)
               if Path(cfg.db_path).exists() else 0)
    pending = sorted(p.name for p in
                     Path(core_db.MIGRATIONS_DIR).glob('[0-9][0-9][0-9]_*.sql')
                     if int(p.name[:3]) > applied)
    return {'applied': applied, 'pending': pending}


def migrate_schema(cfg) -> int:
    """Применяет миграции к существующей БД с подтверждением и бэкапом."""
    from app.core import db as core_db
    return core_db.apply_migrations(Path(cfg.db_path), confirm=True)