"""Этап 4 (§6): поиск расхождений и условие прибыльности.

Сигнал входа: по одному активу — пара бирж с максимальным net-спредом,
проходящая все фильтры. Считаем по реально исполнимым ценам:
  - лонг на дешёвой L по её ask;
  - шорт на дорогой H по её bid;
  - сырой спред = (bid_H - ask_L) / ask_L.

Чистый спред = сырой − round-trip комиссии − слиппедж + ожидаемый чистый funding.
Сделка допускается только если net_spread >= min_net_spread И сырой >= min_gross_spread
И объём исполним на вход и на выход (§6), и расхождение продержалось
>= min_spread_persistence (отсекаем флэш-прострелы).

Расчётные функции чистые и тестируемые; Scanner связывает их с MarketData,
persistence-трекером и dry_run-логированием.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import ArbSignal, FundingInfo, Quote

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  Чистые расчётные функции
# --------------------------------------------------------------------------
def raw_spread(bid_high: float, ask_low: float) -> float:
    """Сырой спред по исполнимым ценам: (bid_H - ask_L) / ask_L."""
    if ask_low <= 0:
        return 0.0
    return (bid_high - ask_low) / ask_low


def round_trip_fee(fee_high: float, fee_low: float) -> float:
    """Комиссии round-trip: 2 входа + 2 выхода = 2*(fee_H + fee_L) от нотионала (§6)."""
    return 2.0 * (fee_high + fee_low)


def expected_net_funding(
    funding_high: Optional[FundingInfo],
    funding_low: Optional[FundingInfo],
    hold_hours: float,
    default_interval_hours: float = 8.0,
) -> float:
    """Ожидаемый чистый funding за время удержания как доля нотионала (§5).

    Держим SHORT на H и LONG на L.
      - При положительном funding лонги платят шортам.
      - Короткая нога на H получает funding_H; длинная нога на L платит funding_L.
    Итого доход = funding_H*периодов_H − funding_L*периодов_L.
    Положительное значение = получаем, отрицательное = платим.
    Период начисления берём фактический (не хардкодим 8ч); если неизвестен —
    используем default_interval_hours.
    """
    def periods(info: Optional[FundingInfo]) -> float:
        if info is None:
            return 0.0
        interval = info.interval_hours or default_interval_hours
        if interval <= 0:
            return 0.0
        return hold_hours / interval

    inc = 0.0
    if funding_high is not None:
        inc += funding_high.funding_rate * periods(funding_high)
    if funding_low is not None:
        inc -= funding_low.funding_rate * periods(funding_low)
    return inc


def net_spread(
    raw: float,
    fee_cost: float,
    slippage_cost: float,
    funding_income: float,
) -> float:
    """Чистый спред = сырой − комиссии − слиппедж + чистый funding (§6)."""
    return raw - fee_cost - slippage_cost + funding_income


def walk_book(levels: list, target_notional: float) -> Optional[tuple[float, float]]:
    """Пройти по уровням стакана, набирая target_notional (USDT).

    levels: [[price, amount(base)], ...] уже в нужном направлении
            (asks по возрастанию для покупки, bids по убыванию для продажи).
    Возвращает (vwap, filled_notional). Если глубины не хватило — filled < target.
    None, если стакан пуст.
    """
    if not levels:
        return None
    remaining = target_notional
    cost = 0.0
    filled = 0.0
    for level in levels:
        price = float(level[0])
        amount = float(level[1]) if len(level) > 1 else 0.0
        level_notional = price * amount
        take = min(level_notional, remaining)
        if price > 0:
            cost += take
            filled += take
            remaining -= take
        if remaining <= 0:
            break
    if filled <= 0:
        return None
    # vwap как отношение потраченного нотионала к набранной базе
    base = 0.0
    remaining = filled
    for level in levels:
        price = float(level[0])
        amount = float(level[1]) if len(level) > 1 else 0.0
        take_notional = min(price * amount, remaining)
        if price > 0:
            base += take_notional / price
            remaining -= take_notional
        if remaining <= 0:
            break
    vwap = filled / base if base > 0 else float(levels[0][0])
    return vwap, filled


def candle_low_high(ohlcv: list) -> Optional[tuple[float, float]]:
    """Мин low и макс high по набору свечей ccxt [[ts,open,high,low,close,vol], ...]."""
    lows, highs = [], []
    for c in ohlcv or []:
        if not c or len(c) < 5:
            continue
        highs.append(float(c[2]))
        lows.append(float(c[3]))
    if not lows or not highs:
        return None
    return min(lows), max(highs)


def historical_price_divergence(ohlcv_a: list, ohlcv_b: list) -> Optional[float]:
    """Расхождение ценовых уровней двух активов за период (для сверки тождественности).

    Сравниваем минимум и максимум цены за последние N дней на двух биржах.
    Для одного и того же актива дневные low/high почти совпадают; для разных
    токенов под одним тикером — расходятся в разы. Возвращаем максимальное из
    относительных расхождений (min и max). None, если данных недостаточно.
    """
    a = candle_low_high(ohlcv_a)
    b = candle_low_high(ohlcv_b)
    if a is None or b is None:
        return None

    def rel(x: float, y: float) -> float:
        ref = (abs(x) + abs(y)) / 2.0
        if ref <= 0:
            return 0.0
        return abs(x - y) / ref

    return max(rel(a[0], b[0]), rel(a[1], b[1]))


def slippage_from_book(levels: list, ref_price: float, target_notional: float) -> Optional[float]:
    """Оценить слиппедж исполнения target_notional относительно ref_price.

    Возвращает долю (>=0). None, если стакан пуст или глубины не хватило.
    """
    res = walk_book(levels, target_notional)
    if res is None:
        return None
    vwap, filled = res
    if filled < target_notional * 0.999:  # глубины не хватило
        return None
    if ref_price <= 0:
        return None
    return abs(vwap - ref_price) / ref_price


# --------------------------------------------------------------------------
#  Трекер устойчивости расхождения (persistence, §6)
# --------------------------------------------------------------------------
class PersistenceTracker:
    """Отслеживает, как долго (symbol, H, L) держит спред >= порога.

    Нужен, чтобы входить только в устойчивые расхождения и отсекать
    флэш-прострелы (§6). Время инъектируется (clock) для детерминизма в тестах.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._since: dict[tuple[str, str, str], float] = {}

    def update(self, key: tuple[str, str, str], above_threshold: bool) -> float:
        """Обновить состояние ключа. Возвращает длительность удержания (сек)."""
        now = self._clock()
        if not above_threshold:
            self._since.pop(key, None)
            return 0.0
        start = self._since.setdefault(key, now)
        return now - start

    def peek(self, key: tuple[str, str, str]) -> float:
        """Текущая длительность удержания без изменения состояния."""
        start = self._since.get(key)
        return (self._clock() - start) if start is not None else 0.0

    def reset(self, key: tuple[str, str, str]) -> None:
        self._since.pop(key, None)


