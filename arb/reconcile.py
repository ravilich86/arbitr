"""Реконсиляция и аварийное закрытие позиций.

Назначение (форс-мажор):
  - собрать РЕАЛЬНЫЕ открытые позиции со всех бирж (fetch_positions);
  - спарить их в дельта-нейтральные арб-пары (шорт на одной бирже + лонг на
    другой по тому же активу, сопоставимый объём);
  - найти «орфанов» — ноги без противоположной пары (незахеджированная
    направленная позиция, главный риск при сбое);
  - закрыть либо всё (паника), либо только орфанов.

Аккаунты выделены под бота и вручную не торгуются, поэтому источник истины —
сама биржа: пары восстанавливаются по факту позиций, локальное хранилище не
обязательно (см. PositionStore — опциональный аудит).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("arb.reconcile")


def _norm_symbol(ccxt_symbol: str) -> str:
    """'BTC/USDT:USDT' -> 'BTC/USDT' (единый ключ актива)."""
    return (ccxt_symbol or "").split(":")[0]


@dataclass
class PosView:
    """Нормализованное представление позиции с биржи."""

    exchange: str
    symbol: str          # нормализованный, напр. 'BTC/USDT'
    raw_symbol: str      # биржевой символ ccxt (для ордера закрытия)
    side: str            # 'long' | 'short'
    size: float          # объём в базовом активе (abs) — для сопоставления пар
    contracts: float = 0.0   # число контрактов (abs) — для ордера закрытия
    entry_price: Optional[float] = None

    def close_side(self) -> str:
        """Сторона рыночного ордера, закрывающего позицию."""
        return "buy" if self.side == "short" else "sell"


def normalize_position(exchange: str, p: dict) -> Optional[PosView]:
    """ccxt-позиция -> PosView. None, если позиция пустая."""
    if not isinstance(p, dict):
        return None
    contracts = p.get("contracts") or 0
    try:
        contracts = float(contracts or 0)
    except (TypeError, ValueError):
        contracts = 0.0
    if contracts == 0:
        return None
    cs = p.get("contractSize") or 1
    try:
        cs = float(cs)
    except (TypeError, ValueError):
        cs = 1.0
    side = p.get("side")
    if side not in ("long", "short"):
        # запасной вывод стороны по знаку contracts
        side = "long" if contracts > 0 else "short"
    raw_symbol = p.get("symbol") or ""
    entry = p.get("entryPrice")
    return PosView(
        exchange=exchange,
        symbol=_norm_symbol(raw_symbol),
        raw_symbol=raw_symbol,
        side=side,
        size=abs(contracts) * cs,
        contracts=abs(contracts),
        entry_price=float(entry) if entry else None,
    )


def pair_positions(
    views: list[PosView], size_tol: float = 0.05,
) -> tuple[list[tuple[PosView, PosView]], list[PosView]]:
    """Спарить позиции в арб-пары и вернуть (пары, орфаны).

    Пара = шорт и лонг по одному активу на РАЗНЫХ биржах с объёмом в пределах
    size_tol. Всё, что не спарилось (или сильно разошлось по объёму), — орфаны.
    """
    by_symbol: dict[str, list[PosView]] = {}
    for v in views:
        by_symbol.setdefault(v.symbol, []).append(v)

    pairs: list[tuple[PosView, PosView]] = []
    orphans: list[PosView] = []

    for symbol, group in by_symbol.items():
        shorts = [v for v in group if v.side == "short"]
        longs = [v for v in group if v.side == "long"]
        used = set()
        for s in shorts:
            match = None
            for i, l in enumerate(longs):
                if i in used or l.exchange == s.exchange:
                    continue
                denom = max(s.size, l.size)
                if denom > 0 and abs(s.size - l.size) / denom <= size_tol:
                    match = i
                    break
            if match is not None:
                used.add(match)
                pairs.append((s, longs[match]))
            else:
                orphans.append(s)
        for i, l in enumerate(longs):
            if i not in used:
                orphans.append(l)

    return pairs, orphans


async def fetch_all_positions(connectors: dict) -> list[PosView]:
    """Собрать все ненулевые позиции со всех бирж."""
    views: list[PosView] = []
    for name, conn in connectors.items():
        client = conn.client
        if not hasattr(client, "fetch_positions"):
            continue
        try:
            positions = await client.fetch_positions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось получить позиции %s: %s", name, exc)
            continue
        for p in positions or []:
            v = normalize_position(name, p)
            if v is not None:
                views.append(v)
    return views


async def close_positions(
    connectors: dict, views: list[PosView], execute: bool,
) -> list[tuple[PosView, str]]:
    """Закрыть переданные позиции reduce-only рыночным ордером.

    execute=False — только отчёт (что было бы закрыто), ордера не шлём.
    """
    results: list[tuple[PosView, str]] = []
    for v in views:
        if not execute:
            results.append((v, "reported"))
            continue
        client = connectors[v.exchange].client
        # Закрываем по ЧИСЛУ КОНТРАКТОВ (не в базовых единицах) — так ждут OKX и др.
        amount = v.contracts or v.size
        if hasattr(client, "amount_to_precision"):
            try:
                amount = float(client.amount_to_precision(v.raw_symbol, amount))
            except Exception:  # noqa: BLE001
                pass
        try:
            await client.create_order(
                v.raw_symbol, "market", v.close_side(), amount, None,
                {"reduceOnly": True})
            results.append((v, "closed"))
            logger.info("Закрыта позиция %s %s %s %g",
                        v.exchange, v.symbol, v.side, amount)
        except Exception as exc:  # noqa: BLE001
            results.append((v, f"error: {exc}"))
            logger.error("Ошибка закрытия %s %s: %s", v.exchange, v.symbol, exc)
    return results


async def close_all(connectors: dict, execute: bool) -> dict:
    """ПАНИКА: закрыть ВСЕ позиции на всех биржах."""
    views = await fetch_all_positions(connectors)
    logger.warning("CLOSE-ALL: найдено %d позиций, execute=%s", len(views), execute)
    results = await close_positions(connectors, views, execute)
    return {"total": len(views), "results": results}


async def reconcile(connectors: dict, execute: bool, size_tol: float = 0.05) -> dict:
    """Проверить парность позиций; закрыть орфанов (execute=True).

    Возвращает сводку: сколько согласованных пар и сколько орфанов (и их закрытие).
    """
    views = await fetch_all_positions(connectors)
    pairs, orphans = pair_positions(views, size_tol)
    logger.info("Реконсиляция: %d ног -> %d согласованных пар, %d орфанов",
                len(views), len(pairs), len(orphans))
    for o in orphans:
        logger.warning("ОРФАН (нет противоположной ноги): %s %s %s %.6f",
                       o.exchange, o.symbol, o.side, o.size)
    closed = await close_positions(connectors, orphans, execute) if orphans else []
    return {"pairs": pairs, "orphans": orphans, "closed": closed}
