"""Тесты получения цен из 1С и интеграции с прогоном расчёта."""

from __future__ import annotations

from decimal import Decimal

from app import onec
from app.calc_run import run_calculation
from app.db import connect
from app.price_source import fetch_prices, load_params


class FakeSource(onec.OnecSource):
    """Подменный источник 1С: отдаёт заданные OnecItem либо падает в fetch."""

    def __init__(self, params=None, items=None, exc: Exception | None = None):
        self._items = list(items or [])
        self._exc = exc
        self.closed = False

    def fetch(self, requests):
        if self._exc is not None:
            raise self._exc
        return self._items

    def close(self) -> None:
        self.closed = True


def _product(pid, article, name="Товар", guid=None):
    return {"id": pid, "article_supplier": article, "name": name, "onec_guid": guid}


def _item(status, price=None, detail="", sku=None, name=None):
    return onec.OnecItem(sku_1c=sku, price=price, status=status,
                         detail=detail, name_1c=name)


def _connect(app):
    return connect(app.config["DB_PATH"])


def _insert_product(con, article, *, name="Товар", manual=0):
    cur = con.execute(
        "INSERT INTO products (article_supplier, name, manual_price, created_at) "
        "VALUES (?, ?, ?, '2026-01-01')",
        (article, name, manual),
    )
    con.commit()
    return cur.lastrowid


def _insert_manual_price(con, pid, price):
    con.execute(
        "INSERT INTO manual_prices (product_id, price, valid_from) "
        "VALUES (?, ?, '2026-01-01')",
        (pid, str(price)),
    )
    con.commit()


def test_fetch_prices_ok():
    products = [_product(1, "ART-1"), _product(2, "ART-2")]
    items = [
        _item(onec.ItemStatus.OK, price=Decimal("1000"), sku="ART-1",
              name="Товар А"),
        _item(onec.ItemStatus.NOT_FOUND, detail="не найден", sku="ART-2"),
    ]
    prices, errors, names = fetch_prices(
        products, None, "com",
        source_factory=lambda params: FakeSource(params, items=items),
    )
    assert prices == {1: Decimal("1000")}
    assert set(errors) == {2}
    assert errors[2].startswith("1С: NOT_FOUND")
    assert names == {1: "Товар А"}


def test_fetch_prices_connection_error():
    """OnecError из источника → fetch_from_source нормализует в CONNECT_ERROR
    на каждый товар (ошибка подключения ≠ отсутствие цены)."""
    products = [_product(1, "ART-1"), _product(2, "ART-2")]
    prices, errors, names = fetch_prices(
        products, None, "com",
        source_factory=lambda params: FakeSource(
            params, exc=onec.OnecError("COM: connect failed")),
    )
    assert prices == {}
    assert set(errors) == {1, 2}
    assert all("CONNECT_ERROR" in text for text in errors.values())


def test_fetch_prices_unexpected_error():
    """Не-OnecError (сбой уровня кода) → все товары «1С недоступна»,
    исключение наружу не утекает."""
    products = [_product(1, "ART-1"), _product(2, "ART-2")]
    prices, errors, names = fetch_prices(
        products, None, "com",
        source_factory=lambda params: FakeSource(
            params, exc=RuntimeError("unexpected boom")),
    )
    assert prices == {}
    assert set(errors) == {1, 2}
    assert all("1С недоступна" in text for text in errors.values())
    assert names == {}


def test_fetch_prices_names_regardless_of_status():
    """Наименование 1С возвращается и для товаров без цены (ошибка статуса)."""
    products = [_product(1, "ART-1"), _product(2, "ART-2")]
    items = [
        _item(onec.ItemStatus.OK, price=Decimal("700"), sku="ART-1",
              name="Товар А"),
        _item(onec.ItemStatus.NOT_FOUND, detail="не найден", sku="ART-2",
              name="Товар Б"),
    ]
    prices, errors, names = fetch_prices(
        products, None, "com",
        source_factory=lambda params: FakeSource(params, items=items),
    )
    assert prices == {1: Decimal("700")}
    assert set(errors) == {2}
    assert names == {1: "Товар А", 2: "Товар Б"}


