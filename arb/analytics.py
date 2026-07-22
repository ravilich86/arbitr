"""Анализ истории сделок: куда уходят деньги.

Главный вопрос — почему сделки в минус. Раскладываем результат на составляющие:
  сырой спред (что видели) → слиппедж входа → слиппедж выхода → комиссии →
  funding → фактический P&L.

Так сразу видно, что именно съедает прибыль: комиссии, проскальзывание на входе,
проскальзывание на выходе или расхождение спреда против нас.
"""

from __future__ import annotations

from typing import Optional


def _safe_div(a: float, b: float) -> Optional[float]:
    return (a / b) if b else None


def entry_slippage(pos: dict) -> Optional[float]:
    """Потери на входе как доля цены (сумма по обеим ногам).

    Шорт должен продаться по signal_bid_high — фактически хуже (ниже).
    Лонг должен купиться по signal_ask_low — фактически хуже (выше).
    """
    sb, sa = pos.get("signal_bid_high"), pos.get("signal_ask_low")
    se, le = pos.get("short_entry_price"), pos.get("long_entry_price")
    if not (sb and sa and se and le):
        return None
    short_slip = (sb - se) / sb          # >0 = продали дешевле, чем видели
    long_slip = (le - sa) / sa           # >0 = купили дороже, чем видели
    return short_slip + long_slip


def exit_slippage(pos: dict) -> Optional[float]:
    """Потери на выходе как доля цены (сумма по обеим ногам).

    Шорт откупаем по ask (факт хуже = дороже), лонг продаём по bid (факт хуже = дешевле).
    """
    qa, qb = pos.get("exit_quote_ask_high"), pos.get("exit_quote_bid_low")
    sc, lc = pos.get("short_close_price"), pos.get("long_close_price")
    if not (qa and qb and sc and lc):
        return None
    short_slip = (sc - qa) / qa          # >0 = откупили дороже, чем видели
    long_slip = (qb - lc) / qb           # >0 = продали дешевле, чем видели
    return short_slip + long_slip


def _avg(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:+.3f}%" if v is not None else "н/д"


def analyze(positions: list[dict]) -> str:
    """Собрать текстовый отчёт по закрытым позициям."""
    closed = [p for p in positions if p.get("realized_pnl") is not None]
    if not closed:
        return "Аналитика: закрытых сделок в базе нет."

    n = len(closed)
    pnls = [float(p["realized_pnl"]) for p in closed]
    total = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    lines = [
        "=== АНАЛИЗ СДЕЛОК ===",
        f"Сделок: {n} | прибыльных: {len(wins)} | убыточных: {len(losses)} | "
        f"винрейт: {len(wins) / n * 100:.1f}%",
        f"Суммарный P&L: {total:+.4f} USDT | средний: {total / n:+.4f} | "
        f"лучший: {max(pnls):+.4f} | худший: {min(pnls):+.4f}",
        "",
        "--- Куда уходят деньги (в среднем на сделку, % от цены) ---",
    ]

    raw = _avg([p.get("entry_raw_spread") for p in closed])
    ent_slip = _avg([entry_slippage(p) for p in closed])
    ex_slip = _avg([exit_slippage(p) for p in closed])
    lines.append(f"  Сырой спред при входе:      {_fmt_pct(raw)}")
    lines.append(f"  − слиппедж входа:           {_fmt_pct(ent_slip)}")
    lines.append(f"  − слиппедж выхода:          {_fmt_pct(ex_slip)}")

    # комиссии/funding в долях нотионала
    fee_fracs, fund_fracs, pnl_fracs = [], [], []
    for p in closed:
        notional = float(p.get("notional") or 0)
        if notional <= 0:
            continue
        fees = float(p.get("entry_fees") or 0) + float(p.get("close_fees") or 0)
        fee_fracs.append(fees / notional)
        fund_fracs.append(float(p.get("funding_accrued") or 0) / notional)
        pnl_fracs.append(float(p["realized_pnl"]) / notional)
    lines.append(f"  − комиссии (round-trip):    {_fmt_pct(_avg(fee_fracs))}")
    lines.append(f"  + funding:                  {_fmt_pct(_avg(fund_fracs))}")
    lines.append(f"  = фактический P&L:          {_fmt_pct(_avg(pnl_fracs))}")

    # по причинам закрытия
    by_reason: dict[str, list] = {}
    for p in closed:
        by_reason.setdefault(p.get("close_reason") or "?", []).append(
            float(p["realized_pnl"]))
    lines.append("")
    lines.append("--- По причинам закрытия ---")
    for reason, vals in sorted(by_reason.items(), key=lambda x: sum(x[1])):
        lines.append(f"  {reason}: {len(vals)} шт, P&L={sum(vals):+.4f}, "
                     f"средний={sum(vals) / len(vals):+.4f}")

    # худшие пары
    by_pair: dict[str, list] = {}
    for p in closed:
        by_pair.setdefault(p.get("symbol") or "?", []).append(float(p["realized_pnl"]))
    worst = sorted(by_pair.items(), key=lambda x: sum(x[1]))[:10]
    lines.append("")
    lines.append("--- Худшие пары ---")
    for sym, vals in worst:
        lines.append(f"  {sym}: {len(vals)} шт, P&L={sum(vals):+.4f}")

    # связки бирж
    by_route: dict[str, list] = {}
    for p in closed:
        route = f"{p.get('exchange_high')}→{p.get('exchange_low')}"
        by_route.setdefault(route, []).append(float(p["realized_pnl"]))
    lines.append("")
    lines.append("--- По связкам бирж ---")
    for route, vals in sorted(by_route.items(), key=lambda x: sum(x[1])):
        lines.append(f"  {route}: {len(vals)} шт, P&L={sum(vals):+.4f}")

    # время удержания
    holds = [float(p["hold_seconds"]) for p in closed if p.get("hold_seconds")]
    if holds:
        lines.append("")
        lines.append(f"Среднее время удержания: {sum(holds) / len(holds):.1f} сек "
                     f"(мин {min(holds):.0f}, макс {max(holds):.0f})")

    # вывод
    lines.append("")
    lines.append("--- Вывод ---")
    parts = []
    if ent_slip and raw and ent_slip > raw * 0.3:
        parts.append("слиппедж ВХОДА съедает значительную часть спреда")
    if ex_slip and raw and ex_slip > raw * 0.3:
        parts.append("слиппедж ВЫХОДА съедает значительную часть спреда")
    avg_fee = _avg(fee_fracs)
    if avg_fee and raw and avg_fee > raw * 0.3:
        parts.append("комиссии съедают значительную часть спреда")
    if not parts:
        parts.append("издержки умеренные — вероятно, спред расходился против нас "
                     "(смотри причины закрытия: stop_loss/max_adverse)")
    lines.append("  " + "; ".join(parts))
    return "\n".join(lines)
