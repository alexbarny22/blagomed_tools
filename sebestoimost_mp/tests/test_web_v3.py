"""Тесты v3: страницы «Эталоны МП», «Шаблоны», «Без цены 1С», история товара,
псевдонимы адресов выгрузки (/download/ozon -> профиль ozon_prices)."""

import io
import sqlite3
import zipfile

import openpyxl
from openpyxl import load_workbook


def _connect(app):
    return sqlite3.connect(app.config['DB_PATH'])


def _make_wb(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Себестоимость за 2026-11'
    ws['A2'] = 'Артикул WB'
    ws['B2'] = 'Артикул поставщика'
    ws['E2'] = 'Себестоимость'
    ws['A3'] = 12345
    ws['B3'] = 'АРТ-900'
    ws['A4'] = 12346
    ws['B4'] = 'АРТ-901'
    wb.save(path)
    return path


def _make_ozon(path):
    """Эталон Ozon без колонки артикула поставщика (как в реальном Ozon)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Товары и цены'
    ws['A2'] = 'Артикул Ozon'
    ws['B2'] = 'Наименование'
    ws['BD2'] = 'Себестоимость'
    ws['A5'] = 555001
    ws['B5'] = 'Крем детский'
    ws['A6'] = 555002
    ws['B6'] = 'Масло сливочное'
    wb.save(path)
    return path


def _upload(client, url, field, path, filename, **extra):
    with open(path, 'rb') as f:
        data = dict(extra)
        data[field] = (f, filename)
        return client.post(url, data=data, content_type='multipart/form-data')


def test_v3_pages_render(client):
    for url in ('/', '/assortment', '/templates', '/mp/wb', '/mp/ozon',
                '/problems', '/expenses'):
        resp = client.get(url)
        assert resp.status_code == 200, url


def test_history_page_removed(client):
    """По ТЗ v3 роут /history удалён."""
    assert client.get('/history').status_code == 404


def test_mp_page_unknown_marketplace_redirects(client):
    resp = client.get('/mp/ebay')
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/')


def test_templates_upload_ozon_matches_by_article_mp(app, client, tmp_path):
    path = _make_ozon(tmp_path / 'ozon.xlsx')
    resp = _upload(client, '/templates/upload', 'file', path, 'ozon.xlsx',
                   profile_key='ozon_prices')
    assert resp.status_code == 302

    con = _connect(app)
    try:
        rows = con.execute(
            'SELECT article_supplier, article_ozon FROM products '
            'ORDER BY article_supplier'
        ).fetchall()
        uploads = con.execute(
            "SELECT COUNT(*) FROM uploads WHERE marketplace = 'ozon_prices'"
        ).fetchone()[0]
    finally:
        con.close()

    assert uploads == 1
    assert rows == [('555001', '555001'), ('555002', '555002')]

    resp = client.get('/mp/ozon')
    assert resp.status_code == 200
    assert '555001' in resp.get_data(as_text=True)


def test_download_aliases(app, client, tmp_path):
    path = _make_wb(tmp_path / 'wb.xlsx')
    resp = _upload(client, '/templates/upload', 'file', path, 'wb.xlsx',
                   profile_key='wb')
    assert resp.status_code == 302

    con = _connect(app)
    con.execute("UPDATE products SET manual_price = 1 "
                "WHERE article_supplier = 'АРТ-900'")
    con.execute(
        "INSERT INTO manual_prices (product_id, price, valid_from) "
        "SELECT id, '1000', '2026-01-01' FROM products "
        "WHERE article_supplier = 'АРТ-900'"
    )
    con.commit()
    con.close()

    resp = client.post('/calc', data={'calc_date': '2026-11-24'})
    assert resp.status_code == 302

    # старый адрес /download/ozon больше не 404/500, а осмысленный редирект
    resp = client.get('/download/ozon')
    assert resp.status_code == 302
    resp = client.get('/download/unknown')
    assert resp.status_code == 302

    resp = client.get('/download/wb')
    assert resp.status_code == 200
    ws = load_workbook(io.BytesIO(resp.data)).active
    assert ws['E3'].value == 1150


def test_download_all_returns_zip(app, client, tmp_path):
    path = _make_wb(tmp_path / 'wb2.xlsx')
    resp = _upload(client, '/templates/upload', 'file', path, 'wb2.xlsx',
                   profile_key='wb')
    assert resp.status_code == 302
    client.post('/calc', data={'calc_date': '2026-11-24'})

    resp = client.get('/download_all')
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as archive:
        names = archive.namelist()
    assert names and all(n.endswith('.xlsx') for n in names)


def test_product_history_page(app, client):
    con = _connect(app)
    con.execute(
        "INSERT INTO products (article_supplier, name, manual_price, created_at) "
        "VALUES ('АРТ-500', 'Тестовый товар', 0, '2026-01-01')"
    )
    con.commit()
    pid = con.execute("SELECT id FROM products WHERE article_supplier = 'АРТ-500'"
                      ).fetchone()[0]
    con.close()

    resp = client.get(f'/products/{pid}/history')
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert 'АРТ-500' in text
