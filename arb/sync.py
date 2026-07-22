"""Сверка наших записей с ФАКТИЧЕСКОЙ историей бирж.

Наша БД хранит то, что бот думает об исполнении. Источник истины — биржа:
`fetch_order` и `fetch_my_trades` дают реальную цену исполнения, реальную
комиссию и реальный размер. Сверка подтягивает эти факты в таблицу orders
(колонки actual_*), после чего аналитика может показать расхождение между
нашими записями и реальностью.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("arb.sync")


def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fee_from(obj: dict) -> tuple[Optional[float], Optional[str]]:
    """Комиссия из ccxt-структуры: fee {cost,currency} или список fees."""
    total, currency = 0.0, None
    found = False
    fee = obj.get("fee") or {}
    if isinstance(fee, dict) and fee.get("cost") is not None:
        c = _to_float(fee.get("cost"))
        if c is not None:
            total += c
            currency = fee.get("currency") or currency
            found = True
    for f in obj.get("fees") or []:
        if isinstance(f, dict) and f.get("cost") is not None:
            c = _to_float(f.get("cost"))
            if c is not None:
                total += c
                currency = f.get("currency") or currency
                found = True
    return (total if found else None), currency


def parse_order_fill(order: dict) -> Optional[dict]:
    """Факт исполнения из ccxt-ордера: {filled, avg_price, fee, fee_currency}."""
    if not isinstance(order, dict):
        return None
    filled = _to_float(order.get("filled"))
    avg = _to_float(order.get("average"))
    cost = _to_float(order.get("cost"))
    if avg is None and cost and filled:
        avg = cost / filled
    if not filled and avg is None:
        return None
    fee, currency = _fee_from(order)
    return {"filled": filled, "avg_price": avg, "fee": fee, "fee_currency": currency}


def aggregate_trades(trades: list) -> Optional[dict]:
    """Свести сделки одного ордера в VWAP-исполнение и суммарную комиссию."""
    amount_sum, cost_sum, fee_sum = 0.0, 0.0, 0.0
    currency = None
    has_fee = False
    for t in trades or []:
        amt = _to_float(t.get("amount")) or 0.0
        price = _to_float(t.get("price"))
        if amt <= 0 or price is None:
            continue
        amount_sum += amt
        cost_sum += amt * price
        fee, cur = _fee_from(t)
        if fee is not None:
            fee_sum += fee
            has_fee = True
            currency = cur or currency
    if amount_sum <= 0:
        return None
    return {
        "filled": amount_sum,
        "avg_price": cost_sum / amount_sum,
        "fee": fee_sum if has_fee else None,
        "fee_currency": currency,
    }


async def fetch_actual_fill(connector, raw_symbol: str,
                            order_id: str) -> Optional[dict]:
    """Достать факт исполнения ордера с биржи: fetch_order -> fetch_my_trades."""
    client = connector.client
    if hasattr(client, "fetch_order"):
        try:
            order = await client.fetch_order(order_id, raw_symbol)
            res = parse_order_fill(order)
            if res and (res["filled"] or res["avg_price"]):
                return res
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_order %s %s: %s", raw_symbol, order_id, exc)

    if hasattr(client, "fetch_my_trades"):
        try:
            trades = await client.fetch_my_trades(raw_symbol)
            matched = [t for t in (trades or [])
                       if str(t.get("order") or "") == str(order_id)]
            res = aggregate_trades(matched)
            if res:
                return res
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_my_trades %s: %s", raw_symbol, exc)
    return None


async def sync_fills(db, connectors: dict, limit: int = 500) -> dict:
    """Сверить несинхронизированные ордера с историей бирж.

    Объём с биржи приходит в контрактах — переводим в базовый актив через
    contractSize, чтобы он был сопоставим с нашими записями.
    """
    rows = db.unsynced_orders(limit)
    synced = 0
    for r in rows:
        conn = connectors.get(r["exchange"])
        if conn is None:
            continue
        meta = conn.contracts.get(r["symbol"])
        raw_symbol = meta.raw_symbol if meta else r["symbol"]
        cs = meta.contract_size if (meta and meta.contract_size) else 1.0
        res = await fetch_actual_fill(conn, raw_symbol, r["order_id"])
        if not res:
            continue
        filled_base = (res["filled"] * cs) if res["filled"] is not None else None
        db.update_order_actuals(r["id"], filled_base, res["avg_price"],
                                res["fee"], res["fee_currency"])
        synced += 1
    return {"checked": len(rows), "synced": synced}
