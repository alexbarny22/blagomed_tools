import pytest
import os
from app import create_app
from app.db import init_db, connect

@pytest.fixture
def app(tmp_path):
    root = tmp_path
    db_path = str(root / 'data' / 'test.db')
    init_db(db_path)
    app = create_app(root)
    app.config['DB_PATH'] = db_path
    app.config.update({'TESTING': True})
    yield app

@pytest.fixture
def client(app):
    return app.test_client()

def test_init_db_creates_file_and_settings(tmp_path):
    db_path = str(tmp_path / 'data' / 'test.db')
    init_db(db_path)
    assert os.path.exists(db_path)
    conn = connect(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key='ppu_rub_per_kg'").fetchone()
    assert row is not None
    assert row['value'] == '405'
    conn.close()

def test_init_db_twice_does_not_corrupt(tmp_path):
    db_path = str(tmp_path / 'data' / 'test.db')
    init_db(db_path)
    conn = connect(db_path)
    conn.execute("INSERT INTO products (article_supplier, created_at) VALUES ('test', '2025-01-01')")
    conn.commit()
    conn.close()
    init_db(db_path)
    conn = connect(db_path)
    cnt = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert cnt == 1
    conn.close()

def test_index_returns_200_and_contains_sebestoimost(client):
    resp = client.get('/')
    assert resp.status_code == 200
    assert 'Себестоимость'.encode('utf-8') in resp.data

def test_health_returns_200_json(client):
    resp = client.get('/health')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}
