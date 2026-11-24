from types import SimpleNamespace

from app.onec import _latest_price_candidate


def _c(sku, price_date):
    return SimpleNamespace(sku=sku, price_date=price_date)


def test_unique_latest_price_wins():
    got = [_c('A', '2024-01-10'), _c('B', '2024-06-01'), _c('C', None)]
    assert _latest_price_candidate(got).sku == 'B'


def test_tied_latest_price_stays_ambiguous():
    got = [_c('A', '2024-06-01'), _c('B', '2024-06-01'), _c('C', '2023-01-01')]
    assert _latest_price_candidate(got) is None


def test_no_price_dates_stays_ambiguous():
    got = [_c('A', None), _c('B', None)]
    assert _latest_price_candidate(got) is None
