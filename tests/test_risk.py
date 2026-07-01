"""Тесты модуля риск-контроля (§9)."""

import pytest

from arb.models import ArbSignal, Leg, Position, PositionStatus, Side
from arb.risk import (
    RiskManager,
    approx_liquidation_price,
    liquidation_buffer_ok,
    liquidation_distance,
)


def test_approx_liquidation_price_long_short():
    long_liq = approx_liquidation_price(100.0, leverage=20, side=Side.LONG)
    short_liq = approx_liquidation_price(100.0, leverage=20, side=Side.SHORT)
    assert long_liq < 100.0 < short_liq
    # при 20x ликвидация примерно в ~5% (за вычетом mmr)
    assert long_liq == pytest.approx(100.0 * (1 - 0.05 + 0.005))


def test_liquidation_distance_and_buffer():
    assert liquidation_distance(100.0, 95.0) == pytest.approx(0.05)
    assert liquidation_buffer_ok(0.05, 0.03) is True
    assert liquidation_buffer_ok(0.02, 0.03) is False


def _pos(status=PositionStatus.OPEN, notional=1000.0):
    s = Leg("h", "BTC/USDT", Side.SHORT, 10, filled_amount=10, avg_price=notional / 10)
    l = Leg("l", "BTC/USDT", Side.LONG, 10, filled_amount=10, avg_price=notional / 10)
    return Position("id", "BTC/USDT", "h", "l", s, l, status=status)


def test_kill_switch():
    rm = RiskManager()
    assert rm.killed is False
    rm.trip_kill_switch("test")
    assert rm.killed is True
    d = rm.can_open("BTC/USDT", [], 50, {"h": 100, "l": 100}, ("h", "l"))
    assert d.allowed is False and "kill" in d.reason


def test_cooldown():
    rm = RiskManager(cooldown=300)
    rm.register_close("BTC/USDT", now=1000)
    assert rm.in_cooldown("BTC/USDT", now=1200) is True
    assert rm.in_cooldown("BTC/USDT", now=1400) is False


def test_can_open_concurrent_limit():
    rm = RiskManager(max_concurrent_positions=1)
    d = rm.can_open("ETH/USDT", [_pos()], 50, {"h": 100, "l": 100}, ("h", "l"))
    assert d.allowed is False and "одновременных" in d.reason


def test_can_open_exposure_limit():
    rm = RiskManager(max_concurrent_positions=5, max_position_per_exchange=100)
    existing = _pos(notional=80.0)  # 80 USDT экспозиции на h и l
    d = rm.can_open("ETH/USDT", [existing], margin_required=50,
                    free_margin={"h": 1000, "l": 1000}, exchanges=("h", "l"))
    assert d.allowed is False and "лимита позиции" in d.reason


def test_can_open_insufficient_margin():
    rm = RiskManager(max_concurrent_positions=5, max_position_per_exchange=1000)
    d = rm.can_open("ETH/USDT", [], margin_required=100,
                    free_margin={"h": 50, "l": 1000}, exchanges=("h", "l"))
    assert d.allowed is False and "маржи" in d.reason


def test_can_open_ok():
    rm = RiskManager(max_concurrent_positions=1, max_position_per_exchange=1000)
    d = rm.can_open("BTC/USDT", [], margin_required=100,
                    free_margin={"h": 500, "l": 500}, exchanges=("h", "l"))
    assert d.allowed is True


def test_effective_leverage():
    rm = RiskManager(leverage=20)
    assert rm.effective_leverage(100) == 20
    assert rm.effective_leverage(10) == 10
    assert rm.effective_leverage(None) == 20


def test_check_liquidation_alerts_when_close():
    rm = RiskManager(leverage=20, liquidation_buffer=0.03)
    pos = _pos(notional=1000.0)  # avg_price 100
    # цена H (short) приблизилась к ликвидации (~104.5): текущая 104 -> запас мал
    d = rm.check_liquidation(pos, current_high=104.0, current_low=100.0)
    assert d.allowed is False


def test_check_liquidation_ok_when_far():
    rm = RiskManager(leverage=20, liquidation_buffer=0.03)
    pos = _pos(notional=1000.0)
    d = rm.check_liquidation(pos, current_high=100.5, current_low=99.5)
    assert d.allowed is True
