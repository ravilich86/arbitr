"""Анализ funding-арбитража между биржами по ИСТОРИЧЕСКИМ данным.

Зачем это отдельно от ценового арбитража
----------------------------------------
Четыре прогона показали, что схождение ЦЕН не даёт устойчивого преимущества:
спред широк только там, где стакан тонкий, и он весь уходит в слиппедж.

Funding-арбитраж устроен иначе. Держим ту же дельта-нейтральную конструкцию
(шорт на одной бирже, лонг на другой), но зарабатываем не на схождении цены, а
на РАЗНИЦЕ СТАВОК финансирования. Поэтому:
  - работает на ЛИКВИДНЫХ парах, где цены совпадают в пределах 0.01%
    (значит риск расхождения, который нас убивал, почти исчезает);
  - глубокие стаканы -> слиппедж мал;
  - горизонт удержания — часы и дни, скорость не нужна вообще.

Как считаем
-----------
Держим SHORT на бирже H и LONG на бирже L. При положительной ставке лонги платят
шортам, значит короткая нога ПОЛУЧАЕТ funding_H, длинная ПЛАТИТ funding_L.
Доход за период = funding_H − funding_L.

Периоды начисления у бирж разные (8ч / 4ч / 1ч), поэтому ставки нельзя вычитать
как есть — нормируем на ЧАС: rate / interval_hours. Иначе биржа с 1-часовым
интервалом выглядела бы в 8 раз хуже, чем есть.

Главный вопрос отчёта: перекрывает ли накопленный доход комиссии round-trip.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("arb.funding")

# Сколько часов в сутках — доход удобнее показывать в % годовых и % в сутки.
HOURS_PER_DAY = 24.0
DAYS_PER_YEAR = 365.0


def hourly_rate(funding_rate: float, interval_hours: Optional[float]) -> float:
    """Ставка за период -> ставка за ЧАС.

    Без этой нормировки биржи с разным интервалом (8ч/4ч/1ч) несопоставимы.
    Если интервал неизвестен — считаем стандартные 8 часов.
    """
    iv = interval_hours if interval_hours and interval_hours > 0 else 8.0
    return funding_rate / iv


def parse_history_row(row: dict) -> Optional[tuple[float, float]]:
    """Строка ccxt fetch_funding_rate_history -> (timestamp_ms, funding_rate)."""
    ts = row.get("timestamp")
    rate = row.get("fundingRate")
    if ts is None or rate is None:
        return None
    try:
        return float(ts), float(rate)
    except (TypeError, ValueError):
        return None


def infer_interval_hours(timestamps: list) -> Optional[float]:
    """Определить период начисления по РЕАЛЬНЫМ меткам истории.

    Надёжнее, чем поле 'interval': биржи меняют период, а история не врёт.
    Берём медиану разниц между соседними начислениями.
    """
    ts = sorted(set(timestamps))
    if len(ts) < 3:
        return None
    diffs = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
    median_ms = diffs[len(diffs) // 2]
    hours = median_ms / 3_600_000.0
    return hours if hours > 0 else None


async def fetch_history(connector, symbol: str, days: int = 30,
                        now_ms: Optional[float] = None) -> list[tuple[float, float]]:
    """История ставок одной пары на одной бирже за N дней -> [(ts_ms, rate)]."""
    client = connector.client
    if not getattr(client, "has", {}).get("fetchFundingRateHistory"):
        return []
    raw_symbol = symbol
    meta = getattr(connector, "contracts", {}).get(symbol)
    if meta is not None:
        raw_symbol = meta.raw_symbol
    import time as _t
    cur = now_ms if now_ms is not None else _t.time() * 1000.0
    since = int(cur - days * 24 * 3_600_000)
    try:
        rows = await client.fetch_funding_rate_history(raw_symbol, since, 1000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("funding history %s %s: %s", connector.name, symbol, exc)
        return []
    out = []
    for r in rows or []:
        parsed = parse_history_row(r)
        if parsed:
            out.append(parsed)
    return sorted(out)


def average_hourly(series: list[tuple[float, float]]) -> Optional[tuple[float, float, int]]:
    """Средняя ЧАСОВАЯ ставка по истории пары на бирже.

    Возвращает (средняя часовая ставка, период начисления в часах, число точек).
    """
    if len(series) < 3:
        return None
    interval = infer_interval_hours([t for t, _ in series]) or 8.0
    rates = [r for _, r in series]
    avg = sum(rates) / len(rates)
    return hourly_rate(avg, interval), interval, len(rates)


def sign_stability(series_h: list[float], series_l: list[float]) -> Optional[float]:
    """Доля периодов, где дифференциал сохранял ЗНАК (устойчивость преимущества).

    Ключевая проверка: средний положительный дифференциал бесполезен, если он
    половину времени отрицательный — тогда мы будем платить ровно так же часто,
    как получать, и средняя цифра ничего не обещает.
    """
    n = min(len(series_h), len(series_l))
    if n < 3:
        return None
    diffs = [series_h[i] - series_l[i] for i in range(n)]
    positive = sum(1 for d in diffs if d > 0)
    return max(positive, n - positive) / n


def pair_opportunity(
    hourly_high: float,
    hourly_low: float,
    round_trip_fee: float,
) -> dict:
    """Экономика одной связки: доход в час и окупаемость комиссий.

    hourly_high — часовая ставка на бирже, где мы ШОРТИМ (получаем);
    hourly_low  — часовая ставка на бирже, где мы ЛОНГУЕМ (платим).
    """
    income_per_hour = hourly_high - hourly_low
    if income_per_hour <= 0:
        return {"income_per_hour": income_per_hour, "hours_to_breakeven": None,
                "daily_pct": income_per_hour * HOURS_PER_DAY * 100,
                "apr_pct": income_per_hour * HOURS_PER_DAY * DAYS_PER_YEAR * 100}
    return {
        "income_per_hour": income_per_hour,
        # Через сколько часов удержания накопленный funding покроет комиссии
        # входа и выхода. Это и есть минимальный разумный горизонт сделки.
        "hours_to_breakeven": round_trip_fee / income_per_hour,
        "daily_pct": income_per_hour * HOURS_PER_DAY * 100,
        "apr_pct": income_per_hour * HOURS_PER_DAY * DAYS_PER_YEAR * 100,
    }


def build_report(
    data: dict,
    fees: dict,
    min_stability: float = 0.7,
    top: int = 25,
) -> str:
    """Отчёт по возможностям funding-арбитража.

    data: {symbol: {exchange: [(ts_ms, rate), ...]}}
    fees: {exchange: taker_fee}
    """
    rows = []
    for symbol, per_ex in data.items():
        stats = {}
        for ex, series in per_ex.items():
            avg = average_hourly(series)
            if avg is not None:
                stats[ex] = (avg[0], avg[1], avg[2], series)
        if len(stats) < 2:
            continue
        for h in stats:
            for l in stats:
                if h == l:
                    continue
                hourly_h, iv_h, n_h, ser_h = stats[h]
                hourly_l, iv_l, n_l, ser_l = stats[l]
                if hourly_h <= hourly_l:
                    continue  # доход только когда ставка шорт-ноги выше
                fee = 2.0 * (fees.get(h, 0.0005) + fees.get(l, 0.0005))
                opp = pair_opportunity(hourly_h, hourly_l, fee)
                # Устойчивость считаем по часовым ставкам обеих ног.
                st = sign_stability(
                    [hourly_rate(r, iv_h) for _, r in ser_h],
                    [hourly_rate(r, iv_l) for _, r in ser_l],
                )
                rows.append({
                    "symbol": symbol, "high": h, "low": l,
                    "apr": opp["apr_pct"], "daily": opp["daily_pct"],
                    "breakeven_h": opp["hours_to_breakeven"],
                    "stability": st, "points": min(n_h, n_l), "fee": fee,
                })

    if not rows:
        return ("Funding-анализ: подходящих связок не найдено "
                "(нет истории или ставки везде равны).")

    rows.sort(key=lambda r: r["apr"], reverse=True)
    lines = [
        "=== FUNDING-АРБИТРАЖ: анализ по истории ===",
        f"Проанализировано пар: {len(data)} | найдено связок с положительным "
        f"дифференциалом: {len(rows)}",
        "",
        "Держим SHORT на бирже H (получаем funding) и LONG на L (платим).",
        "«окуп.» — часов удержания, чтобы накопленный funding покрыл комиссии.",
        "«устойч.» — доля периодов, где знак дифференциала сохранялся.",
        "",
        f"{'пара':<16}{'H → L':<20}{'% годовых':>10}{'% в сутки':>11}"
        f"{'окуп.,ч':>9}{'устойч.':>9}",
    ]
    shown = [r for r in rows
             if r["stability"] is None or r["stability"] >= min_stability][:top]
    for r in shown:
        st = f"{r['stability'] * 100:.0f}%" if r["stability"] is not None else "—"
        be = f"{r['breakeven_h']:.0f}" if r["breakeven_h"] else "—"
        lines.append(f"{r['symbol']:<16}{r['high'] + ' → ' + r['low']:<20}"
                     f"{r['apr']:>10.1f}{r['daily']:>11.3f}{be:>9}{st:>9}")

    # Практический вывод: годовые проценты бесполезны, если окупаемость дольше,
    # чем ставка вообще держится. Поэтому отдельно считаем реалистичные связки.
    good = [r for r in shown if r["breakeven_h"] and r["breakeven_h"] <= 24]
    lines.append("")
    lines.append("--- Вывод ---")
    if good:
        best = good[0]
        lines.append(
            f"  Связок с окупаемостью до суток: {len(good)}. Лучшая — "
            f"{best['symbol']} {best['high']}→{best['low']}: "
            f"{best['apr']:.0f}% годовых, комиссии отбиваются за "
            f"{best['breakeven_h']:.0f}ч.")
        lines.append("  ВАЖНО: это ИСТОРИЯ. Ставки меняются, и прошлый "
                     "дифференциал не гарантирует будущий — проверять в dry_run.")
    else:
        lines.append("  Связок с окупаемостью до суток НЕТ: дифференциал ставок "
                     "не перекрывает комиссии за разумное время удержания.")
        lines.append("  Это значит, что funding-арбитраж на текущих комиссиях "
                     "нежизнеспособен — нужны мейкерские ставки или ниже тариф.")
    return "\n".join(lines)


async def collect(connectors: dict, symbols: list, days: int = 30,
                  concurrency: int = 8) -> dict:
    """Собрать историю ставок по всем парам и биржам: {symbol: {exchange: series}}."""
    sem = asyncio.Semaphore(concurrency)
    data: dict = {}

    async def one(symbol, name, conn):
        async with sem:
            series = await fetch_history(conn, symbol, days)
            if series:
                data.setdefault(symbol, {})[name] = series

    tasks = [one(s, name, conn)
             for s in symbols
             for name, conn in connectors.items()
             if s in getattr(conn, "contracts", {})]
    await asyncio.gather(*tasks, return_exceptions=True)
    return data
