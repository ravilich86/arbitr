"""Тесты сверки записей с фактической историей бирж."""

import pytest

from arb.analytics import compare_report
from arb.exchanges import ExchangeConnector
from arb.models import ContractMeta
from arb.storage import TradeDB
from arb.sync import aggregate_trades, parse_order_fill, sync_fills
from tests.test_storage import _closed_position


def test_parse_order_fill():
    o = {"filled": 10, "average": 100.5, "fee": {"cost": 0.05, "currency": "USDT"}}
    res = parse_order_fill(o)
    assert res["filled"] == 10 and res["avg_price"] == 100.5
    assert res["fee"] == 0.05 and res["fee_currency"] == "USDT"


def test_parse_order_fill_avg_from_cost():
    o = {"filled": 4, "cost": 400.0}
    assert parse_order_fill(o)["avg_price"] == 100.0


def test_aggregate_trades_vwap():
    trades = [
        {"amount": 2, "price": 100.0, "fee": {"cost": 0.01, "currency": "USDT"}},
        {"amount": 3, "price": 105.0, "fee": {"cost": 0.02, "currency": "USDT"}},
    ]
    res = aggregate_trades(trades)
    assert res["filled"] == 5
    assert res["avg_price"] == pytest.approx((2 * 100 + 3 * 105) / 5)
    assert res["fee"] == pytest.approx(0.03)


class MockHistoryClient:
    """Мок биржи с историей ордеров/сделок."""

    def __init__(self, order=None, trades=None):
        self._order = order
        self._trades = trades or []

    async def fetch_order(self, order_id, symbol):
        if self._order is None:
            raise RuntimeError("not supported")
        return self._order

    async def fetch_my_trades(self, symbol):
        return self._trades


def _db_with_order(tmp_path, order_id="ord1"):
    db = TradeDB(str(tmp_path / "t.db"))
    pos = _closed_position(pid="p1")
    for leg in pos.orders:
        leg.order_id = order_id
    db.record_position(pos, leverage=20, dry_run=False)
    return db


def _conn(client, cs=1.0):
    conn = ExchangeConnector("gate", client)
    conn.contracts = {"BTC/USDT": ContractMeta(
        "gate", "BTC/USDT", "BTC/USDT:USDT", "BTC", "USDT", contract_size=cs)}
    return conn


async def test_sync_fills_updates_actuals(tmp_path):
    db = _db_with_order(tmp_path)
    client = MockHistoryClient(order={
        "filled": 10, "average": 99.1, "fee": {"cost": 0.07, "currency": "USDT"}})
    res = await sync_fills(db, {"gate": _conn(client), "binance": _conn(client)})
    assert res["synced"] > 0
    synced = [o for o in db.orders() if o.get("actual_avg_price")]
    assert synced and synced[0]["actual_avg_price"] == 99.1
    assert synced[0]["actual_fee"] == 0.07
    db.close()


async def test_sync_fills_converts_contracts_to_base(tmp_path):
    db = _db_with_order(tmp_path)
    client = MockHistoryClient(order={"filled": 2, "average": 100.0})
    # contractSize=10 -> 2 контракта = 20 базовых единиц
    await sync_fills(db, {"gate": _conn(client, cs=10.0),
                          "binance": _conn(client, cs=10.0)})
    synced = [o for o in db.orders() if o.get("actual_filled")]
    assert synced[0]["actual_filled"] == 20.0
    db.close()


async def test_sync_falls_back_to_trades(tmp_path):
    db = _db_with_order(tmp_path, order_id="X1")
    client = MockHistoryClient(order=None, trades=[
        {"order": "X1", "amount": 5, "price": 98.0,
         "fee": {"cost": 0.02, "currency": "USDT"}},
        {"order": "OTHER", "amount": 5, "price": 200.0},
    ])
    await sync_fills(db, {"gate": _conn(client), "binance": _conn(client)})
    synced = [o for o in db.orders() if o.get("actual_avg_price")]
    assert synced[0]["actual_avg_price"] == 98.0  # чужая сделка не учтена
    db.close()


def test_compare_report_no_sync(tmp_path):
    db = TradeDB(str(tmp_path / "t.db"))
    db.record_position(_closed_position(), 20, False)
    assert "нет синхронизированных" in compare_report(db.orders())
    db.close()


async def test_compare_report_after_sync(tmp_path):
    db = _db_with_order(tmp_path)
    client = MockHistoryClient(order={"filled": 10, "average": 99.1})
    await sync_fills(db, {"gate": _conn(client), "binance": _conn(client)})
    text = compare_report(db.orders())
    assert "СВЕРКА С ИСТОРИЕЙ БИРЖ" in text
    assert "Расхождение ЦЕНЫ" in text
    db.close()
