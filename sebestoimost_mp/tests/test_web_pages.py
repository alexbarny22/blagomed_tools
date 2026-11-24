import os
import sqlite3

import pytest


DB_COLUMNS_PRODUCTS = ('article_supplier', 'name', 'manual_price', 'created_at')


def _make_wb_xlsx(path, article_mp='12345', article_supplier='АРТ-001'):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Себестоимость за 2026-11'
    ws.cell(row=2, column=1, value='Артикул WB')
    ws.cell(row=2, column=2, value='Артикул поставщика')
    ws.cell(row=3, column=1, value=article_mp)
    ws.cell(row=3, column=2, value=article_supplier)
    wb.save(path)


def _insert_product(app, article_supplier, name, created_at='2026-01-01', manual_price=0):
    conn = sqlite3.connect(app.config['DB_PATH'])
    try:
        conn.execute(
            'INSERT OR IGNORE INTO products '
            '(article_supplier, name, manual_price, created_at) VALUES (?, ?, ?, ?)',
            (article_supplier, name, manual_price, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _upload_wb(client, tmp_path, filename='Вайлдберриз Себестоимость.xlsx',
               article_supplier='АРТ-001'):
    path = tmp_path / 'wb_upload.xlsx'
    _make_wb_xlsx(str(path), article_supplier=article_supplier)
    with open(path, 'rb') as f:
        return client.post(
            '/upload',
            data={'wb_file': (f, filename)},
            content_type='multipart/form-data',
        )


def _product_id(app, article_supplier):
    conn = sqlite3.connect(app.config['DB_PATH'])
    try:
        row = conn.execute(
            'SELECT id FROM products WHERE article_supplier = ?',
            (article_supplier,),
        ).fetchone()
    finally:
        conn.close()
    return row[0]


def _run_with_problem(client, tmp_path):
    _upload_wb(client, tmp_path)
    return client.post('/calc', data={'calc_date': '2026-11-24'})


def test_products_search_cyrillic(app, client):
    _insert_product(app, 'АРТ-001', 'Крем детский')
    _insert_product(app, 'АРТ-002', 'Масло')

    resp = client.get('/products?q=крем')
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert 'АРТ-001' in text
    assert 'АРТ-002' not in text

    resp_all = client.get('/products')
    assert resp_all.status_code == 200
    text_all = resp_all.get_data(as_text=True)
    assert 'АРТ-001' in text_all
    assert 'АРТ-002' in text_all


def test_problems_empty(client):
    resp = client.get('/problems')
    assert resp.status_code == 200
    assert 'Расчёт ещё не выполнялся' in resp.get_data(as_text=True)


def test_problems_after_calc_no_1c(client, tmp_path):
    _run_with_problem(client, tmp_path)

    resp = client.get('/problems')
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert 'АРТ-001' in text
    assert '1С не настроена' in text


def test_problem_fix_redirects_back(app, client, tmp_path):
    _run_with_problem(client, tmp_path)
    pid = _product_id(app, 'АРТ-001')

    resp = client.post(
        f'/products/{pid}/price',
        data={'price': '500', 'valid_from': '2026-11-24', 'next': 'problems'},
    )
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/problems')


def test_price_redirect_default(app, client):
    _insert_product(app, 'АРТ-777', 'Товар')
    pid = _product_id(app, 'АРТ-777')

    resp = client.post(
        f'/products/{pid}/price',
        data={'price': '300', 'valid_from': '2026-11-24'},
    )
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/products')


def test_history(client, tmp_path):
    _upload_wb(client, tmp_path)
    client.post('/calc', data={'calc_date': '2026-11-24'})

    resp = client.get('/history')
    assert resp.status_code == 200
    assert '№1' in resp.get_data(as_text=True)


def test_upload_keeps_original_filename(app, client, tmp_path):
    resp = _upload_wb(client, tmp_path, filename='Вайлдберриз Себестоимость.xlsx')
    assert resp.status_code in (200, 302)

    conn = sqlite3.connect(app.config['DB_PATH'])
    try:
        row = conn.execute('SELECT filename, stored_path FROM uploads').fetchone()
    finally:
        conn.close()

    assert row is not None
    filename, stored_path = row
    assert filename == 'Вайлдберриз Себестоимость.xlsx'
    assert all(ord(c) < 128 for c in os.path.basename(stored_path))


def test_index_shows_uploads_and_buttons(client, tmp_path):
    _upload_wb(client, tmp_path, filename='Вайлдберриз Себестоимость.xlsx')

    resp = client.get('/')
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert 'Вайлдберриз Себестоимость.xlsx' in text
    assert 'Скачать WB' in text
