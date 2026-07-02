"""Тесты оркестратора (§12): end-to-end dry_run прогон."""

import asyncio
from pathlib import Path

import pytest

from arb.bot import ArbitrageBot
from arb.config import Config, ExchangeConfig
from arb.exchanges import ExchangeConnector
from arb.executor import Executor
from arb.logger import SessionSummary, TradeLogger
from arb.marketdata import MarketData
from arb.models import Candidate, ContractMeta, PositionStatus, Quote
from arb.risk import RiskManager
from arb.scanner import Scanner
from tests.fixtures import (
    MockBBOClient,
    MockDegradeClient,
    MockMarketClient,
    MockTradeClient,
)


def _meta(ex):
    return ContractMeta(ex, "BTC/USDT", "BTC/USDT:USDT", "BTC", "USDT",
                        step_size=0.001, min_amount=0.001, min_notional=5.0, max_leverage=100)


def _config(tmp_path: Path) -> Config:
    return Config(
        dry_run=True, testnet=False,
        exchanges={
            "h": ExchangeConfig("h", True, 0.0005),
            "l": ExchangeConfig("l", True, 0.0005),
        },
        spread={"min_gross_spread": 0.005, "min_net_spread": 0.002,
                "exit_spread": 0.0, "take_profit": None, "min_spread_persistence": 0.0},
        sizing={"notional_target": 2000.0, "leverage": 20, "max_slippage": 0.001,
                "margin_mode": "isolated"},
        execution={"order_type": "market", "on_leg_failure": "rollback"},
        risk={"max_concurrent_positions": 1, "max_position_per_exchange": 1000.0,
              "max_hold_time": 3600, "max_adverse_spread": 0.02,
              "liquidation_buffer": 0.03, "cooldown": 300},
        logging={"trades_log": str(tmp_path / "trades.jsonl")},
    )


def _bot(tmp_path: Path) -> ArbitrageBot:
    cfg = _config(tmp_path)
    ch = ExchangeConnector("h", MockTradeClient("fill"))
    cl = ExchangeConnector("l", MockTradeClient("fill"))
    ch.contracts = {"BTC/USDT": _meta("h")}
    cl.contracts = {"BTC/USDT": _meta("l")}
    connectors = {"h": ch, "l": cl}
    fees = {"h": 0.0005, "l": 0.0005}
    bot = ArbitrageBot(
        cfg, connectors, MarketData(connectors),
        Scanner(fees=fees, min_gross_spread=0.005, min_net_spread=0.002,
                max_slippage=0.001, min_spread_persistence=0.0),
        Executor(connectors, fees, dry_run=True),
        RiskManager(max_concurrent_positions=1, max_position_per_exchange=1000.0,
                    cooldown=300, leverage=20),
        TradeLogger(str(tmp_path / "trades.jsonl")),
        SessionSummary(),
    )
    bot.candidates = {"BTC/USDT": Candidate("BTC/USDT",
                      {"h": _meta("h"), "l": _meta("l")})}
    return bot


async def test_bot_opens_on_signal(tmp_path):
    bot = _bot(tmp_path)
    # расхождение: H дороже (bid 101), L дешевле (ask 100)
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 101.0, 101.1, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=0)
    await bot.poll_once(now=1000)
    assert len(bot.open_positions) == 1
    assert bot.open_positions[0].status == PositionStatus.OPEN
    assert bot.open_positions[0].exchange_high == "h"


async def test_bot_full_cycle_pnl(tmp_path):
    bot = _bot(tmp_path)
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 101.0, 101.1, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=0)
    await bot.poll_once(now=1000)
    assert len(bot.open_positions) == 1

    # схождение цен -> выход по target
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 100.0, 100.1, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 100.2, 100.3, timestamp=0)
    await bot.poll_once(now=1001)
    assert len(bot.open_positions) == 0
    assert bot.summary.trades == 1
    assert bot.summary.total_pnl > 0
    # запись сделки записана
    lines = Path(bot.trade_logger.path).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


