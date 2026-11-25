"""Веб-интерфейс «Себестоимость МП» — Пульт v3 (Flask Blueprint).

Страницы: Пульт, Ассортимент, Эталоны МП, «Без цены 1С», Шаблоны,
история цен товара, расходы. Расчёт и заполнение файлов идут по профилям
шаблонов из таблицы template_profiles (ключи: wb, ozon_prices, ozon_finance).
"""

import io
import os
import re
import zipfile
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

import app.calc_run as calc_run
import app.db as db_module
import app.excel_io as excel_io
import app.price_source as price_source

web_bp = Blueprint('web', __name__, template_folder='templates')

# Ключи профилей шаблонов: uploads.marketplace хранит именно ключ профиля.
PROFILE_KEYS = ('wb', 'ozon_prices', 'ozon_finance')
PROFILE_LABELS = {
    'wb': 'WB',
    'ozon_prices': 'Ozon — цены',
    'ozon_finance': 'Ozon — финансы',
}
MP_MARKETPLACES = ('wb', 'ozon')
# Обратная совместимость со старыми роутами (/upload, /download/<marketplace>).
MARKETPLACES = MP_MARKETPLACES
SETTING_KEYS = ('ppu_rub_per_kg', 'margin_pct')
XLSX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
ZIP_MIMETYPE = 'application/zip'
DATE_FMT = '%Y-%m-%d'
DATETIME_FMT = '%Y-%m-%d %H:%M:%S'

# Колонка артикула МП в products для каждого маркетплейса
MP_ARTICLE_COLUMNS = {'wb': 'article_wb', 'ozon': 'article_ozon'}

# Переопределения себестоимости в карточке товара: (поле, подпись)
OVERRIDE_FIELDS = (
    ('margin_pct', 'Наценка, %'),
    ('ppu_rub_per_kg', 'ППУ, ₽/кг'),
    ('extras_rub', 'Допы, ₽'),
)
OVERRIDE_FIELD_NAMES = tuple(name for name, _ in OVERRIDE_FIELDS)

# Массовые действия над ассортиментом
BULK_ACTIONS = {
    'activate': 'Поставляется: вкл',
    'deactivate': 'Поставляется: выкл',
    'set_margin': 'Наценка, %',
    'set_ppu': 'ППУ, ₽/кг',
    'set_extras': 'Допы, ₽',
}
BULK_VALUE_FIELD = {
    'set_margin': 'margin_pct',
    'set_ppu': 'ppu_rub_per_kg',
    'set_extras': 'extras_rub',
}

# «Себестоимость за 2026-11» -> «Себестоимость за {YYYY-MM}» (шаблон месяца)
_MONTH_TOKEN_RE = re.compile(r'\d{4}-(?:0[1-9]|1[0-2])')

# ---------------------------------------------------------------- helpers ---

def get_db():
    return db_module.get_db(current_app)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'xlsx'


def _row_dict(row):
    """sqlite3.Row (или dict) -> dict; None, если строки нет."""
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    keys = getattr(row, 'keys', None)
    if callable(keys):
        return {key: row[key] for key in keys()}
    return None


def _today():
    return datetime.now().strftime(DATE_FMT)


def _parse_date_or_today(value):
    """Пусто -> сегодня. Невалидная дата -> flash + None (вызывающий прерывает)."""
    raw = (value or '').strip()
    if not raw:
        return _today()
    try:
        datetime.strptime(raw, DATE_FMT)
    except (TypeError, ValueError):
        flash(f'Некорректная дата: {raw}', 'err')
        return None
    return raw


def _parse_decimal(value, label):
    """Строка -> Decimal. Пусто/мусор -> flash + None."""
    raw = (value or '').strip().replace(',', '.')
    if not raw:
        flash(f'Укажите {label}', 'err')
        return None
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        flash(f'Некорректное значение: {label}', 'err')
        return None
    if not number.is_finite():
        flash(f'Некорректное значение: {label}', 'err')
        return None
    return number


def _parse_non_negative_decimal(value, label):
    number = _parse_decimal(value, label)
    if number is None:
        return None
    if number < 0:
        flash(f'{label} не может быть отрицательным', 'err')
        return None
    return number


def _parse_int(value, label, low=0, high=None):
    raw = (value or '').strip()
    try:
        number = int(raw)
    except (TypeError, ValueError):
        flash(f'Некорректное значение: {label}', 'err')
        return None
    if number < low or (high is not None and number > high):
        if high is None:
            flash(f'{label} должно быть не меньше {low}', 'err')
        else:
            flash(f'{label} должно быть от {low} до {high}', 'err')
        return None
    return number


def _last_upload(db, marketplace):
    return _row_dict(db.execute(
        "SELECT id, marketplace, filename, stored_path, uploaded_at "
        "FROM uploads WHERE marketplace = ? "
        "ORDER BY uploaded_at DESC, id DESC LIMIT 1",
        (marketplace,),
    ).fetchone())


def _last_setting_value(db, key):
    row = _row_dict(db.execute(
        "SELECT value FROM settings WHERE key = ? "
        "ORDER BY valid_from DESC, id DESC LIMIT 1",
        (key,),
    ).fetchone())
    return row['value'] if row else None


# Белый список возврата после форм: ключ поля next -> (endpoint, параметры)
_REDIRECT_TARGETS = {
    'products': ('web.products', {}),
    'problems': ('web.problems', {}),
    'assortment': ('web.assortment', {}),
    'templates': ('web.templates_page', {}),
    'expenses': ('web.expenses', {}),
    'index': ('web.index', {}),
    'mp_wb': ('web.mp_page', {'marketplace': 'wb'}),
    'mp_ozon': ('web.mp_page', {'marketplace': 'ozon'}),
    'product_history': ('web.product_history', {}),
}


