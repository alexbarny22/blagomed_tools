"""Наименования 1С для позиций без цены (карточка есть, цены нет)."""

from decimal import Decimal

from app import onec


def _cand(sku, name, price=None, guid=None):
    return onec._Cand(
        sku=sku, name=name, guid=guid,
        price=(Decimal(str(price)) if price is not None else None),
        price_date=None, price_type=None, supplier=None,
    )


def test_card_without_price_keeps_name():
    """1) Карточка в 1С есть, цены нет → наименование есть, цены нет."""
    cands = [_cand('232876', 'Крем детский')]
    item = onec.resolve_candidates(
        onec.OnecRequest(sku_1c='232876'), cands, 'com')
    assert item.price is None
    assert item.status == onec.ItemStatus.NO_PRICE
    assert item.name_1c == 'Крем детский'


def test_duplicates_same_name_keep_name():
    """2) Дубли артикула с одинаковым наименованием → имя заполняется."""
    cands = [
        _cand('232886', 'Масло сливочное', guid='guid-1'),
        _cand('232886', 'масло   сливочное', guid='guid-2'),
    ]
    item = onec.resolve_candidates(
        onec.OnecRequest(sku_1c='232886'), cands, 'com')
    assert item.price is None
    assert item.status == onec.ItemStatus.AMBIGUOUS
    assert item.name_1c == 'Масло сливочное'


def test_duplicates_different_names_leave_name_empty():
    """3) Дубли артикула с разными наименованиями → имя остаётся пустым."""
    cands = [
        _cand('232886', 'Масло сливочное', guid='guid-1'),
        _cand('232886', 'Масло топлёное', guid='guid-2'),
    ]
    item = onec.resolve_candidates(
        onec.OnecRequest(sku_1c='232886'), cands, 'com')
    assert item.price is None
    assert item.status == onec.ItemStatus.AMBIGUOUS
    assert item.name_1c is None


def test_marketplace_name_is_replaced_by_1c_name():
    """4) В запросе есть наименование МП, но name_1c = наименование 1С."""
    cands = [
        _cand('232876', 'Крем детский 50 мл', guid='guid-1'),
        _cand('232876', 'крем детский 50 мл', guid='guid-2'),
    ]
    item = onec.resolve_candidates(
        onec.OnecRequest(sku_1c='232876', name_1c='КРЕМ ДЕТСКИЙ WB'),
        cands, 'com')
    assert item.price is None
    assert item.status == onec.ItemStatus.AMBIGUOUS
    assert item.name_1c == 'Крем детский 50 мл'
