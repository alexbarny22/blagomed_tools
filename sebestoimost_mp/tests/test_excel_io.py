import io
import os
import shutil
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from app.db import init_db, connect
from app.excel_io import (TEMPLATES, Profile, detect_profile, read_items,
                          read_items_profile, write_costs)


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
    ws["A5"] = 1003
    ws["B5"] = "SUP003"
    ws["C3"] = "НЕ ТРОГАТЬ"
    wb.save(path)
    return path


def _make_ozon(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Себестоимость"
    ws["A3"] = "Артикул Ozon"
    ws["B3"] = "Артикул продавца"
    ws["D3"] = "Себестоимость"
    ws["A4"] = 2001
    ws["B4"] = "OZON_SUP001"
    ws["A5"] = 2002
    ws["B5"] = "OZON_SUP002"
    ws["A6"] = 2003
    ws["B6"] = "OZON_SUP003"
    ws["C4"] = "НЕ ТРОГАТЬ"
    wb.save(path)
    return path


@pytest.fixture
def make_wb(tmp_path):
    return _make_wb(tmp_path / "wb.xlsx")


@pytest.fixture
def make_ozon(tmp_path):
    return _make_ozon(tmp_path / "ozon.xlsx")


def test_read_items_wb(make_wb):
    items = read_items(make_wb, TEMPLATES["wb"])
    assert len(items) == 3
    assert items[0] == {
        "row": 3,
        "article_mp": "1001",
        "article_supplier": "SUP001",
    }


def test_read_items_ozon(make_ozon):
    items = read_items(make_ozon, TEMPLATES["ozon"])
    assert len(items) == 3
    assert items[0] == {
        "row": 4,
        "article_mp": "2001",
        "article_supplier": "OZON_SUP001",
    }


def test_write_costs_wb(make_wb, tmp_path):
    dst = tmp_path / "wb_out.xlsx"
    written = write_costs(make_wb, dst, TEMPLATES["wb"], {3: 100, 5: 200})
    assert written == 2

    wb = load_workbook(dst)
    ws = wb.active
    assert ws["E3"].value == 100
    assert ws["E5"].value == 200
    assert ws["E4"].value is None
    assert ws["C3"].value == "НЕ ТРОГАТЬ"
    assert ws["A2"].value == "Артикул WB"


def test_write_costs_ozon(make_ozon, tmp_path):
    dst = tmp_path / "ozon_out.xlsx"
    written = write_costs(make_ozon, dst, TEMPLATES["ozon"], {4: 150, 6: 250})
    assert written == 2

    wb = load_workbook(dst)
    ws = wb.active
    assert ws["D4"].value == 150
    assert ws["D6"].value == 250
    assert ws["D5"].value is None
    assert ws["C4"].value == "НЕ ТРОГАТЬ"


def test_original_not_modified(make_wb, tmp_path):
    before = Path(make_wb).read_bytes()
    write_costs(make_wb, tmp_path / "out.xlsx", TEMPLATES["wb"], {3: 100})
    after = Path(make_wb).read_bytes()
    assert before == after


def test_upload_route(app, client, make_wb, make_ozon):
    init_db(app.config["DB_PATH"])

    with open(make_wb, "rb") as f:
        wb_bytes = f.read()
    with open(make_ozon, "rb") as f:
        ozon_bytes = f.read()

    resp = client.post(
        "/upload",
        data={
            "wb_file": (io.BytesIO(wb_bytes), "wb.xlsx"),
            "ozon_file": (io.BytesIO(ozon_bytes), "ozon.xlsx"),
            "calc_date": "2026-01-01",
        },
        content_type="multipart/form-data",
    )

    assert resp.status_code == 302

    conn = _connect(app)
    products_count = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
    uploads_count = conn.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"]
    conn.close()

    assert products_count == 6
    assert uploads_count == 2
    assert len(os.listdir(app.config["UPLOAD_DIR"])) == 2


def test_download_route(app, client, make_wb):
    init_db(app.config["DB_PATH"])
    conn = _connect(app)

    conn.execute(
        "INSERT INTO products (article_supplier, manual_price, created_at) VALUES (?, ?, ?)",
        ("SUP001", 1, "2026-01-01"),
    )
    sup001_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    conn.execute(
        "INSERT INTO products (article_supplier, manual_price, created_at) VALUES (?, ?, ?)",
        ("SUP002", 0, "2026-01-01"),
    )

    conn.execute(
        "INSERT INTO products (article_supplier, manual_price, created_at) VALUES (?, ?, ?)",
        ("SUP003", 1, "2026-01-01"),
    )
    sup003_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    conn.execute(
        "INSERT INTO manual_prices (product_id, price, valid_from) VALUES (?, ?, ?)",
        (sup001_id, "1500", "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO manual_prices (product_id, price, valid_from) VALUES (?, ?, ?)",
        (sup003_id, "2000", "2026-01-01"),
    )

    upload_dir = Path(app.config["UPLOAD_DIR"])
    stored_path = upload_dir / "wb_src.xlsx"
    shutil.copyfile(make_wb, stored_path)

    conn.execute(
        "INSERT INTO uploads (marketplace, filename, stored_path, uploaded_at) VALUES (?, ?, ?, ?)",
        ("wb", "wb_src.xlsx", str(stored_path), "2026-01-01 10:00:00"),
    )
    conn.commit()
    conn.close()

    # сначала расчёт, потом скачивание (этап 3: download берёт последний прогон)
    resp = client.post("/calc", data={"calc_date": "2026-06-01"})
    assert resp.status_code == 302

    resp = client.get("/download/wb")
    assert resp.status_code == 200

    wb = load_workbook(io.BytesIO(resp.data))
    ws = wb.active
    # floor(1500 * 1.15) = 1725, floor(2000 * 1.15) = 2300
    assert ws["E3"].value == 1725
    assert ws["E4"].value is None
    assert ws["E5"].value == 2300


def test_empty_items_skipped(tmp_path):
    path = tmp_path / "wb_empty.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Себестоимость за 2026-11"
    ws["A2"] = "Артикул WB"
    ws["B2"] = "Артикул поставщика"
    ws["E2"] = "Себестоимость товара"
    ws["A3"] = 1001
    ws["A4"] = 1002
    ws["B4"] = "SUP002"
    wb.save(path)

    items = read_items(path, TEMPLATES["wb"])
    assert len(items) == 1
    assert items[0]["article_supplier"] == "SUP002"


# ------------------------------------------------------- автодетект Ozon ---

def _make_ozon_prices(path):
    """Реальный «Шаблон для обновления цен» Ozon: A=Артикул, B=SKU, BD=себестоимость."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары и цены"
    ws["A2"] = "Артикул"
    ws["B2"] = "SKU"
    ws["C2"] = "Название товара"
    ws["BD2"] = "Новая себестоимость"
    ws["A5"] = 232922
    ws["B5"] = 761235340
    ws["C5"] = "Крем детский"
    ws["A6"] = 232923
    ws["B6"] = 761235341
    ws["C6"] = "Масло сливочное"
    wb.save(path)
    return path


def test_detect_profile_ozon_sku(tmp_path):
    """Заголовки [«Артикул», «SKU», «Название товара»]: SKU -> артикул МП."""
    path = _make_ozon_prices(tmp_path / "ozon_prices.xlsx")
    result = detect_profile(path)

    assert result["sheet"] == "Товары и цены"
    assert result["header_row"] == 2
    assert result["col_article_mp"] == "B"
    assert result["col_article_supplier"] == "A"
    assert result["col_cost_write"] == "BD"
    assert result["data_row"] == 5
    assert result["warnings"] == []


def test_detect_profile_without_sku_keeps_article_as_mp(tmp_path):
    """Без колонки SKU поведение прежнее: «Артикул» -> артикул МП."""
    path = tmp_path / "ozon_finance.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "Артикул"
    ws["B1"] = "Себестоимость"
    ws["A2"] = 232922
    ws["B2"] = 500
    wb.save(path)

    result = detect_profile(path)
    assert result["col_article_mp"] == "A"
    assert result["col_article_supplier"] is None
    assert result["col_cost_write"] == "B"
    assert result["header_row"] == 1
    assert result["data_row"] == 2


def test_detect_profile_supplier_header_has_priority(tmp_path):
    """Заголовок со словом «поставщик» важнее правила SKU."""
    path = tmp_path / "ozon_prices_supplier.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары и цены"
    ws["A2"] = "Артикул поставщика"
    ws["B2"] = "SKU"
    ws["C2"] = "Новая себестоимость"
    ws["A5"] = 232922
    ws["B5"] = 761235340
    wb.save(path)

    result = detect_profile(path)
    assert result["col_article_mp"] == "B"
    assert result["col_article_supplier"] == "A"
    assert result["col_cost_write"] == "C"


def test_read_items_ozon_prices_profile(tmp_path):
    """Профиль prices (mp=B, supplier=A): поставщик читается из колонки A."""
    path = _make_ozon_prices(tmp_path / "ozon_prices.xlsx")
    profile = Profile(marketplace="ozon", name="prices", sheet="Товары и цены",
                      header_row=2, data_row=5, col_article_mp="B",
                      col_article_supplier="A", col_cost_write="BD")

    items = read_items_profile(path, profile)
    assert items == [
        {"row": 5, "article_mp": "761235340", "article_supplier": "232922"},
        {"row": 6, "article_mp": "761235341", "article_supplier": "232923"},
    ]

