"""Тесты прогона расчёта (app/calc_run.py) и маршрутов /calc, /products, /expenses."""

import io
import shutil
from pathlib import Path

from openpyxl import Workbook, load_workbook

from app import calc_run
from app.db import connect


def _connect(app):
    return connect(app.config["DB_PATH"])


def _make_wb(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Себестоимость за 2026-11"
    ws["A2"] = "Артикул WB"
    ws["B2"] = "Артикул поставщика"
    ws["E2"] = "Себестоимость товара"
    ws["A3"] = 1001
    ws["B3"] = "SUP001"
    ws["A4"] = 1002
    ws["B4"] = "SUP002"
    wb.save(path)
    return path


def _add_product(con, article, *, manual=0):
    cur = con.execute(
        "INSERT INTO products (article_supplier, manual_price, created_at) "
        "VALUES (?, ?, '2026-01-01')",
        (article, manual),
    )
    con.commit()
    return cur.lastrowid


def _add_price(con, pid, price):
    con.execute(
        "INSERT INTO manual_prices (product_id, price, valid_from) VALUES (?, ?, '2026-01-01')",
        (pid, price),
    )
    con.commit()


def test_run_calculation_counts(app):
    con = _connect(app)
    p1 = _add_product(con, "SUP001", manual=1)
    _add_price(con, p1, "1000")
    _add_product(con, "SUP002")  # цены нет, не ручной → проблема
    _add_product(con, "SUP003", manual=1)  # ручной без цены → проблема

    items = [
        {"row": 3, "article_mp": "1001", "article_supplier": "SUP001"},
        {"row": 4, "article_mp": "1002", "article_supplier": "SUP002"},
        {"row": 5, "article_mp": "1003", "article_supplier": "SUP003"},
        {"row": 6, "article_mp": "1004", "article_supplier": "GHOST"},
    ]
    run_id = calc_run.run_calculation(con, calc_date="2026-06-01", marketplaces={"wb": items})

    run = calc_run.get_last_run(con)
    assert run["id"] == run_id
    assert run["total"] == 4
    assert run["ok"] == 1
    assert run["problems"] == 3

    costs = calc_run.get_run_costs(con, run_id, "wb")
    assert costs == {"SUP001": 1150}  # floor(1000 * 1.15), без веса ППУ не добавляется

    problems = calc_run.get_run_problems(con, run_id)
    assert len(problems) == 3
    msgs = {r["article_supplier"]: r["message"] for r in problems}
    assert "не передана закупочная" in msgs["SUP002"]
    assert "manual_prices нет записи" in msgs["SUP003"]
    assert "не найден" in msgs["GHOST"]
    con.close()


def test_calc_route_and_download_flow(app, client, tmp_path):
    """Полный цикл: загрузка → прогон → скачивание из прогона."""
    upload_dir = Path(app.config["UPLOAD_DIR"])
    stored = upload_dir / "wb_src.xlsx"
    shutil.copyfile(_make_wb(tmp_path / "wb.xlsx"), stored)

    con = _connect(app)
    p1 = _add_product(con, "SUP001", manual=1)
    _add_price(con, p1, "1000")
    con.execute(
        "INSERT INTO uploads (marketplace, filename, stored_path, uploaded_at) "
        "VALUES ('wb', 'wb_src.xlsx', ?, '2026-01-01 10:00:00')",
        (str(stored),),
    )
    con.commit()
    con.close()

    # без прогона скачивание отклоняется
    resp = client.get("/download/wb")
    assert resp.status_code == 302

    resp = client.post("/calc", data={"calc_date": "2026-06-01"})
    assert resp.status_code == 302

    resp = client.get("/download/wb")
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.data))
    ws = wb.active
    assert ws["E3"].value == 1150
    assert ws["E4"].value is None  # SUP002 без цены → ячейка не трогается


