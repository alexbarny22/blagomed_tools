import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, session, current_app, jsonify
from werkzeug.utils import secure_filename
from datetime import datetime
import app.db as db_module
import app.excel_io as excel_io

web_bp = Blueprint('web', __name__, template_folder='templates')

def get_db():
    return db_module.get_db(current_app)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'xlsx'

@web_bp.route('/')
def index():
    today = datetime.now().strftime('%Y-%m-%d')
    return render_template('index.html', today=today)

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

@web_bp.route('/download/<marketplace>')
def download(marketplace):
    if marketplace not in ['wb', 'ozon']:
        flash('Недопустимый маркетплейс', 'err')
        return redirect(url_for('web.index'))
    db = get_db()
    upload = db.execute(
        "SELECT id, filename, stored_path, uploaded_at FROM uploads WHERE marketplace = ? ORDER BY uploaded_at DESC LIMIT 1",
        (marketplace,)
    ).fetchone()
    if not upload:
        flash(f'Нет загруженных файлов для {marketplace.upper()}', 'err')
        return redirect(url_for('web.index'))
    spec = excel_io.TEMPLATES[marketplace]
    try:
        items = excel_io.read_items(upload['stored_path'], spec)
    except KeyError as e:
        flash(f"Ошибка чтения файла: {str(e)}", 'err')
        return redirect(url_for('web.index'))
    costs = {}
    problems = 0
    for item in items:
        product = db.execute(
            "SELECT id, manual_price FROM products WHERE article_supplier = ?",
            (item['article_supplier'],)
        ).fetchone()
        if not product or not product['manual_price']:
            problems += 1
            continue
        price = db.execute(
            "SELECT price FROM manual_prices WHERE product_id = ? AND valid_from <= ? ORDER BY valid_from DESC LIMIT 1",
            (product['id'], datetime.now().strftime('%Y-%m-%d'))
        ).fetchone()
        if price:
            costs[item['row']] = int(float(price['price']))
        else:
            problems += 1
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    dst_filename = f"{marketplace}_filled_{timestamp}.xlsx"
    dst_path = os.path.join(current_app.config['UPLOAD_DIR'], dst_filename)
    written = excel_io.write_costs(upload['stored_path'], dst_path, spec, costs)
    return send_file(dst_path, as_attachment=True, download_name=upload['filename'],
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
