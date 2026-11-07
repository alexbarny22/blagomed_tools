# -*- coding: utf-8 -*-
"""Бизнес-тесты расчётного ядра (этап 4): формула, датировка, ошибки.

Все проверки — на синтетических БД (init_db + ручная вставка), без локальных
файлов старого бота. Сверка с эталонным XLSX за 07.2026 — отдельный скрипт
tools/compare_control_2026_07.py и tests/test_calc_compare.py.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.core.calc import (
    CostDetail, InvalidDate, InvalidInput, ManualPriceNotFound,
    PriceRequired, ProductNotFound, SettingNotFound,
    calc_product, compute_cost, get_extras, get_ppu_weight,
)
from app.core.db import init_db

# Закупочные цены из эталонного файла себестоимость_контроль_2026_07.xlsx
# (строки 232899 «Подушка-кольцо БЛАГО-ППК01» и 232914 «Подушка-овал БЛАГО-ППК02»).
REF_PRICE_232899 = 290
REF_PRICE_232914 = 296
PPU_WEIGHT_465 = 465
MARGIN_15 = 15


def _new_con(tmp_path):
    con = init_db(tmp_path / 'cost.db')
    return con


def _add_product(con, sku, manual_price=0):
    cur = con.execute(
        'INSERT INTO products (sku_1c, manual_price, active) VALUES (?, ?, 1)',
        (sku, int(manual_price)))
    return cur.lastrowid


def _add_settings(con, *, ppu=405.0, margin=15.0, valid_from='2026-07-01'):
    con.execute('INSERT INTO settings (key, value, valid_from) VALUES (?,?,?)',
                ('ppu_rub_per_kg', float(ppu), valid_from))
    con.execute('INSERT INTO settings (key, value, valid_from) VALUES (?,?,?)',
                ('margin_pct', float(margin), valid_from))


def _add_weight(con, pid, weight_g, valid_from):
    con.execute('INSERT INTO ppu_weights (product_id, weight_g, valid_from)'
                ' VALUES (?,?,?)', (pid, int(weight_g), valid_from))


def _add_extra(con, pid, kind, rub, valid_from, valid_to=None):
    con.execute("INSERT OR IGNORE INTO extra_kinds (name) VALUES (?)", (kind,))
    kid = con.execute('SELECT id FROM extra_kinds WHERE name = ?',
                      (kind,)).fetchone()['id']
    cur = con.execute('INSERT INTO extras (product_id, kind_id, rub, valid_from,'
                      ' valid_to) VALUES (?,?,?,?,?)',
                      (pid, kid, float(rub), valid_from, valid_to))
    return cur.lastrowid


def _add_manual(con, pid, price, valid_from):
    con.execute('INSERT INTO manual_prices (product_id, price, valid_from)'
                ' VALUES (?,?,?)', (pid, float(price), valid_from))


# ── A. Базовая формула ────────────────────────────────────────────────────

def test_a_basic_formula():
    r = compute_cost(price=1000, weight_g=270, ppu_rate=405,
                     extra_items=[('Поролон', 110)], margin_pct=15)
    assert r.cost == 1402
    assert r.ppu_rub == Decimal('109.35')   # 0.270 × 405, без округления
    assert r.basis == Decimal('1219.35')
    assert r.extras_rub == Decimal('110')
    assert r.price == Decimal('1000')
    assert r.ppu_weight_g == 270


# ── B. Товар без наполнителя ──────────────────────────────────────────────

def test_b_no_filler():
    r = compute_cost(price=1000, weight_g=0, ppu_rate=405, margin_pct=15)
    assert r.cost == 1150
    assert r.ppu_rub == Decimal('0')
    assert r.extras_rub == Decimal('0')


# ── C/D. Контроль 232899 и 232914 (эталонные цены, вес 465, произв. 0) ────

def test_c_check_232899():
    r = compute_cost(price=REF_PRICE_232899, weight_g=PPU_WEIGHT_465,
                     ppu_rate=405, margin_pct=MARGIN_15)
    assert r.cost == 550


def test_d_check_232914():
    r = compute_cost(price=REF_PRICE_232914, weight_g=PPU_WEIGHT_465,
                     ppu_rate=405, margin_pct=MARGIN_15)
    assert r.cost == 556


# ── E. Исключённые товары: ППУ не переносится → вес 0 из БД ───────────────

@pytest.mark.parametrize('sku', ['232900', '232917'])
def test_e_excluded_ppu_weight_is_zero(tmp_path, sku):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, sku, manual_price=0)
        _add_settings(con)
        con.commit()
        # записи веса нет (не переносилась миграцией) — расчёт с весом 0
        assert get_ppu_weight(con, pid, '2026-07-15') == 0
        r = calc_product(con, product_id=pid, price=1000, calc_date='2026-07-15')
        assert r.ppu_weight_g == 0
        assert r.cost == 1150                     # floor(1000 × 1.15)
    finally:
        con.close()


def test_e_explicit_weight_if_present(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232918')
        _add_settings(con)
        _add_weight(con, pid, 270, '2026-07-01')
        con.commit()
        r = calc_product(con, product_id=pid, price=1000, calc_date='2026-07-15')
        assert r.ppu_weight_g == 270
        assert r.cost == 1275                     # floor(1109.35 × 1.15)
    finally:
        con.close()


# ── F. Дополнительные расходы: сумма, детализация, завершение периода ─────

def test_f_extras_summary_and_details(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232956')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 110, '2026-07-01')
        _add_extra(con, pid, 'Упаковка', 25, '2026-07-01')
        con.commit()

        r = calc_product(con, product_id=pid, price=1000, calc_date='2026-07-15')
        assert r.extras_rub == Decimal('135')    # 110 + 25
        assert r.extras == (
            CostDetail(kind='Поролон', rub=Decimal('110')),
            CostDetail(kind='Упаковка', rub=Decimal('25')),
        )
        # та же сумма даётся детализацией из БД
        db_extras = get_extras(con, pid, '2026-07-15')
        assert sum(e.rub for e in db_extras) == r.extras_rub
    finally:
        con.close()


def test_f_ending_article_does_not_affect_past_calculation(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232957')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 100, '2026-07-01')
        _add_extra(con, pid, 'Упаковка', 25, '2026-07-01')
        con.commit()

        before = calc_product(con, product_id=pid, price=500,
                              calc_date='2026-07-20')
        assert before.extras_rub == Decimal('125')

        # завершили действие «Поролон» с 01.08
        con.execute("UPDATE extras SET valid_to = '2026-08-01'"
                    " WHERE product_id = ? AND kind_id ="
                    " (SELECT id FROM extra_kinds WHERE name = 'Поролон')",
                    (pid,))
        con.commit()

        after = calc_product(con, product_id=pid, price=500,
                             calc_date='2026-07-20')
        assert after == before                    # прошлый период не изменился
        aug = calc_product(con, product_id=pid, price=500, calc_date='2026-08-15')
        assert aug.extras_rub == Decimal('25')    # осталась только «Упаковка»
    finally:
        con.close()


# ── F2. Правило замены доп. расходов (владелец, 07.11.2026) ────────────────
#
# Новая запись той же статьи у товара заменяет предыдущую с даты valid_from;
# разные статьи суммируются; закрытая статья не возобновляется.
# (Пример владельца: «Поролон» 100 ₽ с 01.07, «Поролон» 120 ₽ с 01.11,
# «Упаковка» 25 ₽ с 01.07 → октябрь 125, ноябрь 145.)

def test_f2_replacement_from_new_date(tmp_path):
    """«Поролон» 100 ₽ с 01.07 → «Поролон» 120 ₽ с 01.11: замена по дате."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 100, '2026-07-01')
        _add_extra(con, pid, 'Поролон', 120, '2026-11-01')
        con.commit()
        oct_r = calc_product(con, product_id=pid, price=1000,
                             calc_date='2026-10-15')
        assert oct_r.extras_rub == Decimal('100')     # до 01.11 — старая
        nov_r = calc_product(con, product_id=pid, price=1000,
                             calc_date='2026-11-15')
        assert nov_r.extras_rub == Decimal('120')     # с 01.11 — замена
    finally:
        con.close()


