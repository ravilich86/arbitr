"""Общие датаклассы, разделяемые между модулями.

Держим модели отдельно, чтобы избежать циклических импортов между
exchanges / universe / marketdata / scanner / executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    """Сторона ноги."""

    LONG = "long"
    SHORT = "short"


class LegStatus(str, Enum):
    """Статус исполнения ноги."""

    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    FAILED = "failed"
    CLOSED = "closed"


class PositionStatus(str, Enum):
    """Статус арбитражной позиции."""

    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"          # обе ноги не открылись / откат при входе
    UNHEDGED = "unhedged"      # одна нога висит незахеджированной (leg-risk!)


@dataclass(frozen=True)
class ContractMeta:
    """Метаданные линейного бессрочного контракта (USDT-перп) на одной бирже.

    Заполняется на Этапе 1 (§3) из ccxt market-структуры.
    """

    exchange: str            # имя биржи (binance, bybit, ...)
    symbol: str              # нормализованный символ, напр. "BTC/USDT"
    raw_symbol: str          # точный биржевой символ ccxt, напр. "BTC/USDT:USDT"
    base: str                # базовый актив, напр. "BTC"
    quote: str               # котируемый актив, всегда "USDT"
    tick_size: Optional[float] = None      # шаг цены (price precision -> tick)
    step_size: Optional[float] = None      # шаг количества (amount precision -> lot)
    min_amount: Optional[float] = None     # минимальный объём в базовом активе
    min_notional: Optional[float] = None   # минимальный нотионал в USDT
    max_leverage: Optional[float] = None   # макс. доступное плечо
    contract_size: Optional[float] = None  # размер контракта (для сверки коллизий, §4)
    funding_interval_hours: Optional[float] = None  # период начисления funding (не хардкодить 8ч)
    delist_time: Optional[float] = None    # время делистинга/поставки (unix ms), если анонсировано
    taker_fee_default: Optional[float] = None  # публичная taker-комиссия ФЬЮЧЕРСНОГО рынка (из market['taker'])

    def key(self) -> str:
        """Единый ключ актива для сопоставления между биржами."""
        return self.symbol


@dataclass
class Quote:
    """Лучшие bid/ask по символу на бирже в момент времени (§5)."""

    exchange: str
    symbol: str
    bid: float
    ask: float
    bid_volume: Optional[float] = None
    ask_volume: Optional[float] = None
    timestamp: Optional[float] = None  # unix ms


@dataclass
class FundingInfo:
    """Ставка финансирования и время следующего начисления (§5)."""

    exchange: str
    symbol: str
    funding_rate: float                     # текущая ставка (доля, напр. 0.0001)
    next_funding_time: Optional[float] = None  # unix ms
    interval_hours: Optional[float] = None     # фактический период начисления


@dataclass
class Candidate:
    """Актив-кандидат: присутствует минимум на 2 биржах (§4)."""

    symbol: str                              # нормализованный, напр. "BTC/USDT"
    contracts: dict[str, ContractMeta] = field(default_factory=dict)  # exchange -> meta

    @property
    def exchanges(self) -> list[str]:
        return sorted(self.contracts.keys())


@dataclass
class ArbSignal:
    """Арбитражный сигнал по паре бирж для одного актива (§6).

    Шорт на дорогой бирже H (по bid_high), лонг на дешёвой L (по ask_low).
    """

    symbol: str
    exchange_high: str        # H — дорогая, здесь SHORT
    exchange_low: str         # L — дешёвая, здесь LONG
    bid_high: float           # цена, по которой шортим на H
    ask_low: float            # цена, по которой лонгуем на L
    raw_spread: float         # (bid_H - ask_L) / ask_L
    net_spread: float         # чистый спред после комиссий/funding/слиппеджа
    fee_cost: float = 0.0     # доля: round-trip комиссии обеих ног
    funding_income: float = 0.0   # доля: ожидаемый чистый funding (+ доход / − расход)
    slippage_cost: float = 0.0    # доля: ожидаемый слиппедж
    notional: float = 0.0     # нотионал одной ноги (USDT), под который считали
    timestamp: Optional[float] = None

    def as_row(self) -> dict:
        """Плоское представление для логирования."""
        return {
            "symbol": self.symbol,
            "exchange_high": self.exchange_high,
            "exchange_low": self.exchange_low,
            "bid_high": self.bid_high,
            "ask_low": self.ask_low,
            "raw_spread": self.raw_spread,
            "net_spread": self.net_spread,
            "fee_cost": self.fee_cost,
            "funding_income": self.funding_income,
            "slippage_cost": self.slippage_cost,
            "notional": self.notional,
            "timestamp": self.timestamp,
        }


@dataclass
class Leg:
    """Одна нога арбитражной позиции (§7)."""

    exchange: str
    symbol: str
    side: Side
    amount: float                       # запрошенный объём (база)
    filled_amount: float = 0.0          # исполнено (база)
    avg_price: Optional[float] = None   # средняя цена исполнения
    order_id: Optional[str] = None
    status: LegStatus = LegStatus.PENDING
    fee_paid: float = 0.0               # уплаченная комиссия (USDT)
    error: Optional[str] = None

    @property
    def is_filled(self) -> bool:
        return self.status == LegStatus.FILLED and self.filled_amount > 0

    @property
    def notional(self) -> float:
        if self.avg_price is None:
            return 0.0
        return self.filled_amount * self.avg_price


@dataclass
class Position:
    """Арбитражная позиция из двух ног (§7)."""

    id: str
    symbol: str
    exchange_high: str          # SHORT нога
    exchange_low: str           # LONG нога
    short_leg: Leg
    long_leg: Leg
    signal: Optional[ArbSignal] = None
    status: PositionStatus = PositionStatus.OPENING
    open_time: Optional[float] = None
    close_time: Optional[float] = None
    close_reason: Optional[str] = None
    funding_accrued: float = 0.0        # начисленный funding за удержание (USDT)
    realized_pnl: Optional[float] = None  # итоговый P&L (USDT)

    @property
    def legs(self) -> tuple[Leg, Leg]:
        return (self.short_leg, self.long_leg)

    @property
    def both_filled(self) -> bool:
        return self.short_leg.is_filled and self.long_leg.is_filled
