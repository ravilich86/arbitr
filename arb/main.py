"""Точка входа: сборка бота из конфигурации и запуск цикла (§12).

Использование:
    python -m arb.main --config config.yaml            # боевой/dry_run цикл
    python -m arb.main --config config.yaml --iterations 5   # ограниченный прогон

Ключи берутся из .env; при dry_run=true реальные ордера не отправляются.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .bot import ArbitrageBot
from .config import Config, load_config
from .exchanges import create_connectors
from .executor import Executor
from .logger import TradeLogger, setup_app_logger
from .marketdata import MarketData
from .risk import RiskManager
from .scanner import PersistenceTracker, Scanner


def build_bot(config: Config, connectors=None) -> ArbitrageBot:
    """Собрать ArbitrageBot из конфигурации (connectors можно внедрить для тестов)."""
    if connectors is None:
        connectors = create_connectors(config)

    fees = {name: ex.taker_fee for name, ex in config.exchanges.items()}

    sp = config.spread
    sz = config.sizing
    ex_cfg = config.execution
    rk = config.risk

    scanner = Scanner(
        fees=fees,
        min_gross_spread=sp.get("min_gross_spread", 0.005),
        min_net_spread=sp.get("min_net_spread", 0.002),
        max_slippage=sz.get("max_slippage", 0.001),
        min_spread_persistence=sp.get("min_spread_persistence", 0.0),
        notional_target=sz.get("notional_target", 2000.0),
        hold_hours=rk.get("max_hold_time", 3600) / 3600.0,
        max_gross_spread=sp.get("max_gross_spread", 0.05),
        max_quote_age_ms=sp.get("max_quote_age_ms"),
        persistence=PersistenceTracker(),
    )

    executor = Executor(
        connectors=connectors,
        fees=fees,
        dry_run=config.dry_run,
        order_type=ex_cfg.get("order_type", "market"),
        on_leg_failure=ex_cfg.get("on_leg_failure", "rollback"),
        leg_timeout=ex_cfg.get("leg_timeout", 5.0),
        leverage=sz.get("leverage", 20),
        margin_mode=sz.get("margin_mode", "isolated"),
    )

    risk = RiskManager(
        max_concurrent_positions=rk.get("max_concurrent_positions", 1),
        max_position_per_exchange=rk.get("max_position_per_exchange", 100.0),
        liquidation_buffer=rk.get("liquidation_buffer", 0.03),
        cooldown=rk.get("cooldown", 300.0),
        leverage=sz.get("leverage", 20),
    )

    md = MarketData(connectors)
    trade_logger = TradeLogger(
        config.logging.get("trades_log", "logs/trades.jsonl"))

    return ArbitrageBot(config, connectors, md, scanner, executor, risk, trade_logger)


def _install_ws_noise_filter(log) -> None:
    """Приглушить фоновые ошибки WS-соединений (обрывы 1006, «never retrieved»),
    чтобы они не спамили консоль. Реальные проблемы всё равно логируются в стримах."""
    loop = asyncio.get_running_loop()

    def handler(_loop, context):
        exc = context.get("exception")
        name = type(exc).__name__ if exc else ""
        msg = context.get("message", "")
        if name in ("NetworkError", "RequestTimeout") or "never retrieved" in msg \
                or "Connection closed" in str(exc):
            log.debug("WS фоновая ошибка подавлена: %s %s", name, msg)
            return
        _loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


async def _run(args) -> None:
    config = load_config(args.config)
    log = setup_app_logger(
        config.logging.get("app_log", "logs/app.log"),
        level=config.logging.get("level", "INFO"),
    )
    _install_ws_noise_filter(log)
    enabled = list(config.enabled_exchanges.keys())
    log.info("Старт. dry_run=%s testnet=%s биржи=%s", config.dry_run, config.testnet, enabled)
    if not enabled:
        log.error("Нет включённых бирж в config.yaml — нечего запускать.")
        return

    bot = build_bot(config)
    ws_cfg = config.raw.get("ws", {}) or {}
    try:
        await bot.run(
            iterations=args.iterations, interval=args.interval,
            use_ws=not args.rest,
            warmup=ws_cfg.get("warmup", 5.0),
            funding_interval=ws_cfg.get("funding_interval", 300.0),
            subscribe_delay=ws_cfg.get("subscribe_delay", 0.3),
            symbols_per_stream=ws_cfg.get("symbols_per_stream", 100),
            method=ws_cfg.get("method", "auto"),
        )
    finally:
        for conn in bot.connectors.values():
            await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Межбиржевой фьючерсный арбитраж")
    parser.add_argument("--config", default="config.yaml", help="путь к config.yaml")
    parser.add_argument("--iterations", type=int, default=None,
                        help="число итераций цикла (по умолчанию бесконечно)")
    parser.add_argument("--interval", type=float, default=1.0, help="пауза между итерациями, сек")
    parser.add_argument("--rest", action="store_true", help="использовать REST вместо WS")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Остановлено пользователем.")


if __name__ == "__main__":
    main()
