"""Тесты анализа funding-арбитража (arb/funding.py)."""

import pytest

from arb.funding import (
    average_hourly,
    build_report,
    collect,
    fetch_history,
    hourly_rate,
    infer_interval_hours,
    pair_opportunity,
    parse_history_row,
    sign_stability,
)

H = 3_600_000.0  # час в мс


def series(rate, n=10, interval_h=8, start=0.0):
    """История с постоянной ставкой и заданным периодом начисления."""
    return [(start + i * interval_h * H, rate) for i in range(n)]


# ---- нормировка на час ----
def test_hourly_rate_normalizes_interval():
    # 0.008 за 8ч и 0.001 за 1ч — это ОДНА И ТА ЖЕ доходность в час
    assert hourly_rate(0.008, 8) == pytest.approx(0.001)
    assert hourly_rate(0.001, 1) == pytest.approx(0.001)


def test_hourly_rate_defaults_to_8h():
    assert hourly_rate(0.008, None) == pytest.approx(0.001)
    assert hourly_rate(0.008, 0) == pytest.approx(0.001)


def test_infer_interval_from_real_timestamps():
    assert infer_interval_hours([t for t, _ in series(0.001, 6, 8)]) == pytest.approx(8.0)
    assert infer_interval_hours([t for t, _ in series(0.001, 6, 4)]) == pytest.approx(4.0)
    assert infer_interval_hours([0.0, H]) is None       # мало точек


def test_parse_history_row():
    assert parse_history_row({"timestamp": 1, "fundingRate": 0.01}) == (1.0, 0.01)
    assert parse_history_row({"timestamp": None, "fundingRate": 0.01}) is None
    assert parse_history_row({"timestamp": 1}) is None


# ---- экономика связки ----
def test_pair_opportunity_breakeven():
    # разница 0.0001/ч, комиссии round-trip 0.002 -> окупаемость 20 часов
    opp = pair_opportunity(hourly_high=0.0003, hourly_low=0.0002, round_trip_fee=0.002)
    assert opp["hours_to_breakeven"] == pytest.approx(20.0)
    assert opp["daily_pct"] == pytest.approx(0.24)


def test_pair_opportunity_negative_has_no_breakeven():
    opp = pair_opportunity(hourly_high=0.0001, hourly_low=0.0003, round_trip_fee=0.002)
    assert opp["hours_to_breakeven"] is None
    assert opp["income_per_hour"] < 0


# ---- устойчивость знака ----
def test_sign_stability_detects_flipping():
    stable = sign_stability([0.001] * 10, [0.0] * 10)
    assert stable == pytest.approx(1.0)
    # знак скачет каждый период -> преимущества нет
    flip = sign_stability([0.001, -0.001] * 5, [0.0] * 10)
    assert flip == pytest.approx(0.5)


def test_average_hourly_uses_real_interval():
    # 0.008 каждые 8ч -> 0.001/ч
    avg, interval, n = average_hourly(series(0.008, 10, 8))
    assert avg == pytest.approx(0.001)
    assert interval == pytest.approx(8.0)
    assert n == 10


def test_average_hourly_needs_data():
    assert average_hourly(series(0.001, 2)) is None


# ---- отчёт ----
def test_report_finds_opportunity():
    data = {"BTC/USDT": {"binance": series(0.0080, 20, 8),   # 0.0010/ч
                         "bybit": series(0.0016, 20, 8)}}    # 0.0002/ч
    text = build_report(data, {"binance": 0.0005, "bybit": 0.0005})
    assert "FUNDING-АРБИТРАЖ" in text
    assert "binance → bybit" in text          # шортим там, где ставка выше
    assert "окупаемость до суток" in text or "Связок с окупаемостью" in text


def test_report_warns_when_fees_not_covered():
    """Дифференциал крошечный -> комиссии не отбиваются, отчёт обязан сказать прямо."""
    data = {"X/USDT": {"a": series(0.000010, 20, 8), "b": series(0.000001, 20, 8)}}
    text = build_report(data, {"a": 0.0005, "b": 0.0005})
    assert "НЕТ" in text
    assert "нежизнеспособен" in text


def test_report_normalizes_different_intervals():
    """Биржа с 1-часовым периодом не должна выглядеть хуже из-за мелкой ставки."""
    data = {"X/USDT": {"fast": series(0.001, 30, 1),    # 0.001/ч
                       "slow": series(0.0008, 30, 8)}}  # 0.0001/ч
    text = build_report(data, {"fast": 0.0005, "slow": 0.0005})
    assert "fast → slow" in text               # fast выгоднее, а не наоборот


def test_report_empty():
    assert "не найдено" in build_report({}, {})


def test_report_skips_single_exchange_pairs():
    data = {"X/USDT": {"only": series(0.01, 20, 8)}}
    assert "не найдено" in build_report(data, {"only": 0.0005})


# ---- сбор данных ----
class _Conn:
    def __init__(self, name, rows, has=True):
        self.name = name
        self.contracts = {"BTC/USDT": type("M", (), {"raw_symbol": "BTC/USDT:USDT"})()}
        self.client = type("C", (), {
            "has": {"fetchFundingRateHistory": has},
            "fetch_funding_rate_history": staticmethod(
                lambda s, since=None, limit=None: _ret(rows)),
        })()


async def _ret(rows):
    return rows


async def test_fetch_history_parses_and_sorts():
    rows = [{"timestamp": 2 * H, "fundingRate": 0.002},
            {"timestamp": 1 * H, "fundingRate": 0.001}]
    out = await fetch_history(_Conn("a", rows), "BTC/USDT", days=7)
    assert out == [(H, 0.001), (2 * H, 0.002)]      # отсортировано по времени


async def test_fetch_history_skips_unsupported_exchange():
    assert await fetch_history(_Conn("a", [], has=False), "BTC/USDT") == []


async def test_collect_builds_per_symbol_map():
    rows = [{"timestamp": i * H, "fundingRate": 0.001} for i in range(5)]
    conns = {"a": _Conn("a", rows), "b": _Conn("b", rows)}
    data = await collect(conns, ["BTC/USDT"], days=7)
    assert set(data["BTC/USDT"]) == {"a", "b"}
