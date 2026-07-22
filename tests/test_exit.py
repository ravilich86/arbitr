"""Тесты Этапа 6 (§7): выход, P&L, условия закрытия."""

import pytest

from arb.executor import (
    Executor,
    compute_pnl,
    current_spread,
    estimate_open_pnl,
    should_exit,
)
from arb.exchanges import ExchangeConnector
from arb.models import ArbSignal, ContractMeta, Leg, Position, PositionStatus, Quote, Side
from tests.fixtures import MockTradeClient


def q(ex, bid, ask):
    return Quote(ex, "BTC/USDT", bid, ask, timestamp=0)


def test_current_spread():
    assert current_spread(q("h", 101.0, 101.1), q("l", 99.9, 100.0)) == pytest.approx(0.01)


def test_should_exit_target():
    ok, reason = should_exit(cur_spread=0.0, hold_time=10, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.02)
    assert ok and reason == "target"


def test_should_exit_adverse():
    ok, reason = should_exit(cur_spread=0.03, hold_time=10, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.02)
    assert ok and reason == "max_adverse"


def test_should_exit_max_hold():
    ok, reason = should_exit(cur_spread=0.005, hold_time=4000, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.02)
    assert ok and reason == "max_hold_time"


def test_should_exit_take_profit():
    ok, reason = should_exit(cur_spread=0.005, hold_time=10, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.02,
                             est_pnl_pct=0.005, take_profit_pct=0.003)
    assert ok and reason == "take_profit"


def test_stop_loss_not_triggered_right_after_entry():
    # Позиция сразу после входа уже в минусе на издержки (-0.8%). Стоп 0.5% НЕ
    # должен срабатывать мгновенно — считаем просадку от точки входа.
    ok, reason = should_exit(cur_spread=0.012, hold_time=2, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.02,
                             est_pnl_pct=-0.008, stop_loss_pct=0.005,
                             entry_pnl_pct=-0.008)
    assert not ok


def test_stop_loss_triggers_on_drawdown_from_entry():
    # Просело ещё на 0.6% от точки входа (-0.008 -> -0.014) при стопе 0.5%
    ok, reason = should_exit(cur_spread=0.02, hold_time=30, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.05,
                             est_pnl_pct=-0.014, stop_loss_pct=0.005,
                             entry_pnl_pct=-0.008)
    assert ok and reason == "stop_loss"


def test_should_exit_stop_loss():
    ok, reason = should_exit(cur_spread=0.008, hold_time=10, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.02,
                             est_pnl_pct=-0.012, stop_loss_pct=0.01)
    assert ok and reason == "stop_loss"


def test_target_not_closed_at_loss():
    # спред сошёлся (cur<=exit), но позиция в минусе -> НЕ закрываем по target
    ok, reason = should_exit(cur_spread=0.0, hold_time=10, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.02,
                             est_pnl_pct=-0.005, stop_loss_pct=0.01)
    assert not ok  # держим, пока не стоп/время/adverse


def test_target_closed_when_not_loss():
    ok, reason = should_exit(cur_spread=0.0, hold_time=10, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.02,
                             est_pnl_pct=0.002)
    assert ok and reason == "target"


def test_should_not_exit():
    ok, reason = should_exit(cur_spread=0.005, hold_time=10, exit_spread=0.0,
                             max_hold_time=3600, max_adverse_spread=0.02)
    assert not ok and reason is None


def test_compute_pnl_convergence_profit():
    # вошли: шорт 101, лонг 100; вышли при схождении: шорт откуп 100.5, лонг прод 100.5
    pnl = compute_pnl(short_entry=101.0, long_entry=100.0, amount=10,
                      short_close=100.5, long_close=100.5)
    # short: (101-100.5)*10=5 ; long: (100.5-100)*10=5 -> 10
    assert pnl == pytest.approx(10.0)


def test_compute_pnl_with_fees_and_funding():
    pnl = compute_pnl(101.0, 100.0, 10, 100.5, 100.5,
                      entry_fees=2.0, close_fees=2.0, funding_accrued=1.0)
    assert pnl == pytest.approx(10.0 - 2.0 - 2.0 + 1.0)


def meta(ex):
    return ContractMeta(ex, "BTC/USDT", "BTC/USDT:USDT", "BTC", "USDT",
                        step_size=0.001, min_amount=0.001, min_notional=5.0, max_leverage=100)


