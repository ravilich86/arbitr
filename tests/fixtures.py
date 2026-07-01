"""Фикстуры ccxt-подобных market-структур и мок-клиент для тестов.

Форма словарей повторяет то, что возвращает ccxt.load_markets():
{ symbol -> market_dict }.
"""

from __future__ import annotations

from typing import Any


def make_market(
    symbol: str,
    base: str,
    quote: str = "USDT",
    *,
    swap: bool = True,
    linear: bool = True,
    active: bool = True,
    settle: str | None = "USDT",
    tick: float = 0.01,
    step: float = 0.001,
    min_amount: float = 0.001,
    min_cost: float = 5.0,
    max_leverage: float = 20.0,
    contract_size: float = 1.0,
) -> dict[str, Any]:
    """Собрать market-словарь в формате ccxt."""
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "settle": settle,
        "swap": swap,
        "linear": linear,
        "active": active,
        "contract": True,
        "contractSize": contract_size,
        "precision": {"price": tick, "amount": step},
        "limits": {
            "amount": {"min": min_amount},
            "cost": {"min": min_cost},
            "leverage": {"max": max_leverage},
        },
        "info": {},
    }


def binance_markets() -> dict[str, dict]:
    """Набор рынков Binance: 2 перпа + спот + инверсный (должны отсеяться)."""
    return {
        "BTC/USDT:USDT": make_market("BTC/USDT:USDT", "BTC", tick=0.1, step=0.001,
                                     min_amount=0.001, min_cost=5, max_leverage=125),
        "ETH/USDT:USDT": make_market("ETH/USDT:USDT", "ETH", tick=0.01, step=0.001,
                                     min_amount=0.001, min_cost=5, max_leverage=100),
        "BTC/USDT": make_market("BTC/USDT", "BTC", swap=False),  # спот -> отсеять
        "BTC/USD:BTC": make_market("BTC/USD:BTC", "BTC", quote="USD",
                                   settle="BTC", linear=False),  # инверс -> отсеять
        "DOGE/USDT:USDT": make_market("DOGE/USDT:USDT", "DOGE", active=False),  # неактив -> отсеять
    }


def bybit_markets() -> dict[str, dict]:
    """Bybit: BTC, ETH (общие с Binance) + уникальный SOL."""
    return {
        "BTC/USDT:USDT": make_market("BTC/USDT:USDT", "BTC", tick=0.1, step=0.001,
                                     max_leverage=100),
        "ETH/USDT:USDT": make_market("ETH/USDT:USDT", "ETH", max_leverage=50),
        "SOL/USDT:USDT": make_market("SOL/USDT:USDT", "SOL", max_leverage=50),
    }


class MockCCXTClient:
    """Минимальный мок ccxt-клиента для ExchangeConnector."""

    def __init__(self, markets: dict[str, dict]):
        self.markets = markets
        self.load_calls = 0
        self.closed = False

    async def load_markets(self, reload: bool = False) -> dict[str, dict]:
        self.load_calls += 1
        return self.markets

    async def close(self) -> None:
        self.closed = True
