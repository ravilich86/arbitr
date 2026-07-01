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


class MockTradeClient:
    """Мок торгового клиента ccxt для тестов Executor.

    behavior управляет поведением create_order:
      "fill"    -> полностью исполнен по запрошенной цене;
      "reject"  -> отменён, filled=0;
      "fail"    -> бросает исключение;
      "partial" -> исполнена половина.
    behavior можно задать списком (по вызовам) или строкой (на все вызовы).
    """

    def __init__(self, behavior="fill", fee_rate=0.0005):
        self._behavior = behavior
        self.fee_rate = fee_rate
        self.orders: list[dict] = []
        self.leverage_calls: list = []
        self.margin_calls: list = []

    def _next_behavior(self) -> str:
        if isinstance(self._behavior, list):
            return self._behavior[min(len(self.orders), len(self._behavior) - 1)]
        return self._behavior

    async def create_order(self, symbol, type_, side, amount, price=None, params=None):
        beh = self._next_behavior()
        self.orders.append({"symbol": symbol, "type": type_, "side": side,
                            "amount": amount, "price": price, "params": params or {}})
        if beh == "fail":
            raise RuntimeError("exchange error")
        fill_price = price or 100.0
        if beh == "reject":
            return {"id": "o", "status": "canceled", "filled": 0.0,
                    "amount": amount, "average": None}
        filled = amount if beh == "fill" else amount / 2
        status = "closed" if beh == "fill" else "open"
        return {
            "id": f"ord{len(self.orders)}", "status": status,
            "filled": filled, "amount": amount, "average": fill_price,
            "fee": {"cost": filled * fill_price * self.fee_rate},
        }

    async def set_leverage(self, leverage, symbol=None):
        self.leverage_calls.append((leverage, symbol))

    async def set_margin_mode(self, mode, symbol=None):
        self.margin_calls.append((mode, symbol))


class MockMarketClient:
    """Мок клиента с котировками и funding для тестов marketdata.

    tickers/order_books/funding: {raw_symbol -> payload}.
    """

    def __init__(self, tickers=None, order_books=None, funding=None, with_ws=True):
        self.tickers = tickers or {}
        self.order_books = order_books or {}
        self.funding = funding or {}
        self._with_ws = with_ws

    async def fetch_ticker(self, symbol: str) -> dict:
        return self.tickers[symbol]

    async def fetch_funding_rate(self, symbol: str) -> dict:
        return self.funding[symbol]

    # watch_order_book появляется только если with_ws=True
    def __getattr__(self, name):
        if name == "watch_order_book" and self.__dict__.get("_with_ws"):
            async def _watch(symbol: str) -> dict:
                return self.order_books[symbol]
            return _watch
        raise AttributeError(name)
