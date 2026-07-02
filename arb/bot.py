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
from .scanner import Scanner
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
            max_contract_size_ratio=universe_cfg.get("max_contract_size_ratio", 50.0),
        )
        self.candidates = res.candidates
        logger.info("Вселенная: %d кандидатов, %d подозрительных, %d single-exchange",
                    len(res.candidates), len(res.suspicious), len(res.single_exchange))

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

        best = None
        for symbol, cand in self.candidates.items():
            quotes = {ex: q for ex in cand.exchanges
                      if (q := self.md.get_quote(ex, symbol)) is not None}
            if len(quotes) < 2:
                continue
            funding = {ex: f for ex in cand.exchanges
                       if (f := self.md.get_funding(ex, symbol)) is not None}
            sig = self.scanner.scan_symbol(symbol, quotes, funding, now=now)
            if sig and (best is None or sig.net_spread > best.net_spread):
                best = sig

        if best is None:
            return

        margin_required = best.notional / max(self.risk.leverage, 1)
        free_margin = self._free_margin(best.exchange_high, best.exchange_low)
        decision = self.risk.can_open(
            best.symbol, self.open_positions, margin_required, free_margin,
            (best.exchange_high, best.exchange_low), now,
        )
        if not decision.allowed:
            self.summary.record_skip()
            logger.info("Сигнал %s отклонён риском: %s", best.symbol, decision.reason)
            return

        mode = "DRY-RUN" if self.config.dry_run else "LIVE"
        logger.info("[%s] Вход %s: H=%s L=%s raw=%.4f net=%.4f",
                    mode, best.symbol, best.exchange_high, best.exchange_low,
                    best.raw_spread, best.net_spread)
        pos = await self.executor.open_position(best)
        pos.open_time = now  # единый источник времени для расчёта удержания
        if pos.status == PositionStatus.OPEN:
            self.open_positions.append(pos)
        else:
            self.summary.record_skip()
            logger.warning("Вход %s не состоялся: %s", best.symbol, pos.close_reason)

    def _free_margin(self, *exchanges: str) -> dict[str, float]:
        """Свободная маржа по биржам. В dry_run считаем бюджет доступным."""
        cap = self.risk.max_position_per_exchange
        return {ex: cap for ex in exchanges}

    # ---- главный цикл ----
    async def run(self, iterations: Optional[int] = None, interval: float = 1.0,
                  use_ws: bool = True) -> None:
        await self.refresh_universe()
        i = 0
        while iterations is None or i < iterations:
            if self.risk.killed and not self.open_positions:
                logger.info("Kill-switch: новых сделок нет, открытых позиций нет — стоп")
                break
            await self.refresh_market_data(use_ws=use_ws)
            await self.poll_once()
            i += 1
            if iterations is None or i < iterations:
                await asyncio.sleep(interval)
        logger.info(self.summary.render())
