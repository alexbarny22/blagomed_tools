import pytest
from app.db import init_db, connect


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Себестоимость".encode("utf-8") in resp.data


def test_init_db_creates_settings(app):
    init_db(app.config["DB_PATH"])
    conn = connect(app.config["DB_PATH"])
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        ("ppu_rub_per_kg",),
    ).fetchone()
    conn.close()
    assert row["value"] == "405"


def test_init_db_twice_keeps_data(app):
    db_path = app.config["DB_PATH"]
    init_db(db_path)

    conn = connect(db_path)
    conn.execute(
        "INSERT INTO products (article_supplier, created_at) VALUES (?, ?)",
        ("test", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
    conn.close()
    assert count == 1
