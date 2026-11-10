# -*- coding: utf-8 -*-
"""Тесты веб-интерфейса (этап 7): создание БД, загрузка, расчёт, карточки,
расходы с датами, проблемы, история и перезапуск. Только тестовая БД."""
from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_FLOOR
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from werkzeug.security import generate_password_hash

from app.core import export as exmod
from app.core.db import connect, init_db
from app.web import create_app
from app.web import uploads as uploads_lib
from app.web.config import ConfigError, load_config


def make_wb_bytes(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Себестоимость за 2026-11'
    ws.append(['Себестоимость за 2026-11', None, None, None, None])
    ws.append(['Артикул WB', 'Артикул поставщика', 'Размер', 'Бренд',
               'Себестоимость товара'])
    for idx, (mp, sup, size, brand, cost) in enumerate(rows):
        r = 3 + idx
        for col, val in ((1, mp), (2, sup), (3, size), (4, brand), (5, cost)):
            ws.cell(row=r, column=col).value = val
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_ozon_bytes(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Себестоимость'
    ws.append([None, None, None, 'ноябрь 2026 года'])
    ws.append([None, None, None, 'изменяемые только зелёные столбцы'])
    ws.append(['Артикул Ozon', 'Артикул продавца', 'Бренд',
               'Себестоимость товара'])
    for idx, (mp, sup, brand, cost) in enumerate(rows):
        r = 4 + idx
        for col, val in ((1, mp), (2, sup), (3, brand), (4, cost)):
            ws.cell(row=r, column=col).value = val
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def seed(con, sku='A1', *, manual=True, price=500, weight=None,
         extras=(), guid=None, name='Товар'):
    cur = con.execute(
        'INSERT INTO products (sku_1c, manual_price, active, name_1c, guid_1c)'
        ' VALUES (?, ?, 1, ?, ?)', (sku, int(manual), name, guid))
    pid = cur.lastrowid
    if manual and price is not None:
        con.execute('INSERT INTO manual_prices (product_id, price, valid_from)'
                    ' VALUES (?, ?, ?)', (pid, float(price), '2026-01-01'))
    if weight is not None:
        con.execute('INSERT INTO ppu_weights (product_id, weight_g, valid_from)'
                    ' VALUES (?, ?, ?)', (pid, int(weight), '2026-01-01'))
    for kind, rub in extras:
        con.execute('INSERT OR IGNORE INTO extra_kinds (name) VALUES (?)',
                    (kind,))
        kid = con.execute('SELECT id FROM extra_kinds WHERE name = ?',
                          (kind,)).fetchone()['id']
        con.execute('INSERT INTO extras (product_id, kind_id, rub, valid_from)'
                    ' VALUES (?, ?, ?, ?)',
                    (pid, kid, float(rub), '2026-01-01'))
    con.commit()
    return pid


TEST_USER = 'test-user'
TEST_PASSWORD = 'test-password-123'


def auth_env(monkeypatch) -> None:
    monkeypatch.setenv('BLAGOMED_AUTH_USER', TEST_USER)
    monkeypatch.setenv('BLAGOMED_AUTH_HASH',
                       generate_password_hash(TEST_PASSWORD))


def login(client):
    return client.post('/login', data={'user': TEST_USER,
                                       'password': TEST_PASSWORD},
                       follow_redirects=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = tmp_path / 'home'
    monkeypatch.setenv('BLAGOMED_HOME', str(base))
    monkeypatch.setenv('BLAGOMED_DB', str(base / 'data' / 'cost.db'))
    monkeypatch.delenv('BLAGOMED_MAX_UPLOAD_MB', raising=False)
    auth_env(monkeypatch)
    cfg = load_config(base)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.uploads.mkdir(parents=True, exist_ok=True)
    cfg.output.mkdir(parents=True, exist_ok=True)
    cfg.logs.mkdir(parents=True, exist_ok=True)
    con = init_db(cfg.db_path)
    con.execute("INSERT INTO settings (key, value, valid_from)"
                " VALUES ('ppu_rub_per_kg', 405, '2026-01-01')")
    con.execute("INSERT INTO settings (key, value, valid_from)"
                " VALUES ('margin_pct', 15, '2026-01-01')")
    con.commit()
    con.close()
    app = create_app(cfg)
    app.config['TESTING'] = True
    client = app.test_client()
    login(client)
    return {'cfg': cfg, 'app': app, 'client': client,
            'base': base}


def db(env):
    return connect(env['cfg'].db_path)


def run_id(resp) -> int:
    m = re.search(r'/runs/(\d+)', resp.request.path)
    return int(m.group(1)) if m else 0


def meta(env, rid) -> dict:
    path = env['cfg'].history_dir / f'run_{rid}.json'
    return json.loads(path.read_text(encoding='utf-8'))


def expected(price, weight, extras, rate=405, margin=15):
    total = (Decimal(str(price)) + Decimal(weight) / 1000 *
             Decimal(rate) + Decimal(extras)) * (1 + Decimal(margin) / 100)
    return int(total.to_integral_value(rounding=ROUND_FLOOR))


def page_cost(html: str):
    m = re.search(r'Себестоимость</dt>\s*<dd><b>(\d+)', html)
    return int(m.group(1)) if m else None


def upload(client, marketplace, files):
    data = {'marketplace': marketplace,
            'files': [(BytesIO(content), name) for name, content in files]}
    return client.post('/upload', data=data,
                       content_type='multipart/form-data',
                       follow_redirects=True)


def test_setup_requires_explicit_creation(tmp_path, monkeypatch):
    base = tmp_path / 'fresh'
    monkeypatch.setenv('BLAGOMED_HOME', str(base))
    monkeypatch.setenv('BLAGOMED_DB', str(base / 'data' / 'cost.db'))
    auth_env(monkeypatch)
    cfg = load_config(base)
    app = create_app(cfg)
    app.config['TESTING'] = True
    client = app.test_client()
    r = client.get('/')
    assert r.status_code == 302 and '/login' in r.headers['Location']
    login(client)
    r = client.get('/')
    assert r.status_code == 302 and '/setup' in r.headers['Location']
    assert not cfg.db_path.exists()
    r = client.post('/setup/create', follow_redirects=True)
    assert r.status_code == 200 and cfg.db_path.exists()
    con = connect(cfg.db_path)
    assert con.execute('PRAGMA user_version').fetchone()[0] == 2
    assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    con.close()
    r = client.post('/setup/create', follow_redirects=True)
    assert 'уже существует' in r.get_data(as_text=True)


def test_new_database_uses_15_percent_margin(tmp_path, monkeypatch):
    """Новая БД получает 405/15/10; 1000 ₽ без ППУ дают 1150 ₽, не 1000 ₽."""
    base = tmp_path / 'fresh-margin'
    monkeypatch.setenv('BLAGOMED_HOME', str(base))
    monkeypatch.setenv('BLAGOMED_DB', str(base / 'data' / 'cost.db'))
    auth_env(monkeypatch)
    cfg = load_config(base)
    app = create_app(cfg)
    app.config['TESTING'] = True
    client = app.test_client()
    login(client)
    r = client.post('/setup/create', follow_redirects=True)
    assert r.status_code == 200 and cfg.db_path.exists()

    con = connect(cfg.db_path)
    settings = {row['key']: row['value'] for row in con.execute(
        "SELECT key, value FROM settings"
        " WHERE key IN ('ppu_rub_per_kg', 'margin_pct', 'alert_pct')")}
    assert settings['ppu_rub_per_kg'] == pytest.approx(405)
    assert settings['margin_pct'] == pytest.approx(15)
    assert settings['alert_pct'] == pytest.approx(10)
    pid = seed(con, 'A1', manual=True, price=1000)
    con.close()

    page = client.get(f'/products/{pid}?date=2026-11-01').get_data(
        as_text=True)
    assert page_cost(page) == 1150

    expenses = client.get('/expenses?date=2026-11-01').get_data(as_text=True)
    assert '15,00%' in expenses
    assert '0,15%' not in expenses


def test_config_example_margin_is_percent():
    text = (Path(__file__).resolve().parents[1] /
            'config.example.ini').read_text(encoding='utf-8')
    assert 'margin_pct = 15' in text
    assert 'margin_pct = 0.15' not in text


def test_upload_validation_and_multiple_files(env):
    client = env['client']
    wb = make_wb_bytes([('B1', 'A1', '0', '', None)])
    oz = make_ozon_bytes([('O1', 'A1', '', None)])
    r = upload(client, 'auto', [('wb.xlsx', wb), ('oz.xlsx', oz)])
    assert r.status_code == 200
    files = list(env['cfg'].uploads.glob('*.xlsx'))
    assert len(files) == 2
    r = upload(client, 'auto', [('bad.txt', b'hello')])
    assert 'Файл bad.txt отклонён' in r.get_data(as_text=True)
    r = upload(client, 'auto', [('bad.xlsx', b'PK\x03\x04broken')])
    assert ('Не удалось открыть' in r.get_data(as_text=True) or
            'повреждён' in r.get_data(as_text=True))
    wrong = Workbook()
    wrong.active.title = 'Лист1'
    wrong.active['A1'] = 'x'
    buf = BytesIO()
    wrong.save(buf)
    r = upload(client, 'auto', [('wrong.xlsx', buf.getvalue())])
    assert 'Формат отчёта не распознан' in r.get_data(as_text=True)
    assert len(list(env['cfg'].uploads.glob('*.xlsx'))) == 2


def test_calc_download_and_only_cost_cells_changed(env, tmp_path):
    con = db(env)
    pid = seed(con, 'A1', manual=True, price=500)
    con.close()
    wb_c = make_wb_bytes([('B1', 'A1', '0', '', None)])
    oz_c = make_ozon_bytes([('O1', 'A1', '', None),
                            ('O2', 'NOMATCH', '', None)])
    upload(env['client'], 'auto', [('wb.xlsx', wb_c), ('oz.xlsx', oz_c)])
    r = env['client'].post('/calc/run', data={
        'calc_date': '2026-11-01', 'price_mode': 'none'},
        follow_redirects=True)
    assert r.status_code == 200
    rid = run_id(r)
    assert rid > 0
    m = meta(env, rid)
    assert m['summary']['ok'] == 2 and m['summary']['no_match'] == 1
    assert m['summary']['skipped'] == 1
    assert m['summary']['wb_total'] == 1 and m['summary']['ozon_total'] == 2
    r = env['client'].get(f'/runs/{rid}/download/wb')
    assert 'черновик' in r.get_data(as_text=True)
    r = env['client'].get(f'/runs/{rid}/download/wb?confirm=1')
    assert r.status_code == 200 and r.data[:2] == b'PK'
    dst = tmp_path / 'wb_out.xlsx'
    dst.write_bytes(r.data)
    src = env['cfg'].uploads.glob('wb_*.xlsx')
    src = sorted(src, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    allow = {row['row_number']: row['cost'] for row in m['rows']
             if row['marketplace'] == 'wb' and row['status'] == 'OK'}
    exmod.verify_ouput_only_target(src, dst, 'wb', 5, allow)
    ws = load_workbook(dst, data_only=True)['Себестоимость за 2026-11']
    assert ws.cell(row=3, column=5).value == expected(500, 0, 0)
    assert ws.cell(row=3, column=1).value == 'B1'
    con = db(env)
    assert con.execute('SELECT COUNT(*) FROM cost_snapshots'
                       ' WHERE run_id = ?', (rid,)).fetchone()[0] == 1
    con.close()


def test_manual_price_history_by_date(env):
    con = db(env)
    pid = seed(con, 'A1', manual=False)
    con.close()
    client = env['client']
    client.post(f'/products/{pid}/manual', data={
        'action': 'set', 'price': '500', 'valid_from': '2026-11-01'})
    client.post(f'/products/{pid}/manual', data={
        'action': 'set', 'price': '700', 'valid_from': '2026-11-05'})
    d1 = client.get(f'/products/{pid}?date=2026-11-01').get_data(as_text=True)
    d2 = client.get(f'/products/{pid}?date=2026-11-05').get_data(as_text=True)
    assert page_cost(d1) == expected(500, 0, 0)
    assert page_cost(d2) == expected(700, 0, 0)


def test_weight_extras_and_settings_dates(env):
    con = db(env)
    pid = seed(con, 'A1', manual=True, price=500)
    con.close()
    client = env['client']
    client.post(f'/products/{pid}/ppu', data={
        'weight_g': '100', 'valid_from': '2026-11-01'})
    client.post(f'/products/{pid}/extras', data={
        'kind': 'Поролон', 'rub': '50', 'valid_from': '2026-11-01'})
    d1 = client.get(f'/products/{pid}?date=2026-11-01').get_data(as_text=True)
    assert page_cost(d1) == expected(500, 100, 50, rate=405, margin=15)

    r = client.post('/expenses/settings', data={
        'ppu_rub_per_kg': '500', 'margin_pct': '20',
        'valid_from': '2026-11-05'}, follow_redirects=True)
    assert r.status_code == 200
    d1_after = client.get(f'/products/{pid}?date=2026-11-01'
                          ).get_data(as_text=True)
    d2 = client.get(f'/products/{pid}?date=2026-11-05').get_data(as_text=True)
    assert page_cost(d1_after) == expected(500, 100, 50, rate=405, margin=15)
    assert page_cost(d2) == expected(500, 100, 50, rate=500, margin=20)


def test_single_platform_http_run(env):
    con = db(env)
    seed(con, 'A1', manual=True, price=500)
    con.close()
    wb = make_wb_bytes([('B1', 'A1', '0', '', None)])
    upload(env['client'], 'wb', [('wb.xlsx', wb)])
    r = env['client'].post('/calc/run', data={
        'calc_date': '2026-11-01', 'price_mode': 'none'},
        follow_redirects=True)
    rid = run_id(r)
    assert rid > 0
    m = meta(env, rid)
    assert m['summary']['wb_total'] == 1
    assert m['summary']['ozon_total'] == 0
    assert m['summary']['ok'] == 1
    html = r.get_data(as_text=True)
    assert 'WB XLSX' in html and 'Ozon XLSX' not in html
    assert 'только одна площадка' in html
    r = env['client'].get(f'/runs/{rid}/download/ozon')
    assert r.status_code == 302


def test_history_survives_new_app_instance(env):
    con = db(env)
    seed(con, 'A1', manual=True, price=500)
    con.close()
    wb = make_wb_bytes([('B1', 'A1', '0', '', None)])
    upload(env['client'], 'wb', [('wb.xlsx', wb)])
    r = env['client'].post('/calc/run', data={
        'calc_date': '2026-11-01', 'price_mode': 'none'},
        follow_redirects=True)
    rid = run_id(r)
    app2 = create_app(env['cfg'])
    app2.config['TESTING'] = True
    client2 = app2.test_client()
    login(client2)
    html = client2.get('/history').get_data(as_text=True)
    assert f'/runs/{rid}' in html
    con = db(env)
    assert con.execute('SELECT COUNT(*) FROM runs WHERE id = ?',
                       (rid,)).fetchone()[0] == 1
    con.close()


def test_issues_explain_and_skip(env):
    con = db(env)
    pid = seed(con, 'A1', manual=False)
    con.close()
    wb = make_wb_bytes([('B1', 'A1', '0', '', None)])
    upload(env['client'], 'wb', [('wb.xlsx', wb)])
    r = env['client'].post('/calc/run', data={
        'calc_date': '2026-11-01', 'price_mode': 'none'},
        follow_redirects=True)
    rid = run_id(r)
    html = env['client'].get(f'/issues?run_id={rid}').get_data(as_text=True)
    assert 'NEEDS_CONFIRM' in html and 'требует подтверждения' in html
    r = env['client'].post('/issues/skip', data={
        'product_id': pid, 'run_id': rid}, follow_redirects=True)
    assert r.status_code == 200
    con = db(env)
    n = con.execute("SELECT COUNT(*) FROM audit_log WHERE entity = 'issues'"
                    ).fetchone()[0]
    con.close()
    assert n == 1


def test_login_required_and_logout(env):
    client = env['app'].test_client()
    r = client.get('/products')
    assert r.status_code == 302 and '/login' in r.headers['Location']
    r = client.get('/healthz')
    assert r.status_code == 200
    r = client.post('/login', data={'user': TEST_USER, 'password': 'wrong'})
    assert r.status_code == 200 and 'Неверный логин или пароль' in \
        r.get_data(as_text=True)
    login(client)
    assert client.get('/products').status_code == 200
    r = client.post('/logout', follow_redirects=True)
    assert r.status_code == 200 and 'Вы вышли' in r.get_data(as_text=True)
    assert client.get('/products').status_code == 302


def test_auth_not_configured_blocks_access(tmp_path, monkeypatch):
    base = tmp_path / 'noauth'
    monkeypatch.setenv('BLAGOMED_HOME', str(base))
    monkeypatch.setenv('BLAGOMED_DB', str(base / 'data' / 'cost.db'))
    monkeypatch.delenv('BLAGOMED_AUTH_USER', raising=False)
    monkeypatch.delenv('BLAGOMED_AUTH_HASH', raising=False)
    cfg = load_config(base)
    app = create_app(cfg)
    app.config['TESTING'] = True
    client = app.test_client()
    r = client.get('/login')
    assert r.status_code == 200 and 'Вход не настроен' in \
        r.get_data(as_text=True)
    r = client.get('/products')
    assert r.status_code == 302 and '/login' in r.headers['Location']


def test_settings_are_date_versioned_and_audited(env):
    con = db(env)
    pid = seed(con, 'A1', manual=True, price=500, weight=100)
    con.close()
    client = env['client']
    r = client.post('/expenses/settings', data={
        'ppu_rub_per_kg': '500', 'margin_pct': '20',
        'valid_from': '2026-11-10'}, follow_redirects=True)
    assert r.status_code == 200
    before = client.get(
        f'/products/{pid}?date=2026-11-09').get_data(as_text=True)
    after = client.get(
        f'/products/{pid}?date=2026-11-10').get_data(as_text=True)
    assert page_cost(before) == expected(500, 100, 0, 405, 15)
    assert page_cost(after) == expected(500, 100, 0, 500, 20)
    con = db(env)
    row = con.execute(
        "SELECT user, new FROM audit_log WHERE entity = 'settings'"
        ' ORDER BY id DESC LIMIT 1').fetchone()
    con.close()
    assert row['user'] == TEST_USER and '2026-11-10' in row['new']


def test_setup_migrate_requires_confirmation(env):
    client = env['app'].test_client()
    login(client)
    con = db(env)
    version = con.execute('PRAGMA user_version').fetchone()[0]
    con.close()
    r = client.post('/setup/migrate', follow_redirects=True)
    assert 'требует явного подтверждения' in r.get_data(as_text=True)
    con = db(env)
    assert con.execute('PRAGMA user_version').fetchone()[0] == version
    con.close()
    r = client.post('/setup/migrate', data={'confirm': 'yes'},
                    follow_redirects=True)
    assert 'Миграции применены: 0' in r.get_data(as_text=True)
    con = db(env)
    pid = seed(con, 'A1', manual=True, price=500)
    con.close()
    html = env['client'].get(f'/products/{pid}').get_data(as_text=True)
    assert 'не подтверждён' in html and 'GUID запрещено' in html
    assert 'name="guid' not in html
    con = db(env)
    con.execute("UPDATE products SET guid_1c = ? WHERE id = ?",
                ('11111111-2222-3333-4444-555555555555', pid))
    con.commit()
    con.close()
    html = env['client'].get(f'/products/{pid}').get_data(as_text=True)
    assert '11111111-2222-3333-4444-555555555555' in html


def test_upload_size_and_path_traversal(env):
    with pytest.raises(uploads_lib.UploadError):
        uploads_lib.save_upload(BytesIO(b'PK\x03\x04' + b'0' * 100),
                                'big.xlsx', env['cfg'].uploads, 10)
    assert not list(env['cfg'].uploads.glob('.tmp_*'))
    with pytest.raises(uploads_lib.UploadError):
        uploads_lib.safe_join(env['cfg'].uploads, '..', 'evil.xlsx')


def test_host_from_ini_and_env_override(tmp_path, monkeypatch):
    base = tmp_path / 'home'
    base.mkdir()
    (base / 'config.ini').write_text(
        '[web]\nhost = 0.0.0.0\nport = 8123\n', encoding='utf-8')
    monkeypatch.delenv('BLAGOMED_HOST', raising=False)
    monkeypatch.delenv('BLAGOMED_PORT', raising=False)
    cfg = load_config(base)
    assert (cfg.host, cfg.port) == ('0.0.0.0', 8123)
    monkeypatch.setenv('BLAGOMED_HOST', '127.0.0.1')
    assert load_config(base).host == '127.0.0.1'


def test_invalid_host_from_ini_rejected(tmp_path, monkeypatch):
    base = tmp_path / 'home'
    base.mkdir()
    (base / 'config.ini').write_text(
        '[web]\nhost = 10.0.0.5\n', encoding='utf-8')
    monkeypatch.delenv('BLAGOMED_HOST', raising=False)
    with pytest.raises(ConfigError, match='запрещён'):
        load_config(base)