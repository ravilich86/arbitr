"""Тесты хранилища сделок и аналитики."""

import pytest

from arb.analytics import analyze, entry_slippage, exit_slippage
from arb.models import ArbSignal, Leg, LegStatus, Position, PositionStatus, Side
from arb.storage import TradeDB


def _closed_position(pnl=1.5, sym="BTC/USDT", pid="p1"):
    s = Leg("gate", sym, Side.SHORT, 10, filled_amount=10, avg_price=100.0,
            fee_paid=0.5, status=LegStatus.CLOSED, role="entry")
    l = Leg("binance", sym, Side.LONG, 10, filled_amount=10, avg_price=99.0,
            fee_paid=0.5, status=LegStatus.CLOSED, role="entry")
    close_s = Leg("gate", sym, Side.LONG, 10, filled_amount=10, avg_price=99.6,
                  fee_paid=0.5, status=LegStatus.CLOSED, role="exit")
    close_l = Leg("binance", sym, Side.SHORT, 10, filled_amount=10, avg_price=99.4,
                  fee_paid=0.5, status=LegStatus.CLOSED, role="exit")
    sig = ArbSignal(sym, "gate", "binance", bid_high=100.5, ask_low=98.8,
                    raw_spread=0.017, net_spread=0.012, fee_cost=0.002)
    pos = Position(pid, sym, "gate", "binance", s, l, signal=sig,
                   status=PositionStatus.CLOSED, open_time=1000, close_time=1060,
                   close_reason="take_profit", realized_pnl=pnl)
    pos.orders = [s, l, close_s, close_l]
    pos.exit_quote_ask_high = 99.5
    pos.exit_quote_bid_low = 99.5
    return pos


def test_db_records_position_and_orders(tmp_path):
    db = TradeDB(str(tmp_path / "t.db"))
    db.record_position(_closed_position(), leverage=20, dry_run=False)
    rows = db.positions()
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "BTC/USDT"
    assert r["realized_pnl"] == 1.5
    assert r["short_entry_price"] == 100.0
    assert r["short_close_price"] == 99.6      # взято из ордера с ролью exit
    assert r["entry_fees"] == 1.0
    assert r["close_fees"] == 1.0
    orders = db.orders("p1")
    assert len(orders) == 4
    assert {o["role"] for o in orders} == {"entry", "exit"}
    db.close()


def test_db_records_signal(tmp_path):
    db = TradeDB(str(tmp_path / "t.db"))
    sig = ArbSignal("X/USDT", "a", "b", 10.0, 9.8, raw_spread=0.02, net_spread=0.01)
    db.record_signal(sig, entered=False, reject_reason="нет баланса")
    rows = db.signals()
    assert len(rows) == 1
    assert rows[0]["entered"] == 0
    assert rows[0]["reject_reason"] == "нет баланса"
    db.close()


def test_entry_and_exit_slippage():
    pos = _closed_position()
    row = {
        "signal_bid_high": 100.5, "signal_ask_low": 98.8,
        "short_entry_price": 100.0, "long_entry_price": 99.0,
        "exit_quote_ask_high": 99.5, "exit_quote_bid_low": 99.5,
        "short_close_price": 99.6, "long_close_price": 99.4,
    }
    # вход: продали дешевле (100.0 vs 100.5) и купили дороже (99.0 vs 98.8)
    assert entry_slippage(row) > 0
    # выход: откупили дороже (99.6 vs 99.5) и продали дешевле (99.4 vs 99.5)
    assert exit_slippage(row) > 0


def test_analyze_report(tmp_path):
    db = TradeDB(str(tmp_path / "t.db"))
    db.record_position(_closed_position(pnl=1.0, sym="AAA/USDT", pid="p1"), 20, False)
    db.record_position(_closed_position(pnl=-2.0, sym="BBB/USDT", pid="p2"), 20, False)
    text = analyze(db.positions())
    assert "АНАЛИЗ СДЕЛОК" in text
    assert "Сделок: 2" in text
    assert "BBB/USDT" in text          # худшая пара попала в отчёт
    assert "слиппедж входа" in text
    db.close()


def test_analyze_empty():
    assert "нет" in analyze([])