def test_fetch_prices_needs_confirm_not_used():
    """Неоднозначная цена НЕ попадает в расчёт автоматически."""
    products = [_product(1, "ART-1")]
    items = [_item(onec.ItemStatus.NEEDS_CONFIRM,
                   price=Decimal("500"), detail="источник не точный", sku="ART-1")]
    prices, errors, names = fetch_prices(
        products, None, "com",
        source_factory=lambda params: FakeSource(params, items=items),
    )
    assert prices == {}
    assert "NEEDS_CONFIRM" in errors[1]
    assert names == {}


def test_run_calculation_with_prices(app):
    con = _connect(app)
    pid_price = _insert_product(con, "ART-PRICE")
    pid_err = _insert_product(con, "ART-ERR")
    pid_manual = _insert_product(con, "ART-MAN", manual=1)
    _insert_manual_price(con, pid_manual, "900")

    marketplaces = {"wb": [
        {"row": 3, "article_mp": "A1", "article_supplier": "ART-PRICE"},
        {"row": 4, "article_mp": "A2", "article_supplier": "ART-ERR"},
        {"row": 5, "article_mp": "A3", "article_supplier": "ART-MAN"},
    ]}
    run_id = run_calculation(
        con, calc_date="2026-06-01", marketplaces=marketplaces,
        prices={pid_price: "1000"},
        price_errors={pid_err: "1С недоступна: таймаут"},
    )

    run = con.execute("SELECT total, ok, problems FROM runs WHERE id = ?",
                      (run_id,)).fetchone()
    assert (run["total"], run["ok"], run["problems"]) == (3, 2, 1)

    rows = {r["article_supplier"]: r for r in con.execute(
        "SELECT * FROM run_items WHERE run_id = ?", (run_id,)).fetchall()}
    assert rows["ART-PRICE"]["status"] == "ok"
    assert rows["ART-PRICE"]["cost"] == 1150  # floor(1000 * 1.15)
    assert rows["ART-PRICE"]["price_source"] == "1c"
    assert rows["ART-ERR"]["status"] == "problem"
    assert rows["ART-ERR"]["message"] == "1С недоступна: таймаут"
    assert rows["ART-MAN"]["status"] == "ok"
    con.close()


def test_run_calculation_backward_compat(app):
    """Без prices/price_errors поведение прежнее: external → PriceRequired."""
    con = _connect(app)
    _insert_product(con, "ART-EXT")
    run_id = run_calculation(
        con, calc_date="2026-06-01",
        marketplaces={"wb": [
            {"row": 3, "article_mp": "A1", "article_supplier": "ART-EXT"}]},
    )
    row = con.execute(
        "SELECT status, message FROM run_items WHERE run_id = ?",
        (run_id,)).fetchone()
    assert row["status"] == "problem"
    assert "не передана закупочная" in row["message"]
    con.close()


def test_load_params_missing(tmp_path):
    assert load_params(tmp_path) is None


def test_load_params_ok(tmp_path):
    (tmp_path / "config.ini").write_text(
        "[1C]\n"
        "server=srv-1c\n"
        "base=TradeBase\n"
        "user=Ivanov\n"
        "password=secret\n"
        "mode=COM\n"
        "\n"
        "[Цены]\n"
        "вид_цены=Закупочная\n"
        "поставщик=ГАВРЮШКИН\n",
        encoding="utf-8",
    )
    result = load_params(tmp_path)
    assert result is not None
    params, mode = result
    assert mode == "com"
    assert params.price_type == "Закупочная"
    assert params.supplier == "ГАВРЮШКИН"
    assert params.server == "srv-1c"
    assert params.base == "TradeBase"
