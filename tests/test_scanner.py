"""Тесты Этапа 4 (§6): расчёт спредов и фильтры сканера."""

import pytest

from arb.models import FundingInfo, Quote
from arb.scanner import (
    PersistenceTracker,
    Scanner,
    candle_low_high,
    expected_net_funding,
    historical_price_divergence,
    net_spread,
    raw_spread,
    round_trip_fee,
    slippage_from_book,
    walk_book,
)


def q(exchange, symbol, bid, ask):
    return Quote(exchange, symbol, bid, ask, timestamp=0)


# ---- чистые функции ----
def test_raw_spread():
    assert raw_spread(101.0, 100.0) == pytest.approx(0.01)
    assert raw_spread(100.0, 0.0) == 0.0


def test_round_trip_fee():
    assert round_trip_fee(0.0005, 0.00055) == pytest.approx(0.0021)


def test_expected_net_funding_short_high_long_low():
    # funding_H положит. -> шорт на H получает; funding_L положит. -> лонг на L платит
    fh = FundingInfo("h", "BTC/USDT", 0.0001, interval_hours=8)
    fl = FundingInfo("l", "BTC/USDT", 0.00005, interval_hours=8)
    inc = expected_net_funding(fh, fl, hold_hours=8)
    assert inc == pytest.approx(0.0001 - 0.00005)


def test_expected_net_funding_uses_actual_interval():
    fh = FundingInfo("h", "BTC/USDT", 0.0001, interval_hours=4)  # 2 периода за 8ч
    inc = expected_net_funding(fh, None, hold_hours=8)
    assert inc == pytest.approx(0.0001 * 2)


def test_expected_net_funding_default_interval_when_missing():
    fh = FundingInfo("h", "BTC/USDT", 0.0001, interval_hours=None)
    inc = expected_net_funding(fh, None, hold_hours=8, default_interval_hours=8)
    assert inc == pytest.approx(0.0001)


def test_net_spread():
    assert net_spread(0.01, 0.0021, 0.001, 0.0002) == pytest.approx(0.01 - 0.0021 - 0.001 + 0.0002)


def test_walk_book_enough_depth():
    asks = [[100.0, 10], [101.0, 10]]  # 1000 + 1010 нотионала
    vwap, filled = walk_book(asks, 1000)
    assert filled == pytest.approx(1000)
    assert vwap == pytest.approx(100.0)


def test_walk_book_partial():
    asks = [[100.0, 1]]  # только 100 нотионала
    vwap, filled = walk_book(asks, 1000)
    assert filled == pytest.approx(100)


def test_slippage_from_book():
    asks = [[100.0, 5], [110.0, 100]]  # 500 по 100, дальше по 110
    slip = slippage_from_book(asks, ref_price=100.0, target_notional=1000)
    assert slip is not None and slip > 0


def test_slippage_none_when_shallow():
    asks = [[100.0, 1]]
    assert slippage_from_book(asks, 100.0, 1000) is None


# ---- persistence ----
def test_persistence_tracker():
    t = {"v": 0.0}
    tr = PersistenceTracker(clock=lambda: t["v"])
    key = ("BTC/USDT", "h", "l")
    assert tr.update(key, True) == 0.0
    t["v"] = 3.0
    assert tr.update(key, True) == 3.0
    # падение ниже порога сбрасывает
    assert tr.update(key, False) == 0.0
    t["v"] = 5.0
    assert tr.update(key, True) == 0.0


# ---- Scanner ----
def _scanner(**kw):
    defaults = dict(
        fees={"h": 0.0005, "l": 0.0005},
        min_gross_spread=0.005,
        min_net_spread=0.002,
        max_slippage=0.001,
        min_spread_persistence=0.0,
    )
    defaults.update(kw)
    return Scanner(**defaults)


def test_evaluate_pair_passes():
    s = _scanner()
    ev = s.evaluate_pair("BTC/USDT", "h", "l",
                         q("h", "BTC/USDT", 101.0, 101.1),
                         q("l", "BTC/USDT", 99.9, 100.0))
    assert ev.passed is True
    assert ev.signal.raw_spread == pytest.approx(0.01)


def test_evaluate_pair_rejects_low_gross():
    s = _scanner()
    ev = s.evaluate_pair("BTC/USDT", "h", "l",
                         q("h", "BTC/USDT", 100.2, 100.3),
                         q("l", "BTC/USDT", 99.9, 100.0))
    assert ev.passed is False
    assert any("raw<" in r for r in ev.reasons)