def _connectors(bh="fill", bl="fill"):
    ch = ExchangeConnector("h", MockTradeClient(bh))
    cl = ExchangeConnector("l", MockTradeClient(bl))
    ch.contracts = {"BTC/USDT": meta("h")}
    cl.contracts = {"BTC/USDT": meta("l")}
    return {"h": ch, "l": cl}


async def test_close_position_dry_run_pnl():
    ex = Executor(_connectors(), fees={"h": 0.0005, "l": 0.0005}, dry_run=True)
    sig = ArbSignal("BTC/USDT", "h", "l", bid_high=101.0, ask_low=100.0,
                    raw_spread=0.01, net_spread=0.005, notional=2000.0)
    pos = await ex.open_position(sig)
    assert pos.status == PositionStatus.OPEN
    # закрытие при схождении: H ask=100.4, L bid=100.5
    pos = await ex.close_position(pos, q("h", 100.4, 100.4), q("l", 100.5, 100.6), "target")
    assert pos.status == PositionStatus.CLOSED
    assert pos.close_reason == "target"
    assert pos.realized_pnl is not None
    assert pos.realized_pnl > 0  # спред сошёлся в плюс


async def test_estimate_open_pnl():
    ex = Executor(_connectors(), fees={"h": 0.0005, "l": 0.0005}, dry_run=True)
    sig = ArbSignal("BTC/USDT", "h", "l", bid_high=101.0, ask_low=100.0,
                    raw_spread=0.01, net_spread=0.005, notional=2000.0)
    pos = await ex.open_position(sig)
    est = estimate_open_pnl(pos, q("h", 100.4, 100.4), q("l", 100.5, 100.6))
    assert est > 0


async def test_estimate_open_pnl_slippage_is_conservative():
    ex = Executor(_connectors(), fees={"h": 0.0005, "l": 0.0005}, dry_run=True)
    sig = ArbSignal("BTC/USDT", "h", "l", bid_high=101.0, ask_low=100.0,
                    raw_spread=0.01, net_spread=0.005, notional=2000.0)
    pos = await ex.open_position(sig)
    qh, ql = q("h", 100.4, 100.4), q("l", 100.5, 100.6)
    est0 = estimate_open_pnl(pos, qh, ql, slippage_pct=0.0)
    est_slip = estimate_open_pnl(pos, qh, ql, slippage_pct=0.002)
    assert est_slip < est0  # с учётом слиппеджа оценка прибыли ниже (реалистичнее)


def test_estimate_open_pnl_uses_signal_price_when_average_missing():
    # Если биржа не вернула average по одной ноге, нельзя считать её вход по нулю:
    # это давало ложный take_profit сразу после входа и закрытие в минус.
    amount = 100_000.0
    sig = ArbSignal(
        "HMSTR/USDT", "gate", "binance",
        bid_high=0.000177, ask_low=0.0001744,
        raw_spread=0.0149, net_spread=0.0109, notional=20.0,
    )
    pos = Position(
        "p", "HMSTR/USDT", "gate", "binance",
        Leg("gate", "HMSTR/USDT", Side.SHORT, amount,
            filled_amount=amount, avg_price=0.000177, fee_paid=0.01),
        Leg("binance", "HMSTR/USDT", Side.LONG, amount,
            filled_amount=amount, avg_price=None, fee_paid=0.01),
        signal=sig,
    )
    est = estimate_open_pnl(
        pos,
        Quote("gate", "HMSTR/USDT", bid=0.000177, ask=0.0001772),
        Quote("binance", "HMSTR/USDT", bid=0.0001743, ask=0.0001744),
        fee_rate_high=0.0005,
        fee_rate_low=0.0005,
    )
    assert est < 0


async def test_close_position_leg_fail_unhedged():
    conns = _connectors("fill", "fill")
    ex = Executor(conns, fees={"h": 0.0005, "l": 0.0005}, dry_run=False)
    sig = ArbSignal("BTC/USDT", "h", "l", 101.0, 100.0, 0.01, 0.005, notional=2000.0)
    pos = await ex.open_position(sig)
    # при закрытии H нога отклоняется
    conns["h"].client._behavior = "reject"
    pos = await ex.close_position(pos, q("h", 100.4, 100.4), q("l", 100.5, 100.6), "target")
    assert pos.status == PositionStatus.UNHEDGED
