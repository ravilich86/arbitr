"""Этап 5 (§7, §8): открытие двух ног, leg-risk, откат; объём и плечо.

Когда сигнал прошёл фильтры сканера:
  - на бирже H (дороже) открываем SHORT;
  - на бирже L (дешевле) открываем LONG;
  - объём равный на обеих ногах (дельта-нейтральность), с округлением под
    step size каждой биржи и с учётом минимумов обеих (§8).

Ноги выставляются конкурентно (asyncio.gather). Если одна исполнилась, а
вторая нет — это незахеджированная позиция; поведение по конфигу:
on_leg_failure = rollback (закрыть открытую ногу) | retry (довыставить вторую).

Функции размера/парсинга ордеров чистые и тестируемые; сетевые вызовы идут
через ccxt-клиент (в тестах — мок), в dry_run ордера симулируются.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from typing import Any, Optional

from .exchanges import ExchangeConnector
from .models import (
    ArbSignal,
    ContractMeta,
    Leg,
    LegStatus,
    Position,
    PositionStatus,
    Quote,
    Side,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  Расчёт объёма (§8)
# --------------------------------------------------------------------------
def _floor_to_step(amount: float, step: Optional[float]) -> float:
    if not step or step <= 0:
        return amount
    return math.floor(amount / step) * step


def compute_base_amount(
    price: float,
    notional: float,
    meta_high: ContractMeta,
    meta_low: ContractMeta,
) -> float:
    """Равный объём базового актива под обе ноги (§8).

    Берём наибольшее допустимое количество, кратное step size обеих бирж и
    удовлетворяющее минимумам (min_amount, min_notional) обеих. Возвращает 0.0,
    если минимумы обеих бирж одновременно выполнить нельзя.
    """
    if price <= 0 or notional <= 0:
        return 0.0

    target = notional / price
    # Округляем вниз под каждый шаг и берём минимум (кратно обоим при кратных шагах)
    a_high = _floor_to_step(target, meta_high.step_size)
    a_low = _floor_to_step(target, meta_low.step_size)
    amount = min(a_high, a_low)
    amount = _floor_to_step(amount, meta_high.step_size)
    amount = _floor_to_step(amount, meta_low.step_size)
    if amount <= 0:
        return 0.0

    # Проверка минимумов обеих бирж
    for meta in (meta_high, meta_low):
        if meta.min_amount and amount < meta.min_amount:
            return 0.0
        if meta.min_notional and amount * price < meta.min_notional:
            return 0.0
    return amount


# --------------------------------------------------------------------------
#  Парсинг ответа ордера ccxt
# --------------------------------------------------------------------------
def parse_order(order: dict) -> dict:
    """Нормализовать ccxt-ответ ордера -> {filled, avg_price, id, status, fee}."""
    filled = order.get("filled") or 0.0
    amount = order.get("amount") or 0.0
    avg = order.get("average") or order.get("price")
    raw_status = order.get("status")

    if raw_status == "closed" or (filled and amount and filled >= amount * 0.999):
        status = LegStatus.FILLED
    elif filled and filled > 0:
        status = LegStatus.PARTIAL
    elif raw_status in ("canceled", "rejected", "expired"):
        status = LegStatus.FAILED
    else:
        status = LegStatus.PENDING

    fee = 0.0
    fee_obj = order.get("fee") or {}
    if isinstance(fee_obj, dict):
        fee = fee_obj.get("cost") or 0.0
    fees_list = order.get("fees") or []
    for f in fees_list:
        if isinstance(f, dict) and f.get("cost"):
            fee += f["cost"]

    return {
        "filled": float(filled),
        "avg_price": float(avg) if avg else None,
        "id": order.get("id"),
        "status": status,
        "fee": float(fee),
    }


# --------------------------------------------------------------------------
#  Исполнитель
# --------------------------------------------------------------------------
class Executor:
    """Открытие/закрытие ног с контролем leg-risk (§7)."""

    def __init__(
        self,
        connectors: dict[str, ExchangeConnector],
        fees: dict[str, float],
        dry_run: bool = True,
        order_type: str = "market",
        on_leg_failure: str = "rollback",
        leg_timeout: float = 5.0,
        leverage: int = 20,
        margin_mode: str = "isolated",
        clock=time.time,
    ):
        self.connectors = connectors
        self.fees = fees
        self.dry_run = dry_run
        self.order_type = order_type
        self.on_leg_failure = on_leg_failure
        self.leg_timeout = leg_timeout
        self.leverage = leverage
        self.margin_mode = margin_mode
        self._clock = clock

    # ---- служебное ----
    def _raw_symbol(self, exchange: str, symbol: str) -> str:
        conn = self.connectors.get(exchange)
        if conn and symbol in conn.contracts:
            return conn.contracts[symbol].raw_symbol
        return symbol

    def _meta(self, exchange: str, symbol: str) -> Optional[ContractMeta]:
        conn = self.connectors.get(exchange)
        return conn.contracts.get(symbol) if conn else None

    async def _place_leg(
        self, exchange: str, symbol: str, side: Side, amount: float,
        ref_price: float, reduce_only: bool = False,
    ) -> Leg:
        """Выставить одну ногу (market/limit). В dry_run — симуляция."""
        leg = Leg(exchange=exchange, symbol=symbol, side=side, amount=amount)
        ccxt_side = "sell" if side == Side.SHORT else "buy"
        fee_rate = self.fees.get(exchange, 0.0)

        if self.dry_run:
            leg.filled_amount = amount
            leg.avg_price = ref_price
            leg.order_id = f"dry-{uuid.uuid4().hex[:8]}"
            leg.status = LegStatus.FILLED
            leg.fee_paid = amount * ref_price * fee_rate
            return leg

        client = self.connectors[exchange].client
        raw_symbol = self._raw_symbol(exchange, symbol)
        params: dict[str, Any] = {}
        if reduce_only:
            params["reduceOnly"] = True
        price = None if self.order_type == "market" else ref_price
        try:
            order = await asyncio.wait_for(
                client.create_order(raw_symbol, self.order_type, ccxt_side,
                                    amount, price, params),
                timeout=self.leg_timeout,
            )
            parsed = parse_order(order)
            leg.filled_amount = parsed["filled"]
            leg.avg_price = parsed["avg_price"]
            leg.order_id = parsed["id"]
            leg.status = parsed["status"]
            leg.fee_paid = parsed["fee"]
        except asyncio.TimeoutError:
            leg.status = LegStatus.FAILED
            leg.error = "timeout"
        except Exception as exc:  # noqa: BLE001 - логируем любую ошибку биржи
            leg.status = LegStatus.FAILED
            leg.error = str(exc)
            logger.error("Ошибка ноги %s %s: %s", exchange, symbol, exc)
        return leg

    async def _prepare_leverage(self, exchange: str, symbol: str, meta: ContractMeta) -> None:
        """Выставить плечо и режим маржи (§8). min(20, max_leverage биржи)."""
        if self.dry_run:
            return
        client = self.connectors[exchange].client
        raw_symbol = self._raw_symbol(exchange, symbol)
        lev = self.leverage
        if meta.max_leverage:
            lev = int(min(self.leverage, meta.max_leverage))
        try:
            if hasattr(client, "set_margin_mode"):
                await client.set_margin_mode(self.margin_mode, raw_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_margin_mode %s: %s", exchange, exc)
        try:
            if hasattr(client, "set_leverage"):
                await client.set_leverage(lev, raw_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_leverage %s: %s", exchange, exc)

    # ---- вход двумя ногами ----
    async def open_position(self, signal: ArbSignal) -> Position:
        """Открыть позицию: SHORT на H и LONG на L конкурентно (§7)."""
        symbol = signal.symbol
        meta_h = self._meta(signal.exchange_high, symbol)
        meta_l = self._meta(signal.exchange_low, symbol)
        if meta_h is None or meta_l is None:
            raise ValueError(f"Нет метаданных контракта для {symbol}")

        amount = compute_base_amount(signal.ask_low, signal.notional, meta_h, meta_l)
        pos = Position(
            id=uuid.uuid4().hex[:12], symbol=symbol,
            exchange_high=signal.exchange_high, exchange_low=signal.exchange_low,
            short_leg=Leg(signal.exchange_high, symbol, Side.SHORT, amount),
            long_leg=Leg(signal.exchange_low, symbol, Side.LONG, amount),
            signal=signal, open_time=self._clock(), status=PositionStatus.OPENING,
        )
        if amount <= 0:
            pos.status = PositionStatus.FAILED
            pos.close_reason = "amount=0 (минимумы бирж не выполнимы)"
            logger.warning("Сигнал %s: объём 0, вход отменён", symbol)
            return pos

        # плечо/маржа на обеих биржах
        await asyncio.gather(
            self._prepare_leverage(signal.exchange_high, symbol, meta_h),
            self._prepare_leverage(signal.exchange_low, symbol, meta_l),
        )

        # конкурентное выставление ног
        short_leg, long_leg = await asyncio.gather(
            self._place_leg(signal.exchange_high, symbol, Side.SHORT, amount, signal.bid_high),
            self._place_leg(signal.exchange_low, symbol, Side.LONG, amount, signal.ask_low),
        )
        pos.short_leg = short_leg
        pos.long_leg = long_leg

        await self._resolve_legs(pos)
        return pos

    async def _resolve_legs(self, pos: Position) -> None:
        """Обработать результат выставления: leg-risk, откат, частичное (§7)."""
        s, l = pos.short_leg, pos.long_leg

        if s.is_filled and l.is_filled:
            # выровнять при частичном расхождении объёмов
            await self._equalize(pos)
            pos.status = PositionStatus.OPEN
            return

        # одна или обе ноги не исполнены
        filled = [leg for leg in (s, l) if leg.is_filled or leg.filled_amount > 0]
        if not filled:
            pos.status = PositionStatus.FAILED
            pos.close_reason = "обе ноги не исполнены"
            logger.warning("Позиция %s: обе ноги не исполнены", pos.symbol)
            return

        # ровно одна нога висит -> leg-risk
        if self.on_leg_failure == "retry":
            await self._retry_missing_leg(pos)
            if pos.short_leg.is_filled and pos.long_leg.is_filled:
                pos.status = PositionStatus.OPEN
                return
        # rollback (по умолчанию) или неудачный retry -> закрыть открытую ногу
        await self._rollback(pos)

    async def _equalize(self, pos: Position) -> None:
        """Довести обе ноги до одинакового исполненного объёма (§7)."""
        s, l = pos.short_leg, pos.long_leg
        diff = round(s.filled_amount - l.filled_amount, 12)
        if diff == 0:
            return
        # уменьшаем большую ногу до размера меньшей (симметрия) reduce-only
        if diff > 0:  # short больше -> откупаем излишек short
            extra = await self._place_leg(s.exchange, s.symbol, Side.LONG, diff,
                                          pos.signal.bid_high if pos.signal else 0.0,
                                          reduce_only=True)
            if extra.is_filled:
                s.filled_amount -= extra.filled_amount
        else:  # long больше -> продаём излишек long
            extra = await self._place_leg(l.exchange, l.symbol, Side.SHORT, -diff,
                                          pos.signal.ask_low if pos.signal else 0.0,
                                          reduce_only=True)
            if extra.is_filled:
                l.filled_amount -= extra.filled_amount

    async def _retry_missing_leg(self, pos: Position) -> None:
        """Довыставить недостающую ногу (on_leg_failure=retry)."""
        s, l = pos.short_leg, pos.long_leg
        if s.is_filled and not l.is_filled:
            new = await self._place_leg(l.exchange, l.symbol, Side.LONG, s.filled_amount,
                                        pos.signal.ask_low if pos.signal else 0.0)
            pos.long_leg = new
        elif l.is_filled and not s.is_filled:
            new = await self._place_leg(s.exchange, s.symbol, Side.SHORT, l.filled_amount,
                                        pos.signal.bid_high if pos.signal else 0.0)
            pos.short_leg = new

    async def _rollback(self, pos: Position) -> None:
        """Закрыть уже открытую ногу по рынку (откат несостоявшегося входа, §7)."""
        for leg in (pos.short_leg, pos.long_leg):
            if leg.is_filled and leg.filled_amount > 0:
                close_side = Side.LONG if leg.side == Side.SHORT else Side.SHORT
                ref = pos.signal.bid_high if leg.side == Side.SHORT else pos.signal.ask_low
                closer = await self._place_leg(leg.exchange, leg.symbol, close_side,
                                               leg.filled_amount, ref or 0.0,
                                               reduce_only=True)
                if closer.is_filled:
                    leg.status = LegStatus.CLOSED
                else:
                    pos.status = PositionStatus.UNHEDGED
                    pos.close_reason = "откат не удался — нога висит!"
                    logger.error("Позиция %s: откат не удался, нога %s висит!",
                                 pos.symbol, leg.exchange)
                    return
        if pos.status != PositionStatus.UNHEDGED:
            pos.status = PositionStatus.FAILED
            pos.close_reason = pos.close_reason or "leg-fail -> откат выполнен"

    # ---- выход (§7) ----
    async def close_position(
        self, pos: Position, quote_high: Quote, quote_low: Quote, reason: str,
    ) -> Position:
        """Закрыть обе ноги конкурентно и посчитать P&L (§7).

        SHORT на H закрываем покупкой по ask_H; LONG на L — продажей по bid_L.
        """
        pos.status = PositionStatus.CLOSING
        amount = min(pos.short_leg.filled_amount, pos.long_leg.filled_amount)

        close_short, close_long = await asyncio.gather(
            self._place_leg(pos.exchange_high, pos.symbol, Side.LONG, amount,
                            quote_high.ask, reduce_only=True),
            self._place_leg(pos.exchange_low, pos.symbol, Side.SHORT, amount,
                            quote_low.bid, reduce_only=True),
        )

        # leg-risk на выходе: если одна нога не закрылась — пометить и вернуть
        if not (close_short.is_filled and close_long.is_filled):
            pos.status = PositionStatus.UNHEDGED
            pos.close_reason = f"{reason}; выход: одна нога не закрылась"
            logger.error("Позиция %s: выход неполный (leg-risk)", pos.symbol)
            return pos

        pos.short_leg.status = LegStatus.CLOSED
        pos.long_leg.status = LegStatus.CLOSED
        pos.realized_pnl = compute_pnl(
            short_entry=pos.short_leg.avg_price or 0.0,
            long_entry=pos.long_leg.avg_price or 0.0,
            amount=amount,
            short_close=close_short.avg_price or 0.0,
            long_close=close_long.avg_price or 0.0,
            entry_fees=pos.short_leg.fee_paid + pos.long_leg.fee_paid,
            close_fees=close_short.fee_paid + close_long.fee_paid,
            funding_accrued=pos.funding_accrued,
        )
        pos.status = PositionStatus.CLOSED
        pos.close_time = self._clock()
        pos.close_reason = reason
        return pos


def current_spread(quote_high: Quote, quote_low: Quote) -> float:
    """Текущий спред позиции той же формулой, что на входе: (bid_H - ask_L)/ask_L."""
    from .scanner import raw_spread
    return raw_spread(quote_high.bid, quote_low.ask)


def should_exit(
    cur_spread: float,
    hold_time: float,
    exit_spread: float,
    max_hold_time: float,
    max_adverse_spread: float,
    est_pnl: Optional[float] = None,
    take_profit: Optional[float] = None,
) -> tuple[bool, Optional[str]]:
    """Решить, пора ли закрывать позицию, и по какой причине (§7).

    Порядок: схождение до цели -> take-profit -> расхождение сверх лимита ->
    предельное время удержания.
    """
    if cur_spread <= exit_spread:
        return True, "target"
    if take_profit is not None and est_pnl is not None and est_pnl >= take_profit:
        return True, "take_profit"
    if cur_spread >= max_adverse_spread:
        return True, "max_adverse"
    if hold_time >= max_hold_time:
        return True, "max_hold_time"
    return False, None


def compute_pnl(
    short_entry: float,
    long_entry: float,
    amount: float,
    short_close: float,
    long_close: float,
    entry_fees: float = 0.0,
    close_fees: float = 0.0,
    funding_accrued: float = 0.0,
) -> float:
    """Итоговый P&L позиции в USDT (§10).

    SHORT: продали по short_entry, откупили по short_close -> (entry-close)*amt.
    LONG:  купили по long_entry, продали по long_close  -> (close-entry)*amt.
    Минус все комиссии, плюс начисленный funding.
    """
    pnl_short = (short_entry - short_close) * amount
    pnl_long = (long_close - long_entry) * amount
    return pnl_short + pnl_long - entry_fees - close_fees + funding_accrued


def estimate_open_pnl(pos: Position, quote_high: Quote, quote_low: Quote,
                      fee_rate_high: float = 0.0, fee_rate_low: float = 0.0) -> float:
    """Оценка нереализованного P&L при закрытии по текущим ценам (для take-profit)."""
    amount = min(pos.short_leg.filled_amount, pos.long_leg.filled_amount)
    close_fees = amount * (quote_high.ask * fee_rate_high + quote_low.bid * fee_rate_low)
    return compute_pnl(
        short_entry=pos.short_leg.avg_price or 0.0,
        long_entry=pos.long_leg.avg_price or 0.0,
        amount=amount,
        short_close=quote_high.ask,
        long_close=quote_low.bid,
        entry_fees=pos.short_leg.fee_paid + pos.long_leg.fee_paid,
        close_fees=close_fees,
        funding_accrued=pos.funding_accrued,
    )
