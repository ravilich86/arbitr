"""Тесты Этапа 5 (§7, §8): объём, парсинг ордеров, вход и leg-risk."""

import pytest

from arb.exchanges import ExchangeConnector
from arb.executor import Executor, compute_base_amount, parse_order
from arb.models import ArbSignal, ContractMeta, LegStatus, PositionStatus
from tests.fixtures import MockTradeClient


def meta(exchange, step=0.001, min_amount=0.001, min_notional=5.0):
    return ContractMeta(exchange, "BTC/USDT", "BTC/USDT:USDT", "BTC", "USDT",
                        step_size=step, min_amount=min_amount,
                        min_notional=min_notional, max_leverage=100)


# ---- объём (§8) ----
def test_compute_base_amount_aligns_step():
    m = meta("binance", step=0.001)
    amt = compute_base_amount(price=100.0, notional=2000.0, meta_high=m, meta_low=m)
    assert amt == pytest.approx(20.0)


def test_compute_base_amount_respects_coarser_step():
    m_fine = meta("a", step=0.001)
    m_coarse = meta("b", step=1.0)
    amt = compute_base_amount(price=100.0, notional=2050.0, meta_high=m_fine, meta_low=m_coarse)
    assert amt == pytest.approx(20.0)  # округление под шаг 1.0


def test_compute_base_amount_zero_when_below_min():
    m = meta("a", step=0.001, min_amount=1000.0)
    assert compute_base_amount(100.0, 200.0, m, m) == 0.0


# ---- парсинг ордера ----
def test_parse_order_filled():
    p = parse_order({"id": "1", "status": "closed", "filled": 5, "amount": 5,
                     "average": 100, "fee": {"cost": 0.25}})
    assert p["status"] == LegStatus.FILLED
    assert p["avg_price"] == 100
    assert p["fee"] == 0.25


def test_parse_order_rejected():
    p = parse_order({"id": "1", "status": "canceled", "filled": 0, "amount": 5})
    assert p["status"] == LegStatus.FAILED


def test_parse_order_partial():
    p = parse_order({"id": "1", "status": "open", "filled": 2, "amount": 5, "average": 100})
    assert p["status"] == LegStatus.PARTIAL


# ---- Executor ----
def _connectors(behavior_h="fill", behavior_l="fill"):
    ch = ExchangeConnector("h", MockTradeClient(behavior_h))
    cl = ExchangeConnector("l", MockTradeClient(behavior_l))
    ch.contracts = {"BTC/USDT": meta("h")}
    cl.contracts = {"BTC/USDT": meta("l")}
    return {"h": ch, "l": cl}


def _signal():
    return ArbSignal("BTC/USDT", "h", "l", bid_high=101.0, ask_low=100.0,
                     raw_spread=0.01, net_spread=0.005, notional=2000.0)


async def test_open_position_dry_run():
    ex = Executor(_connectors(), fees={"h": 0.0005, "l": 0.0005}, dry_run=True)
    pos = await ex.open_position(_signal())
    assert pos.status == PositionStatus.OPEN
    assert pos.both_filled
    assert pos.short_leg.filled_amount == pytest.approx(20.0)
    assert pos.short_leg.avg_price == 101.0
    assert pos.long_leg.avg_price == 100.0


async def test_open_position_live_both_fill():
    conns = _connectors("fill", "fill")
    ex = Executor(conns, fees={"h": 0.0005, "l": 0.0005}, dry_run=False)
    pos = await ex.open_position(_signal())
    assert pos.status == PositionStatus.OPEN
    assert conns["h"].client.leverage_calls  # плечо выставлялось
    assert conns["h"].client.margin_calls


async def test_open_position_leg_fail_rollback():
    # short исполнился, long отклонён -> rollback закрывает short
    conns = _connectors("fill", "reject")
    ex = Executor(conns, fees={"h": 0.0005, "l": 0.0005}, dry_run=False,
                  on_leg_failure="rollback")
    pos = await ex.open_position(_signal())
    assert pos.status == PositionStatus.FAILED
    assert pos.short_leg.status == LegStatus.CLOSED  # откат исполнен


async def test_open_position_leg_fail_retry_success():
    # long сначала reject, при retry -> fill
    conns = _connectors("fill", ["reject", "fill"])
    ex = Executor(conns, fees={"h": 0.0005, "l": 0.0005}, dry_run=False,
                  on_leg_failure="retry")
    pos = await ex.open_position(_signal())
    assert pos.status == PositionStatus.OPEN
    assert pos.long_leg.is_filled


async def test_open_position_both_fail():
    conns = _connectors("reject", "reject")
    ex = Executor(conns, fees={"h": 0.0005, "l": 0.0005}, dry_run=False)
    pos = await ex.open_position(_signal())
    assert pos.status == PositionStatus.FAILED
    assert "обе ноги" in pos.close_reason


async def test_open_position_amount_zero():
    conns = _connectors()
    conns["h"].contracts = {"BTC/USDT": meta("h", min_amount=1e9)}
    ex = Executor(conns, fees={"h": 0.0005, "l": 0.0005}, dry_run=True)
    pos = await ex.open_position(_signal())
    assert pos.status == PositionStatus.FAILED
    assert "объём" in pos.close_reason or "минимум" in pos.close_reason.lower()