async def test_bot_max_hold_time_forces_exit(tmp_path):
    bot = _bot(tmp_path)
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 101.0, 101.1, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=0)
    await bot.poll_once(now=1000)
    # спред не сошёлся, но истекло max_hold_time
    await bot.poll_once(now=1000 + 4000)
    assert len(bot.open_positions) == 0
    assert bot.open_positions == []
    row = Path(bot.trade_logger.path).read_text(encoding="utf-8")
    assert "max_hold_time" in row


async def test_bot_kill_switch_blocks_entry(tmp_path):
    bot = _bot(tmp_path)
    bot.risk.trip_kill_switch("test")
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 101.0, 101.1, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=0)
    await bot.poll_once(now=1000)
    assert len(bot.open_positions) == 0


def _hist_candles(low, high):
    return [[i, low, high, low, high, 1] for i in range(10)]


def _enable_history(bot, ohlcv_h, ohlcv_l):
    bot.history_cfg = {"enabled": True, "check_spread": 0.01,
                       "timeframe": "1d", "days": 10, "max_divergence": 0.003}
    bot.connectors["h"].client.ohlcv = ohlcv_h
    bot.connectors["l"].client.ohlcv = ohlcv_l


def _wide_quotes(bot):
    # сырой спред ~2% (> check_spread 1%)
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 102.0, 102.1, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=0)


async def test_history_accepts_matching_asset(tmp_path):
    bot = _bot(tmp_path)
    _enable_history(bot, _hist_candles(100.0, 110.0), _hist_candles(100.1, 110.1))
    _wide_quotes(bot)
    await bot.poll_once(now=1000)
    assert len(bot.open_positions) == 1  # уровни совпадают -> вошли


async def test_history_rejects_divergent_asset(tmp_path):
    bot = _bot(tmp_path)
    _enable_history(bot, _hist_candles(100.0, 110.0), _hist_candles(200.0, 220.0))
    _wide_quotes(bot)
    await bot.poll_once(now=1000)
    assert len(bot.open_positions) == 0  # разные активы -> пропуск
    assert bot.summary.skipped_signals >= 1


async def test_history_rejects_when_no_data(tmp_path):
    bot = _bot(tmp_path)
    _enable_history(bot, [], [])  # свечей нет
    _wide_quotes(bot)
    await bot.poll_once(now=1000)
    assert len(bot.open_positions) == 0


async def test_history_rejects_small_spread_when_required(tmp_path):
    bot = _bot(tmp_path)
    # сверка обязательна; текущий спред ~0.6% < check_spread(1%) -> ещё не разошлось
    _enable_history(bot, _hist_candles(100.0, 110.0), _hist_candles(100.1, 110.1))
    # спред ~0.9% (< check_spread 1%) -> ещё не разошлось
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 100.9, 101.0, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=0)
    await bot.poll_once(now=1000)
    assert len(bot.open_positions) == 0  # не разошлось до порога -> не входим


async def test_history_soft_mode_allows_small_spread(tmp_path):
    bot = _bot(tmp_path)
    # require_divergence=false: мелкий спред (~0.9%) проходит без исторической сверки
    _enable_history(bot, [], [])
    bot.history_cfg["require_divergence"] = False
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 100.9, 101.0, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=0)
    await bot.poll_once(now=1000)
    assert len(bot.open_positions) == 1


async def test_bot_cooldown_blocks_reentry(tmp_path):
    bot = _bot(tmp_path)
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 101.0, 101.1, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=0)
    await bot.poll_once(now=1000)
    # закрыть по схождению
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 100.0, 100.1, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 100.2, 100.3, timestamp=0)
    await bot.poll_once(now=1001)
    assert len(bot.open_positions) == 0
    # снова расхождение сразу — cooldown должен заблокировать вход
    bot.md.quotes[("h", "BTC/USDT")] = Quote("h", "BTC/USDT", 101.0, 101.1, timestamp=0)
    bot.md.quotes[("l", "BTC/USDT")] = Quote("l", "BTC/USDT", 99.9, 100.0, timestamp=0)
    await bot.poll_once(now=1002)
    assert len(bot.open_positions) == 0
    assert bot.summary.skipped_signals >= 1