def test_f2_different_kinds_sum(tmp_path):
    """Разные статьи суммируются: 120 (Поролон) + 25 (Упаковка) = 145 ₽."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 120, '2026-11-01')
        _add_extra(con, pid, 'Упаковка', 25, '2026-07-01')
        con.commit()
        r = calc_product(con, product_id=pid, price=1000,
                         calc_date='2026-11-15')
        assert r.extras_rub == Decimal('145')
        assert r.extras == (
            CostDetail(kind='Поролон', rub=Decimal('120')),
            CostDetail(kind='Упаковка', rub=Decimal('25')),
        )
    finally:
        con.close()


def test_f2_historical_october_unchanged(tmp_path):
    """Исторический расчёт за октябрь остаётся 125 ₽, ноябрь 145 ₽."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 100, '2026-07-01')
        _add_extra(con, pid, 'Упаковка', 25, '2026-07-01')
        _add_extra(con, pid, 'Поролон', 120, '2026-11-01')
        con.commit()
        oc = calc_product(con, product_id=pid, price=1000,
                          calc_date='2026-10-15')
        assert oc.extras_rub == Decimal('125')        # 100 + 25
        no = calc_product(con, product_id=pid, price=1000,
                          calc_date='2026-11-15')
        assert no.extras_rub == Decimal('145')        # 120 + 25
    finally:
        con.close()


