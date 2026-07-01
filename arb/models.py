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