# --------------------------------------------------------------------------
#  Результат оценки пары бирж
# --------------------------------------------------------------------------
@dataclass
class PairEvaluation:
    """Итог оценки одной упорядоченной пары бирж (H, L) для актива."""

    signal: Optional[ArbSignal] = None
    passed: bool = False
    reasons: list[str] = field(default_factory=list)  # причины отклонения


# --------------------------------------------------------------------------
#  Сканер
# --------------------------------------------------------------------------
class Scanner:
    """Ищет лучший арбитражный сигнал по кандидатам (§6)."""

    def __init__(
        self,
        fees: dict[str, float],
        min_gross_spread: float = 0.005,
        min_net_spread: float = 0.002,
        max_slippage: float = 0.001,
        min_spread_persistence: float = 0.0,
        notional_target: float = 2000.0,
        hold_hours: float = 1.0,
        default_funding_interval_hours: float = 8.0,
        max_gross_spread: float = 0.05,
        max_quote_age_ms: Optional[float] = None,
        check_top_depth: bool = True,
        filters: Optional[dict] = None,
        persistence: Optional[PersistenceTracker] = None,
    ):
        self.fees = fees
        self.min_gross_spread = min_gross_spread
        self.min_net_spread = min_net_spread
        self.max_slippage = max_slippage
        self.min_spread_persistence = min_spread_persistence
        self.notional_target = notional_target
        self.hold_hours = hold_hours
        self.default_funding_interval_hours = default_funding_interval_hours
        # Переключатели доп-фильтров (базовые критерии — сырой порог и история —
        # действуют всегда). Позволяют упростить вход, отключив «тяжёлые» проверки.
        default_filters = {
            "net_spread": True, "top_depth": True, "persistence": True,
            "max_gross_spread": True, "quote_age": True,
        }
        self.filters = {**default_filters, **(filters or {})}
        # Верхняя граница сырого спреда: расхождение выше почти всегда означает
        # ошибку данных (устаревшая котировка / пустой стакан / разные единицы),
        # а не исполнимый арбитраж — такие сигналы отсекаем (защита от фантомов).
        self.max_gross_spread = max_gross_spread
        # Максимальный возраст котировки (мс); старше — данные протухли.
        self.max_quote_age_ms = max_quote_age_ms
        # Грубая проверка, что наш объём пролезает по верхушке стакана (вход+выход).
        self.check_top_depth = check_top_depth
        self.persistence = persistence or PersistenceTracker()

    def _quote_stale(self, quote: Quote, now: Optional[float]) -> bool:
        """Устарела ли котировка. now — wall-clock секунды; timestamp котировки — мс."""
        if self.max_quote_age_ms is None or now is None or quote.timestamp is None:
            return False
        age_ms = now * 1000.0 - quote.timestamp
        return age_ms > self.max_quote_age_ms

    def _top_depth_ok(self, required_base: float, quote_high: Quote, quote_low: Quote) -> bool:
        """Пролезает ли required_base по верхушке стакана на вход И на выход (§6).

        Вход: покупаем L по ask_L, продаём H по bid_H.
        Выход: откупаем H по ask_H, продаём L по bid_L.
        Если объём уровня неизвестен (None) — по нему не судим.
        """
        needed = [
            quote_low.ask_volume,   # вход: buy L
            quote_high.bid_volume,  # вход: sell H
            quote_high.ask_volume,  # выход: buy H
            quote_low.bid_volume,   # выход: sell L
        ]
        for vol in needed:
            if vol is not None and vol < required_base:
                return False
        return True

    def evaluate_pair(
        self,
        symbol: str,
        high: str,
        low: str,
        quote_high: Quote,
        quote_low: Quote,
        funding_high: Optional[FundingInfo] = None,
        funding_low: Optional[FundingInfo] = None,
        est_slippage: Optional[float] = None,
        now: Optional[float] = None,
        track_persistence: bool = True,
    ) -> PairEvaluation:
        """Оценить пару (H=short, L=long) и решить, проходит ли фильтры (§6).

        track_persistence=False — не менять состояние persistence (для диагностики).
        """
        reasons: list[str] = []
        bid_h = quote_high.bid
        ask_l = quote_low.ask

        raw = raw_spread(bid_h, ask_l)
        fee_cost = round_trip_fee(self.fees.get(high, 0.0), self.fees.get(low, 0.0))
        funding_income = expected_net_funding(
            funding_high, funding_low, self.hold_hours, self.default_funding_interval_hours
        )
        # Слиппедж: оценка из стакана, иначе консервативно max_slippage на обе ноги входа.
        slip = est_slippage if est_slippage is not None else self.max_slippage * 2
        net = net_spread(raw, fee_cost, slip, funding_income)

        signal = ArbSignal(
            symbol=symbol, exchange_high=high, exchange_low=low,
            bid_high=bid_h, ask_low=ask_l, raw_spread=raw, net_spread=net,
            fee_cost=fee_cost, funding_income=funding_income, slippage_cost=slip,
            notional=self.notional_target, timestamp=now,
        )

        f = self.filters
        # Фильтр 0: sanity — устаревшие котировки и аномально большой спред (опц.)
        if f["quote_age"] and (self._quote_stale(quote_high, now)
                               or self._quote_stale(quote_low, now)):
            reasons.append("устаревшая котировка")
        if f["max_gross_spread"] and raw > self.max_gross_spread:
            reasons.append(f"raw>{self.max_gross_spread} (вероятно ошибка данных)")
        # Фильтр 1: сырой порог — БАЗОВЫЙ критерий «сейчас разошлось», всегда.
        if raw < self.min_gross_spread:
            reasons.append(f"raw<{self.min_gross_spread}")
        # Фильтр 2: чистый порог (опц.)
        if f["net_spread"] and net < self.min_net_spread:
            reasons.append(f"net<{self.min_net_spread}")
        # Фильтр 3: исполнимость по слиппеджу из стакана (если оценивали)
        if est_slippage is not None and est_slippage > self.max_slippage:
            reasons.append(f"slippage>{self.max_slippage}")
        # Фильтр 4: грубая проверка глубины верхушки стакана (вход и выход) (опц.)
        if f["top_depth"] and self.check_top_depth:
            required_base = self.notional_target / ask_l if ask_l > 0 else 0.0
            if not self._top_depth_ok(required_base, quote_high, quote_low):
                reasons.append("не хватает глубины стакана (вход/выход)")
        # Фильтр 5: устойчивость расхождения (persistence) (опц.)
        # «Прошёл до persistence» = нет иных причин отклонения.
        above = not reasons
        key = (symbol, high, low)
        held = (self.persistence.update(key, above) if track_persistence
                else self.persistence.peek(key))
        if f["persistence"] and above and held < self.min_spread_persistence:
            reasons.append(f"persistence<{self.min_spread_persistence}s(={held:.1f})")

        passed = not reasons
        return PairEvaluation(signal=signal, passed=passed, reasons=reasons)

    def scan_symbol(
        self,
        symbol: str,
        quotes: dict[str, Quote],
        funding: Optional[dict[str, FundingInfo]] = None,
        now: Optional[float] = None,
    ) -> Optional[ArbSignal]:
        """Найти лучший проходящий сигнал по активу среди всех пар бирж (§6).

        Перебираем упорядоченные пары (H, L): шорт на H с большим bid, лонг на L
        с меньшим ask. Выбираем максимальный net-спред среди прошедших фильтры.
        """
        funding = funding or {}
        exchanges = list(quotes.keys())
        best: Optional[ArbSignal] = None

        for high in exchanges:
            for low in exchanges:
                if high == low:
                    continue
                qh, ql = quotes[high], quotes[low]
                if qh.bid <= ql.ask:
                    continue  # нет расхождения в эту сторону
                ev = self.evaluate_pair(
                    symbol, high, low, qh, ql,
                    funding.get(high), funding.get(low), now=now,
                )
                if ev.passed and ev.signal is not None:
                    if best is None or ev.signal.net_spread > best.net_spread:
                        best = ev.signal
        return best
