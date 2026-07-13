"""Тесты реконсиляции и аварийного закрытия позиций."""

import pytest

from arb.exchanges import ExchangeConnector
from arb.reconcile import (
    PosView,
    close_all,
    close_positions,
    normalize_position,
    pair_positions,
    reconcile,
)


def _pos(exchange, symbol, side, size, raw=None):
    return PosView(exchange, symbol, raw or f"{symbol}:USDT", side, size)


def test_normalize_position():
    p = {"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 10,
         "contractSize": 1, "entryPrice": 100.0}
    v = normalize_position("binance", p)
    assert v.exchange == "binance"
    assert v.symbol == "BTC/USDT"
    assert v.side == "short"
    assert v.size == 10
    assert v.close_side() == "buy"


def test_normalize_position_empty():
    assert normalize_position("binance", {"symbol": "BTC/USDT:USDT", "contracts": 0}) is None


def test_pair_positions_matched():
    views = [
        _pos("binance", "BTC/USDT", "short", 10),
        _pos("gate", "BTC/USDT", "long", 10),
    ]
    pairs, orphans = pair_positions(views)
    assert len(pairs) == 1
    assert orphans == []


def test_pair_positions_orphan():
    # шорт без противоположного лонга -> орфан
    views = [
        _pos("binance", "BTC/USDT", "short", 10),
        _pos("gate", "ETH/USDT", "long", 5),
    ]
    pairs, orphans = pair_positions(views)
    assert pairs == []
    assert len(orphans) == 2


def test_pair_positions_size_mismatch_is_orphan():
    # объёмы сильно расходятся -> не пара
    views = [
        _pos("binance", "BTC/USDT", "short", 10),
        _pos("gate", "BTC/USDT", "long", 3),
    ]
    pairs, orphans = pair_positions(views, size_tol=0.05)
    assert pairs == []
    assert len(orphans) == 2


def test_pair_positions_same_exchange_not_paired():
    # шорт и лонг на ОДНОЙ бирже — не хедж
    views = [
        _pos("binance", "BTC/USDT", "short", 10),
        _pos("binance", "BTC/USDT", "long", 10),
    ]
    pairs, orphans = pair_positions(views)
    assert pairs == []
    assert len(orphans) == 2


class MockPosClient:
    def __init__(self, positions):
        self._positions = positions
        self.orders = []

    async def fetch_positions(self):
        return self._positions

    async def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.orders.append({"symbol": symbol, "side": side, "amount": amount,
                            "params": params})
        return {"id": "o", "status": "closed"}


def _conns(pos_binance, pos_gate):
    return {
        "binance": ExchangeConnector("binance", MockPosClient(pos_binance)),
        "gate": ExchangeConnector("gate", MockPosClient(pos_gate)),
    }


async def test_close_all_executes_orders():
    conns = _conns(
        [{"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 10, "contractSize": 1}],
        [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 10, "contractSize": 1}],
    )
    res = await close_all(conns, execute=True)
    assert res["total"] == 2
    # обе ноги закрыты reduce-only противоположной стороной
    assert conns["binance"].client.orders[0]["side"] == "buy"
    assert conns["gate"].client.orders[0]["side"] == "sell"
    assert conns["binance"].client.orders[0]["params"]["reduceOnly"] is True


async def test_close_all_report_only():
    conns = _conns(
        [{"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 10, "contractSize": 1}],
        [],
    )
    res = await close_all(conns, execute=False)
    assert res["total"] == 1
    assert conns["binance"].client.orders == []  # ордера не отправлялись


async def test_reconcile_closes_only_orphans():
    # binance: захеджированный BTC-шорт + орфан ETH-шорт; gate: только BTC-лонг
    conns = _conns(
        [
            {"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 10, "contractSize": 1},
            {"symbol": "ETH/USDT:USDT", "side": "short", "contracts": 5, "contractSize": 1},
        ],
        [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 10, "contractSize": 1}],
    )
    res = await reconcile(conns, execute=True)
    assert len(res["pairs"]) == 1        # BTC спарен
    assert len(res["orphans"]) == 1      # ETH-шорт орфан
    # закрыт только орфан (ETH), BTC-пара не тронута
    assert len(conns["binance"].client.orders) == 1
    assert "ETH" in conns["binance"].client.orders[0]["symbol"]
    assert conns["gate"].client.orders == []
