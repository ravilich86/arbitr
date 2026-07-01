"""Логирование (§10): технический лог + машиночитаемый лог сделок и сводка.

Два потока:
  1. app.log — запуск, подключения, ошибки, разрывы, rate-limit, отклонённые
     сигналы с причиной.
  2. trades (jsonl|csv) — по каждой арбитражной сделке одна запись со всеми
     числами: спреды, объёмы, цены/комиссии по ногам, funding, итоговый P&L
     и причина закрытия, leg-status.

Плюс сводка за сессию: число сделок, винрейт, суммарный/средний P&L, пропуски.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .models import Position, PositionStatus


# --------------------------------------------------------------------------
#  Технический лог (§10.1)
# --------------------------------------------------------------------------
def setup_app_logger(
    log_path: str = "logs/app.log", level: str = "INFO", to_console: bool = True,
) -> logging.Logger:
    """Настроить корневой логгер приложения (файл + консоль)."""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("arb")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if to_console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    logger.propagate = False
    return logger


# --------------------------------------------------------------------------
#  Запись сделки (§10.2)
# --------------------------------------------------------------------------
TRADE_FIELDS = [
    "trade_id", "open_time", "close_time", "symbol",
    "exchange_high", "exchange_low",
    "entry_raw_spread", "entry_net_spread", "exit_spread",
    "base_amount", "notional", "leverage",
    "short_entry_price", "short_close_price", "short_fee",
    "long_entry_price", "long_close_price", "long_fee",
    "funding_accrued", "pnl_usdt", "pnl_pct", "close_reason", "leg_status",
]


def position_to_trade_row(pos: Position, leverage: Optional[int] = None,
                          exit_spread: Optional[float] = None) -> dict[str, Any]:
    """Собрать плоскую запись сделки из позиции (§10.2)."""
    s, l = pos.short_leg, pos.long_leg
    base_amount = min(s.filled_amount, l.filled_amount)
    entry_notional = (s.avg_price or 0.0) * base_amount
    pnl = pos.realized_pnl
    pnl_pct = None
    if pnl is not None and entry_notional > 0:
        pnl_pct = pnl / entry_notional * 100.0

    if s.status.value == "closed" and l.status.value == "closed":
        leg_status = "both_ok"
    elif pos.status == PositionStatus.UNHEDGED:
        leg_status = "unhedged"
    elif pos.status == PositionStatus.FAILED:
        leg_status = "rollback"
    else:
        leg_status = pos.status.value

    return {
        "trade_id": pos.id,
        "open_time": pos.open_time,
        "close_time": pos.close_time,
        "symbol": pos.symbol,
        "exchange_high": pos.exchange_high,
        "exchange_low": pos.exchange_low,
        "entry_raw_spread": pos.signal.raw_spread if pos.signal else None,
        "entry_net_spread": pos.signal.net_spread if pos.signal else None,
        "exit_spread": exit_spread,
        "base_amount": base_amount,
        "notional": entry_notional,
        "leverage": leverage,
        "short_entry_price": s.avg_price,
        "short_close_price": None,
        "short_fee": s.fee_paid,
        "long_entry_price": l.avg_price,
        "long_close_price": None,
        "long_fee": l.fee_paid,
        "funding_accrued": pos.funding_accrued,
        "pnl_usdt": pnl,
        "pnl_pct": pnl_pct,
        "close_reason": pos.close_reason,
        "leg_status": leg_status,
    }


class TradeLogger:
    """Пишет записи сделок в jsonl или csv (§10.2)."""

    def __init__(self, path: str = "logs/trades.jsonl", fmt: Optional[str] = None):
        self.path = path
        self.fmt = fmt or ("csv" if path.endswith(".csv") else "jsonl")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._csv_header_written = os.path.exists(path) and os.path.getsize(path) > 0

    def log_trade(self, row: dict[str, Any]) -> None:
        if self.fmt == "csv":
            self._write_csv(row)
        else:
            self._write_jsonl(row)

    def log_position(self, pos: Position, leverage: Optional[int] = None,
                     exit_spread: Optional[float] = None) -> dict[str, Any]:
        row = position_to_trade_row(pos, leverage, exit_spread)
        self.log_trade(row)
        return row

    def _write_jsonl(self, row: dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _write_csv(self, row: dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
            if not self._csv_header_written:
                writer.writeheader()
                self._csv_header_written = True
            writer.writerow({k: row.get(k) for k in TRADE_FIELDS})


# --------------------------------------------------------------------------
#  Сводка за сессию (§10)
# --------------------------------------------------------------------------
@dataclass
class SessionSummary:
    """Агрегированная статистика сессии (§10)."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    skipped_signals: int = 0
    pnls: list[float] = field(default_factory=list)

    def record_trade(self, pnl: Optional[float]) -> None:
        if pnl is None:
            return
        self.trades += 1
        self.total_pnl += pnl
        self.pnls.append(pnl)
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

    def record_skip(self) -> None:
        self.skipped_signals += 1

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0

    @property
    def avg_pnl(self) -> float:
        return (self.total_pnl / self.trades) if self.trades else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(self.win_rate, 2),
            "total_pnl": round(self.total_pnl, 6),
            "avg_pnl": round(self.avg_pnl, 6),
            "skipped_signals": self.skipped_signals,
        }

    def render(self) -> str:
        d = self.as_dict()
        return (
            f"Сводка сессии: сделок={d['trades']} винрейт={d['win_rate_pct']}% "
            f"P&L={d['total_pnl']} USDT средний={d['avg_pnl']} "
            f"пропущено сигналов={d['skipped_signals']}"
        )
