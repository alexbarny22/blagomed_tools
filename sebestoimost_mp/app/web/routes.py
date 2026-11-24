import os
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

web_bp = Blueprint('web', __name__, template_folder='templates')

MARKETPLACES = ('wb', 'ozon')
SETTING_KEYS = ('ppu_rub_per_kg', 'margin_pct')
XLSX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
DATE_FMT = '%Y-%m-%d'
DATETIME_FMT = '%Y-%m-%d %H:%M:%S'


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


# ----------------------------------------------------------------- routes ---

@web_bp.route('/')
def index():
    db = get_db()
    today = _today()
    last_run = _row_dict(calc_run.get_last_run(db))
    return render_template('index.html', today=today, last_run=last_run)


@web_bp.route('/health')
def health():
    return jsonify({'status': 'ok'})


@web_bp.route('/upload', methods=['POST'])
def upload():
    wb_file = request.files.get('wb_file')
    ozon_file = request.files.get('ozon_file')
    if not wb_file and not ozon_file:
        flash('Необходимо загрузить хотя бы один файл (WB или Ozon)', 'err')
        return redirect(url_for('web.index'))
    results = []
    errors = []
    for marketplace, file_obj in [('wb', wb_file), ('ozon', ozon_file)]:
        if not file_obj or file_obj.filename == '':
            continue
        if not allowed_file(file_obj.filename):
            errors.append(f"{marketplace.upper()}: файл должен быть .xlsx")
            continue
        try:
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            safe_name = f"{marketplace}_{timestamp}_{secure_filename(file_obj.filename)}"
            stored_path = os.path.join(current_app.config['UPLOAD_DIR'], safe_name)
            file_obj.save(stored_path)
            spec = excel_io.TEMPLATES[marketplace]
            items = excel_io.read_items(stored_path, spec)
            db = get_db()
            new_products = 0
            for item in items:
                cur = db.execute(
                    "INSERT OR IGNORE INTO products (article_supplier, created_at) VALUES (?, ?)",
                    (item['article_supplier'], datetime.now().strftime('%Y-%m-%d'))
                )
                if cur.rowcount > 0:
                    new_products += cur.rowcount
            db.execute(
                "INSERT INTO uploads (marketplace, filename, stored_path, uploaded_at) VALUES (?, ?, ?, ?)",
                (marketplace, safe_name, stored_path, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            db.commit()
            results.append(f"{marketplace.upper()}: загружено {len(items)} строк, новых товаров {new_products}")
        except KeyError as e:
            errors.append(f"{marketplace.upper()}: {str(e)}")
        except Exception as e:
            errors.append(f"{marketplace.upper()}: {str(e)}")
    for msg in results:
        flash(msg, 'ok')
    for msg in errors:
        flash(msg, 'err')
    return redirect(url_for('web.index'))


@web_bp.route('/calc', methods=['POST'])
def calc():
    db = get_db()
    calc_date = _parse_date_or_today(request.form.get('calc_date', ''))
    if calc_date is None:
        return redirect(url_for('web.index'))

    marketplaces = {}
    for marketplace in MARKETPLACES:
        upload_row = _last_upload(db, marketplace)
        if not upload_row:
            continue
        try:
            spec = excel_io.TEMPLATES[marketplace]
            items = excel_io.read_items(upload_row['stored_path'], spec)
        except KeyError as e:
            flash(f"{marketplace.upper()}: {str(e)}", 'err')
            continue
        except Exception as e:
            flash(f"{marketplace.upper()}: {str(e)}", 'err')
            continue
        marketplaces[marketplace] = items

    if not marketplaces:
        flash('Сначала загрузите файлы WB/Ozon', 'err')
        return redirect(url_for('web.index'))

    try:
        run_id = calc_run.run_calculation(
            db, calc_date=calc_date, marketplaces=marketplaces
        )
    except Exception as e:
        flash(f'Ошибка расчёта: {str(e)}', 'err')
        return redirect(url_for('web.index'))

    run_row = _row_dict(db.execute(
        "SELECT id, calc_date, total, ok, problems FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone())
    if run_row is None:
        run_row = _row_dict(calc_run.get_last_run(db))
    total = run_row['total'] if run_row else '?'
    ok = run_row['ok'] if run_row else '?'
    problems = run_row['problems'] if run_row else '?'

    flash(
        f'Прогон №{run_id}: всего {total}, рассчитано {ok}, проблем {problems}',
        'ok',
    )
    return redirect(url_for('web.index'))


@web_bp.route('/download/<marketplace>')
def download(marketplace):
    if marketplace not in MARKETPLACES:
        flash('Неизвестный маркетплейс', 'err')
        return redirect(url_for('web.index'))

    db = get_db()
    upload_row = _last_upload(db, marketplace)
    if not upload_row:
        flash(f'Сначала загрузите файл {marketplace.upper()}', 'err')
        return redirect(url_for('web.index'))

    run = _row_dict(calc_run.get_last_run(db))
    if not run:
        flash('Сначала выполните расчёт', 'err')
        return redirect(url_for('web.index'))

    try:
        spec = excel_io.TEMPLATES[marketplace]
        items = excel_io.read_items(upload_row['stored_path'], spec)
        costs_by_article = calc_run.get_run_costs(db, run['id'], marketplace) or {}
        costs = {
            item['row']: costs_by_article[item['article_supplier']]
            for item in items
            if item['article_supplier'] in costs_by_article
        }
        upload_dir = current_app.config['UPLOAD_DIR']
        os.makedirs(upload_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        dst = os.path.join(upload_dir, f"{marketplace}_filled_{timestamp}.xlsx")
        excel_io.write_costs(upload_row['stored_path'], dst, spec, costs)
    except Exception as e:
        flash(f'Ошибка формирования файла: {str(e)}', 'err')
        return redirect(url_for('web.index'))

    return send_file(
        dst,
        as_attachment=True,
        download_name=f"{marketplace}_sebestoimost_{run['calc_date']}.xlsx",
        mimetype=XLSX_MIMETYPE,
    )


@web_bp.route('/products')
def products():
    db = get_db()
    rows = db.execute(
        "SELECT p.id AS id, "
        "       p.article_supplier AS article_supplier, "
        "       p.name AS name, "
        "       p.manual_price AS manual_price, "
        "       (SELECT m.price FROM manual_prices AS m "
        "         WHERE m.product_id = p.id "
        "         ORDER BY m.valid_from DESC, m.id DESC LIMIT 1) AS last_price, "
        "       (SELECT w.weight_g FROM ppu_weights AS w "
        "         WHERE w.product_id = p.id "
        "         ORDER BY w.valid_from DESC, w.id DESC LIMIT 1) AS last_weight "
        "FROM products AS p "
        "ORDER BY p.article_supplier"
    ).fetchall()
    product_list = [_row_dict(row) for row in rows]
    return render_template('products.html', products=product_list)


@web_bp.route('/products/<int:pid>/price', methods=['POST'])
def product_price(pid):
    price = _parse_non_negative_decimal(request.form.get('price', ''), 'цена')
    if price is None:
        return redirect(url_for('web.products'))

    valid_from = _parse_date_or_today(request.form.get('valid_from', ''))
    if valid_from is None:
        return redirect(url_for('web.products'))

    db = get_db()
    db.execute(
        "INSERT INTO manual_prices (product_id, price, valid_from) VALUES (?, ?, ?)",
        (pid, str(price), valid_from),
    )
    db.execute(
        "UPDATE products SET manual_price = 1 WHERE id = ?",
        (pid,),
    )
    db.commit()
    flash('Ручная цена сохранена', 'ok')
    return redirect(url_for('web.products'))


@web_bp.route('/products/<int:pid>/toggle_manual', methods=['POST'])
def product_toggle_manual(pid):
    db = get_db()
    db.execute(
        "UPDATE products SET manual_price = 1 - manual_price WHERE id = ?",
        (pid,),
    )
    db.commit()
    return redirect(url_for('web.products'))


@web_bp.route('/products/<int:pid>/weight', methods=['POST'])
def product_weight(pid):
    weight_g = _parse_int(request.form.get('weight_g', ''), 'вес ППУ, г', low=0, high=10000)
    if weight_g is None:
        return redirect(url_for('web.products'))

    valid_from = _parse_date_or_today(request.form.get('valid_from', ''))
    if valid_from is None:
        return redirect(url_for('web.products'))

    db = get_db()
    db.execute(
        "INSERT INTO ppu_weights (product_id, weight_g, valid_from) VALUES (?, ?, ?)",
        (pid, weight_g, valid_from),
    )
    db.commit()
    flash('Вес ППУ сохранён', 'ok')
    return redirect(url_for('web.products'))


@web_bp.route('/expenses')
def expenses():
    db = get_db()
    ppu_rub_per_kg = _last_setting_value(db, 'ppu_rub_per_kg')
    margin_pct = _last_setting_value(db, 'margin_pct')

    kind_rows = db.execute(
        "SELECT id, name FROM extra_kinds ORDER BY name"
    ).fetchall()
    extra_kinds = [_row_dict(row) for row in kind_rows]

    product_rows = db.execute(
        "SELECT id, article_supplier FROM products ORDER BY article_supplier"
    ).fetchall()
    product_list = [_row_dict(row) for row in product_rows]

    return render_template(
        'expenses.html',
        ppu_rub_per_kg=ppu_rub_per_kg,
        margin_pct=margin_pct,
        extra_kinds=extra_kinds,
        products=product_list,
    )


@web_bp.route('/expenses/setting', methods=['POST'])
def expense_setting():
    key = (request.form.get('key') or '').strip()
    if key not in SETTING_KEYS:
        flash('Недопустимый параметр настройки', 'err')
        return redirect(url_for('web.expenses'))

    value = _parse_non_negative_decimal(request.form.get('value', ''), 'значение')
    if value is None:
        return redirect(url_for('web.expenses'))

    valid_from = _parse_date_or_today(request.form.get('valid_from', ''))
    if valid_from is None:
        return redirect(url_for('web.expenses'))

    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value, valid_from) VALUES (?, ?, ?)",
        (key, str(value), valid_from),
    )
    db.commit()
    flash('Настройка сохранена', 'ok')
    return redirect(url_for('web.expenses'))


@web_bp.route('/expenses/kind', methods=['POST'])
def expense_kind():
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Укажите название вида расхода', 'err')
        return redirect(url_for('web.expenses'))

    db = get_db()
    db.execute("INSERT OR IGNORE INTO extra_kinds (name) VALUES (?)", (name,))
    db.commit()
    flash('Вид расхода добавлен', 'ok')
    return redirect(url_for('web.expenses'))


@web_bp.route('/expenses/extra', methods=['POST'])
def expense_extra():
    product_id = _parse_int(request.form.get('product_id', ''), 'товар', low=0)
    if product_id is None:
        return redirect(url_for('web.expenses'))

    kind_id = _parse_int(request.form.get('kind_id', ''), 'вид расхода', low=0)
    if kind_id is None:
        return redirect(url_for('web.expenses'))

    rub = _parse_non_negative_decimal(request.form.get('rub', ''), 'сумма')
    if rub is None:
        return redirect(url_for('web.expenses'))

    valid_from = _parse_date_or_today(request.form.get('valid_from', ''))
    if valid_from is None:
        return redirect(url_for('web.expenses'))

    valid_to_raw = (request.form.get('valid_to') or '').strip()
    valid_to = None
    if valid_to_raw:
        try:
            datetime.strptime(valid_to_raw, DATE_FMT)
        except (TypeError, ValueError):
            flash(f'Некорректная дата окончания: {valid_to_raw}', 'err')
            return redirect(url_for('web.expenses'))
        if valid_to_raw <= valid_from:
            flash('Дата окончания должна быть позже даты начала', 'err')
            return redirect(url_for('web.expenses'))
        valid_to = valid_to_raw

    db = get_db()
    db.execute(
        "INSERT INTO extras (product_id, kind_id, rub, valid_from, valid_to) "
        "VALUES (?, ?, ?, ?, ?)",
        (product_id, kind_id, str(rub), valid_from, valid_to),
    )
    db.commit()
    flash('Дополнительный расход сохранён', 'ok')
    return redirect(url_for('web.expenses'))