def _redirect_back(default='web.products'):
    """Редирект после формы: скрытое поле next из белого списка, иначе default."""
    target = (request.form.get('next') or '').strip()
    endpoint, kwargs = _REDIRECT_TARGETS.get(target, (default, {}))
    if endpoint == 'web.product_history':
        raw_pid = (request.form.get('product_id') or '').strip()
        if raw_pid.isdigit():
            kwargs = {'pid': int(raw_pid)}
        else:
            endpoint, kwargs = default, {}
    return redirect(url_for(endpoint, **kwargs))


def _parse_optional_decimal(value, label):
    """Пустая строка -> (None, True); число -> (строка, True); мусор -> (None, False)."""
    raw = (value or '').strip().replace(',', '.')
    if not raw:
        return None, True
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        flash(f'Некорректное значение: {label}', 'err')
        return None, False
    if not number.is_finite() or number < 0:
        flash(f'Некорректное значение: {label}', 'err')
        return None, False
    return str(number), True

# --------------------------------------------------------------- профили ---

# Ключ профиля -> (marketplace, name) в таблице template_profiles
_PROFILE_IDS = {
    'wb': ('wb', 'wb'),
    'ozon_prices': ('ozon', 'prices'),
    'ozon_finance': ('ozon', 'finance'),
}

# Запасные описания профилей: нужны, если БД не мигрирована до v2
_PROFILE_DEFAULTS = {
    'wb': {
        'marketplace': 'wb', 'name': 'wb', 'sheet': 'Себестоимость за {YYYY-MM}',
        'header_row': 2, 'data_row': 3, 'col_article_mp': 'A',
        'col_article_supplier': 'B', 'col_cost_write': 'E',
    },
    'ozon_prices': {
        'marketplace': 'ozon', 'name': 'prices', 'sheet': 'Товары и цены',
        'header_row': 2, 'data_row': 5, 'col_article_mp': 'A',
        'col_article_supplier': None, 'col_cost_write': 'BD',
    },
    'ozon_finance': {
        'marketplace': 'ozon', 'name': 'finance', 'sheet': 'Sheet1',
        'header_row': 1, 'data_row': 2, 'col_article_mp': 'A',
        'col_article_supplier': None, 'col_cost_write': 'B',
    },
}


def _load_profiles(db):
    """{ключ профиля: excel_io.Profile} по данным template_profiles."""
    profiles = {}
    for key in PROFILE_KEYS:
        marketplace, name = _PROFILE_IDS[key]
        row = _row_dict(db.execute(
            "SELECT * FROM template_profiles WHERE marketplace = ? AND name = ? LIMIT 1",
            (marketplace, name),
        ).fetchone())
        if row is None:
            row = dict(_PROFILE_DEFAULTS[key])
            row['updated_at'] = None
        profiles[key] = excel_io.Profile.from_row(row)
    return profiles


def _profile_for_read(profile):
    """Профиль для чтения эталона: '{YYYY-MM}' в имени листа -> префикс листа."""
    sheet = profile.sheet or ''
    if '{YYYY-MM}' in sheet:
        prefix = sheet.split('{YYYY-MM}', 1)[0].strip()
        if prefix:
            return replace(profile, sheet=prefix)
    return profile


def _read_profile_items(path, profile):
    """Позиции эталона по профилю шаблона."""
    return excel_io.read_items_profile(path, _profile_for_read(profile))


def _upload_items(db, key):
    """(позиции, upload) по последнему эталону ключа; ошибка чтения -> []."""
    upload = _last_upload(db, key)
    if upload is None:
        return [], None
    profile = _load_profiles(db).get(key)
    if profile is None:
        return [], upload
    try:
        return _read_profile_items(upload['stored_path'], profile), upload
    except Exception:
        return [], upload


def _mp_column(key):
    """Колонка products для артикула маркетплейса по ключу профиля."""
    return MP_ARTICLE_COLUMNS['wb' if key == 'wb' else 'ozon']


def _article_sets(items):
    """(множество артикулов МП, множество артикулов поставщика) по позициям."""
    articles, suppliers = set(), set()
    for item in items or []:
        article_mp = (item.get('article_mp') or '').strip()
        supplier = (item.get('article_supplier') or '').strip()
        if article_mp:
            articles.add(article_mp)
        if supplier:
            suppliers.add(supplier)
    return articles, suppliers

def _save_upload_file(file_storage):
    """Сохраняет присланный .xlsx в UPLOAD_DIR. Возвращает (имя, путь)."""
    filename = (getattr(file_storage, 'filename', '') or '').strip()
    if not filename:
        raise ValueError('файл не выбран')
    if not allowed_file(filename):
        raise ValueError('файл должен быть .xlsx')

    upload_dir = current_app.config['UPLOAD_DIR']
    os.makedirs(upload_dir, exist_ok=True)
    safe = secure_filename(filename) or 'upload'
    if not safe.lower().endswith('.xlsx'):
        safe += '.xlsx'
    stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe}"
    stored_path = os.path.join(upload_dir, stored_name)
    file_storage.save(stored_path)
    return filename, stored_path


def _insert_upload(db, key, filename, stored_path):
    """Пишет запись о загрузке эталона (uploads.marketplace = ключ профиля)."""
    db.execute(
        "INSERT INTO uploads (marketplace, filename, stored_path, uploaded_at) "
        "VALUES (?, ?, ?, ?)",
        (key, filename, stored_path, datetime.now().strftime(DATETIME_FMT)),
    )
    db.commit()


def _sync_products_from_items(db, key, items):
    """Справочник products: новые артикулы + артикулы МП для мэтчинга.

    Если в эталоне нет колонки артикула поставщика (частый случай Ozon),
    товар заводится по артикулу МП — иначе позиция не с чем сопоставить.
    """
    column = _mp_column(key)
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
        if article_mp:
            db.execute(
                f"UPDATE products SET {column} = ? WHERE article_supplier = ?",
                (article_mp, supplier),
            )
    db.commit()
    return created


