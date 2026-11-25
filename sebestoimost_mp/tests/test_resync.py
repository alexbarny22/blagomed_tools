"""Одноразовое авто-перечитывание сохранённых эталонов (после миграции v4).

Сценарий: эталон Ozon был загружён давно и прочитан старым профилем
(только SKU) — товары заведены по SKU. После исправления профилей при
старте приложение само перечитывает сохранённый файл: SKU привязывается
к товару с артикулом поставщика, отдельный товар-SKU не создаётся.
"""

import sqlite3

import openpyxl

from app.db import init_db
from app.products_sync import resync_templates_if_needed


def _make_ozon_prices_xlsx(path):
    """Мини-эталон «Товары и цены»: A=Артикул поставщика, B=SKU, данные с 5."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Товары и цены'
    ws['A2'] = 'Артикул'
    ws['B2'] = 'SKU'
    ws['C2'] = 'Название товара'
    ws['A5'] = '232922'
    ws['B5'] = '761235340'
    wb.save(path)


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_resync_links_sku_to_supplier_product(tmp_path):
    db_path = tmp_path / 'test.db'
    init_db(str(db_path))
    xlsx = tmp_path / 'ozon.xlsx'
    _make_ozon_prices_xlsx(xlsx)

    conn = _connect(db_path)
    # Товар от finance-эталона (артикул поставщика) уже есть, SKU нет
    conn.execute(
        "INSERT INTO products (article_supplier, created_at) VALUES ('232922', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO uploads (marketplace, filename, stored_path, uploaded_at) "
        "VALUES ('ozon_prices', 'ozon.xlsx', ?, '2026-01-01 10:00:00')",
        (str(xlsx),),
    )
    conn.commit()

    result = resync_templates_if_needed(conn)

    assert result is not None
    row = conn.execute(
        "SELECT article_ozon FROM products WHERE article_supplier = '232922'"
    ).fetchone()
    assert row['article_ozon'] == '761235340'
    # отдельный товар по SKU не создан
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM products WHERE article_supplier = '761235340'"
    ).fetchone()['c'] == 0

    # повторный запуск — ничего не делает
    assert resync_templates_if_needed(conn) is None
    conn.close()


def test_resync_without_uploads_is_noop(tmp_path):
    db_path = tmp_path / 'test.db'
    init_db(str(db_path))
    conn = _connect(db_path)

    result = resync_templates_if_needed(conn)

    assert result == {'wb': 0, 'ozon_prices': 0, 'ozon_finance': 0}
    assert resync_templates_if_needed(conn) is None
    conn.close()


def test_resync_survives_broken_template(tmp_path):
    """Битый файл эталона не роняет перечитывание, флаг всё равно ставится."""
    db_path = tmp_path / 'test.db'
    init_db(str(db_path))
    broken = tmp_path / 'broken.xlsx'
    broken.write_bytes(b'not an xlsx')

    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO uploads (marketplace, filename, stored_path, uploaded_at) "
        "VALUES ('wb', 'wb.xlsx', ?, '2026-01-01 10:00:00')",
        (str(broken),),
    )
    conn.commit()

    result = resync_templates_if_needed(conn)

    assert result is not None
    assert resync_templates_if_needed(conn) is None
    conn.close()
