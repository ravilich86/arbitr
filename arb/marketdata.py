"""Этап 3 (§5): поток котировок (лучший bid/ask) и funding rate.

Котировки берём через WebSocket (ccxt.pro watch_order_book / watch_ticker) с
fallback на REST (fetch_ticker), funding — через fetch_funding_rate. Данные
кэшируются per (биржа, символ) и используются сканером (§6).

Парсеры (ticker/funding -> модели) вынесены в чистые функции для тестов.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from .exchanges import ExchangeConnector
from .models import FundingInfo, Quote

logger = logging.getLogger(__name__)

_INTERVAL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*h", re.IGNORECASE)


# --------------------------------------------------------------------------
#  Парсеры
# --------------------------------------------------------------------------
def parse_ticker(exchange: str, symbol: str, ticker: dict) -> Optional[Quote]:
    """ccxt ticker -> Quote. None, если нет bid/ask."""
    bid = ticker.get("bid")
    ask = ticker.get("ask")
    if bid is None or ask is None:
        return None
    return Quote(
        exchange=exchange,
        symbol=symbol,
        bid=float(bid),
        ask=float(ask),
        bid_volume=_opt_float(ticker.get("bidVolume")),
        ask_volume=_opt_float(ticker.get("askVolume")),
        timestamp=_opt_float(ticker.get("timestamp")),
    )


def parse_order_book(exchange: str, symbol: str, ob: dict) -> Optional[Quote]:
    """ccxt order book -> Quote (лучший bid/ask + их объёмы)."""
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    if not bids or not asks:
        return None
    best_bid = bids[0]
    best_ask = asks[0]
    return Quote(
        exchange=exchange,
        symbol=symbol,
        bid=float(best_bid[0]),
        ask=float(best_ask[0]),
        bid_volume=_opt_float(best_bid[1]) if len(best_bid) > 1 else None,
        ask_volume=_opt_float(best_ask[1]) if len(best_ask) > 1 else None,
        timestamp=_opt_float(ob.get("timestamp")),
    )


def parse_interval_hours(raw: dict) -> Optional[float]:
    """Определить период начисления funding в часах (не хардкодить 8ч, §5).

    Пытаемся: 1) поле 'interval' вида '8h'/'4h';
              2) разница nextFundingTimestamp - fundingTimestamp (мс -> ч).
    """
    interval = raw.get("interval")
    if isinstance(interval, str):
        m = _INTERVAL_RE.search(interval)
        if m:
            return float(m.group(1))
    if isinstance(interval, (int, float)) and interval > 0:
        return float(interval)

    cur = raw.get("fundingTimestamp")
    if cur is None:
        cur = raw.get("timestamp")
    nxt = raw.get("nextFundingTimestamp")
    if cur is not None and nxt is not None and nxt > cur:
        return (nxt - cur) / 3_600_000.0  # мс -> часы
    return None


def parse_funding(exchange: str, symbol: str, raw: dict) -> Optional[FundingInfo]:
    """ccxt funding rate -> FundingInfo. None, если нет ставки."""
    rate = raw.get("fundingRate")
    if rate is None:
        return None
    return FundingInfo(
        exchange=exchange,
        symbol=symbol,
        funding_rate=float(rate),
        next_funding_time=_opt_float(raw.get("nextFundingTimestamp")),
        interval_hours=parse_interval_hours(raw),
    )


def _opt_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
#  Кэш маркет-данных
# --------------------------------------------------------------------------
class MarketData:
    """Кэш котировок и funding по (биржа, символ).

    Работает с ExchangeConnector-ами; методы update_* дергают биржу, get_* —
    читают кэш. Для тестов клиенты — моки с fetch_ticker/fetch_funding_rate.
    """

    def __init__(self, connectors: dict[str, ExchangeConnector]):
        self.connectors = connectors
        self.quotes: dict[tuple[str, str], Quote] = {}
        self.funding: dict[tuple[str, str], FundingInfo] = {}
        # OHLCV-кэш: (биржа, символ, timeframe) -> (список свечей, время загрузки, unix c)
        self.ohlcv: dict[tuple[str, str, str], tuple[list, float]] = {}

    # ---- котировки ----
    async def update_quote(self, exchange: str, symbol: str, use_ws: bool = True) -> Optional[Quote]:
        """Обновить котировку. WS (watch_*) если доступно, иначе REST fetch_ticker."""
        client = self.connectors[exchange].client
        raw_symbol = self._raw_symbol(exchange, symbol)
        quote: Optional[Quote] = None

        if use_ws and hasattr(client, "watch_order_book"):
            ob = await client.watch_order_book(raw_symbol)
            quote = parse_order_book(exchange, symbol, ob)
        elif hasattr(client, "fetch_ticker"):
            ticker = await client.fetch_ticker(raw_symbol)
            quote = parse_ticker(exchange, symbol, ticker)

        if quote is not None:
            self.quotes[(exchange, symbol)] = quote
        return quote

    def get_quote(self, exchange: str, symbol: str) -> Optional[Quote]:
        return self.quotes.get((exchange, symbol))

    # ---- funding ----
    async def update_funding(self, exchange: str, symbol: str) -> Optional[FundingInfo]:
        client = self.connectors[exchange].client
        raw_symbol = self._raw_symbol(exchange, symbol)
        if not hasattr(client, "fetch_funding_rate"):
            return None
        raw = await client.fetch_funding_rate(raw_symbol)
        info = parse_funding(exchange, symbol, raw)
        if info is not None:
            self.funding[(exchange, symbol)] = info
        return info

    def get_funding(self, exchange: str, symbol: str) -> Optional[FundingInfo]:
        return self.funding.get((exchange, symbol))

    # ---- дневные свечи (для исторической сверки тождественности) ----
    async def update_ohlcv(
        self, exchange: str, symbol: str, timeframe: str = "1d", limit: int = 10,
        ttl: float = 3600.0, now: Optional[float] = None,
    ) -> Optional[list]:
        """Загрузить OHLCV с кэшем по TTL (дневные свечи меняются редко).

        Возвращает список свечей [[ts, open, high, low, close, vol], ...].
        """
        key = (exchange, symbol, timeframe)
        cur = now if now is not None else time.time()
        cached = self.ohlcv.get(key)
        if cached is not None and (cur - cached[1]) < ttl:
            return cached[0]

        client = self.connectors[exchange].client
        if not hasattr(client, "fetch_ohlcv"):
            return None
        raw_symbol = self._raw_symbol(exchange, symbol)
        data = await client.fetch_ohlcv(raw_symbol, timeframe, limit=limit)
        if data:
            self.ohlcv[key] = (data, cur)
        return data

    def get_ohlcv(self, exchange: str, symbol: str, timeframe: str = "1d") -> Optional[list]:
        cached = self.ohlcv.get((exchange, symbol, timeframe))
        return cached[0] if cached else None

    # ---- служебное ----
    def _raw_symbol(self, exchange: str, symbol: str) -> str:
        """Нормализованный символ -> точный биржевой (из метаданных контракта)."""
        conn = self.connectors.get(exchange)
        if conn and symbol in conn.contracts:
            return conn.contracts[symbol].raw_symbol
        return symbol

    def quote_age_ms(self, exchange: str, symbol: str, now_ms: Optional[float] = None) -> Optional[float]:
        """Возраст котировки в мс (для отсева устаревших данных)."""
        q = self.get_quote(exchange, symbol)
        if q is None or q.timestamp is None:
            return None
        now = now_ms if now_ms is not None else time.time() * 1000
        return now - q.timestamp
