"""Тесты хранилища сделок и аналитики."""

import pytest

from arb.analytics import (
    analyze,
    entry_slippage,
    entry_spread_actual,
    exit_slippage,
    exit_spread_actual,
)
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


def _exact_position(short_close: float, ex_high="bitget"):
    """Сделка с ТОЧНОЙ арифметикой: P&L выведен из цен, а не задан руками.

    Вход: шорт 101, лонг 100 (захвачено 1%). База = notional/цена = 0.1.
    """
    base = 0.1
    gross = (101.0 - short_close) * base          # шорт: прибыль при падении
    fees = 0.011 + 0.011
    return {
        "symbol": "ACE/USDT", "exchange_high": ex_high, "exchange_low": "gate",
        "entry_raw_spread": 0.0168,
        "signal_bid_high": 102.0, "signal_ask_low": 99.5,
        "short_entry_price": 101.0, "long_entry_price": 100.0,
        "short_close_price": short_close, "long_close_price": 100.0,
        "exit_quote_ask_high": short_close, "exit_quote_bid_low": 100.0,
        "notional": 10.0, "entry_fees": 0.011, "close_fees": 0.011,
        "funding_accrued": 0.0, "realized_pnl": gross - fees,
        "close_reason": "max_adverse", "hold_seconds": 700,
    }


def test_exit_spread_actual_detects_divergence():
    """Спред разошёлся дальше (101->102.5 при лонге на 100) -> отдали 2.5%."""
    assert exit_spread_actual(_exact_position(102.5)) == pytest.approx(0.025)
    # сошёлся полностью -> отдали 0
    assert exit_spread_actual(_exact_position(100.0)) == pytest.approx(0.0)


def test_entry_spread_actual():
    # реально захвачено: (101-100)/100 = 1%
    assert entry_spread_actual(_exact_position(100.0)) == pytest.approx(0.01)


def test_decomposition_balances():
    """Разложение должно сходиться с фактическим P&L (расхождение ~0).

    Регресс: раньше строки «отдано на выходе» не было, и по компонентам
    выходил ПЛЮС там, где фактически был минус.
    """
    rows = [_exact_position(102.5), _exact_position(100.2)]
    text = analyze(rows)
    line = [l for l in text.splitlines() if "расхождение модели" in l][0]
    val = float(line.split(":")[1].strip().rstrip("%)").replace("+", ""))
    assert abs(val) < 0.01          # сходится с точностью до сотых процента
    assert "отдано на выходе" in text


def test_analyze_flags_poisoning_exchange():
    """Одна биржа даёт почти весь убыток -> отчёт должен на неё указать."""
    rows = [_exact_position(102.5, "bitget") for _ in range(20)]
    rows += [_exact_position(100.5, "bybit") for _ in range(20)]
    text = analyze(rows)
    assert "БЕЗ bitget" in text
    assert "bitget даёт" in text


def test_analyze_reports_outliers():
    """Единичные катастрофические сделки выносятся отдельно и группируются по паре."""
    rows = [_exact_position(101.2) for _ in range(50)]      # мелкий фон
    big = _exact_position(140.0)                            # обвал: -3.9 за сделку
    big["symbol"] = "JCT/USDT"
    rows.append(big)
    text = analyze(rows)
    assert "Выбросы" in text
    assert "JCT/USDT: 1 шт" in text
    assert "БЕЗ выбросов" in text
    assert "нетождествен" in text or "не тождественны" in text


def test_analyze_does_not_blame_most_frequent_exchange():
    """Регресс: биржа, которая просто участвует чаще всех, не должна обвиняться.

    На реальных данных отчёт предлагал исключить binance — биржу с лучшей
    ликвидностью, — хотя убыток сидел в нескольких парах-выбросах.
    """
    rows = []
    for _ in range(60):                        # binance везде, убыток ровный
        rows.append(_exact_position(101.05, "bybit"))
    for _ in range(60):
        rows.append(_exact_position(101.05, "okx"))
    big = _exact_position(150.0, "gate")       # выброс тянет статистику
    big["symbol"] = "JCT/USDT"
    rows.append(big)
    text = analyze(rows)
    assert "БЕЗ gate" not in text              # выброс не повод обвинять биржу
    assert "Выбросы" in text


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