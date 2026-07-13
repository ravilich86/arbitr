"""Оркестратор: связывает все модули в рабочий цикл (§12).

Цикл poll_once():
  1. мониторинг открытых позиций -> закрытие по условиям выхода (§7);
  2. при наличии свободного слота — скан кандидатов, риск-чек, вход (§6–9).

В dry_run ордера не отправляются на биржу (Executor симулирует исполнение),
но сигналы и симулированный P&L логируются — чтобы убедиться, что расчёт
корректен перед боевым запуском (§11).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .config import Config
from .exchanges import ExchangeConnector, fetch_taker_fee
from .executor import Executor, current_spread, estimate_open_pnl, should_exit
from .logger import SessionSummary, TradeLogger
from .marketdata import MarketData, parse_ticker
from .models import Candidate, Position, PositionStatus
from .reconcile import close_positions, fetch_all_positions, pair_positions
from .risk import RiskManager
from .scanner import (
    Scanner,
    candle_low_high,
    expected_net_funding,
    historical_price_divergence,
    raw_spread,
)
from .universe import build_universe


def _rel_diff(x: float, y: float) -> float:
    """Относительная разница двух цен (доля)."""
    ref = (abs(x) + abs(y)) / 2.0
    return abs(x - y) / ref if ref > 0 else 0.0

logger = logging.getLogger("arb.bot")


class ArbitrageBot:
    """Связывает universe/marketdata/scanner/executor/risk/logger в цикл."""

    def __init__(
        self,
        config: Config,
        connectors: dict[str, ExchangeConnector],
        marketdata: MarketData,
        scanner: Scanner,
        executor: Executor,
        risk: RiskManager,
        trade_logger: TradeLogger,
        summary: Optional[SessionSummary] = None,
        notifier=None,
        store=None,
        clock=None,
    ):
        import time as _time
        self.config = config
        self.connectors = connectors
        self.md = marketdata
        self.scanner = scanner
        self.executor = executor
        self.risk = risk
        self.trade_logger = trade_logger
        self.summary = summary or SessionSummary()
        self.notifier = notifier
        self.store = store
        self._clock = clock or _time.time
        self._bg_tasks: set = set()

        self.candidates: dict = {}
        self.open_positions: list[Position] = []
        # Историческая сверка тождественности при большом текущем спреде
        self.history_cfg = (config.raw.get("history") or {}) if config.raw else {}
        # WS-стриминг
        self._running = False
        self._stream_tasks: list = []

    # ---- построение вселенной (§3–4) ----
    async def refresh_universe(self) -> None:
        async def load(name, conn):
            try:
                return name, await conn.load_perp_contracts()
            except Exception as exc:  # noqa: BLE001 - одна биржа не должна ронять старт
                logger.warning("Не удалось загрузить рынки %s: %s — биржа пропущена",
                               name, exc)
                return name, {}

        results = await asyncio.gather(
            *[load(n, c) for n, c in self.connectors.items()])
        contracts = {name: c for name, c in results}
        loaded = [n for n, c in results if c]
        logger.info("Рынки загружены: %s", ", ".join(loaded) or "нет")
        universe_cfg = self.config.raw.get("universe", {}) or {}
        res = build_universe(
            contracts,
            allow_list=self.config.allow_list,
            deny_list=self.config.deny_list,
            max_contract_size_ratio=universe_cfg.get("max_contract_size_ratio"),
            skip_delisting_days=universe_cfg.get("skip_delisting_days", 0.0),
        )
        self.candidates = res.candidates
        logger.info(
            "Вселенная: %d кандидатов, %d подозрительных, %d single-exchange, %d делистинг",
            len(res.candidates), len(res.suspicious), len(res.single_exchange),
            len(res.delisting),
        )

    # ---- реконсиляция на старте (проверка орфанов) ----
    async def startup_reconcile(self) -> None:
        """Проверить на старте, нет ли незахеджированных (одиночных) ног.

        Читает реальные позиции со всех бирж, спаривает их в арб-пары; если
        находит «орфана» (ногу без противоположной) — алертит и, при
        risk.close_orphans_on_start и боевом режиме, закрывает его.
        """
        try:
            views = await fetch_all_positions(self.connectors)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Реконсиляция на старте не удалась: %s", exc)
            return
        if not views:
            logger.info("Реконсиляция: открытых позиций на биржах нет — чисто")
            return
        pairs, orphans = pair_positions(views)
        logger.info("Реконсиляция: %d ног -> %d согласованных пар, %d орфанов",
                    len(views), len(pairs), len(orphans))
        if not orphans:
            return
        desc = ", ".join(f"{o.exchange}:{o.symbol}:{o.side}({o.size:g})" for o in orphans)
        logger.warning("АНОМАЛИЯ: незахеджированные ноги: %s", desc)
        if self.notifier:
            self._notify_event(
                "anomaly",
                f"🤖 {self._app_name()}\n"
                f"⚠️ Аномалия на старте: незахеджированные ноги\n{desc}")
        if self.config.risk.get("close_orphans_on_start", False) and not self.config.dry_run:
            await close_positions(self.connectors, orphans, execute=True)
            logger.warning("Орфаны закрыты (close_orphans_on_start=true).")

    # ---- актуальные комиссии с бирж ----
    async def refresh_fees(self) -> None:
        """Подтянуть реальные taker-комиссии с бирж (учёт VIP). Фолбэк — конфиг."""
        fees_cfg = (self.config.raw.get("fees") or {})
        if not fees_cfg.get("fetch_from_exchange", True):
            return

        async def one(name, conn):
            try:
                return name, await fetch_taker_fee(conn)
            except Exception:  # noqa: BLE001
                return name, None

        results = await asyncio.gather(
            *[one(n, c) for n, c in self.connectors.items()])
        for name, taker in results:
            if taker is not None:
                self.scanner.fees[name] = taker
                self.executor.fees[name] = taker
                logger.info("Комиссия %s: taker=%.4f%% (с биржи)", name, taker * 100)
            else:
                cur = self.scanner.fees.get(name, 0.0)
                logger.info("Комиссия %s: taker=%.4f%% (из конфига)", name, cur * 100)

    # ---- предфильтр вселенной по истории (10 дней, ≤0.3%) ----
    @staticmethod
    def _agreeing_exchanges(levels: dict, tol: float) -> list:
        """Оставить биржи, чьи 10-дневные min/max совпадают в пределах tol.

        levels: {биржа -> (min_low, max_high)}. За эталон берём медиану min и max;
        оставляем биржи, у которых и минимум, и максимум близки к эталону — значит
        это один и тот же актив, исторически шедший вместе.
        """
        if len(levels) < 2:
            return list(levels.keys())
        mins = sorted(v[0] for v in levels.values())
        maxs = sorted(v[1] for v in levels.values())
        ref_min = mins[len(mins) // 2]
        ref_max = maxs[len(maxs) // 2]
        keep = []
        for ex, (lo, hi) in levels.items():
            if _rel_diff(lo, ref_min) <= tol and _rel_diff(hi, ref_max) <= tol:
                keep.append(ex)
        return keep

    async def prequalify_universe(self, now: Optional[float] = None,
                                  concurrency: int = 20) -> None:
        """Отобрать пары, которые исторически шли вместе (≤ max_divergence).

        До поиска арбитража тянем дневные свечи за N дней по каждой ноге, считаем
        min/max и оставляем только те пары, где уровни бирж совпадают в пределах
        порога. Это подтверждает тождественность актива один раз заранее, а не в
        момент сигнала. Свечи кэшируются (меняются раз в день).
        """
        cfg = self.history_cfg or {}
        if not cfg.get("enabled", False) or not cfg.get("prefilter", True):
            return
        timeframe = cfg.get("timeframe", "1d")
        days = int(cfg.get("days", 10))
        tol = cfg.get("max_divergence", 0.003)
        sem = asyncio.Semaphore(concurrency)

        async def fetch(ex, symbol):
            async with sem:
                try:
                    oh = await self.md.update_ohlcv(ex, symbol, timeframe, days, now=now)
                    return candle_low_high(oh) if oh else None
                except Exception:  # noqa: BLE001
                    return None

        async def qualify(symbol, cand):
            res = await asyncio.gather(*[fetch(ex, symbol) for ex in cand.exchanges])
            levels = {ex: r for ex, r in zip(cand.exchanges, res) if r is not None}
            if len(levels) < 2:
                return symbol, None, "нет данных"
            keep = self._agreeing_exchanges(levels, tol)
            if len(keep) < 2:
                return symbol, None, "разошлись"
            return symbol, Candidate(symbol, {ex: cand.contracts[ex] for ex in keep}), None

        logger.info("Предфильтр по истории: анализирую %d пар за %d дней (≤%.2f%%)…",
                    len(self.candidates), days, tol * 100)
        items = list(self.candidates.items())
        results = await asyncio.gather(*[qualify(s, c) for s, c in items])

        qualified, dropped, nodata = {}, 0, 0
        for symbol, cand, reason in results:
            if cand is not None:
                qualified[symbol] = cand
            elif reason == "нет данных":
                nodata += 1
            else:
                dropped += 1
        self.candidates = qualified
        logger.info("Предфильтр: в работе %d пар (отсеяно по расхождению %d, нет данных %d)",
                    len(qualified), dropped, nodata)

    # ---- обновление котировок/funding (§5) ----
    async def refresh_market_data(self, use_ws: bool = True) -> None:
        tasks = []
        for symbol, cand in self.candidates.items():
            for ex in cand.exchanges:
                tasks.append(self.md.update_quote(ex, symbol, use_ws=use_ws))
                tasks.append(self.md.update_funding(ex, symbol))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- один проход цикла ----
    async def poll_once(self, now: Optional[float] = None) -> None:
        now = now if now is not None else self._clock()
        await self._monitor_positions(now)
        await self._harvest_losers(now)
        if not self.risk.killed:
            await self._scan_and_enter(now)

    # ---- мониторинг и выход (§7) ----
    async def _monitor_positions(self, now: float) -> None:
        still_open: list[Position] = []
        for pos in self.open_positions:
            if pos.status != PositionStatus.OPEN:
                continue
            qh = self.md.get_quote(pos.exchange_high, pos.symbol)
            ql = self.md.get_quote(pos.exchange_low, pos.symbol)
            if qh is None or ql is None:
                still_open.append(pos)
                continue

            cur = current_spread(qh, ql)
            hold = now - (pos.open_time or now)
            fee_h = self.scanner.fees.get(pos.exchange_high, 0.0)
            fee_l = self.scanner.fees.get(pos.exchange_low, 0.0)
            est_pnl = estimate_open_pnl(pos, qh, ql, fee_h, fee_l)

            exit_now, reason = should_exit(
                cur_spread=cur, hold_time=hold,
                exit_spread=self.config.spread.get("exit_spread", 0.0),
                max_hold_time=self.config.risk.get("max_hold_time", 3600),
                max_adverse_spread=self.config.risk.get("max_adverse_spread", 0.02),
                est_pnl=est_pnl, take_profit=self.config.spread.get("take_profit"),
            )
            # риск-контроль ликвидации может форсировать закрытие (§9)
            liq = self.risk.check_liquidation(pos, qh.bid, ql.ask)
            if not liq.allowed:
                exit_now, reason = True, "liquidation_buffer"
                logger.warning("Позиция %s: %s", pos.symbol, liq.reason)

            if exit_now:
                await self._close_position(pos, qh, ql, reason or "target", now,
                                           hold, exit_spread=cur)
            else:
                still_open.append(pos)
        self.open_positions = still_open

    async def _close_position(self, pos: Position, qh, ql, reason: str, now: float,
                              hold: float, exit_spread: Optional[float] = None) -> None:
        """Закрыть позицию, посчитать P&L, залогировать, уведомить + общий баланс."""
        # Начислить funding за время удержания (оценка) перед расчётом P&L.
        pos.funding_accrued = self._accrued_funding(pos, hold)
        await self.executor.close_position(pos, qh, ql, reason)
        self.risk.register_close(pos.symbol, now)
        self.trade_logger.log_position(
            pos, leverage=self.risk.leverage,
            exit_spread=exit_spread if exit_spread is not None else current_spread(qh, ql))
        self.summary.record_trade(pos.realized_pnl)
        logger.info("Закрыта %s: причина=%s P&L=%s", pos.symbol, reason, pos.realized_pnl)
        if self.store:
            self.store.remove(pos.id)
        # Пункт 10: общий баланс всех бирж после закрытия любой позиции.
        total, parts = await self._total_balance()
        pstr = ", ".join(
            f"{n}={v:.2f}" if v is not None else f"{n}=?" for n, v in parts.items())
        logger.info("Общий баланс всех бирж: %.2f USDT (%s)", total, pstr)
        if self.notifier:
            msg = self.notifier.close_message(pos, self.config.dry_run)
            msg += f"\n💵 Общий баланс: {total:.2f} USDT"
            self._notify_event("close", msg)

    async def _harvest_losers(self, now: float) -> None:
        """Пункт 6: закрывать убыточные пары за счёт накопленной прибыли.

        Логика: если по паре нереализованный убыток, а сессионный P&L его
        перекрывает — закрываем убыточную позицию сейчас, «профинансировав» её
        прибылью. Правило безопасности: после закрытия сессионный net не должен
        уйти ниже profit_buffer_keep. Начинаем с самых убыточных.
        """
        if not self.config.risk.get("profit_buffer_close", False):
            return
        keep = self.config.risk.get("profit_buffer_keep", 0.0)

        scored = []
        for pos in self.open_positions:
            if pos.status != PositionStatus.OPEN:
                continue
            qh = self.md.get_quote(pos.exchange_high, pos.symbol)
            ql = self.md.get_quote(pos.exchange_low, pos.symbol)
            if qh is None or ql is None:
                continue
            fee_h = self.scanner.fees.get(pos.exchange_high, 0.0)
            fee_l = self.scanner.fees.get(pos.exchange_low, 0.0)
            est = estimate_open_pnl(pos, qh, ql, fee_h, fee_l)
            if est < 0:
                scored.append((est, pos, qh, ql))
        scored.sort(key=lambda x: x[0])  # самые убыточные первыми

        for est, pos, qh, ql in scored:
            if self.summary.total_pnl + est >= keep:  # прибыль перекрывает убыток
                hold = now - (pos.open_time or now)
                await self._close_position(pos, qh, ql, "profit_buffer", now, hold,
                                           exit_spread=current_spread(qh, ql))
                logger.info("Убыточная %s закрыта за счёт прибыли (est=%.4f)",
                            pos.symbol, est)
        self.open_positions = [p for p in self.open_positions
                               if p.status == PositionStatus.OPEN]

    def _accrued_funding(self, pos: Position, hold_seconds: float) -> float:
        """Оценка чистого funding за время удержания в USDT (§5).

        Держим шорт на H и лонг на L: доход = funding_H*периодов − funding_L*периодов.
        Оценка линейная по времени удержания (реально начисляется в дискретные
        моменты, но для приближённого P&L этого достаточно). Знак: + получили, − заплатили.
        """
        fh = self.md.get_funding(pos.exchange_high, pos.symbol)
        fl = self.md.get_funding(pos.exchange_low, pos.symbol)
        hold_hours = max(0.0, hold_seconds / 3600.0)
        income_frac = expected_net_funding(
            fh, fl, hold_hours, self.scanner.default_funding_interval_hours)
        return income_frac * pos.short_leg.notional

    # ---- скан и вход (§6–9) ----
    async def _scan_and_enter(self, now: float) -> None:
        active = [p for p in self.open_positions
                  if p.status in (PositionStatus.OPEN, PositionStatus.OPENING)]
        # max_concurrent_positions == 0 -> без лимита (пункт 7).
        limit = self.risk.max_concurrent_positions
        if limit and len(active) >= limit:
            return

        # Собираем ВСЕ проходящие сигналы и сортируем по убыванию чистого спреда.
        # Перебор (а не только «лучший») нужен, потому что верхний сигнал может не
        # пройти историческую сверку/риск — тогда берём следующий подходящий.
        signals = []
        for symbol, cand in self.candidates.items():
            quotes = {ex: q for ex in cand.exchanges
                      if (q := self.md.get_quote(ex, symbol)) is not None}
            if len(quotes) < 2:
                continue
            funding = {ex: f for ex in cand.exchanges
                       if (f := self.md.get_funding(ex, symbol)) is not None}
            sig = self.scanner.scan_symbol(symbol, quotes, funding, now=now)
            if sig:
                signals.append(sig)
        signals.sort(key=lambda s: s.net_spread, reverse=True)

        max_check = int((self.history_cfg or {}).get("max_candidates_check", 10))
        for sig in signals[:max_check]:
            margin_required = sig.notional / max(self.risk.leverage, 1)
            free_margin = await self._free_margin(sig.exchange_high, sig.exchange_low)
            decision = self.risk.can_open(
                sig.symbol, self.open_positions, margin_required, free_margin,
                (sig.exchange_high, sig.exchange_low), now,
            )
            if not decision.allowed:
                logger.info("Сигнал %s отклонён риском: %s", sig.symbol, decision.reason)
                # Пункт 8: если не хватает баланса — уведомить в Telegram.
                if "маржи" in (decision.reason or ""):
                    self._notify_event(
                        "balance",
                        f"🤖 {self._app_name()}\n"
                        f"⚠️ Не хватает баланса для входа\n"
                        f"📊 Пара: {sig.symbol} ({sig.exchange_high} ↔ {sig.exchange_low})\n"
                        f"💵 Нужно ~{margin_required:.2f} USDT маржи, свободно: "
                        f"{', '.join(f'{e}={free_margin.get(e, 0):.2f}' for e in (sig.exchange_high, sig.exchange_low))}")
                continue

            # Обязательная историческая сверка «было вместе -> сейчас разошлось».
            hist_ok, hist_reason = await self._history_check(sig, now)
            if not hist_ok:
                logger.info("Сигнал %s отклонён историей: %s", sig.symbol, hist_reason)
                continue

            mode = "DRY-RUN" if self.config.dry_run else "LIVE"
            logger.info("[%s] Вход %s: H=%s L=%s raw=%.4f net=%.4f",
                        mode, sig.symbol, sig.exchange_high, sig.exchange_low,
                        sig.raw_spread, sig.net_spread)
            pos = await self.executor.open_position(sig)
            pos.open_time = now  # единый источник времени для расчёта удержания
            if pos.status == PositionStatus.OPEN:
                self.open_positions.append(pos)
                if self.notifier:
                    self._notify_event(
                        "entry", self.notifier.entry_message(sig, self.config.dry_run))
                if self.store:
                    self.store.add({
                        "id": pos.id, "symbol": pos.symbol,
                        "exchange_high": pos.exchange_high, "exchange_low": pos.exchange_low,
                        "short_amount": pos.short_leg.filled_amount,
                        "long_amount": pos.long_leg.filled_amount,
                        "open_time": pos.open_time,
                    })
                return  # вошли — на этой итерации больше не открываем
            logger.warning("Вход %s не состоялся: %s", sig.symbol, pos.close_reason)

        if signals:
            self.summary.record_skip()

    async def _history_check(self, signal, now: float) -> tuple[bool, Optional[str]]:
        """Вход «было вместе -> сейчас разошлось» (по требованию).

        Берём пару в работу, только если ОБА условия выполнены:
          1) сейчас разошлось: текущий сырой спред >= check_spread (напр. 1%);
          2) исторически шли вместе: за последние N дней на дневных свечах
             минимумы/максимумы цены обеих ног расходятся не более чем на
             max_divergence (напр. 0.3%) — значит это один и тот же актив.

        Если require_divergence=True (по умолчанию), сверка ОБЯЗАТЕЛЬНА для всех
        входов: пара с текущим спредом ниже check_spread или без исторических
        данных отклоняется. Если проверка выключена (enabled=false) — всегда True.
        """
        cfg = self.history_cfg
        if not cfg or not cfg.get("enabled", False):
            return True, None
        check_spread = cfg.get("check_spread", 0.01)
        require = cfg.get("require_divergence", True)
        if signal.raw_spread < check_spread:
            # ещё не разошлось до порога
            if require:
                return False, (f"текущий спред {signal.raw_spread:.4f} < {check_spread} "
                               "(пара ещё не разошлась)")
            return True, None

        # В режиме предфильтра тождественность уже подтверждена заранее
        # (prequalify_universe) — на входе свечи повторно не тянем.
        if cfg.get("prefilter", True):
            return True, None

        timeframe = cfg.get("timeframe", "1d")
        days = int(cfg.get("days", 10))
        max_div = cfg.get("max_divergence", 0.003)

        oh_h, oh_l = await asyncio.gather(
            self.md.update_ohlcv(signal.exchange_high, signal.symbol, timeframe, days, now=now),
            self.md.update_ohlcv(signal.exchange_low, signal.symbol, timeframe, days, now=now),
            return_exceptions=True,
        )
        if isinstance(oh_h, Exception) or isinstance(oh_l, Exception) or not oh_h or not oh_l:
            return False, "нет исторических данных для сверки"

        div = historical_price_divergence(oh_h, oh_l)
        if div is None:
            return False, "недостаточно свечей для сверки"
        if div > max_div:
            return False, (f"историческое расхождение {div:.4f} > {max_div} "
                           "(вероятно разные активы)")
        logger.info("История %s: расхождение уровней %.4f ≤ %.4f — активы тождественны",
                    signal.symbol, div, max_div)
        return True, None

    async def _free_margin(self, *exchanges: str) -> dict[str, float]:
        """Свободная маржа (USDT) по биржам.

        В dry_run считаем бюджет доступным (бюджет per-exchange из риска). В боевом
        режиме — читаем реальный свободный баланс (fetch_balance)."""
        if self.config.dry_run:
            cap = self.risk.max_position_per_exchange
            return {ex: cap for ex in exchanges}
        result: dict[str, float] = {}
        for ex in exchanges:
            client = self.connectors[ex].client
            try:
                bal = await client.fetch_balance()
                usdt = bal.get("USDT", {}) if isinstance(bal.get("USDT"), dict) else {}
                free = usdt.get("free")
                if free is None:
                    free = (bal.get("free", {}) or {}).get("USDT", 0)
                result[ex] = float(free or 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Баланс %s недоступен: %s", ex, exc)
                result[ex] = 0.0
        return result

    async def _total_balance(self) -> tuple[float, dict]:
        """Суммарный баланс USDT по всем биржам (equity). Для сводки после закрытия."""
        total = 0.0
        parts: dict[str, Optional[float]] = {}
        for name, conn in self.connectors.items():
            try:
                bal = await conn.client.fetch_balance()
                usdt = bal.get("USDT", {}) if isinstance(bal.get("USDT"), dict) else {}
                eq = usdt.get("total")
                if eq is None:
                    eq = (bal.get("total", {}) or {}).get("USDT", 0)
                parts[name] = float(eq or 0)
                total += parts[name]
            except Exception as exc:  # noqa: BLE001
                logger.warning("Баланс %s недоступен: %s", name, exc)
                parts[name] = None
        return total, parts

    def _app_name(self) -> str:
        return getattr(self.notifier, "app_name", "Бот") if self.notifier else "Бот"

    def _notify_event(self, category: str, text: str) -> None:
        """Отправить уведомление с учётом политики (пункт 9).

        В боевом режиме (dry_run=false) при telegram.live_only_trades=true шлём
        ТОЛЬКО события открытия/закрытия позиций и КРИТИЧНЫЕ алерты о балансе;
        прочие (старт, аномалии, диагностика) — не шлём. В dry_run шлём всё.
        """
        # Всегда шлём: сделки (entry/close) и баланс-алерт — важное исключение.
        always = ("entry", "close", "balance")
        tg = self.config.telegram or {}
        live_only = tg.get("live_only_trades", True)
        if not self.config.dry_run and live_only and category not in always:
            return
        self._notify(text)

    def _notify(self, text: str) -> None:
        """Отправить уведомление в фоне (не блокируя цикл; ошибки глушатся)."""
        if not self.notifier or not getattr(self.notifier, "enabled", False):
            return
        task = asyncio.create_task(self.notifier.send(text))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ---- WS-стриминг ----
    def _raw_map(self, exchange: str, symbols: list) -> tuple[list, dict]:
        """Список биржевых символов и обратная карта raw->нормализованный."""
        conn = self.connectors[exchange]
        raw_list, raw_map = [], {}
        for s in symbols:
            raw = conn.contracts[s].raw_symbol if s in conn.contracts else s
            raw_list.append(raw)
            raw_map[raw] = s
        return raw_list, raw_map

    def _ingest_bbo(self, exchange: str, raw_map: dict, data: dict) -> None:
        """Разложить пачку BBO/тикеров {raw_symbol -> ticker} в кэш котировок.

        Берём только наши кандидатные символы (в all-market стриме приходят все
        пары биржи — чужие игнорируем)."""
        if not data:
            return
        for raw, ticker in data.items():
            symbol = raw_map.get(raw)
            if symbol is None:
                continue
            quote = parse_ticker(exchange, symbol, ticker or {})
            if quote is not None:
                self.md.quotes[(exchange, symbol)] = quote

    @staticmethod
    def _is_unsupported(exc: Exception) -> bool:
        """Похоже ли исключение на «метод не поддержан для этого рынка»."""
        msg = str(exc).lower()
        return ("not support" in msg or "only support" in msg
                or "notsupported" in type(exc).__name__.lower())

    async def _stream_batch(self, exchange: str, symbols: list) -> None:
        """Один батчевый поток BBO/tickers на биржу с очередью способов.

        Пробуем по порядку: all-market bids_asks -> all-market tickers -> список
        символов -> стакан по паре. Это покрывает разные ограничения бирж (MEXC
        не даёт BBO для перпов; Bitget не тянет сотни подписок на одном коннекте).
        Одна задача на биржу — ccxt сам чанкует подписки внутри.
        """
        raw_list, raw_map = self._raw_map(exchange, symbols)
        client0 = self.connectors[exchange].client
        # Очередь попыток: сначала all-market (один стрим на весь рынок — нет лимита
        # подписок), затем явный список, затем стакан по паре. all-market пробуем и
        # на bids_asks, и на tickers, т.к. разные биржи поддерживают разное.
        attempts: list = []
        for m in ("bids_asks", "tickers"):
            if hasattr(client0, f"watch_{m}"):
                attempts.append((m, True))    # all-market
        for m in ("bids_asks", "tickers"):
            if hasattr(client0, f"watch_{m}"):
                attempts.append((m, False))   # явный список
        attempts.append(("order_book", False))

        idx = 0
        backoff = 1.0
        fails = 0
        while self._running:
            method, all_market = attempts[idx]
            if method == "order_book":
                logger.info("WS %s: перехожу на стакан по паре (%d пар)",
                            exchange, len(symbols))
                for s in symbols:
                    if not self._running:
                        return
                    self._stream_tasks.append(
                        asyncio.create_task(self._stream_quotes(exchange, s)))
                return
            client = self.connectors[exchange].client
            watch = getattr(client, f"watch_{method}")
            try:
                data = await watch(None if all_market else raw_list)
                self._ingest_bbo(exchange, raw_map, data)
                backoff = 1.0
                fails = 0
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                tn = type(exc).__name__.lower()
                needs_symbols = "argumentsrequired" in tn or "requires" in str(exc).lower()
                if needs_symbols or self._is_unsupported(exc):
                    idx = min(idx + 1, len(attempts) - 1)
                    logger.info("WS %s: %s(all_market=%s) не подходит -> следующий способ",
                                exchange, method, all_market)
                    backoff = 1.0
                    continue
                # прочая ошибка (обрыв 1006, лимит стримов): ретрай, а после серии
                # неудач переходим к следующему способу (напр. bitget не тянет список).
                fails += 1
                logger.warning("WS %s (%d пар, %s): %s", exchange, len(symbols), method, exc)
                if fails >= 5:
                    idx = min(idx + 1, len(attempts) - 1)
                    logger.info("WS %s: %s нестабилен -> следующий способ", exchange, method)
                    fails = 0
                    backoff = 1.0
                    continue
                await asyncio.sleep(min(backoff, 30.0))
                backoff *= 2

    async def _stream_quotes(self, exchange: str, symbol: str) -> None:
        """Фолбэк: стрим стакана по одной паре (если нет BBO/tickers)."""
        backoff = 1.0
        while self._running:
            try:
                await self.md.update_quote(exchange, symbol, use_ws=True)
                backoff = 1.0
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("WS %s %s: %s", exchange, symbol, exc)
                await asyncio.sleep(min(backoff, 30.0))
                backoff *= 2

    def _stream_method(self, exchange: str, prefer: str) -> str:
        """Выбрать метод стрима для биржи: bids_asks -> tickers -> order_book."""
        client = self.connectors[exchange].client
        if prefer in ("bids_asks", "tickers", "order_book"):
            # проверим, что метод реально есть; иначе деградируем
            if prefer != "order_book" and hasattr(client, f"watch_{prefer}"):
                return prefer
            if prefer == "order_book":
                return "order_book"
        if hasattr(client, "watch_bids_asks"):
            return "bids_asks"
        if hasattr(client, "watch_tickers"):
            return "tickers"
        return "order_book"

    async def _funding_loop(self, interval: float) -> None:
        """Периодически обновляет funding по всем кандидатам (меняется медленно)."""
        while self._running:
            tasks = [self.md.update_funding(ex, symbol)
                     for symbol, cand in self.candidates.items()
                     for ex in cand.exchanges]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(interval)

    async def _launch_exchange_streams(
        self, exchange: str, symbols: list, subscribe_delay: float,
        symbols_per_stream: int, prefer_method: str,
    ) -> None:
        """Поднять стримы одной биржи: один батч BBO/tickers, либо стакан по паре.

        Для BBO/tickers — одна задача на биржу (ccxt чанкует подписки сам). Для
        стакана-фолбэка — по одной паре со стаггером subscribe_delay, чтобы не
        словить лимит частоты подписок.
        """
        client = self.connectors[exchange].client
        has_batch = hasattr(client, "watch_bids_asks") or hasattr(client, "watch_tickers")
        logger.info("WS %s: %d пар, режим=%s", exchange, len(symbols),
                    "batch" if has_batch else "order_book")
        if has_batch:
            self._stream_tasks.append(
                asyncio.create_task(self._stream_batch(exchange, symbols)))
            return
        # order_book: по одной паре со стаггером
        for symbol in symbols:
            if not self._running:
                return
            self._stream_tasks.append(
                asyncio.create_task(self._stream_quotes(exchange, symbol)))
            if subscribe_delay > 0:
                await asyncio.sleep(subscribe_delay)

    async def start_streams(
        self, funding_interval: float = 300.0, subscribe_delay: float = 0.3,
        symbols_per_stream: int = 100, method: str = "auto",
    ) -> None:
        """Запустить фоновые WS-стримы котировок + периодический funding.

        По умолчанию используем батчевый BBO (watch_bids_asks): один поток на группу
        пар вместо стакана на каждую пару — кратно меньше подписок, без ошибок
        checksum стакана и лимита "request too many".
        """
        self._running = True
        self._stream_tasks = []

        by_exchange: dict[str, list] = {}
        for symbol, cand in self.candidates.items():
            for ex in cand.exchanges:
                by_exchange.setdefault(ex, []).append(symbol)

        for ex, symbols in by_exchange.items():
            self._stream_tasks.append(
                asyncio.create_task(self._launch_exchange_streams(
                    ex, symbols, subscribe_delay, symbols_per_stream, method)))
        self._stream_tasks.append(
            asyncio.create_task(self._funding_loop(funding_interval)))
        total = sum(len(s) for s in by_exchange.values())
        logger.info("Поднимаю WS-стримы: %d пар по %d биржам (батч=%d, пауза=%.2fс)",
                    total, len(by_exchange), symbols_per_stream, subscribe_delay)

    async def stop_streams(self) -> None:
        self._running = False
        await asyncio.sleep(0)  # дать лаунчерам увидеть _running=False и выйти
        tasks = list(self._stream_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stream_tasks = []

    # ---- диагностика (heartbeat) ----
    def log_diagnostics(self, now: Optional[float] = None) -> None:
        """Показать, что бот реально сканирует: сколько пар с котировками на ≥2
        биржах и какие сейчас максимальные спреды (даже ниже порога входа).

        Это отвечает на вопрос «почему нет входов»: видно покрытие данными и то,
        насколько текущие спреды далеки от порога.
        """
        now = now if now is not None else self._clock()
        max_age = (self.scanner.max_quote_age_ms or 10000)
        fresh_pairs = 0
        spreads: list = []
        for symbol, cand in self.candidates.items():
            quotes = {}
            for ex in cand.exchanges:
                q = self.md.get_quote(ex, symbol)
                if q is None:
                    continue
                if q.timestamp is not None and now * 1000 - q.timestamp > max_age:
                    continue
                quotes[ex] = q
            if len(quotes) < 2:
                continue
            fresh_pairs += 1
            exs = list(quotes)
            best, bh, bl = 0.0, None, None
            for h in exs:
                for l in exs:
                    if h == l:
                        continue
                    r = raw_spread(quotes[h].bid, quotes[l].ask)
                    if r > best:
                        best, bh, bl = r, h, l
            if bh:
                spreads.append((best, symbol, bh, bl, quotes))
        spreads.sort(key=lambda x: x[0], reverse=True)

        # Для топ-спредов показываем, ПОЧЕМУ они не входят (какой фильтр отсёк).
        parts = []
        for r, symbol, h, l, quotes in spreads[:5]:
            ev = self.scanner.evaluate_pair(
                symbol, h, l, quotes[h], quotes[l],
                self.md.get_funding(h, symbol), self.md.get_funding(l, symbol),
                now=now, track_persistence=False,
            )
            status = "ВХОД ✓" if ev.passed else "; ".join(ev.reasons)
            parts.append(f"{symbol} {r*100:.2f}% ({h}->{l}) [{status}]")
        top = " | ".join(parts) or "—"
        cov = f"{fresh_pairs}/{len(self.candidates)}"
        logger.info("Диагностика: пар с котировками на ≥2 биржах: %s\n  топ: %s",
                    cov, top)

    # ---- главный цикл ----
    async def run(self, iterations: Optional[int] = None, interval: float = 1.0,
                  use_ws: bool = True, warmup: float = 5.0,
                  funding_interval: float = 300.0, subscribe_delay: float = 0.3,
                  symbols_per_stream: int = 100, method: str = "auto",
                  stats_interval: float = 20.0) -> None:
        await self.refresh_universe()
        await self.refresh_fees()
        await self.startup_reconcile()
        await self.prequalify_universe(
            concurrency=int((self.history_cfg or {}).get("prefilter_concurrency", 20)))
        if self.notifier:
            self._notify_event("startup", self.notifier.startup_message(
                list(self.connectors.keys()), len(self.candidates), self.config.dry_run))
        if use_ws:
            await self._run_streaming(iterations, interval, warmup, funding_interval,
                                      subscribe_delay, symbols_per_stream, method,
                                      stats_interval)
        else:
            await self._run_rest(iterations, interval)
        logger.info(self.summary.render())

    async def _run_streaming(self, iterations, interval, warmup, funding_interval,
                             subscribe_delay, symbols_per_stream, method,
                             stats_interval: float = 20.0) -> None:
        """Цикл на WS: стримы в фоне непрерывно освежают кэш, цикл сканирует кэш."""
        await self.start_streams(funding_interval, subscribe_delay,
                                 symbols_per_stream, method)
        try:
            if warmup:
                await asyncio.sleep(warmup)  # дать стримам наполнить кэш
            i = 0
            last_stats = self._clock()
            self.log_diagnostics()  # первая диагностика сразу после прогрева
            while iterations is None or i < iterations:
                if self.risk.killed and not self.open_positions:
                    logger.info("Kill-switch: открытых позиций нет — стоп")
                    break
                await self.poll_once()
                now = self._clock()
                if stats_interval and now - last_stats >= stats_interval:
                    self.log_diagnostics(now)
                    last_stats = now
                i += 1
                if iterations is None or i < iterations:
                    await asyncio.sleep(interval)
        finally:
            await self.stop_streams()

    async def _run_rest(self, iterations, interval) -> None:
        """Цикл на REST: перед каждым проходом обходим биржи запросами (медленно)."""
        i = 0
        while iterations is None or i < iterations:
            if self.risk.killed and not self.open_positions:
                logger.info("Kill-switch: открытых позиций нет — стоп")
                break
            await self.refresh_market_data(use_ws=False)
            await self.poll_once()
            i += 1
            if iterations is None or i < iterations:
                await asyncio.sleep(interval)