def test_f2_same_valid_from_takes_higher_id(tmp_path):
    """При одинаковой дате начала действует запись с большим id."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        k1 = _add_extra(con, pid, 'Поролон', 100, '2026-07-01')
        k2 = _add_extra(con, pid, 'Поролон', 120, '2026-07-01')
        assert k2 > k1
        con.commit()
        r = calc_product(con, product_id=pid, price=1000,
                         calc_date='2026-07-15')
        assert r.extras_rub == Decimal('120')
    finally:
        con.close()


def test_f2_closed_article_not_revived(tmp_path):
    """Закрытая последняя запись не «возрождает» старую с valid_to=NULL."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 100, '2026-07-01')
        _add_extra(con, pid, 'Поролон', 120, '2026-11-01',
                   valid_to='2026-12-01')
        con.commit()
        nov_r = calc_product(con, product_id=pid, price=1000,
                             calc_date='2026-11-15')
        assert nov_r.extras_rub == Decimal('120')
        dec_r = calc_product(con, product_id=pid, price=1000,
                             calc_date='2026-12-15')
        assert dec_r.extras_rub == Decimal('0')       # старая 100 НЕ вернулась
        assert dec_r.extras == ()
    finally:
        con.close()


def test_f2_future_record_does_not_affect(tmp_path):
    """Запись с будущей датой начала не влияет на текущий расчёт."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 100, '2026-07-01')
        _add_extra(con, pid, 'Поролон', 999, '2027-01-01')
        con.commit()
        r = calc_product(con, product_id=pid, price=1000,
                         calc_date='2026-10-15')
        assert r.extras_rub == Decimal('100')
    finally:
        con.close()


def test_f2_invalid_period_raises(tmp_path):
    """valid_to <= valid_from — некорректные данные: понятная ошибка."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 100, '2026-11-01',
                   valid_to='2026-10-01')
        con.commit()
        with pytest.raises(InvalidInput, match='период действия'):
            get_extras(con, pid, '2026-12-15')
    finally:
        con.close()


def test_f2_zero_replacement_not_sum(tmp_path):
    """Нулевая новая запись заменяет старую: 100 → 0, а не 100 + 0 = 100."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 100, '2026-07-01')
        _add_extra(con, pid, 'Поролон', 0, '2026-11-01')
        con.commit()
        r = calc_product(con, product_id=pid, price=1000,
                         calc_date='2026-11-15')
        assert r.extras_rub == Decimal('0')
    finally:
        con.close()


def test_f2_repeated_calc_identical(tmp_path):
    """Повторные расчёты на одну дату возвращают идентичный результат."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 100, '2026-07-01')
        _add_extra(con, pid, 'Поролон', 120, '2026-11-01')
        _add_extra(con, pid, 'Упаковка', 25, '2026-07-01')
        con.commit()
        r1 = calc_product(con, product_id=pid, price=1000,
                          calc_date='2026-11-15')
        r2 = calc_product(con, product_id=pid, price=1000,
                          calc_date='2026-11-15')
        assert r1 == r2
    finally:
        con.close()


def test_f2_core_does_not_modify_extras_rows(tmp_path):
    """Расчётное ядро (calc/get_extras) не изменяет строки extras."""
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232958')
        _add_settings(con)
        _add_extra(con, pid, 'Поролон', 100, '2026-07-01')
        _add_extra(con, pid, 'Поролон', 120, '2026-11-01')
        _add_extra(con, pid, 'Упаковка', 25, '2026-07-01')
        con.commit()
        q = ('SELECT product_id, kind_id, rub, valid_from, valid_to'
             ' FROM extras ORDER BY id')
        before = [dict(row) for row in con.execute(q)]
        calc_product(con, product_id=pid, price=1000, calc_date='2026-11-15')
        get_extras(con, pid, '2026-11-15')
        after = [dict(row) for row in con.execute(q)]
        assert after == before                       # строки не изменены
    finally:
        con.close()


# ── G. Исторические ставки ППУ ────────────────────────────────────────────