def _last_run_prices(db):
    """{product_id: price} по позициям последнего прогона."""
    run = calc_run.get_last_run(db)
    if run is None:
        return {}
    rows = db.execute(
        "SELECT product_id, price FROM run_items "
        "WHERE run_id = ? AND product_id IS NOT NULL ORDER BY id",
        (run['id'],),
    ).fetchall()
    prices = {}
    for row in rows:
        prices.setdefault(int(row['product_id']), row['price'])
    return prices


def _problem_ids(db):
    """Множество product_id с проблемой в последнем прогоне."""
    run = calc_run.get_last_run(db)
    if run is None:
        return set()
    rows = db.execute(
        "SELECT DISTINCT product_id FROM run_items "
        "WHERE run_id = ? AND status = 'problem' AND product_id IS NOT NULL",
        (run['id'],),
    ).fetchall()
    return {int(row['product_id']) for row in rows}


def _mp_flags(db):
    """{product_id: {'wb', 'ozon'}} — по артикулам МП и последнему прогону."""
    flags = {}
    for row in db.execute(
            "SELECT id, article_wb, article_ozon FROM products").fetchall():
        keys = set()
        if (row['article_wb'] or '').strip():
            keys.add('wb')
        if (row['article_ozon'] or '').strip():
            keys.add('ozon')
        flags[int(row['id'])] = keys
    run = calc_run.get_last_run(db)
    if run is not None:
        for row in db.execute(
                "SELECT product_id, marketplace FROM run_items "
                "WHERE run_id = ? AND product_id IS NOT NULL",
                (run['id'],)).fetchall():
            mp = 'wb' if str(row['marketplace']).startswith('wb') else 'ozon'
            flags.setdefault(int(row['product_id']), set()).add(mp)
    return flags


# ------------------------------------------------------------------- 1С ---

def _ping_1c():
    """Состояние 1С для плитки ПУЛЬТа: {ok, at, text}."""
    at = datetime.now().strftime('%Y-%m-%d %H:%M')
    try:
        info = price_source.load_params(current_app.config['ROOT'])
    except Exception as exc:
        return {'ok': False, 'at': at, 'text': f'ошибка config.ini: {exc}'}
    if info is None:
        return {'ok': False, 'at': at, 'text': 'не настроена (нет config.ini)'}
    params, mode = info
    return {'ok': True, 'at': at, 'text': f'Подключено ({mode})'}


def _onec_status():
    """Статус 1С: пинг при старте приложения, далее — по кнопке «Обновить»."""
    status = current_app.config.get('ONEC_STATUS')
    if status is None:
        status = _ping_1c()
        current_app.config['ONEC_STATUS'] = status
    return status


def _collect_prices(db, calc_date):
    """(prices, price_errors) из 1С для поставляемых товаров с manual_price=0."""
    products = [_row_dict(row) for row in db.execute(
        "SELECT id, article_supplier, name, onec_guid FROM products "
        "WHERE manual_price = 0 AND is_active = 1",
    ).fetchall()]
    if not products:
        return {}, {}
    try:
        info = price_source.load_params(current_app.config['ROOT'])
    except Exception as exc:
        message = f'1С не настроена: ошибка config.ini — {exc}'
        return {}, {int(p['id']): message for p in products}
    if info is None:
        message = '1С не настроена: нет config.ini'
        return {}, {int(p['id']): message for p in products}
    params, mode = info
    prices, errors, names = price_source.fetch_prices(products, params, mode)
    for pid, name in names.items():
        db.execute(
            "UPDATE products SET name = ? WHERE id = ? "
            "AND (name IS NULL OR name = '')",
            (name, pid),
        )
    db.commit()
    return prices, errors


@web_bp.before_app_request
def _ensure_onec_status():
    """Плитка 1С на Пульте: пинг при старте (первом запросе) приложения."""
    _onec_status()

@web_bp.app_context_processor
def _nav_badge():
    """Badge «Без цены 1С» в навигации: число из последнего прогона."""
    try:
        return {'nav_no_price': calc_run.get_no_price_count(get_db())}
    except Exception:
        # рендер навигации не должен падать из-за отсутствия БД/таблиц
        return {'nav_no_price': 0}


# --------------------------------------------------------------- страницы ---

@web_bp.route('/health')
def health():
    return jsonify({'status': 'ok'})


@web_bp.route('/')
def index():
    """Пульт: статус 1С, эталоны, ассортимент, последний прогон, файлы."""
    db = get_db()
    profiles = _load_profiles(db)
    uploads, items_by_key = {}, {}
    for key in PROFILE_KEYS:
        upload = _last_upload(db, key)
        uploads[key] = upload
        if upload is None:
            items_by_key[key] = []
            continue
        try:
            items_by_key[key] = _read_profile_items(upload['stored_path'],
                                                   profiles[key])
        except Exception:
            items_by_key[key] = []

    wb_articles, wb_suppliers = _article_sets(items_by_key['wb'])
    ozon_items = items_by_key['ozon_prices'] + items_by_key['ozon_finance']
    ozon_articles, _ = _article_sets(ozon_items)

    products = db.execute(
        "SELECT id, article_supplier, article_wb, article_ozon FROM products"
    ).fetchall()
    wb_matched = sum(
        1 for p in products
        if (p['article_wb'] or '').strip() in wb_articles
        or (p['article_supplier'] or '').strip() in wb_suppliers)
    ozon_matched = sum(
        1 for p in products
        if (p['article_ozon'] or '').strip() in ozon_articles)

    return render_template(
        'index.html',
        today=_today(),
        onec=_onec_status(),
        last_run=calc_run.get_last_run(db),
        uploads=uploads,
        counts={key: len(items_by_key[key]) for key in PROFILE_KEYS},
        products_total=len(products),
        wb_matched=wb_matched,
        ozon_matched=ozon_matched,
        no_price_count=calc_run.get_no_price_count(db),
        profile_labels=PROFILE_LABELS,
    )


