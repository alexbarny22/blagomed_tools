#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Расчётное ядро «Себестоимость МП» (этап 4).

Формула (округление — вниз до целого рубля):

    cost = floor((price + weight_g / 1000 × ppu_rate + sum(extras))
                 × (1 + margin_pct / 100))

Модуль независим от Flask, 1С и Excel: работает только с sqlite3.Connection
(из app.core.db.connect) и явно переданной датой расчёта. В БД ничего не пишет —
сохранение исторических снимков реализуется на этапе 6.

Все значения выбираются «на дату расчёта»: для ppu_weights, settings и
manual_prices берётся последняя запись с valid_from <= date. Для extras
действует правило владельца (07.11.2026): новая запись той же статьи
заменяет предыдущую с даты valid_from, разные статьи суммируются, закрытая
статья не возобновляется (алгоритм — в get_extras).

    Входные значения валидируются: денежные величины — конечные числа
    (NaN/Infinity/булевы отклоняются), вес — целое число граммов.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Iterable

__all__ = [
    'CalcError', 'InvalidDate', 'InvalidInput', 'ManualPriceNotFound',
    'PriceRequired', 'ProductNotFound', 'SettingNotFound',
    'CostDetail', 'CostResult',
    'compute_cost', 'calc_product', 'get_ppu_weight', 'get_manual_price',
    'get_setting', 'get_settings', 'get_extras',
]


# ── Собственные исключения ────────────────────────────────────────────────


class CalcError(Exception):
    """Базовая ошибка расчёта себестоимости с понятным сообщением."""


class InvalidDate(CalcError):
    """Дата расчёта не является корректной ISO YYYY-MM-DD."""


class ProductNotFound(CalcError):
    """Товар с указанным product_id отсутствует в базе."""


class PriceRequired(CalcError):
    """Для обычного товара не передана закупочная цена (внешний источник)."""


class ManualPriceNotFound(CalcError):
    """Товар помечен как ручная цена, но manual_prices на дату нет."""


class SettingNotFound(CalcError):
    """Обязательная настройка расчёта отсутствует на дату."""


class InvalidInput(CalcError):
    """Некорректное входное значение (отрицательные/сверхдиапазонные)."""


# ── Значения по умолчанию ─────────────────────────────────────────────────

D = Decimal
WEIGHT_MAX_G = 10_000


# ── Служебные преобразования ──────────────────────────────────────────────


def _as_date(calc_date) -> str:
    """Принимает ISO-строку или datetime.date."""
    if isinstance(calc_date, date):
        return calc_date.isoformat()
    if isinstance(calc_date, str):
        s = calc_date.strip()
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            raise InvalidDate(
                f'Некорректная дата расчёта: {calc_date!r}. '
                f'Ожидается ISO YYYY-MM-DD (например 2026-07-01).') from None
    raise InvalidDate(
        f'Некорректная дата расчёта: {calc_date!r}. '
        'Ожидается строка ISO YYYY-MM-DD.')


def _dec(value, what: str) -> Decimal:
    """Точное преобразование в Decimal; NaN/Infinity/булевы отклоняются."""
    if value is None:
        raise InvalidInput(f'{what}: пустое значение (None)')
    if isinstance(value, bool):
        raise InvalidInput(f'{what}: булево значение недопустимо: {value!r}')
    try:
        d = Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        raise InvalidInput(f'{what}: некорректное число: {value!r}') from None
    if not d.is_finite():
        raise InvalidInput(f'{what}: недопустимое не-конечное значение: {value!r}')
    return d


def _require_dec(value, what: str, *, min_value: Decimal = Decimal('0')) -> Decimal:
    d = _dec(value, what)
    if d < min_value:
        raise InvalidInput(f'{what} не может быть меньше {min_value}: {d}')
    return d


# ── Результаты ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CostDetail:
    """Одна статья дополнительных расходов."""
    kind: str
    rub: Decimal


@dataclass(frozen=True)
class CostResult:
    """Раскладка расчёта, совместимая с полями cost_snapshots.

    cost — целая себестоимость (округление вниз); price_source — '1c' или
    'manual'. extras — детализация по статьям, extras_rub — их сумма.
    Сохранение в БД (runs/cost_snapshots) выполняется на этапе 6.
    """
    product_id: int
    price: Decimal
    price_source: str
    ppu_weight_g: int
    ppu_rate: Decimal
    ppu_rub: Decimal
    extras: tuple[CostDetail, ...]
    extras_rub: Decimal
    margin_pct: Decimal
    basis: Decimal
    cost: int


# ── A. Чистая функция расчёта ─────────────────────────────────────────────


