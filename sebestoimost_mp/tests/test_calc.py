"""Тесты расчётного ядра app/calc.py."""

from contextlib import contextmanager
from decimal import Decimal

import pytest

from app import db as app_db
from app.calc import (
    InvalidInput,
    ManualPriceNotFound,
    PriceRequired,
    calc_product,
    compute_cost,
    get_extras,
    get_setting,
)


@contextmanager
def _open(app):
    app_db.init_db(app.config['DB_PATH'])
    con = app_db.connect(app.config['DB_PATH'])
    try:
        yield con
    finally:
        con.close()


def _add_product(con, article, *, manual_price=0, created_at='2026-01-01', name='Товар'):
    cur = con.execute(
        "INSERT INTO products (article_supplier, name, manual_price, created_at) "
        "VALUES (?, ?, ?, ?)",
        (article, name, manual_price, created_at),
    )
    con.commit()
    return cur.lastrowid


def _add_manual_price(con, product_id, price, valid_from):
    con.execute(
        "INSERT INTO manual_prices (product_id, price, valid_from) VALUES (?, ?, ?)",
        (product_id, str(price), valid_from),
    )
    con.commit()


def _add_kind(con, name):
    cur = con.execute("INSERT INTO extra_kinds (name) VALUES (?)", (name,))
    con.commit()
    return cur.lastrowid


def _add_extra(con, product_id, kind_id, rub, valid_from, valid_to=None):
    con.execute(
        "INSERT INTO extras (product_id, kind_id, rub, valid_from, valid_to) "
        "VALUES (?, ?, ?, ?, ?)",
        (product_id, kind_id, str(rub), valid_from, valid_to),
    )
    con.commit()


def _add_weight(con, product_id, weight_g, valid_from):
    con.execute(
        "INSERT INTO ppu_weights (product_id, weight_g, valid_from) VALUES (?, ?, ?)",
        (product_id, weight_g, valid_from),
    )
    con.commit()


def _extras_total(details):
    return sum((d.rub for d in details), Decimal('0'))


def test_control_example():
    res = compute_cost(
        price=1000, weight_g=200, ppu_rate=405,
        extra_items=[('Упаковка', 50)], margin_pct=15,
    )
    assert res.cost == 1300
    assert res.ppu_rub == Decimal('81')


def test_no_weight_no_ppu():
    res = compute_cost(price=1000, ppu_rate=405, margin_pct=15)
    assert res.ppu_rub == 0
    assert res.cost == 1150


def test_price_none_raises():
    with pytest.raises(InvalidInput):
        compute_cost(price=None)


def test_margin_is_percent():
    res = compute_cost(price=1000, margin_pct=15)
    assert res.cost == 1150


def test_calc_product_manual_price(app):
    with _open(app) as con:
        pid = _add_product(con, 'A1', manual_price=1)
        _add_manual_price(con, pid, '1000', '2026-01-01')

        res = calc_product(con, product_id=pid, calc_date='2026-06-01')
        assert res.price_source == 'manual'
        assert res.price == Decimal('1000')

        pid_no_price = _add_product(con, 'A2', manual_price=1)
        with pytest.raises(ManualPriceNotFound):
            calc_product(con, product_id=pid_no_price, calc_date='2026-06-01')


def test_calc_product_external_price(app):
    with _open(app) as con:
        pid = _add_product(con, 'B1', manual_price=0)
        res = calc_product(con, product_id=pid, price=500, calc_date='2026-06-01')
        assert res.price_source == '1c'
        assert res.price == Decimal('500')
        with pytest.raises(PriceRequired):
            calc_product(con, product_id=pid, calc_date='2026-06-01')


def test_settings_on_date(app):
    with _open(app) as con:
        con.execute(
            "INSERT INTO settings (key, value, valid_from) VALUES (?, ?, ?)",
            ('ppu_rub_per_kg', '500', '2026-07-01'),
        )
        con.commit()
        assert get_setting(con, 'ppu_rub_per_kg', '2026-06-01') == Decimal('405')
        assert get_setting(con, 'ppu_rub_per_kg', '2026-07-01') == Decimal('500')


def test_extras_versioning(app):
    with _open(app) as con:
        pid = _add_product(con, 'C1')
        k_porolon = _add_kind(con, 'Поролон')
        k_upak = _add_kind(con, 'Упаковка')
        _add_extra(con, pid, k_porolon, '100', '2026-01-01')
        _add_extra(con, pid, k_porolon, '150', '2026-06-01')
        _add_extra(con, pid, k_upak, '50', '2026-01-01')

        assert _extras_total(get_extras(con, pid, '2026-05-01')) == Decimal('150')
        assert _extras_total(get_extras(con, pid, '2026-06-01')) == Decimal('200')

        # закрытие статьи: не возобновляется после valid_to
        _add_extra(con, pid, k_upak, '50', '2026-08-01', valid_to='2026-09-01')
        extras_after = get_extras(con, pid, '2026-10-01')
        kinds = {d.kind for d in extras_after}
        assert 'Упаковка' not in kinds
        assert _extras_total(extras_after) == Decimal('150')


def test_extras_invalid_period(app):
    with _open(app) as con:
        pid = _add_product(con, 'D1')
        k = _add_kind(con, 'Брак')
        _add_extra(con, pid, k, '10', '2026-02-01', valid_to='2026-01-01')
        with pytest.raises(InvalidInput):
            get_extras(con, pid, '2026-03-01')


def test_weight_from_db(app):
    with _open(app) as con:
        pid = _add_product(con, 'E1', manual_price=0)
        _add_weight(con, pid, 200, '2026-01-01')
        res = calc_product(con, product_id=pid, price=1000, calc_date='2026-06-01')
        assert res.ppu_weight_g == 200
        assert res.ppu_rub == Decimal('81.000')
        assert res.cost == 1243


def test_history_not_rewritten(app):
    with _open(app) as con:
        pid = _add_product(con, 'F1', manual_price=1)
        _add_manual_price(con, pid, '1000', '2026-01-01')
        res1 = calc_product(con, product_id=pid, calc_date='2026-06-01')
        assert res1.price == Decimal('1000')
        _add_manual_price(con, pid, '2000', '2026-07-01')
        res2 = calc_product(con, product_id=pid, calc_date='2026-06-01')
        assert res2.price == Decimal('1000')
        res3 = calc_product(con, product_id=pid, calc_date='2026-08-01')
        assert res3.price == Decimal('2000')