def test_evaluate_pair_rejects_persistence():
    t = {"v": 0.0}
    s = _scanner(min_spread_persistence=5.0,
                 persistence=PersistenceTracker(clock=lambda: t["v"]))
    ev = s.evaluate_pair("BTC/USDT", "h", "l",
                         q("h", "BTC/USDT", 101.0, 101.1),
                         q("l", "BTC/USDT", 99.9, 100.0))
    assert ev.passed is False
    assert any("persistence" in r for r in ev.reasons)


def test_scan_symbol_picks_best_pair():
    s = _scanner()
    quotes = {
        "a": q("a", "BTC/USDT", 100.0, 100.1),
        "b": q("b", "BTC/USDT", 99.0, 99.1),
        "c": q("c", "BTC/USDT", 101.5, 101.6),  # самый дорогой -> H
    }
    sig = s.scan_symbol("BTC/USDT", quotes)
    assert sig is not None
    # лучший спред: шорт на c (bid 101.5), лонг на b (ask 99.1)
    assert sig.exchange_high == "c"
    assert sig.exchange_low == "b"


def test_scan_symbol_none_when_no_edge():
    s = _scanner()
    quotes = {
        "a": q("a", "BTC/USDT", 100.0, 100.1),
        "b": q("b", "BTC/USDT", 100.0, 100.1),
    }
    assert s.scan_symbol("BTC/USDT", quotes) is None


# ---- защита от фейковых спредов ----
def test_evaluate_pair_rejects_huge_spread():
    # сырой спред 7.6% > max_gross_spread -> отклонить как ошибку данных
    s = _scanner(max_gross_spread=0.05)
    ev = s.evaluate_pair("TAIKO/USDT", "h", "l",
                         q("h", "TAIKO/USDT", 1.076, 1.077),
                         q("l", "TAIKO/USDT", 0.999, 1.0))
    assert ev.passed is False
    assert any("ошибка данных" in r for r in ev.reasons)


def test_evaluate_pair_rejects_stale_quote():
    s = _scanner(max_quote_age_ms=5000)
    # now=100с -> 100000мс; котировка H с timestamp=90000мс -> возраст 10с > 5с
    qh = Quote("h", "BTC/USDT", 101.0, 101.1, timestamp=90000)
    ql = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=100000)
    ev = s.evaluate_pair("BTC/USDT", "h", "l", qh, ql, now=100.0)
    assert ev.passed is False
    assert any("устаревшая" in r for r in ev.reasons)


def test_evaluate_pair_rejects_thin_book():
    # объёма верхушки стакана не хватает под нужный нотионал
    s = _scanner(notional_target=2000.0, check_top_depth=True)
    qh = Quote("h", "BTC/USDT", 101.0, 101.1, bid_volume=0.001, ask_volume=0.001)
    ql = Quote("l", "BTC/USDT", 99.9, 100.0, bid_volume=0.001, ask_volume=0.001)
    ev = s.evaluate_pair("BTC/USDT", "h", "l", qh, ql)
    assert ev.passed is False
    assert any("глубины стакана" in r for r in ev.reasons)


def test_evaluate_pair_passes_deep_book():
    s = _scanner(notional_target=2000.0, check_top_depth=True)
    # нужно ~20 базы при цене 100; объёмов достаточно
    qh = Quote("h", "BTC/USDT", 101.0, 101.1, bid_volume=100, ask_volume=100)
    ql = Quote("l", "BTC/USDT", 99.9, 100.0, bid_volume=100, ask_volume=100)
    ev = s.evaluate_pair("BTC/USDT", "h", "l", qh, ql)
    assert ev.passed is True


# ---- историческая сверка тождественности ----
def _candles(low, high):
    # [ts, open, high, low, close, vol] x 3 дня
    return [[i, low, high, low, high, 1] for i in range(3)]


def test_candle_low_high():
    assert candle_low_high(_candles(100.0, 110.0)) == (100.0, 110.0)


def test_candle_low_high_empty():
    assert candle_low_high([]) is None


def test_historical_divergence_same_asset_small():
    a = _candles(100.0, 110.0)
    b = _candles(100.2, 110.1)  # почти те же уровни
    div = historical_price_divergence(a, b)
    assert div < 0.003  # < 0.3% -> один актив


def test_historical_divergence_different_asset_large():
    a = _candles(100.0, 110.0)
    b = _candles(200.0, 220.0)  # цены в 2 раза выше -> другой актив
    div = historical_price_divergence(a, b)
    assert div > 0.3


def test_historical_divergence_missing_data():
    assert historical_price_divergence([], _candles(100, 110)) is None
