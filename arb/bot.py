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
from .exchanges import ExchangeConnector
from .executor import Executor, current_spread, estimate_open_pnl, should_exit
from .logger import SessionSummary, TradeLogger
from .marketdata import MarketData
from .models import Position, PositionStatus
from .risk import RiskManager
from .scanner import Scanner, historical_price_divergence
from .universe import build_universe

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
        self._clock = clock or _time.time

        self.candidates: dict = {}
        self.open_positions: list[Position] = []
        # Историческая сверка тождественности при большом текущем спреде
        self.history_cfg = (config.raw.get("history") or {}) if config.raw else {}
        # WS-стриминг
        self._running = False
        self._stream_tasks: list = []

    # ---- построение вселенной (§3–4) ----
    async def refresh_universe(self) -> None:
        contracts = {}
        for name, conn in self.connectors.items():
            contracts[name] = await conn.load_perp_contracts()
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
                await self.executor.close_position(pos, qh, ql, reason or "target")
                self.risk.register_close(pos.symbol, now)
                row = self.trade_logger.log_position(
                    pos, leverage=self.risk.leverage, exit_spread=cur)
                self.summary.record_trade(pos.realized_pnl)
                logger.info("Закрыта %s: причина=%s P&L=%s", pos.symbol, reason,
                            pos.realized_pnl)
            else:
                still_open.append(pos)
        self.open_positions = still_open

    # ---- скан и вход (§6–9) ----
    async def _scan_and_enter(self, now: float) -> None:
        active = [p for p in self.open_positions
                  if p.status in (PositionStatus.OPEN, PositionStatus.OPENING)]
        if len(active) >= self.risk.max_concurrent_positions:
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
            free_margin = self._free_margin(sig.exchange_high, sig.exchange_low)
            decision = self.risk.can_open(
                sig.symbol, self.open_positions, margin_required, free_margin,
                (sig.exchange_high, sig.exchange_low), now,
            )
            if not decision.allowed:
                logger.info("Сигнал %s отклонён риском: %s", sig.symbol, decision.reason)
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

    def _free_margin(self, *exchanges: str) -> dict[str, float]:
        """Свободная маржа по биржам. В dry_run считаем бюджет доступным."""
        cap = self.risk.max_position_per_exchange
        return {ex: cap for ex in exchanges}

    # ---- WS-стриминг ----
    async def _stream_quotes(self, exchange: str, symbol: str) -> None:
        """Фоновый стрим стакана по (биржа, символ): watch_order_book в цикле.

        watch_order_book сам ждёт следующего апдейта, поэтому цикл паузится
        естественно. Ошибки/разрывы логируем и переподключаемся с backoff.
        """
        backoff = 1.0
        while self._running:
            try:
                await self.md.update_quote(exchange, symbol, use_ws=True)
                backoff = 1.0
                # Гарантированно уступаем управление циклу событий: даже если
                # watch вернулся мгновенно, не монополизируем поток (и позволяем
                # доставить отмену задачи при остановке).
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - разрыв WS/ошибка биржи
                logger.warning("WS %s %s: %s", exchange, symbol, exc)
                await asyncio.sleep(min(backoff, 30.0))
                backoff *= 2

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
    ) -> None:
        """Постепенно поднимать подписки одной биржи, чтобы не словить
        «request too many»: между подписками выдерживаем subscribe_delay."""
        for symbol in symbols:
            if not self._running:
                return
            self._stream_tasks.append(
                asyncio.create_task(self._stream_quotes(exchange, symbol)))
            if subscribe_delay > 0:
                await asyncio.sleep(subscribe_delay)

    async def start_streams(
        self, funding_interval: float = 300.0, subscribe_delay: float = 0.1,
    ) -> None:
        """Запустить фоновые WS-стримы котировок + периодический funding.

        Подписки поднимаются НЕ залпом, а порционно (per-exchange лаунчеры с паузой
        subscribe_delay), иначе биржи отбивают поток subscribe-запросов лимитом
        «request too many» (напр. Bitget code 30006).
        """
        self._running = True
        self._stream_tasks = []

        by_exchange: dict[str, list] = {}
        for symbol, cand in self.candidates.items():
            for ex in cand.exchanges:
                by_exchange.setdefault(ex, []).append(symbol)

        for ex, symbols in by_exchange.items():
            self._stream_tasks.append(
                asyncio.create_task(
                    self._launch_exchange_streams(ex, symbols, subscribe_delay)))
        self._stream_tasks.append(
            asyncio.create_task(self._funding_loop(funding_interval)))
        total = sum(len(s) for s in by_exchange.values())
        logger.info("Поднимаю WS-подписки: %d по %d биржам (пауза %.3fс между подписками)",
                    total, len(by_exchange), subscribe_delay)

    async def stop_streams(self) -> None:
        self._running = False
        await asyncio.sleep(0)  # дать лаунчерам увидеть _running=False и выйти
        tasks = list(self._stream_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stream_tasks = []

    # ---- главный цикл ----
    async def run(self, iterations: Optional[int] = None, interval: float = 1.0,
                  use_ws: bool = True, warmup: float = 5.0,
                  funding_interval: float = 300.0, subscribe_delay: float = 0.1) -> None:
        await self.refresh_universe()
        if use_ws:
            await self._run_streaming(iterations, interval, warmup,
                                      funding_interval, subscribe_delay)
        else:
            await self._run_rest(iterations, interval)
        logger.info(self.summary.render())

    async def _run_streaming(self, iterations, interval, warmup, funding_interval,
                             subscribe_delay) -> None:
        """Цикл на WS: стримы в фоне непрерывно освежают кэш, цикл сканирует кэш."""
        await self.start_streams(funding_interval, subscribe_delay)
        try:
            if warmup:
                await asyncio.sleep(warmup)  # дать стримам наполнить кэш
            i = 0
            while iterations is None or i < iterations:
                if self.risk.killed and not self.open_positions:
                    logger.info("Kill-switch: открытых позиций нет — стоп")
                    break
                await self.poll_once()
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
