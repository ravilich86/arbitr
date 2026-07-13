"""Тесты Этапа 1 (§3): фильтрация перпов и коннектор."""

import pytest

from arb.exchanges import (
    ExchangeConnector,
    _representative_taker,
    extract_delist_time,
    fetch_taker_fee,
    filter_perp_markets,
    is_usdt_perp,
    market_to_contract,
    normalize_symbol,
)
from tests.fixtures import MockCCXTClient, MockFeeClient, binance_markets, make_market


def test_normalize_symbol():
    assert normalize_symbol("btc") == "BTC/USDT"
    assert normalize_symbol("ETH", "USDT") == "ETH/USDT"


def test_is_usdt_perp_accepts_linear_swap():
    m = make_market("BTC/USDT:USDT", "BTC")
    assert is_usdt_perp(m) is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"swap": False},          # спот
        {"linear": False, "quote": "USD", "settle": "BTC"},  # инверс
        {"quote": "USDC"},        # не USDT
        {"active": False},        # неактивный
    ],
)
def test_is_usdt_perp_rejects(kwargs):
    m = make_market("X/USDT:USDT", "X", **kwargs)
    assert is_usdt_perp(m) is False


def test_market_to_contract_extracts_metadata():
    m = make_market("BTC/USDT:USDT", "BTC", tick=0.1, step=0.001,
                    min_amount=0.002, min_cost=5, max_leverage=125, contract_size=1.0)
    c = market_to_contract("binance", m)
    assert c.exchange == "binance"
    assert c.symbol == "BTC/USDT"
    assert c.raw_symbol == "BTC/USDT:USDT"
    assert c.base == "BTC"
    assert c.tick_size == 0.1
    assert c.step_size == 0.001
    assert c.min_amount == 0.002
    assert c.min_notional == 5
    assert c.max_leverage == 125
    assert c.contract_size == 1.0


def test_filter_perp_markets_only_perps():
    contracts = filter_perp_markets("binance", binance_markets())
    # из 5 рынков перпами являются только BTC и ETH (DOGE неактивен)
    assert set(contracts.keys()) == {"BTC/USDT", "ETH/USDT"}


async def test_connector_load_perp_contracts():
    client = MockCCXTClient(binance_markets())
    conn = ExchangeConnector("binance", client)
    contracts = await conn.load_perp_contracts()
    assert client.load_calls == 1
    assert set(contracts.keys()) == {"BTC/USDT", "ETH/USDT"}
    assert conn.contracts is contracts


async def test_connector_close():
    client = MockCCXTClient({})
    conn = ExchangeConnector("binance", client)
    await conn.close()
    assert client.closed is True


def test_extract_delist_time_from_info():
    m = make_market("FOO/USDT:USDT", "FOO")
    m["info"] = {"deliveryTime": 1_700_000_000_000}
    assert extract_delist_time(m) == 1_700_000_000_000


def test_extract_delist_time_ignores_far_future_sentinel():
    m = make_market("FOO/USDT:USDT", "FOO")
    # Binance-подобный сентинел ~2100 год -> это бессрочный перп, не делистинг
    m["info"] = {"deliveryDate": 4_133_404_800_000}
    assert extract_delist_time(m) is None


def test_extract_delist_time_none_when_absent():
    m = make_market("FOO/USDT:USDT", "FOO")
    assert extract_delist_time(m) is None


def test_representative_taker_picks_common():
    fees = {
        "BTC/USDT:USDT": {"taker": 0.0004, "maker": 0.0002},
        "ETH/USDT:USDT": {"taker": 0.0004, "maker": 0.0002},
        "WEIRD/USDT:USDT": {"taker": 0.001},
    }
    assert _representative_taker(fees) == 0.0004


def test_representative_taker_none_when_empty():
    assert _representative_taker({}) is None


async def test_fetch_taker_fee_from_trading_fees():
    client = MockFeeClient(trading_fees={"BTC/USDT:USDT": {"taker": 0.0003}})
    conn = ExchangeConnector("binance", client)
    assert await fetch_taker_fee(conn) == 0.0003


async def test_fetch_taker_fee_default_fallback():
    # нет fetch_trading_fees -> берём дефолт из метаданных клиента
    client = MockFeeClient(trading_fees=None, default_taker=0.0006)
    conn = ExchangeConnector("bybit", client)
    assert await fetch_taker_fee(conn) == 0.0006
