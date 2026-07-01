"""Тесты Этапа 3 (§5): котировки и funding."""

from arb.exchanges import ExchangeConnector
from arb.marketdata import (
    MarketData,
    parse_funding,
    parse_interval_hours,
    parse_order_book,
    parse_ticker,
)
from tests.fixtures import MockMarketClient


def test_parse_ticker():
    t = {"bid": 100.0, "ask": 100.5, "bidVolume": 3, "askVolume": 2, "timestamp": 111}
    q = parse_ticker("binance", "BTC/USDT", t)
    assert q.bid == 100.0 and q.ask == 100.5
    assert q.bid_volume == 3 and q.ask_volume == 2
    assert q.timestamp == 111


def test_parse_ticker_missing_returns_none():
    assert parse_ticker("binance", "BTC/USDT", {"bid": None, "ask": 1}) is None


def test_parse_order_book():
    ob = {"bids": [[100.0, 5.0]], "asks": [[100.4, 4.0]], "timestamp": 222}
    q = parse_order_book("bybit", "ETH/USDT", ob)
    assert q.bid == 100.0 and q.ask == 100.4
    assert q.bid_volume == 5.0 and q.ask_volume == 4.0


def test_parse_order_book_empty():
    assert parse_order_book("bybit", "ETH/USDT", {"bids": [], "asks": []}) is None


def test_parse_interval_from_string():
    assert parse_interval_hours({"interval": "8h"}) == 8.0
    assert parse_interval_hours({"interval": "4h"}) == 4.0


def test_parse_interval_from_timestamps():
    raw = {"fundingTimestamp": 0, "nextFundingTimestamp": 4 * 3_600_000}
    assert parse_interval_hours(raw) == 4.0


def test_parse_funding():
    raw = {"fundingRate": 0.0001, "nextFundingTimestamp": 123, "interval": "8h"}
    f = parse_funding("okx", "BTC/USDT", raw)
    assert f.funding_rate == 0.0001
    assert f.next_funding_time == 123
    assert f.interval_hours == 8.0


def test_parse_funding_missing_none():
    assert parse_funding("okx", "BTC/USDT", {"fundingRate": None}) is None


def _connector(client):
    conn = ExchangeConnector("binance", client)
    return conn


async def test_marketdata_update_quote_ws():
    client = MockMarketClient(order_books={"BTC/USDT": {
        "bids": [[100.0, 5]], "asks": [[100.5, 4]], "timestamp": 1}})
    md = MarketData({"binance": _connector(client)})
    q = await md.update_quote("binance", "BTC/USDT")
    assert q.bid == 100.0
    assert md.get_quote("binance", "BTC/USDT") is q


async def test_marketdata_update_quote_rest_fallback():
    client = MockMarketClient(tickers={"BTC/USDT": {
        "bid": 100.0, "ask": 100.5, "timestamp": 1}}, with_ws=False)
    md = MarketData({"binance": _connector(client)})
    q = await md.update_quote("binance", "BTC/USDT", use_ws=False)
    assert q.ask == 100.5


async def test_marketdata_update_funding():
    client = MockMarketClient(funding={"BTC/USDT": {
        "fundingRate": 0.0002, "interval": "8h", "nextFundingTimestamp": 999}})
    md = MarketData({"binance": _connector(client)})
    f = await md.update_funding("binance", "BTC/USDT")
    assert f.funding_rate == 0.0002
    assert md.get_funding("binance", "BTC/USDT").interval_hours == 8.0


def test_quote_age():
    md = MarketData({})
    from arb.models import Quote
    md.quotes[("binance", "BTC/USDT")] = Quote("binance", "BTC/USDT", 1, 2, timestamp=1000)
    assert md.quote_age_ms("binance", "BTC/USDT", now_ms=1500) == 500
