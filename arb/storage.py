"""Хранилище сделок в SQLite для анализа (что покупали, по какой цене, где потеряли).

Пишем максимум фактов по каждой сделке:
  - positions — арбитражная позиция целиком (сигнал, цены входа/выхода, комиссии,
    funding, итоговый P&L, причина закрытия);
  - orders    — КАЖДЫЙ выставленный ордер (вход/выход/выравнивание/откат) с
    запрошенным и исполненным объёмом, средней ценой и комиссией;
  - signals   — обнаруженные сигналы (в т.ч. отклонённые) с причиной отказа.

Этого достаточно, чтобы посчитать, куда уходят деньги: спред, комиссии,
слиппедж входа и выхода, funding.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("arb.storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    symbol TEXT, exchange_high TEXT, exchange_low TEXT,
    open_time REAL, close_time REAL, hold_seconds REAL,
    status TEXT, close_reason TEXT, leg_status TEXT,
    signal_bid_high REAL, signal_ask_low REAL,
    entry_raw_spread REAL, entry_net_spread REAL, entry_fee_cost REAL,
    short_entry_price REAL, long_entry_price REAL,
    short_close_price REAL, long_close_price REAL,
    exit_quote_ask_high REAL, exit_quote_bid_low REAL,
    base_amount REAL, notional REAL, leverage INTEGER,
    entry_fees REAL, close_fees REAL,
    funding_accrued REAL, equalize_pnl REAL,
    realized_pnl REAL, pnl_pct REAL,
    dry_run INTEGER, created_at REAL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT, role TEXT, exchange TEXT, symbol TEXT, side TEXT,
    requested_amount REAL, filled_amount REAL, avg_price REAL,
    fee_paid REAL, status TEXT, order_id TEXT, error TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, symbol TEXT, exchange_high TEXT, exchange_low TEXT,
    bid_high REAL, ask_low REAL,
    raw_spread REAL, net_spread REAL, fee_cost REAL,
    slippage_cost REAL, funding_income REAL,
    entered INTEGER, reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_pos ON orders(position_id);
CREATE INDEX IF NOT EXISTS idx_pos_symbol ON positions(symbol);
"""


class TradeDB:
    """SQLite-хранилище сделок. Ошибки записи не роняют бота (только лог)."""

    def __init__(self, path: str = "data/trades.db"):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- запись ----
    def record_position(self, pos, leverage: Optional[int] = None,
                        dry_run: bool = True) -> None:
        """Записать позицию и все её ордера."""
        try:
            row = _position_row(pos, leverage, dry_run)
            cols = ", ".join(row.keys())
            marks = ", ".join("?" for _ in row)
            self.conn.execute(
                f"INSERT OR REPLACE INTO positions ({cols}) VALUES ({marks})",
                list(row.values()))
            for leg in getattr(pos, "orders", []) or []:
                self._insert_order(pos.id, leg)
            self.conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось записать позицию %s: %s",
                           getattr(pos, "id", "?"), exc)

    def _insert_order(self, position_id: str, leg) -> None:
        self.conn.execute(
            "INSERT INTO orders (position_id, role, exchange, symbol, side,"
            " requested_amount, filled_amount, avg_price, fee_paid, status,"
            " order_id, error, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, getattr(leg, "role", "entry"), leg.exchange, leg.symbol,
             getattr(leg.side, "value", str(leg.side)), leg.amount, leg.filled_amount,
             leg.avg_price, leg.fee_paid, getattr(leg.status, "value", str(leg.status)),
             leg.order_id, leg.error, getattr(leg, "ts", None) or time.time()))

    def record_signal(self, sig, entered: bool, reject_reason: Optional[str] = None,
                      ts: Optional[float] = None) -> None:
        """Записать обнаруженный сигнал (в т.ч. отклонённый)."""
        try:
            self.conn.execute(
                "INSERT INTO signals (ts, symbol, exchange_high, exchange_low,"
                " bid_high, ask_low, raw_spread, net_spread, fee_cost,"
                " slippage_cost, funding_income, entered, reject_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts or time.time(), sig.symbol, sig.exchange_high, sig.exchange_low,
                 sig.bid_high, sig.ask_low, sig.raw_spread, sig.net_spread,
                 sig.fee_cost, sig.slippage_cost, sig.funding_income,
                 1 if entered else 0, reject_reason))
            self.conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось записать сигнал: %s", exc)

    # ---- чтение (для аналитики) ----
    def positions(self, only_closed: bool = True) -> list[dict]:
        q = "SELECT * FROM positions"
        if only_closed:
            q += " WHERE realized_pnl IS NOT NULL"
        q += " ORDER BY COALESCE(close_time, open_time, created_at)"
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def orders(self, position_id: Optional[str] = None) -> list[dict]:
        if position_id:
            rows = self.conn.execute(
                "SELECT * FROM orders WHERE position_id=? ORDER BY ts", (position_id,))
        else:
            rows = self.conn.execute("SELECT * FROM orders ORDER BY ts")
        return [dict(r) for r in rows.fetchall()]

    def signals(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows.fetchall()]


def _leg_price(pos, role: str, exchange: str) -> Optional[float]:
    for leg in getattr(pos, "orders", []) or []:
        if getattr(leg, "role", "") == role and leg.exchange == exchange:
            return leg.avg_price
    return None


def _position_row(pos, leverage: Optional[int], dry_run: bool) -> dict[str, Any]:
    s, l = pos.short_leg, pos.long_leg
    base_amount = min(s.filled_amount, l.filled_amount)
    notional = (s.avg_price or 0.0) * base_amount
    pnl = pos.realized_pnl
    pnl_pct = (pnl / notional * 100.0) if (pnl is not None and notional > 0) else None
    hold = None
    if pos.open_time and pos.close_time:
        hold = pos.close_time - pos.open_time
    sig = pos.signal
    entry_fees = s.fee_paid + l.fee_paid
    close_fees = sum(leg.fee_paid for leg in (getattr(pos, "orders", []) or [])
                     if getattr(leg, "role", "") == "exit")
    return {
        "id": pos.id,
        "symbol": pos.symbol,
        "exchange_high": pos.exchange_high,
        "exchange_low": pos.exchange_low,
        "open_time": pos.open_time,
        "close_time": pos.close_time,
        "hold_seconds": hold,
        "status": getattr(pos.status, "value", str(pos.status)),
        "close_reason": pos.close_reason,
        "leg_status": getattr(s.status, "value", str(s.status)),
        "signal_bid_high": sig.bid_high if sig else None,
        "signal_ask_low": sig.ask_low if sig else None,
        "entry_raw_spread": sig.raw_spread if sig else None,
        "entry_net_spread": sig.net_spread if sig else None,
        "entry_fee_cost": sig.fee_cost if sig else None,
        "short_entry_price": s.avg_price,
        "long_entry_price": l.avg_price,
        "short_close_price": _leg_price(pos, "exit", pos.exchange_high),
        "long_close_price": _leg_price(pos, "exit", pos.exchange_low),
        "exit_quote_ask_high": getattr(pos, "exit_quote_ask_high", None),
        "exit_quote_bid_low": getattr(pos, "exit_quote_bid_low", None),
        "base_amount": base_amount,
        "notional": notional,
        "leverage": leverage,
        "entry_fees": entry_fees,
        "close_fees": close_fees,
        "funding_accrued": pos.funding_accrued,
        "equalize_pnl": getattr(pos, "equalize_pnl", 0.0),
        "realized_pnl": pnl,
        "pnl_pct": pnl_pct,
        "dry_run": 1 if dry_run else 0,
        "created_at": time.time(),
    }