@web_bp.route('/refresh', methods=['POST'])
def refresh():
    """Кнопка «Обновить»: повторный пинг 1С."""
    current_app.config['ONEC_STATUS'] = _ping_1c()
    flash('Статус 1С обновлён', 'ok')
    return _redirect_back('web.index')


@web_bp.route('/upload', methods=['POST'])
def upload():
    """Старая форма загрузки эталонов: wb_file -> wb, ozon_file -> ozon_prices."""
    db = get_db()
    profiles = _load_profiles(db)
    saved = 0
    for field, key in (('wb_file', 'wb'), ('ozon_file', 'ozon_prices')):
        storage = request.files.get(field)
        if storage is None or not (storage.filename or '').strip():
            continue
        try:
            filename, stored_path = _save_upload_file(storage)
        except ValueError as exc:
            flash(f'{PROFILE_LABELS[key]}: {exc}', 'err')
            continue
        _insert_upload(db, key, filename, stored_path)
        try:
            detected = excel_io.detect_profile(stored_path)
        except Exception as exc:
            flash(f'{PROFILE_LABELS[key]}: профиль не определён ({exc})', 'err')
            detected = {}
        _save_detected_profile(db, key, detected)
        profiles = _load_profiles(db)
        try:
            items = _read_profile_items(stored_path, profiles[key])
        except Exception as exc:
            flash(f'{PROFILE_LABELS[key]}: файл сохранён, но не прочитан ({exc})',
                  'err')
            continue
        _sync_products_from_items(db, key, items)
        saved += 1
        flash(f'{PROFILE_LABELS[key]}: загружено позиций {len(items)}', 'ok')
    if not saved:
        flash('Добавьте файл эталона (.xlsx)', 'err')
    return _redirect_back('web.index')