def compute_cost(*, price, weight_g=0, ppu_rate=0,
                 extra_items: Iterable | None = None,
                 margin_pct=0,
                 product_id: int = 0, price_source: str = '1c') -> CostResult:
    """Считает себестоимость по основной формуле без обращения к БД.

    - price — обязательная закупочная цена (None/пустое не равно нулю);
    - weight_g — вес ППУ в граммах, целое неотрицательное (пустое = 0;
      дробные граммы и булевы отклоняются; диапазон 0..10000);
    - ppu_rate — цена ППУ за кг (>= 0);
    - extra_items — статьи доп. расходов: последовательность (kind, rub)
      или {'kind': ..., 'rub': ...};
    - margin_pct — процент наценки (>= 0).

    Промежуточные составляющие не округляются; округляется только итог
    (ROUND_FLOOR). Возвращает полную раскладку (CostResult).
    """
    if price is None:
        raise InvalidInput('Закупочная цена не передана (None). Ноль вместо цены '
                           'не подставляется — проверьте данные 1С/ручной цены.')
    price = _require_dec(price, 'Закупочная цена')
    ppu_rate = _require_dec(ppu_rate, 'Цена ППУ за кг')
    margin_pct = _require_dec(margin_pct, 'Наценка, %')

    if weight_g is None or str(weight_g).strip() == '':
        weight_g = 0
    if isinstance(weight_g, bool):
        raise InvalidInput(f'Вес ППУ: булево значение недопустимо: {weight_g!r}')
    w = _dec(weight_g, 'Вес ППУ')
    if w != w.to_integral_value():
        raise InvalidInput(
            f'Вес ППУ должен быть целым числом граммов: {weight_g!r}')
    if w < 0 or w > WEIGHT_MAX_G:
        raise InvalidInput(
            f'Вес ППУ вне диапазона 0..{WEIGHT_MAX_G} г: {w}')
    weight_g = int(w)

    extras: list[CostDetail] = []
    extra_sum = Decimal('0')
    for el in (extra_items or ()):
        try:
            if isinstance(el, CostDetail):
                kind, rub = el.kind, el.rub
            elif isinstance(el, dict):
                kind = el.get('kind', el.get('name'))
                rub = el.get('rub', el.get('value'))
            else:
                kind, rub = el[0], el[1]
        except (TypeError, IndexError, KeyError, AttributeError):
            raise InvalidInput(
                f'Доп. расход: некорректная запись: {el!r}') from None
        if kind is None or str(kind).strip() == '':
            raise InvalidInput(
                f'Доп. расход: пустое наименование статьи: {el!r}')
        rub = _require_dec(rub, f'Доп. расход «{kind}»')
        extras.append(CostDetail(kind=str(kind), rub=rub))
        extra_sum += rub

    ppu_rub = (Decimal(weight_g) / Decimal(1000)) * ppu_rate
    basis = price + ppu_rub + extra_sum
    raw = basis * (Decimal('1') + margin_pct / Decimal('100'))
    cost = int(raw.to_integral_value(rounding=ROUND_FLOOR))

    return CostResult(
        product_id=product_id,
        price=price,
        price_source=price_source,
        ppu_weight_g=weight_g,
        ppu_rate=ppu_rate,
        ppu_rub=ppu_rub,
        extras=tuple(extras),
        extras_rub=extra_sum,
        margin_pct=margin_pct,
        basis=basis,
        cost=cost,
    )


# ── B. Чтение параметров из SQLite на дату ────────────────────────────────


def get_setting(con: sqlite3.Connection, key: str, calc_date) -> Decimal:
    """Последняя запись settings с valid_from <= calc_date; обязательна."""
    d = _as_date(calc_date)
    row = con.execute(
        'SELECT value FROM settings WHERE key = ? AND valid_from <= ?'
        ' ORDER BY valid_from DESC, id DESC LIMIT 1', (key, d)).fetchone()
    if row is None:
        raise SettingNotFound(
            f'Не найдена обязательная настройка «{key}» на дату {d}. '
            f'Проверьте данные settings (нет записи с valid_from <= {d}).')
    return _require_dec(row['value'], f'settings.{key}')


def get_settings(con: sqlite3.Connection, calc_date) -> dict[str, Decimal]:
    """ppu_rub_per_kg и margin_pct — обязательные параметры расчёта."""
    return {
        'ppu_rub_per_kg': get_setting(con, 'ppu_rub_per_kg', calc_date),
        'margin_pct': get_setting(con, 'margin_pct', calc_date),
    }


def get_ppu_weight(con: sqlite3.Connection, product_id: int, calc_date) -> int:
    """Вес ППУ на дату; при отсутствии записи — 0 (нет ошибки)."""
    d = _as_date(calc_date)
    row = con.execute(
        'SELECT weight_g FROM ppu_weights WHERE product_id = ?'
        ' AND valid_from <= ? ORDER BY valid_from DESC, id DESC LIMIT 1',
        (product_id, d)).fetchone()
    if row is None or row['weight_g'] is None:
        return 0
    w = int(row['weight_g'])
    if w < 0 or w > WEIGHT_MAX_G:
        raise InvalidInput(
            f'Вес ППУ товара {product_id} вне диапазона 0..{WEIGHT_MAX_G} г: {w}')
    return w