def test_g_ppu_rate_historical(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232898')
        _add_settings(con)                       # 405 с 2026-07-01
        _add_weight(con, pid, 270, '2026-07-01')
        con.execute("INSERT INTO settings (key, value, valid_from) VALUES"
                    " ('ppu_rub_per_kg', 450.0, '2026-11-01')")
        con.commit()

        oct_r = calc_product(con, product_id=pid, price=1000,
                             calc_date='2026-10-15')
        assert oct_r.ppu_rate == Decimal('405')
        assert oct_r.ppu_rub == Decimal('109.35')

        nov_r = calc_product(con, product_id=pid, price=1000,
                             calc_date='2026-11-15')
        assert nov_r.ppu_rate == Decimal('450')
        assert nov_r.ppu_rub == Decimal('121.50')

        # добавление ещё более новой ставки не меняет прежний расчёт
        con.execute("INSERT INTO settings (key, value, valid_from) VALUES"
                    " ('ppu_rub_per_kg', 500.0, '2026-12-01')")
        con.commit()
        assert calc_product(con, product_id=pid, price=1000,
                            calc_date='2026-10-15') == oct_r
    finally:
        con.close()


# ── H. История наценки ────────────────────────────────────────────────────

def test_h_margin_history(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232946')
        _add_settings(con)                       # margin 15 с 2026-07-01
        con.execute("INSERT INTO settings (key, value, valid_from) VALUES"
                    " ('margin_pct', 18.0, '2026-11-01')")
        con.commit()

        old = calc_product(con, product_id=pid, price=1000, calc_date='2026-10-15')
        assert old.margin_pct == Decimal('15')
        assert old.cost == 1150

        new = calc_product(con, product_id=pid, price=1000, calc_date='2026-11-15')
        assert new.margin_pct == Decimal('18')
        assert new.cost == 1180
    finally:
        con.close()


# ── I. Ручная закупочная цена (FIXED_COSTS) ───────────────────────────────

def test_i_manual_price_priority(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232924', manual_price=1)
        _add_settings(con)
        _add_manual(con, pid, 350, '2026-07-01')
        con.commit()

        # внешняя цена 1000 есть, но приоритет у ручной
        r = calc_product(con, product_id=pid, price=1000, calc_date='2026-07-15')
        assert r.price_source == 'manual'
        assert r.price == Decimal('350')
        assert r.cost == 402                     # floor(350 × 1.15)

        # смена ручной цены с новой даты не влияет на прошлый период
        before = calc_product(con, product_id=pid, price=1000,
                              calc_date='2026-10-15')
        _add_manual(con, pid, 400, '2026-11-01')
        con.commit()
        assert calc_product(con, product_id=pid, price=1000,
                            calc_date='2026-10-15') == before
        later = calc_product(con, product_id=pid, price=1000,
                             calc_date='2026-11-15')
        assert later.price == Decimal('400')
        assert later.cost == 460
    finally:
        con.close()


def test_i_manual_flag_but_no_price_is_error(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232983', manual_price=1)
        _add_settings(con)
        con.commit()
        with pytest.raises(ManualPriceNotFound):
            calc_product(con, product_id=pid, price=1000, calc_date='2026-07-15')
    finally:
        con.close()


def test_i_usual_product_uses_external_price(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232888')
        _add_settings(con, ppu=405, margin=15)
        con.commit()
        r = calc_product(con, product_id=pid, price=40, calc_date='2026-07-15')
        assert r.price_source == '1c'
        assert r.cost == 46                      # floor(40 × 1.15)
    finally:
        con.close()


# ── J. Ошибочные данные ───────────────────────────────────────────────────

def test_j_missing_purchase_price_raises(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232947')
        _add_settings(con)
        con.commit()
        with pytest.raises(PriceRequired, match='закупочная цена'):
            calc_product(con, product_id=pid, calc_date='2026-07-15')
    finally:
        con.close()


@pytest.mark.parametrize('kwargs, exc', [
    ({'price': -1}, InvalidInput),                                  # отриц. цена
    ({'price': 100, 'extra_items': [('X', -5)]}, InvalidInput),     # отриц. расходы
    ({'price': 100, 'weight_g': -12}, InvalidInput),                # отриц. вес
    ({'price': 100, 'weight_g': 10001}, InvalidInput),              # вес > 10000
    ({'price': 100, 'weight_g': 'abc'}, InvalidInput),              # не-число
])
def test_j_invalid_inputs(kwargs, exc):
    with pytest.raises(exc):
        compute_cost(margin_pct=15, **kwargs)


def test_j_missing_required_setting(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232947')
        con.execute("INSERT INTO settings (key, value, valid_from) VALUES"
                    " ('margin_pct', 15.0, '2026-07-01')")   # нет ppu_rub_per_kg
        con.commit()
        with pytest.raises(SettingNotFound, match='ppu_rub_per_kg'):
            calc_product(con, product_id=pid, price=100, calc_date='2026-07-15')
    finally:
        con.close()


def test_j_settings_not_yet_valid_on_date(tmp_path):
    con = _new_con(tmp_path)
    try:
        pid = _add_product(con, '232947')
        _add_settings(con, valid_from='2026-08-01')     # на июль настроек нет
        con.commit()
        with pytest.raises(SettingNotFound, match='2026-07-01'):
            calc_product(con, product_id=pid, price=100, calc_date='2026-07-01')
    finally:
        con.close()


def test_j_unknown_product(tmp_path):
    con = _new_con(tmp_path)
    try:
        with pytest.raises(ProductNotFound):
            calc_product(con, product_id=987654, price=100, calc_date='2026-07-15')
    finally:
        con.close()


@pytest.mark.parametrize('bad_date', ['2026-13-40', 'not-a-date', '2026-7',
                                      None, object()])
def test_j_invalid_date(bad_date):
    with pytest.raises(InvalidDate):
        calc_product(con=None, product_id=1, price=100, calc_date=bad_date)


# ── K. Валидация значений (этап 4, аудит) ──────────────────────────────────

@pytest.mark.parametrize('field, value', [
    ('price', float('nan')), ('price', float('inf')),
    ('price', float('-inf')), ('price', True), ('price', False),
    ('ppu_rate', float('nan')), ('ppu_rate', float('inf')),
    ('ppu_rate', float('-inf')),
    ('margin_pct', float('nan')), ('margin_pct', float('inf')),
    ('margin_pct', float('-inf')), ('margin_pct', True),
])
def test_k_non_finite_and_bool_rejected(field, value):
    kw = {'price': 100, 'ppu_rate': 405, 'margin_pct': 15}
    kw[field] = value
    with pytest.raises(InvalidInput):
        compute_cost(**kw)


@pytest.mark.parametrize('weight', [270.9, -0.5, '270.9', '-0.5', True, False,
                                    10001, -12])
def test_k_bad_weight_rejected(weight):
    with pytest.raises(InvalidInput):
        compute_cost(price=100, weight_g=weight, margin_pct=15)


@pytest.mark.parametrize('weight', [270, '270', 270.0, 0])
def test_k_weight_must_be_whole_ok(weight):
    r = compute_cost(price=100, weight_g=weight, margin_pct=15)
    assert r.cost == 115


@pytest.mark.parametrize('rub', [float('nan'), float('inf'), True, None, -5,
                                 'abc', object()])
def test_k_bad_extra_rub_rejected(rub):
    with pytest.raises(InvalidInput):
        compute_cost(price=100, margin_pct=15,
                     extra_items=[('Поролон', rub)])


@pytest.mark.parametrize('item', [5, ('Поролон',), {'kind': 'X'},
                                  {'rub': 1}, object()])
def test_k_bad_extra_item_shape_rejected(item):
    with pytest.raises(InvalidInput):
        compute_cost(price=100, margin_pct=15, extra_items=[item])


def test_k_missing_price_is_explicit():
    with pytest.raises(InvalidInput, match='(?i)закупочная цена'):
        compute_cost(price=None, margin_pct=15)


def test_k_intermediate_sums_not_rounded():
    """Промежуточные суммы (ППУ, база) без округления; округляется итог."""
    r = compute_cost(price=1000, weight_g=333, ppu_rate=405, margin_pct=15)
    assert r.ppu_rub == Decimal('134.865')          # 0.333 × 405 — точно
    assert r.basis == Decimal('1134.865')
    assert r.cost == 1305                           # floor(1134.865 × 1.15)


def test_k_control_articles_old_and_new_logic():
    """232899/232914: старая логика (с доп. 75) повторяет эталон, новая даёт
    550/556 без переноса доп. расхода."""
    for price, new_expected, old_expected in ((290, 550, 636),
                                              (296, 556, 643)):
        old = compute_cost(price=price, weight_g=465, ppu_rate=405,
                           margin_pct=15, extra_items=[('доп', 75)])
        assert old.cost == old_expected
        new = compute_cost(price=price, weight_g=465, ppu_rate=405,
                           margin_pct=15)
        assert new.cost == new_expected
