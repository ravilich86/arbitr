"""Тесты Telegram-уведомлений."""

import pytest

from arb.models import ArbSignal, Leg, LegStatus, Position, PositionStatus, Side
from arb.notifier import TelegramNotifier, build_notifier


def _notifier():
    return TelegramNotifier("tok", "123", app_name="Арбитраж-бот", enabled=True)


def test_entry_message_contains_key_info():
    n = _notifier()
    sig = ArbSignal("BTC/USDT", "bybit", "bitget", bid_high=101.5, ask_low=100.0,
                    raw_spread=0.015, net_spread=0.011)
    msg = n.entry_message(sig, dry_run=True)
    assert "Арбитраж-бот" in msg          # метка приложения
    assert "BTC/USDT" in msg
    assert "bybit" in msg and "bitget" in msg
    assert "1.50%" in msg                 # сырой спред
    assert "DRY-RUN" in msg


def test_close_message_shows_pnl_and_reason():
    n = _notifier()
    s = Leg("bybit", "BTC/USDT", Side.SHORT, 10, status=LegStatus.CLOSED)
    l = Leg("bitget", "BTC/USDT", Side.LONG, 10, status=LegStatus.CLOSED)
    pos = Position("t1", "BTC/USDT", "bybit", "bitget", s, l,
                   status=PositionStatus.CLOSED, close_reason="target", realized_pnl=3.25)
    msg = n.close_message(pos, dry_run=False)
    assert "+3.2500" in msg
    assert "target" in msg
    assert "LIVE" in msg


def test_startup_message():
    n = _notifier()
    msg = n.startup_message(["binance", "bybit"], pairs=42, dry_run=True)
    assert "42" in msg and "binance" in msg and "Арбитраж-бот" in msg


async def test_send_disabled_when_no_token():
    n = TelegramNotifier(None, None, enabled=True)
    assert n.enabled is False
    assert await n.send("hi") is False


def test_build_notifier_from_config():
    n = build_notifier({"enabled": True, "token": "t", "chat_id": "c", "app_name": "X"})
    assert n is not None and n.enabled is True and n.app_name == "X"


def test_build_notifier_disabled():
    n = build_notifier({"enabled": False, "token": "t", "chat_id": "c"})
    assert n.enabled is False