def get_manual_price(con: sqlite3.Connection, product_id: int,
                     calc_date) -> Decimal | None:
    """Ручная закупочная цена на дату (последний valid_from <= date) или None."""
    d = _as_date(calc_date)
    row = con.execute(
        'SELECT price FROM manual_prices WHERE product_id = ?'
        ' AND valid_from <= ? ORDER BY valid_from DESC, id DESC LIMIT 1',
        (product_id, d)).fetchone()
    if row is None:
        return None
    return _require_dec(row['price'], f'manual_prices[{product_id}]')


def get_extras(con: sqlite3.Connection, product_id: int,
               calc_date) -> tuple[CostDetail, ...]:
    """Доп. расходы на дату — правило владельца от 07.11.2026.

    Новая запись той же статьи заменяет предыдущую с даты valid_from;
    разные статьи суммируются. Схема extras не изменялась, исторические
    записи не редактируются и не закрываются автоматически.

    Алгоритм для product_id на дату d = _as_date(calc_date):
    1. берутся ВСЕ записи статьи с valid_from <= d;
    2. среди них для каждой статьи (kind_id) выбирается самая поздняя по
       valid_from (при совпадении дат — больший id, детерминированный
       порядок);
    3. статья учитывается, только если у выбранной записи valid_to IS NULL
       или valid_to > d; более ранняя запись НЕ возвращается, даже с
       valid_to = NULL (закрытая статья не возобновляется);
    4. записи с valid_to <= valid_from некорректны: понятная ошибка
       InvalidInput, а не «тихий» неправильный расход.
    """
    d = _as_date(calc_date)
    rows = con.execute(
        'SELECT e.kind_id, k.name AS kind, e.rub, e.valid_from, e.valid_to'
        ' FROM extras e JOIN extra_kinds k ON k.id = e.kind_id'
        ' WHERE e.product_id = ? AND e.valid_from <= ?'
        ' ORDER BY e.kind_id, e.valid_from DESC, e.id DESC',
        (product_id, d)).fetchall()

    for r in rows:
        if r['valid_to'] is not None and r['valid_to'] <= r['valid_from']:
            raise InvalidInput(
                f'Доп. расход «{r["kind"]}» товара {product_id}: некорректный '
                f'период действия valid_to ({r["valid_to"]}) <= valid_from '
                f'({r["valid_from"]}). Исправьте данные extras периодов.')

    result: list[CostDetail] = []
    last_kind: object = None
    for r in rows:
        if r['kind_id'] == last_kind:
            continue          # у статьи уже учтена более поздняя запись
        last_kind = r['kind_id']
        if r['valid_to'] is None or r['valid_to'] > d:
            result.append(CostDetail(
                kind=r['kind'],
                rub=_require_dec(r['rub'],
                                 f'extras[{product_id}].{r["kind"]}'),
            ))
    return tuple(result)


# ── C. Расчёт позиции на дату ─────────────────────────────────────────────


def calc_product(con: sqlite3.Connection, *, product_id: int,
                 price=None, calc_date) -> CostResult:
    """Полный расчёт позиции на дату, источник цены — по products.manual_price.

    Если products.manual_price = 1 — берётся ручная цена из manual_prices на
    дату (при её отсутствии — ManualPriceNotFound, а не 0). Иначе используется
    переданная закупочная цена; при отсутствии — PriceRequired.
    """
    d = _as_date(calc_date)
    row = con.execute(
        'SELECT id, manual_price FROM products WHERE id = ?', (product_id,)
    ).fetchone()
    if row is None:
        raise ProductNotFound(
            f'Товар product_id={product_id} не найден в products.')
    is_manual = int(row['manual_price'] or 0) == 1

    settings = get_settings(con, d)

    if is_manual:
        manual_rub = get_manual_price(con, product_id, d)
        if manual_rub is None:
            raise ManualPriceNotFound(
                f'Товар product_id={product_id} помечен как «ручная цена» '
                f'(products.manual_price=1), но в manual_prices нет записи, '
                f'действующей на {d}. Ноль вместо цены не подставляется.')
        used_price, price_source = manual_rub, 'manual'
    else:
        if price is None:
            raise PriceRequired(
                f'Для товара product_id={product_id} не передана закупочная '
                f'цена (внешний источник). None не равно нулю — проверьте '
                f'данные 1С/экспорта.')
        used_price = _require_dec(price, f'Закупочная цена товара {product_id}')
        price_source = '1c'

    return compute_cost(
        price=used_price,
        weight_g=get_ppu_weight(con, product_id, d),
        ppu_rate=settings['ppu_rub_per_kg'],
        extra_items=get_extras(con, product_id, d),
        margin_pct=settings['margin_pct'],
        product_id=product_id,
        price_source=price_source,
    )