async def test_ws_streams_populate_cache(tmp_path):
    ob = {"bids": [[100.0, 5]], "asks": [[100.5, 4]], "timestamp": 1}
    ch = ExchangeConnector("h", MockMarketClient(order_books={"BTC/USDT": ob}))
    cl = ExchangeConnector("l", MockMarketClient(order_books={"BTC/USDT": ob}))
    connectors = {"h": ch, "l": cl}
    fees = {"h": 0.0005, "l": 0.0005}
    bot = ArbitrageBot(
        _config(tmp_path), connectors, MarketData(connectors),
        Scanner(fees=fees), Executor(connectors, fees, dry_run=True),
        RiskManager(), TradeLogger(str(tmp_path / "trades.jsonl")), SessionSummary(),
    )
    bot.candidates = {"BTC/USDT": Candidate("BTC/USDT", {"h": _meta("h"), "l": _meta("l")})}

    await bot.start_streams(funding_interval=999)
    await asyncio.sleep(0.05)          # дать стримам обновить кэш
    await bot.stop_streams()

    assert bot.md.get_quote("h", "BTC/USDT") is not None
    assert bot.md.get_quote("l", "BTC/USDT").bid == 100.0
    assert bot._stream_tasks == []     # задачи остановлены


async def test_ws_bbo_streams_populate_cache(tmp_path):
    # Батчевый BBO: watch_bids_asks отдаёт лучший bid/ask пачкой
    bbo = {"BTC/USDT:USDT": {"bid": 100.0, "ask": 100.5, "timestamp": 1}}
    ch = ExchangeConnector("h", MockBBOClient(bbo))
    cl = ExchangeConnector("l", MockBBOClient(bbo))
    ch.contracts = {"BTC/USDT": _meta("h")}
    cl.contracts = {"BTC/USDT": _meta("l")}
    connectors = {"h": ch, "l": cl}
    fees = {"h": 0.0005, "l": 0.0005}
    bot = ArbitrageBot(
        _config(tmp_path), connectors, MarketData(connectors),
        Scanner(fees=fees), Executor(connectors, fees, dry_run=True),
        RiskManager(), TradeLogger(str(tmp_path / "trades.jsonl")), SessionSummary(),
    )
    bot.candidates = {"BTC/USDT": Candidate("BTC/USDT", {"h": _meta("h"), "l": _meta("l")})}

    assert bot._stream_method("h", "auto") == "bids_asks"
    await bot.start_streams(funding_interval=999, subscribe_delay=0)
    await asyncio.sleep(0.05)
    await bot.stop_streams()

    q = bot.md.get_quote("h", "BTC/USDT")
    assert q is not None and q.bid == 100.0 and q.ask == 100.5


async def test_ws_degrades_to_orderbook_when_bbo_unsupported(tmp_path):
    # MEXC-случай: watch_bids_asks/tickers не поддержаны для перпов -> стакан
    ob = {"bids": [[100.0, 5]], "asks": [[100.5, 4]], "timestamp": 1}
    ch = ExchangeConnector("h", MockDegradeClient(ob))
    ch.contracts = {"BTC/USDT": _meta("h")}
    connectors = {"h": ch}
    fees = {"h": 0.0005}
    bot = ArbitrageBot(
        _config(tmp_path), connectors, MarketData(connectors),
        Scanner(fees=fees), Executor(connectors, fees, dry_run=True),
        RiskManager(), TradeLogger(str(tmp_path / "trades.jsonl")), SessionSummary(),
    )
    bot.candidates = {"BTC/USDT": Candidate("BTC/USDT", {"h": _meta("h")})}

    await bot.start_streams(funding_interval=999, subscribe_delay=0)
    await asyncio.sleep(0.05)
    await bot.stop_streams()

    q = bot.md.get_quote("h", "BTC/USDT")
    assert q is not None and q.bid == 100.0  # данные пришли через фолбэк-стакан
