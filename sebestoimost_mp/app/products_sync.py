"""Синхронизация справочника products по позициям эталонов.

Общая логика, используемая и при загрузке эталона через веб, и при
одноразовом авто-перечитывании сохранённых эталонов после миграции v4
(профили Ozon стали читать артикул поставщика — товары надо пересвязать,
не требуя от пользователя повторной загрузки файлов).
"""

from dataclasses import replace
from datetime import datetime

from app import excel_io

PROFILE_KEYS = ('wb', 'ozon_prices', 'ozon_finance')

_PROFILE_IDS = {
    'wb': ('wb', 'wb'),
    'ozon_prices': ('ozon', 'prices'),
    'ozon_finance': ('ozon', 'finance'),
}

_MP_COLUMN = {
    'wb': 'article_wb',
    'ozon_prices': 'article_ozon',
    'ozon_finance': 'article_ozon',
}

_RESYNC_FLAG = 'resync_v4_done'


def _today():
    return datetime.now().strftime('%Y-%m-%d')


def sync_products_from_items(db, key, items):
    """Справочник products: новые артикулы + артикулы МП для мэтчинга.

    Если в эталоне нет колонки артикула поставщика (частый случай Ozon),
    товар заводится по артикулу МП — иначе позиция не с чем сопоставить.
    """
    column = _MP_COLUMN[key]
    created = 0
    for item in items or []:
        article_mp = (item.get('article_mp') or '').strip()
        supplier = (item.get('article_supplier') or '').strip() or article_mp
        if not supplier:
            continue
        cur = db.execute(
            "INSERT OR IGNORE INTO products (article_supplier, created_at) "
            "VALUES (?, ?)",
            (supplier, _today()),
        )
        created += cur.rowcount or 0
        # Если артикул МП совпал с артикулом поставщика (в файле себестоимостей
        # Ozon SKU нет), артикул МП писать некуда — иначе он затрёт SKU.
        if article_mp and article_mp != supplier:
            db.execute(
                f"UPDATE products SET {column} = ? WHERE article_supplier = ?",
                (article_mp, supplier),
            )
    db.commit()
    return created


def _load_profile(db, key):
    """excel_io.Profile по данным template_profiles; None, если строки нет."""
    marketplace, name = _PROFILE_IDS[key]
    row = db.execute(
        "SELECT * FROM template_profiles WHERE marketplace = ? AND name = ? LIMIT 1",
        (marketplace, name),
    ).fetchone()
    if row is None:
        return None
    return excel_io.Profile.from_row(dict(row))


def _profile_for_read(profile):
    """'{YYYY-MM}' в имени листа -> префикс листа (как при чтении в routes)."""
    sheet = profile.sheet or ''
    if '{YYYY-MM}' in sheet:
        prefix = sheet.split('{YYYY-MM}', 1)[0].strip()
        if prefix:
            return replace(profile, sheet=prefix)
    return profile


def _last_upload_path(db, key):
    row = db.execute(
        "SELECT stored_path FROM uploads WHERE marketplace = ? "
        "ORDER BY uploaded_at DESC, id DESC LIMIT 1",
        (key,),
    ).fetchone()
    return row['stored_path'] if row else None


def resync_templates_if_needed(db, log=None):
    """Одноразовое перечитывание сохранённых эталонов текущими профилями.

    Возвращает {ключ: новых товаров} при выполненной синхронизации,
    None — если уже выполнялась (флаг в app_meta). Ошибки чтения отдельных
    эталонов не роняют старт приложения: ключ пропускается, флаг всё равно
    ставится (перечитывание не должно блокировать запуск).
    """
    db.execute(
        "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    row = db.execute(
        "SELECT value FROM app_meta WHERE key = ?", (_RESYNC_FLAG,)
    ).fetchone()
    if row is not None:
        return None

    result = {}
    for key in PROFILE_KEYS:
        result[key] = 0
        path = _last_upload_path(db, key)
        if not path:
            continue
        profile = _load_profile(db, key)
        if profile is None:
            continue
        try:
            items = excel_io.read_items_profile(path, _profile_for_read(profile))
            result[key] = sync_products_from_items(db, key, items)
            if log:
                log(f'{key}: перечитано позиций {len(items)}, новых товаров {result[key]}')
        except Exception as exc:
            if log:
                log(f'{key}: эталон не перечитан ({exc})')

    db.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
        (_RESYNC_FLAG, datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
    )
    db.commit()
    return result