@web_bp.route('/calc', methods=['POST'])
def calc():
    """Расчёт по последним эталонам всех профилей; then=download -> сразу файлы."""
    calc_date = _parse_date_or_today(request.form.get('calc_date'))
    if calc_date is None:
        return _redirect_back('web.index')

    db = get_db()
    profiles = _load_profiles(db)
    marketplaces = {}
    for key in PROFILE_KEYS:
        upload = _last_upload(db, key)
        if upload is None:
            continue
        try:
            marketplaces[key] = _read_profile_items(upload['stored_path'],
                                                    profiles[key])
        except Exception as exc:
            flash(f'{PROFILE_LABELS[key]}: эталон не прочитан ({exc})', 'err')

    if not marketplaces:
        flash('Сначала загрузите файлы эталонов', 'err')
        return _redirect_back('web.index')

    prices, price_errors = _collect_prices(db, calc_date)
    run_id = calc_run.run_calculation(db, calc_date, marketplaces,
                                      prices, price_errors)
    run = _row_dict(db.execute(
        "SELECT id, calc_date, total, ok, problems FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone())
    flash(f'Расчёт выполнен: прогон №{run["id"]} — всего {run["total"]}, '
          f'ОК {run["ok"]}, проблем {run["problems"]}', 'ok')

    if (request.form.get('then') or '').strip() == 'download':
        return redirect(url_for('web.download_all'))
    return _redirect_back('web.index')

# ------------------------------------------------------------- выгрузка ---

def _run_cost_maps(db, run_id, key):
    """({article_mp: cost}, {article_supplier: cost}) по успешным позициям."""
    by_article, by_supplier = {}, {}
    rows = db.execute(
        "SELECT article_mp, article_supplier, cost FROM run_items "
        "WHERE run_id = ? AND marketplace = ? AND status = 'ok'",
        (run_id, key),
    ).fetchall()
    for row in rows:
        cost = int(row['cost'])
        article_mp = (row['article_mp'] or '').strip()
        supplier = (row['article_supplier'] or '').strip()
        if article_mp:
            by_article.setdefault(article_mp, cost)
        if supplier:
            by_supplier.setdefault(supplier, cost)
    return by_article, by_supplier


def _build_cost_file(db, run, key):
    """Заполняет эталон себестоимостью. Возвращает (путь, имя файла)."""
    upload = _last_upload(db, key)
    if upload is None:
        raise LookupError('эталон не загружен')
    profile = _load_profiles(db)[key]
    try:
        items = _read_profile_items(upload['stored_path'], profile)
    except Exception as exc:
        raise LookupError(f'эталон не прочитан: {exc}') from None

    by_article, by_supplier = _run_cost_maps(db, run['id'], key)
    costs = {}
    for item in items:
        row_num = item.get('row')
        if row_num is None:
            continue
        article_mp = (item.get('article_mp') or '').strip()
        supplier = (item.get('article_supplier') or '').strip()
        cost = by_article.get(article_mp)
        if cost is None and supplier:
            cost = by_supplier.get(supplier)
        if cost is not None:
            costs[int(row_num)] = cost

    upload_dir = current_app.config['UPLOAD_DIR']
    os.makedirs(upload_dir, exist_ok=True)
    out_name = f"{key}_sebestoimost_{run['calc_date']}.xlsx"
    out_path = os.path.join(upload_dir, out_name)
    excel_io.write_costs_profile(upload['stored_path'], out_path, profile,
                                 costs, calc_date=run['calc_date'])
    return out_path, out_name


# Старые адреса выгрузки: /download/<marketplace>
_DOWNLOAD_ALIASES = {
    'wb': 'wb',
    'ozon': 'ozon_prices',
    'ozon_prices': 'ozon_prices',
    'ozon_finance': 'ozon_finance',
}


@web_bp.route('/download/<key>')
def download(key):
    """Готовый файл профиля (wb, ozon_prices, ozon_finance) последнего расчёта."""
    key = _DOWNLOAD_ALIASES.get(str(key).strip().lower())
    if key is None:
        flash('Неизвестный профиль шаблона', 'err')
        return _redirect_back('web.index')

    db = get_db()
    run = calc_run.get_last_run(db)
    if run is None:
        flash('Расчёт ещё не выполнялся', 'err')
        return _redirect_back('web.index')

    try:
        path, name = _build_cost_file(db, run, key)
    except LookupError as exc:
        flash(f'{PROFILE_LABELS[key]}: {exc}', 'err')
        return _redirect_back('web.index')
    except Exception as exc:
        flash(f'{PROFILE_LABELS[key]}: не удалось заполнить файл ({exc})', 'err')
        return _redirect_back('web.index')

    return send_file(path, mimetype=XLSX_MIMETYPE, as_attachment=True,
                     download_name=name)


@web_bp.route('/download_all')
def download_all():
    """ZIP с готовыми файлами всех загруженных профилей."""
    db = get_db()
    run = calc_run.get_last_run(db)
    if run is None:
        flash('Расчёт ещё не выполнялся', 'err')
        return _redirect_back('web.index')

    buffer = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for key in PROFILE_KEYS:
            try:
                path, name = _build_cost_file(db, run, key)
            except LookupError as exc:
                flash(f'{PROFILE_LABELS[key]}: {exc}', 'err')
                continue
            except Exception as exc:
                flash(f'{PROFILE_LABELS[key]}: не удалось заполнить файл ({exc})',
                      'err')
                continue
            with open(path, 'rb') as handle:
                archive.writestr(name, handle.read())
            added += 1

    if not added:
        flash('Нет готовых файлов: загрузите эталоны и выполните расчёт', 'err')
        return _redirect_back('web.index')

    buffer.seek(0)
    return send_file(buffer, mimetype=ZIP_MIMETYPE, as_attachment=True,
                     download_name=f"sebestoimost_{run['calc_date']}.zip")

# ------------------------------------------------------------ ассортимент ---

_ASSORTMENT_SQL = """
    SELECT p.id AS id, p.article_supplier AS article_supplier, p.name AS name,
           p.manual_price AS manual_price, p.is_active AS is_active,
           p.margin_pct AS margin_pct, p.ppu_rub_per_kg AS ppu_rub_per_kg,
           p.extras_rub AS extras_rub, p.article_wb AS article_wb,
           p.article_ozon AS article_ozon,
           (SELECT m.price FROM manual_prices m WHERE m.product_id = p.id
             ORDER BY m.valid_from DESC, m.id DESC LIMIT 1) AS last_price,
           (SELECT w.weight_g FROM ppu_weights w WHERE w.product_id = p.id
             ORDER BY w.valid_from DESC, w.id DESC LIMIT 1) AS last_weight
    FROM products p
    ORDER BY p.article_supplier
"""


@web_bp.route('/assortment')
def assortment():
    """Ассортимент: фильтры, массовые действия, переопределения, история."""
    db = get_db()
    query = (request.args.get('q') or '').strip()
    mp = (request.args.get('mp') or '').strip().lower()
    only_no_price = (request.args.get('no_price') or '').strip() == '1'
    only_inactive = (request.args.get('inactive') or '').strip() == '1'

    flags = _mp_flags(db)
    costs = calc_run.get_last_costs_map(db)
    prices = _last_run_prices(db)
    problems = _problem_ids(db)

    items = []
    needle = query.lower()
    for row in db.execute(_ASSORTMENT_SQL).fetchall():
        product = _row_dict(row)
        product_id = int(product['id'])
        if needle:
            article = (product['article_supplier'] or '').lower()
            name = (product['name'] or '').lower()
            if needle not in article and needle not in name:
                continue
        product_keys = flags.get(product_id, set())
        if mp in MP_MARKETPLACES and mp not in product_keys:
            continue
        if only_no_price and product_id not in problems:
            continue
        if only_inactive and product['is_active']:
            continue
        product['mp_keys'] = product_keys
        product['last_run_price'] = prices.get(product_id)
        product['cost'], product['prev_cost'], product['jump'] = costs.get(
            product_id, (None, None, 0))
        product['no_price'] = product_id in problems
        items.append(product)

    return render_template(
        'assortment.html',
        items=items,
        q=query,
        mp=mp,
        only_no_price=only_no_price,
        only_inactive=only_inactive,
        override_fields=OVERRIDE_FIELDS,
        bulk_actions=BULK_ACTIONS,
        defaults={
            'ppu_rub_per_kg': _last_setting_value(db, 'ppu_rub_per_kg'),
            'margin_pct': _last_setting_value(db, 'margin_pct'),
        },
        no_price_count=len(problems),
    )


@web_bp.route('/products/<int:product_id>/override', methods=['POST'])
def product_override(product_id):
    """Персональные наценка / ППУ / допы; пустое поле -> NULL (глобальный дефолт)."""
    db = get_db()
    if db.execute("SELECT id FROM products WHERE id = ?",
                  (product_id,)).fetchone() is None:
        flash(f'Товар {product_id} не найден', 'err')
        return _redirect_back('web.assortment')

    labels = dict(OVERRIDE_FIELDS)
    values = {}
    for field in OVERRIDE_FIELD_NAMES:
        value, ok = _parse_optional_decimal(request.form.get(field),
                                            labels[field])
        if not ok:
            return _redirect_back('web.assortment')
        values[field] = value

    db.execute(
        "UPDATE products SET margin_pct = ?, ppu_rub_per_kg = ?, extras_rub = ? "
        "WHERE id = ?",
        (values['margin_pct'], values['ppu_rub_per_kg'], values['extras_rub'],
         product_id),
    )
    db.commit()
    flash('Переопределения сохранены (пусто = глобальная настройка)', 'ok')
    return _redirect_back('web.assortment')


@web_bp.route('/products/<int:product_id>/toggle_active', methods=['POST'])
def product_toggle_active(product_id):
    """Переключатель «Поставляется»."""
    db = get_db()
    cur = db.execute(
        "UPDATE products SET is_active = 1 - is_active WHERE id = ?",
        (product_id,),
    )
    db.commit()
    if not cur.rowcount:
        flash(f'Товар {product_id} не найден', 'err')
    else:
        flash('Признак «Поставляется» изменён', 'ok')
    return _redirect_back('web.assortment')


@web_bp.route('/assortment/bulk', methods=['POST'])
def assortment_bulk():
    """Массовые действия: поставляется, наценка, ППУ, допы."""
    db = get_db()
    action = (request.form.get('action') or '').strip()
    if action not in BULK_ACTIONS:
        flash('Неизвестное массовое действие', 'err')
        return _redirect_back('web.assortment')

    ids = []
    for raw in request.form.getlist('ids'):
        try:
            ids.append(int(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    if not ids:
        flash('Не выбраны товары для массового действия', 'err')
        return _redirect_back('web.assortment')

    if action in ('activate', 'deactivate'):
        value = 1 if action == 'activate' else 0
        db.executemany(
            "UPDATE products SET is_active = ? WHERE id = ?",
            [(value, product_id) for product_id in ids],
        )
    else:
        field = BULK_VALUE_FIELD[action]
        parsed, ok = _parse_optional_decimal(request.form.get('value'),
                                             BULK_ACTIONS[action])
        if not ok:
            return _redirect_back('web.assortment')
        db.executemany(
            f"UPDATE products SET {field} = ? WHERE id = ?",
            [(parsed, product_id) for product_id in ids],
        )
    db.commit()
    flash(f'{BULK_ACTIONS[action]}: обработано товаров {len(ids)}', 'ok')
    return _redirect_back('web.assortment')

# ------------------------------------------------------- эталоны МП / шаблоны ---

_MP_KEYS = {'wb': ('wb',), 'ozon': ('ozon_prices', 'ozon_finance')}


@web_bp.route('/mp/<marketplace>')
def mp_page(marketplace):
    """Страница маркетплейса: позиции последних эталонов + карточки товаров."""
    if marketplace not in _MP_KEYS:
        flash('Неизвестный маркетплейс', 'err')
        return _redirect_back('web.index')

    db = get_db()
    profiles = _load_profiles(db)
    only_no_price = (request.args.get('no_price') or '').strip() == '1'
    problems = _problem_ids(db)
    costs = calc_run.get_last_costs_map(db)
    prices = _last_run_prices(db)
    column = MP_ARTICLE_COLUMNS[marketplace]

    rows, seen = [], set()
    for key in _MP_KEYS[marketplace]:
        upload = _last_upload(db, key)
        if upload is None:
            continue
        try:
            items = _read_profile_items(upload['stored_path'], profiles[key])
        except Exception as exc:
            flash(f'{PROFILE_LABELS[key]}: эталон не прочитан ({exc})', 'err')
            continue
        for item in items:
            article_mp = (item.get('article_mp') or '').strip()
            if article_mp and article_mp in seen:
                continue
            if article_mp:
                seen.add(article_mp)
            supplier = (item.get('article_supplier') or '').strip()

            product = None
            if supplier:
                product = _row_dict(db.execute(
                    "SELECT id, name, is_active FROM products "
                    "WHERE article_supplier = ?", (supplier,)).fetchone())
            if product is None and article_mp:
                product = _row_dict(db.execute(
                    f"SELECT id, name, is_active FROM products WHERE {column} = ?",
                    (article_mp,)).fetchone())

            product_id = int(product['id']) if product else None
            if only_no_price and product_id is not None and product_id not in problems:
                continue
            cost, prev_cost, jump = costs.get(product_id, (None, None, 0))
            rows.append({
                'profile_key': key,
                'row': item.get('row'),
                'article_mp': article_mp,
                'article_supplier': supplier,
                'product_id': product_id,
                'name': (product or {}).get('name') or '',
                'is_active': (product or {}).get('is_active', 1),
                'price': prices.get(product_id),
                'cost': cost,
                'prev_cost': prev_cost,
                'jump': jump,
                'no_price': product_id is None or product_id in problems,
            })

    return render_template(
        'mp.html',
        marketplace=marketplace,
        profile_keys=_MP_KEYS[marketplace],
        rows=rows,
        only_no_price=only_no_price,
        no_price_count=len(problems),
        override_fields=OVERRIDE_FIELDS,
    )

@web_bp.route('/problems')
def problems():
    """«Без цены 1С»: проблемные позиции последнего прогона."""
    db = get_db()
    run = calc_run.get_last_run(db)
    items = calc_run.get_run_problems(db, run['id']) if run is not None else []
    count = len({row['product_id'] for row in items
                 if row['product_id'] is not None})
    return render_template('problems.html', run=run, problems=items,
                           no_price_count=count, today=_today())


def _save_detected_profile(db, key, detected):
    """Пишет автодетект в template_profiles (None-значения профиль не затирают)."""
    marketplace, name = _PROFILE_IDS[key]
    fields = {}
    for field in ('sheet', 'header_row', 'data_row', 'col_article_mp',
                  'col_article_supplier', 'col_cost_write'):
        value = detected.get(field)
        if value in (None, ''):
            continue
        fields[field] = value
    if fields.get('sheet'):
        fields['sheet'] = _MONTH_TOKEN_RE.sub('{YYYY-MM}', str(fields['sheet']))
    if not fields:
        return {}
    fields['updated_at'] = datetime.now().strftime(DATETIME_FMT)
    assignments = ', '.join(f'{field} = ?' for field in fields)
    db.execute(
        f"UPDATE template_profiles SET {assignments} "
        f"WHERE marketplace = ? AND name = ?",
        tuple(fields.values()) + (marketplace, name),
    )
    db.commit()
    return fields


def _describe_profile(label, profile, count, created):
    return (f'{label}: лист «{profile.sheet}», заголовок {profile.header_row}, '
            f'данные с {profile.data_row}, артикул {profile.col_article_mp}, '
            f'поставщик {profile.col_article_supplier or "—"}, '
            f'себестоимость {profile.col_cost_write}. '
            f'Позиций: {count}, новых товаров: {created}')


@web_bp.route('/templates')
def templates_page():
    """Шаблоны эталонов: последняя загрузка и текущий профиль каждого ключа."""
    db = get_db()
    profiles = _load_profiles(db)
    blocks = []
    for key in PROFILE_KEYS:
        items, upload = _upload_items(db, key)
        blocks.append({
            'key': key,
            'label': PROFILE_LABELS[key],
            'profile': profiles[key],
            'upload': upload,
            'items_count': len(items),
        })
    return render_template('templates.html', blocks=blocks)


@web_bp.route('/templates/upload', methods=['POST'])
def templates_upload():
    """Загрузка эталона с автодетектом профиля и мэтчингом товаров."""
    key = (request.form.get('profile_key') or '').strip()
    if key not in PROFILE_KEYS:
        flash('Неизвестный профиль шаблона', 'err')
        return _redirect_back('web.templates_page')

    storage = request.files.get('file')
    if storage is None or not (storage.filename or '').strip():
        flash('Выберите файл эталона (.xlsx)', 'err')
        return _redirect_back('web.templates_page')

    try:
        filename, stored_path = _save_upload_file(storage)
    except ValueError as exc:
        flash(f'{PROFILE_LABELS[key]}: {exc}', 'err')
        return _redirect_back('web.templates_page')

    db = get_db()
    try:
        detected = excel_io.detect_profile(stored_path)
    except Exception as exc:
        flash(f'{PROFILE_LABELS[key]}: профиль не определён ({exc})', 'err')
        return _redirect_back('web.templates_page')

    _save_detected_profile(db, key, detected)
    _insert_upload(db, key, filename, stored_path)

    profile = _load_profiles(db)[key]
    try:
        items = _read_profile_items(stored_path, profile)
    except Exception as exc:
        flash(f'{PROFILE_LABELS[key]}: файл сохранён, но не прочитан ({exc})',
              'err')
        return _redirect_back('web.templates_page')

    created = _sync_products_from_items(db, key, items)
    flash(_describe_profile(PROFILE_LABELS[key], profile, len(items), created),
          'ok')
    for warning in detected.get('warnings') or ():
        flash(f'{PROFILE_LABELS[key]}: {warning}', 'err')
    return _redirect_back('web.templates_page')

# ------------------------------------------------- старые роуты товаров ---

@web_bp.route('/products')
def products():
    """Список товаров (старая страница): поиск по артикулу и названию."""
    db = get_db()
    query = (request.args.get('q') or '').strip()
    rows = db.execute(
        "SELECT p.id AS id, p.article_supplier AS article_supplier, "
        "p.name AS name, p.manual_price AS manual_price, "
        "(SELECT m.price FROM manual_prices m WHERE m.product_id = p.id "
        " ORDER BY m.valid_from DESC, m.id DESC LIMIT 1) AS last_price, "
        "(SELECT w.weight_g FROM ppu_weights w WHERE w.product_id = p.id "
        " ORDER BY w.valid_from DESC, w.id DESC LIMIT 1) AS last_weight "
        "FROM products p ORDER BY p.article_supplier"
    ).fetchall()
    items = [_row_dict(row) for row in rows]
    if query:
        needle = query.lower()
        items = [p for p in items
                 if needle in (p['article_supplier'] or '').lower()
                 or needle in (p['name'] or '').lower()]
    return render_template('products.html', products=items, q=query)


@web_bp.route('/products/<int:pid>/price', methods=['POST'])
def product_price(pid):
    """Новая ручная закупочная цена; товар переводится в режим «ручная цена»."""
    price = _parse_non_negative_decimal(request.form.get('price'), 'Цена')
    if price is None:
        return _redirect_back()
    valid_from = _parse_date_or_today(request.form.get('valid_from'))
    if valid_from is None:
        return _redirect_back()

    db = get_db()
    db.execute(
        "INSERT INTO manual_prices (product_id, price, valid_from) "
        "VALUES (?, ?, ?)",
        (pid, str(price), valid_from),
    )
    db.execute("UPDATE products SET manual_price = 1 WHERE id = ?", (pid,))
    db.commit()
    flash('Ручная цена сохранена', 'ok')
    return _redirect_back()


@web_bp.route('/products/<int:pid>/weight', methods=['POST'])
def product_weight(pid):
    """Новый вес ППУ товара на дату."""
    weight = _parse_int(request.form.get('weight_g'), 'Вес, г', 0, 10000)
    if weight is None:
        return _redirect_back()
    valid_from = _parse_date_or_today(request.form.get('valid_from'))
    if valid_from is None:
        return _redirect_back()

    db = get_db()
    db.execute(
        "INSERT INTO ppu_weights (product_id, weight_g, valid_from) "
        "VALUES (?, ?, ?)",
        (pid, weight, valid_from),
    )
    db.commit()
    flash('Вес ППУ сохранён', 'ok')
    return _redirect_back()


@web_bp.route('/products/<int:pid>/toggle_manual', methods=['POST'])
def product_toggle_manual(pid):
    """Переключение источника цены: 1С <-> ручная цена."""
    db = get_db()
    cur = db.execute(
        "UPDATE products SET manual_price = 1 - manual_price WHERE id = ?",
        (pid,),
    )
    db.commit()
    if not cur.rowcount:
        flash(f'Товар {pid} не найден', 'err')
    else:
        flash('Источник цены изменён', 'ok')
    return _redirect_back()


@web_bp.route('/products/<int:pid>/history')
def product_history(pid):
    """Карточка товара: история цен, весов, допов и прогонов."""
    db = get_db()
    product = _row_dict(db.execute(
        "SELECT id, article_supplier, name, manual_price, is_active, margin_pct, "
        "ppu_rub_per_kg, extras_rub, article_wb, article_ozon, created_at "
        "FROM products WHERE id = ?", (pid,)).fetchone())
    if product is None:
        flash(f'Товар {pid} не найден', 'err')
        return _redirect_back('web.products')

    prices = [_row_dict(row) for row in db.execute(
        "SELECT price, valid_from FROM manual_prices WHERE product_id = ? "
        "ORDER BY valid_from DESC, id DESC", (pid,)).fetchall()]
    weights = [_row_dict(row) for row in db.execute(
        "SELECT weight_g, valid_from FROM ppu_weights WHERE product_id = ? "
        "ORDER BY valid_from DESC, id DESC", (pid,)).fetchall()]
    extras = [_row_dict(row) for row in db.execute(
        "SELECT k.name AS kind, e.rub, e.valid_from, e.valid_to "
        "FROM extras e JOIN extra_kinds k ON k.id = e.kind_id "
        "WHERE e.product_id = ? ORDER BY e.valid_from DESC, e.id DESC",
        (pid,)).fetchall()]
    history = [_row_dict(row) for row in calc_run.get_price_history(db, pid)]

    return render_template(
        'product_history.html',
        product=product,
        prices=prices,
        weights=weights,
        extras=extras,
        history=history,
        override_fields=OVERRIDE_FIELDS,
    )

# ------------------------------------------------------ расходы и история ---


@web_bp.route('/expenses')
def expenses():
    """Расходы: настройки на дату, виды расходов и допы по товарам."""
    db = get_db()
    kinds = [_row_dict(row) for row in db.execute(
        "SELECT id, name FROM extra_kinds ORDER BY name").fetchall()]
    products_rows = [_row_dict(row) for row in db.execute(
        "SELECT id, article_supplier FROM products "
        "ORDER BY article_supplier").fetchall()]
    return render_template(
        'expenses.html',
        ppu_rub_per_kg=_last_setting_value(db, 'ppu_rub_per_kg'),
        margin_pct=_last_setting_value(db, 'margin_pct'),
        extra_kinds=kinds,
        products=products_rows,
    )


@web_bp.route('/expenses/setting', methods=['POST'])
def expense_setting():
    """Новое значение настройки с даты (история настроек не переписывается)."""
    key = (request.form.get('key') or '').strip()
    if key not in SETTING_KEYS:
        flash('Неизвестная настройка', 'err')
        return _redirect_back('web.expenses')
    value = _parse_non_negative_decimal(request.form.get('value'),
                                        'Значение настройки')
    if value is None:
        return _redirect_back('web.expenses')
    valid_from = _parse_date_or_today(request.form.get('valid_from'))
    if valid_from is None:
        return _redirect_back('web.expenses')

    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value, valid_from) VALUES (?, ?, ?)",
        (key, str(value), valid_from),
    )
    db.commit()
    flash(f'Настройка {key} = {value} с {valid_from}', 'ok')
    return _redirect_back('web.expenses')


@web_bp.route('/expenses/kind', methods=['POST'])
def expense_kind():
    """Добавление вида дополнительного расхода."""
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Укажите название вида расхода', 'err')
        return _redirect_back('web.expenses')

    db = get_db()
    exists = db.execute(
        "SELECT id FROM extra_kinds WHERE name = ?", (name,)).fetchone()
    if exists is not None:
        flash(f'Вид расхода «{name}» уже есть', 'err')
        return _redirect_back('web.expenses')
    db.execute("INSERT INTO extra_kinds (name) VALUES (?)", (name,))
    db.commit()
    flash(f'Вид расхода «{name}» добавлен', 'ok')
    return _redirect_back('web.expenses')


@web_bp.route('/expenses/extra', methods=['POST'])
def expense_extra():
    """Дополнительный расход по товару за период."""
    db = get_db()
    product_id = _parse_int(request.form.get('product_id'), 'Товар', 1)
    kind_id = _parse_int(request.form.get('kind_id'), 'Вид расхода', 1)
    if product_id is None or kind_id is None:
        return _redirect_back('web.expenses')
    rub = _parse_non_negative_decimal(request.form.get('rub'), 'Сумма расхода')
    if rub is None:
        return _redirect_back('web.expenses')
    valid_from = _parse_date_or_today(request.form.get('valid_from'))
    if valid_from is None:
        return _redirect_back('web.expenses')

    valid_to = None
    valid_to_raw = (request.form.get('valid_to') or '').strip()
    if valid_to_raw:
        valid_to = _parse_date_or_today(valid_to_raw)
        if valid_to is None:
            return _redirect_back('web.expenses')
        if valid_to <= valid_from:
            flash('Дата окончания должна быть позже даты начала', 'err')
            return _redirect_back('web.expenses')

    db.execute(
        "INSERT INTO extras (product_id, kind_id, rub, valid_from, valid_to) "
        "VALUES (?, ?, ?, ?, ?)",
        (product_id, kind_id, str(rub), valid_from, valid_to),
    )
    db.commit()
    flash('Дополнительный расход добавлен', 'ok')
    return _redirect_back('web.expenses')


# Роут /history и шаблон history.html удалены по ТЗ v3:
# история конкретного товара — /products/<pid>/history.
