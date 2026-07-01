"""Модуль управления рисками (§9).

Контролирует то, что напрямую влияет на прибыльность стратегии, а не только
на теорию: плечо и близость ликвидации, лимиты на позиции и экспозицию,
cooldown по паре, достаточность маржи и общий kill-switch.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .models import Position, PositionStatus, Side

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  Ликвидация (§9)
# --------------------------------------------------------------------------
def approx_liquidation_price(
    entry_price: float, leverage: float, side: Side, maintenance_margin_rate: float = 0.005,
) -> float:
    """Приблизительная цена ликвидации для изолированной маржи (§9).

    LONG:  liq ≈ entry * (1 - 1/leverage + mmr)
    SHORT: liq ≈ entry * (1 + 1/leverage - mmr)
    Реальную поддерживающую маржу исполнитель уточняет по бирже.
    """
    if leverage <= 0:
        return 0.0
    im = 1.0 / leverage
    if side == Side.LONG:
        return entry_price * (1 - im + maintenance_margin_rate)
    return entry_price * (1 + im - maintenance_margin_rate)


def liquidation_distance(current_price: float, liq_price: float) -> float:
    """Относительное расстояние от текущей цены до ликвидации (доля)."""
    if current_price <= 0:
        return 0.0
    return abs(current_price - liq_price) / current_price


def liquidation_buffer_ok(distance: float, buffer: float) -> bool:
    """Достаточен ли запас до ликвидации (§9)."""
    return distance >= buffer


# --------------------------------------------------------------------------
#  Менеджер рисков
# --------------------------------------------------------------------------
@dataclass
class RiskDecision:
    allowed: bool
    reason: Optional[str] = None


@dataclass
class RiskManager:
    """Состояние и правила риск-контроля (§9)."""

    max_concurrent_positions: int = 1
    max_position_per_exchange: float = 100.0   # USDT залога на бирже
    liquidation_buffer: float = 0.03
    cooldown: float = 300.0
    leverage: int = 20
    maintenance_margin_rate: float = 0.005

    _killed: bool = field(default=False, init=False)
    _cooldowns: dict[str, float] = field(default_factory=dict, init=False)  # symbol -> ts закрытия

    # ---- kill-switch (§9) ----
    def trip_kill_switch(self, reason: str = "manual") -> None:
        self._killed = True
        logger.warning("KILL-SWITCH активирован: %s", reason)

    def reset_kill_switch(self) -> None:
        self._killed = False

    @property
    def killed(self) -> bool:
        return self._killed

    # ---- cooldown по паре (§9) ----
    def register_close(self, symbol: str, now: Optional[float] = None) -> None:
        self._cooldowns[symbol] = now if now is not None else time.time()

    def in_cooldown(self, symbol: str, now: Optional[float] = None) -> bool:
        ts = self._cooldowns.get(symbol)
        if ts is None:
            return False
        cur = now if now is not None else time.time()
        return (cur - ts) < self.cooldown

    # ---- эффективное плечо (§8, §9) ----
    def effective_leverage(self, max_leverage: Optional[float]) -> int:
        """min(желаемое, доступное на бирже); при необходимости можно снизить."""
        if max_leverage:
            return int(min(self.leverage, max_leverage))
        return self.leverage

    # ---- допуск на открытие (§9) ----
    def can_open(
        self,
        symbol: str,
        open_positions: list[Position],
        margin_required: float,
        free_margin: dict[str, float],
        exchanges: tuple[str, str],
        now: Optional[float] = None,
    ) -> RiskDecision:
        """Проверить все предусловия перед входом (§9)."""
        if self._killed:
            return RiskDecision(False, "kill-switch активен")

        active = [p for p in open_positions
                  if p.status in (PositionStatus.OPEN, PositionStatus.OPENING,
                                  PositionStatus.CLOSING)]
        if len(active) >= self.max_concurrent_positions:
            return RiskDecision(False, "достигнут лимит одновременных позиций")

        if self.in_cooldown(symbol, now):
            return RiskDecision(False, "пара в cooldown")

        # экспозиция и маржа по каждой из двух бирж
        exposure = self._exposure_by_exchange(active)
        for ex in exchanges:
            if exposure.get(ex, 0.0) + margin_required > self.max_position_per_exchange:
                return RiskDecision(False, f"превышение лимита позиции на {ex}")
            if free_margin.get(ex, 0.0) < margin_required:
                return RiskDecision(False, f"недостаточно свободной маржи на {ex}")

        return RiskDecision(True)

    def _exposure_by_exchange(self, positions: list[Position]) -> dict[str, float]:
        exposure: dict[str, float] = {}
        for p in positions:
            for leg in p.legs:
                exposure[leg.exchange] = exposure.get(leg.exchange, 0.0) + leg.notional
        return exposure

    # ---- контроль ликвидации открытой позиции (§9) ----
    def check_liquidation(
        self, pos: Position, current_high: float, current_low: float,
    ) -> RiskDecision:
        """Проверить запас до ликвидации по обеим ногам (§9).

        Возвращает allowed=False (нужен алерт/сокращение), если запас нарушен.
        """
        s_leg, l_leg = pos.short_leg, pos.long_leg
        lev = self.leverage

        short_liq = approx_liquidation_price(
            s_leg.avg_price or 0.0, lev, Side.SHORT, self.maintenance_margin_rate)
        long_liq = approx_liquidation_price(
            l_leg.avg_price or 0.0, lev, Side.LONG, self.maintenance_margin_rate)

        short_dist = liquidation_distance(current_high, short_liq)
        long_dist = liquidation_distance(current_low, long_liq)

        if not liquidation_buffer_ok(short_dist, self.liquidation_buffer):
            return RiskDecision(False, f"SHORT нога близко к ликвидации ({short_dist:.3f})")
        if not liquidation_buffer_ok(long_dist, self.liquidation_buffer):
            return RiskDecision(False, f"LONG нога близко к ликвидации ({long_dist:.3f})")
        return RiskDecision(True)
