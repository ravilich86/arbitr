"""Тесты логирования и сводок (§10)."""

import json
from pathlib import Path

import pytest

from arb.logger import (
    SessionSummary,
    TradeLogger,
    position_to_trade_row,
    setup_app_logger,
    summarize_trades,
)
from arb.models import ArbSignal, Leg, LegStatus, Position, PositionStatus, Side


def _closed_position(pnl=5.0):
    s = Leg("h", "BTC/USDT", Side.SHORT, 10, filled_amount=10, avg_price=101.0,
            fee_paid=0.5, status=LegStatus.CLOSED)
    l = Leg("l", "BTC/USDT", Side.LONG, 10, filled_amount=10, avg_price=100.0,
            fee_paid=0.5, status=LegStatus.CLOSED)
    sig = ArbSignal("BTC/USDT", "h", "l", 101.0, 100.0, raw_spread=0.01, net_spread=0.005)
    return Position("t1", "BTC/USDT", "h", "l", s, l, signal=sig,
                    status=PositionStatus.CLOSED, open_time=1000, close_time=1100,
                    close_reason="target", realized_pnl=pnl)


def test_position_to_trade_row():
    row = position_to_trade_row(_closed_position(5.0), leverage=20)
    assert row["symbol"] == "BTC/USDT"
    assert row["pnl_usdt"] == 5.0
    assert row["leg_status"] == "both_ok"
    assert row["close_reason"] == "target"
    assert row["base_amount"] == 10
    assert row["pnl_pct"] > 0


def test_trade_logger_jsonl(tmp_path: Path):
    path = tmp_path / "trades.jsonl"
    tl = TradeLogger(str(path))
    tl.log_position(_closed_position(3.0), leverage=20)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["pnl_usdt"] == 3.0
    assert rec["exchange_high"] == "h"


def test_trade_logger_csv(tmp_path: Path):
    path = tmp_path / "trades.csv"
    tl = TradeLogger(str(path), fmt="csv")
    tl.log_position(_closed_position(1.0), leverage=20)
    tl.log_position(_closed_position(-2.0), leverage=20)
    content = path.read_text(encoding="utf-8").strip().splitlines()
    assert content[0].startswith("trade_id,")  # заголовок
    assert len(content) == 3  # header + 2 строки


def test_session_summary():
    s = SessionSummary()
    s.record_trade(5.0)
    s.record_trade(-2.0)
    s.record_trade(3.0)
    s.record_skip()
    s.record_skip()
    assert s.trades == 3
    assert s.wins == 2 and s.losses == 1
    assert s.total_pnl == 6.0
    assert s.win_rate == pytest.approx(66.67, abs=0.01)
    assert s.avg_pnl == pytest.approx(2.0)
    assert s.skipped_signals == 2
    assert "Сводка сессии" in s.render()


def test_summarize_trades(tmp_path: Path):
    p = tmp_path / "trades.jsonl"
    p.write_text(
        '{"symbol":"BTC/USDT","exchange_high":"gate","exchange_low":"binance",'
        '"pnl_usdt":1.5,"close_reason":"target","leg_status":"both_ok"}\n'
        '{"symbol":"BTC/USDT","pnl_usdt":-0.5,"close_reason":"stop_loss"}\n',
        encoding="utf-8")
    s = summarize_trades(str(p))
    assert "всего=2" in s
    assert "прибыльных=1" in s and "убыточных=1" in s
    assert "BTC/USDT" in s


def test_summarize_trades_empty(tmp_path: Path):
    s = summarize_trades(str(tmp_path / "nope.jsonl"))
    assert "пуст" in s


def test_setup_app_logger(tmp_path: Path):
    log_path = tmp_path / "app.log"
    logger = setup_app_logger(str(log_path), level="DEBUG", to_console=False)
    logger.info("тестовое сообщение")
    for h in logger.handlers:
        h.flush()
    assert log_path.exists()
    assert "тестовое сообщение" in log_path.read_text(encoding="utf-8")