def test_calc_route_without_uploads(app, client):
    resp = client.post("/calc", data={"calc_date": "2026-06-01"}, follow_redirects=True)
    assert resp.status_code == 200
    assert "Сначала загрузите файлы".encode("utf-8") in resp.data


def test_product_price_and_weight_routes(app, client):
    con = _connect(app)
    pid = _add_product(con, "SUP100")
    con.close()

    resp = client.post(f"/products/{pid}/price", data={"price": "999.50", "valid_from": "2026-01-01"})
    assert resp.status_code == 302
    resp = client.post(f"/products/{pid}/weight", data={"weight_g": "200", "valid_from": "2026-01-01"})
    assert resp.status_code == 302

    con = _connect(app)
    row = con.execute("SELECT manual_price FROM products WHERE id = ?", (pid,)).fetchone()
    assert row["manual_price"] == 1
    price = con.execute("SELECT price FROM manual_prices WHERE product_id = ?", (pid,)).fetchone()
    assert price["price"] == "999.50"
    weight = con.execute("SELECT weight_g FROM ppu_weights WHERE product_id = ?", (pid,)).fetchone()
    assert weight["weight_g"] == 200
    con.close()


def test_product_price_invalid(app, client):
    con = _connect(app)
    pid = _add_product(con, "SUP200")
    con.close()
    resp = client.post(f"/products/{pid}/price", data={"price": "abc"}, follow_redirects=True)
    assert resp.status_code == 200
    con = _connect(app)
    n = con.execute("SELECT COUNT(*) AS c FROM manual_prices WHERE product_id = ?", (pid,)).fetchone()["c"]
    con.close()
    assert n == 0


def test_expense_setting_and_extra_routes(app, client):
    con = _connect(app)
    pid = _add_product(con, "SUP300")
    con.close()

    resp = client.post("/expenses/setting", data={"key": "margin_pct", "value": "20", "valid_from": "2026-06-01"})
    assert resp.status_code == 302
    resp = client.post("/expenses/kind", data={"name": "Упаковка"})
    assert resp.status_code == 302

    con = _connect(app)
    kind = con.execute("SELECT id FROM extra_kinds WHERE name = 'Упаковка'").fetchone()
    con.close()

    resp = client.post("/expenses/extra", data={
        "product_id": str(pid), "kind_id": str(kind["id"]), "rub": "50",
        "valid_from": "2026-01-01", "valid_to": "",
    })
    assert resp.status_code == 302

    # наценка 20% действует с 2026-06-01, до неё — 15%
    con = _connect(app)
    from app.calc import get_setting, get_extras
    assert get_setting(con, "margin_pct", "2026-05-31") == 15
    assert get_setting(con, "margin_pct", "2026-06-01") == 20
    extras = get_extras(con, pid, "2026-06-01")
    assert len(extras) == 1 and extras[0].kind == "Упаковка"
    con.close()


def test_expense_extra_invalid_period(app, client):
    con = _connect(app)
    pid = _add_product(con, "SUP400")
    con.execute("INSERT INTO extra_kinds (name) VALUES ('Брак')")
    con.commit()
    kind_id = con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    con.close()

    resp = client.post("/expenses/extra", data={
        "product_id": str(pid), "kind_id": str(kind_id), "rub": "10",
        "valid_from": "2026-06-01", "valid_to": "2026-05-01",
    }, follow_redirects=True)
    assert resp.status_code == 200
    con = _connect(app)
    n = con.execute("SELECT COUNT(*) AS c FROM extras").fetchone()["c"]
    con.close()
    assert n == 0


def test_products_and_expenses_pages_render(app, client):
    con = _connect(app)
    _add_product(con, "SUP500", manual=1)
    con.close()
    resp = client.get("/products")
    assert resp.status_code == 200
    assert "SUP500".encode("utf-8") in resp.data
    resp = client.get("/expenses")
    assert resp.status_code == 200
    assert "405".encode("utf-8") in resp.data
