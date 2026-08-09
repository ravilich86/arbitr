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


def entry_spread_actual(pos: dict) -> Optional[float]:
    """Спред, РЕАЛЬНО захваченный на входе (по фактическим ценам исполнения).

    Отличается от сигнального сырого спреда на величину слиппеджа входа.
    """
    se, le = pos.get("short_entry_price"), pos.get("long_entry_price")
    if not (se and le):
        return None
    return (se - le) / le


def exit_spread_actual(pos: dict) -> Optional[float]:
    """Спред, ОТДАННЫЙ на выходе (по фактическим ценам закрытия).

    Шорт откупаем на H, лонг продаём на L. Если расхождение сошлось — цены
    сравнялись и спред ≈ 0, мы забираем захваченное на входе. Если разошлось
    дальше (max_adverse) — здесь будет крупная положительная величина, и именно
    она съедает результат. Без этой строки разложение не сходится с P&L.
    """
    sc, lc = pos.get("short_close_price"), pos.get("long_close_price")
    if not (sc and lc):
        return None
    return (sc - lc) / lc


def _avg(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v * 100:+.3f}%" if v is not None else "н/д"


def compare_report(orders: list[dict]) -> str:
    """Сверка наших записей с фактом биржи (после --sync-fills).

    Показывает, врут ли наши цены/комиссии/объёмы. Если расхождения близки к нулю —
    записи верны, и потери реальны (исполнение/спред). Если большие — проблема в
    том, что мы записываем, и аналитике верить нельзя.
    """
    synced = [o for o in orders if o.get("actual_avg_price") or o.get("actual_filled")]
    if not synced:
        return ("Сверка с биржами: нет синхронизированных ордеров "
                "(запусти: python -m arb.main --sync-fills)")

    price_diffs, fee_diffs, amt_diffs = [], [], []
    for o in synced:
        rec_p, act_p = o.get("avg_price"), o.get("actual_avg_price")
        if rec_p and act_p:
            price_diffs.append((act_p - rec_p) / rec_p)
        rec_f, act_f = o.get("fee_paid"), o.get("actual_fee")
        if act_f is not None and rec_f is not None:
            fee_diffs.append(act_f - rec_f)
        rec_a, act_a = o.get("filled_amount"), o.get("actual_filled")
        if rec_a and act_a:
            amt_diffs.append((act_a - rec_a) / rec_a)

    lines = [
        "=== СВЕРКА С ИСТОРИЕЙ БИРЖ ===",
        f"Сверено ордеров: {len(synced)} из {len(orders)}",
        f"  Расхождение ЦЕНЫ (факт vs наша запись):  среднее {_fmt_pct(_avg(price_diffs))}"
        f", макс {_fmt_pct(max(price_diffs, key=abs) if price_diffs else None)}",
        f"  Расхождение ОБЪЁМА:                      среднее {_fmt_pct(_avg(amt_diffs))}",
    ]
    if fee_diffs:
        lines.append(f"  Расхождение КОМИССИИ (факт − наша):       "
                     f"сумма {sum(fee_diffs):+.6f}, среднее {_avg(fee_diffs):+.6f}")
    worst = sorted(
        (o for o in synced if o.get("avg_price") and o.get("actual_avg_price")),
        key=lambda o: abs((o["actual_avg_price"] - o["avg_price"]) / o["avg_price"]),
        reverse=True)[:5]
    if worst:
        lines.append("  Худшие расхождения по цене:")
        for o in worst:
            d = (o["actual_avg_price"] - o["avg_price"]) / o["avg_price"]
            lines.append(f"    {o['exchange']} {o['symbol']} [{o['role']}]: "
                         f"наша {o['avg_price']:g} vs факт {o['actual_avg_price']:g} "
                         f"({d * 100:+.3f}%)")
    avg_price_diff = _avg(price_diffs)
    lines.append("")
    if avg_price_diff is not None and abs(avg_price_diff) < 0.0005:
        lines.append("  Вывод: наши записи совпадают с биржей — потери реальные "
                     "(исполнение/спред), а не ошибка учёта.")
    else:
        lines.append("  Вывод: записи РАСХОДЯТСЯ с биржей — сначала чиним учёт цен, "
                     "аналитике P&L пока верить нельзя.")
    return "\n".join(lines)


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
    ent_act = _avg([entry_spread_actual(p) for p in closed])
    ex_act = _avg([exit_spread_actual(p) for p in closed])

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
    fee = _avg(fee_fracs)
    fund = _avg(fund_fracs)
    pnl_pct = _avg(pnl_fracs)

    lines.append(f"  Сырой спред при входе:      {_fmt_pct(raw)}")
    lines.append(f"  − слиппедж входа:           {_fmt_pct(ent_slip)}")
    lines.append(f"  = захвачено на входе:       {_fmt_pct(ent_act)}")
    # Ключевая строка: если расхождение не сошлось (а разошлось дальше),
    # здесь будет крупная величина — она и съедает результат.
    lines.append(f"  − отдано на выходе:         {_fmt_pct(ex_act)}")
    lines.append(f"  − комиссии (round-trip):    {_fmt_pct(fee)}")
    lines.append(f"  + funding:                  {_fmt_pct(fund)}")
    lines.append(f"  = фактический P&L:          {_fmt_pct(pnl_pct)}")

    # Контроль сходимости: если остаток не близок к нулю, часть издержек не
    # учтена и выводам по разложению верить нельзя.
    if None not in (ent_act, ex_act, fee, fund, pnl_pct):
        residual = pnl_pct - (ent_act - ex_act - fee + fund)
        lines.append(f"  (расхождение модели:        {_fmt_pct(residual)})")

    ex_slip = _avg([exit_slippage(p) for p in closed])
    if ex_slip is not None:
        lines.append(f"  справочно, слиппедж выхода: {_fmt_pct(ex_slip)}")

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

    # По БИРЖАМ (а не связкам): показывает, не отравляет ли статистику одна
    # площадка. Сделка учитывается обеим ногам, поэтому сумма больше общего P&L.
    # ВЫБРОСЫ: единичные сделки с катастрофическим убытком. Обычно это признак,
    # что пара на деле не тождественна (или случилось реальное событие) — цены
    # уезжают рывком далеко за лимит по спреду. Такие сделки перекашивают всю
    # статистику, поэтому показываем их отдельно и исключаем из выводов.
    avg_abs = sum(abs(x) for x in pnls) / n
    outliers = [p for p in closed
                if float(p["realized_pnl"]) < -5 * avg_abs] if avg_abs > 0 else []
    if outliers:
        out_sum = sum(float(p["realized_pnl"]) for p in outliers)
        lines.append("")
        lines.append("--- Выбросы (катастрофические сделки) ---")
        # Группируем по паре: одна и та же пара обычно даёт серию выбросов.
        by_out: dict[str, list] = {}
        for p in outliers:
            by_out.setdefault(p.get("symbol") or "?", []).append(p)
        for sym, ps in sorted(by_out.items(),
                              key=lambda x: sum(float(p["realized_pnl"]) for p in x[1]))[:10]:
            s = sum(float(p["realized_pnl"]) for p in ps)
            notional = float(ps[0].get("notional") or 0)
            pct = f", {s / len(ps) / notional * 100:+.1f}% нотионала за сделку" \
                if notional else ""
            routes = {f"{p.get('exchange_high')}→{p.get('exchange_low')}" for p in ps}
            lines.append(f"  {sym}: {len(ps)} шт, {s:+.4f}{pct} "
                         f"({', '.join(sorted(routes))})")
        lines.append(f"  ИТОГО {len(outliers)} шт из {n} = {out_sum:+.4f} "
                     f"({out_sum / total * 100:.0f}% всего убытка)" if total else "")
        rest_pnls = [x for p, x in zip(closed, pnls) if p not in outliers]
        if rest_pnls:
            lines.append(f"  БЕЗ выбросов: {len(rest_pnls)} шт, "
                         f"P&L={sum(rest_pnls):+.4f}, "
                         f"средний={sum(rest_pnls) / len(rest_pnls):+.4f}")

    by_ex: dict[str, list] = {}
    excluded_ex, excluded_rest = None, None
    for p in closed:
        for ex in (p.get("exchange_high"), p.get("exchange_low")):
            if ex:
                by_ex.setdefault(ex, []).append(float(p["realized_pnl"]))
    if by_ex:
        lines.append("")
        lines.append("--- По биржам (участие в сделке) ---")
        for ex, vals in sorted(by_ex.items(), key=lambda x: sum(x[1])):
            share = sum(vals) / total * 100 if total else 0.0
            lines.append(f"  {ex}: {len(vals)} шт, P&L={sum(vals):+.4f}, "
                         f"средний={sum(vals) / len(vals):+.4f} ({share:.0f}% от общего)")
        # Какую биржу выгоднее всего исключить? Ранжируем не по сумме (её набирает
        # та, что просто участвует чаще), а по тому, насколько исключение улучшает
        # СРЕДНИЙ результат остальных сделок.
        #
        # ВАЖНО: считаем это БЕЗ выбросов. Иначе биржа, которая просто участвует
        # чаще других, набирает наибольшую сумму убытка и ошибочно обвиняется.
        # Реальный случай: убыток сидел в нескольких парах, а отчёт предлагал
        # исключить binance — биржу с лучшей ликвидностью и стабильным потоком.
        base = [p for p in closed if p not in outliers] or closed
        avg_base = sum(float(p["realized_pnl"]) for p in base) / len(base)
        best_gain, best_ex, best_rest = 0.0, None, None
        for ex in by_ex:
            rest = [float(p["realized_pnl"]) for p in base
                    if ex not in (p.get("exchange_high"), p.get("exchange_low"))]
            # Нужен осмысленный остаток: если биржа есть почти везде, судить не о чем.
            if len(rest) < max(10, len(base) * 0.2):
                continue
            gain = (sum(rest) / len(rest)) - avg_base
            if gain > best_gain:
                best_gain, best_ex, best_rest = gain, ex, rest
        # Обвиняем биржу, только если без неё убыток падает хотя бы вдвое.
        if best_ex and avg_base < 0 and best_gain > abs(avg_base) * 0.5:
            lines.append(
                f"  БЕЗ {best_ex} (и без выбросов): {len(best_rest)} шт, "
                f"P&L={sum(best_rest):+.4f}, средний={sum(best_rest) / len(best_rest):+.4f} "
                f"(было {avg_base:+.4f})")
            excluded_ex, excluded_rest = best_ex, best_rest

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
    # Главное подозрение: расхождение не сошлось, а разошлось дальше. Это самая
    # частая причина минуса, и раньше её в выводе не было вовсе.
    if ent_act and ex_act is not None and ex_act > ent_act * 0.5:
        back = ex_act / ent_act * 100
        tail = ("расхождение РАСТЁТ, а не возвращается" if ex_act > ent_act
                else "расхождение УСТОЙЧИВОЕ — оно не схлопывается, "
                     "и весь результат съедают издержки")
        parts.append(f"спред НЕ СОШЁЛСЯ: вернули {back:.0f}% захваченного "
                     f"({_fmt_pct(ex_act)} из {_fmt_pct(ent_act)}) — {tail}")
    if ent_slip and raw and ent_slip > raw * 0.3:
        parts.append("слиппедж ВХОДА съедает значительную часть спреда")
    if ex_slip and raw and ex_slip > raw * 0.3:
        parts.append("слиппедж ВЫХОДА съедает значительную часть спреда")
    avg_fee = _avg(fee_fracs)
    if avg_fee and raw and avg_fee > raw * 0.3:
        parts.append("комиссии съедают значительную часть спреда")
    if outliers and total < 0:
        out_sum = sum(float(p["realized_pnl"]) for p in outliers)
        parts.append(
            f"{len(outliers)} сделок-выбросов из {n} дают {out_sum / total * 100:.0f}% "
            "убытка — вероятно, пары не тождественны; поможет risk.max_loss_pct")
    # Одна биржа тянет вниз весь результат?
    if excluded_ex and total < 0:
        share = (total - sum(excluded_rest)) / total * 100
        parts.append(
            f"биржа {excluded_ex} даёт {share:.0f}% убытка — без неё средний "
            f"{sum(excluded_rest) / len(excluded_rest):+.4f} против {total / n:+.4f}; "
            "проверь качество её котировок (обрывы WS) или исключи её")
    if not parts:
        parts.append("издержки умеренные — вероятно, спред расходился против нас "
                     "(смотри причины закрытия: stop_loss/max_adverse)")
    lines.append("  " + "; ".join(parts))
    return "\n".join(lines)
