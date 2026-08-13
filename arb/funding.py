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


def _short_money(v: Optional[float]) -> str:
    """Оборот в компактном виде: 1.2M, 340K."""
    if v is None:
        return "—"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"{v / div:.1f}{suf}"
    return f"{v:.0f}"


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
    slippage: float = 0.0,
) -> dict:
    """Экономика одной связки: доход в час и окупаемость ВСЕХ издержек.

    hourly_high — часовая ставка на бирже, где мы ШОРТИМ (получаем);
    hourly_low  — часовая ставка на бирже, где мы ЛОНГУЕМ (платим).

    slippage — проскальзывание входа и выхода суммарно (доля цены). Учитывать
    ОБЯЗАТЕЛЬНО: на тонких парах оно кратно превышает комиссии (замер: 1.56%
    против 0.28%), и без него окупаемость занижается в разы. Именно эта ошибка
    делала первую версию отчёта чрезмерно оптимистичной.
    """
    income_per_hour = hourly_high - hourly_low
    cost = round_trip_fee + slippage
    base = {"income_per_hour": income_per_hour, "cost": cost,
            "daily_pct": income_per_hour * HOURS_PER_DAY * 100,
            "apr_pct": income_per_hour * HOURS_PER_DAY * DAYS_PER_YEAR * 100}
    if income_per_hour <= 0:
        return {**base, "hours_to_breakeven": None}
    # Через сколько часов удержания накопленный funding покроет вход и выход.
    return {**base, "hours_to_breakeven": cost / income_per_hour}


def build_report(
    data: dict,
    fees: dict,
    min_stability: float = 0.7,
    top: int = 25,
    slippage: float = 0.0,
    volumes: Optional[dict] = None,
    min_volume: float = 0.0,
) -> str:
    """Отчёт по возможностям funding-арбитража.

    data: {symbol: {exchange: [(ts_ms, rate), ...]}}
    fees: {exchange: taker_fee}
    volumes: {symbol: {exchange: суточный оборот в USDT}} — для отсева неликвида.

    ЗАЧЕМ ФИЛЬТР ЛИКВИДНОСТИ. Весь смысл funding-арбитража в том, чтобы работать
    на парах, где цены бирж совпадают и нет риска расхождения. На тонких монетах
    этого нет: там цена может разъехаться на несколько процентов и одним движением
    съесть недели накопленного funding. Без фильтра отчёт выносит наверх ровно тот
    неликвид, на котором мы уже потеряли деньги в ценовом арбитраже.
    """
    rows = []
    skipped_illiquid = 0
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
                # Ликвидность нужна на ОБЕИХ ногах — узкое место определяет риск.
                if min_volume and volumes is not None:
                    v = volumes.get(symbol, {})
                    if min(v.get(h, 0.0), v.get(l, 0.0)) < min_volume:
                        skipped_illiquid += 1
                        continue
                fee = 2.0 * (fees.get(h, 0.0005) + fees.get(l, 0.0005))
                opp = pair_opportunity(hourly_h, hourly_l, fee, slippage)
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
                    "vol": (min(volumes.get(symbol, {}).get(h, 0.0),
                                volumes.get(symbol, {}).get(l, 0.0))
                            if volumes else None),
                })

    if not rows:
        extra = (f" Отсеяно по ликвидности: {skipped_illiquid}."
                 if skipped_illiquid else "")
        return ("Funding-анализ: подходящих связок не найдено "
                "(нет истории или ставки везде равны)." + extra)

    rows.sort(key=lambda r: r["apr"], reverse=True)
    avg_fee = sum(r["fee"] for r in rows) / len(rows)
    lines = [
        "=== FUNDING-АРБИТРАЖ: анализ по истории ===",
        f"Проанализировано пар: {len(data)} | связок с положительным "
        f"дифференциалом: {len(rows)}"
        + (f" | отсеяно по ликвидности: {skipped_illiquid}" if skipped_illiquid else ""),
        "",
        "Держим SHORT на бирже H (получаем funding) и LONG на L (платим).",
        f"«окуп.» — часов удержания, чтобы доход покрыл ВСЕ издержки входа и "
        f"выхода: комиссии ~{avg_fee * 100:.2f}% + слиппедж {slippage * 100:.2f}%.",
        "«устойч.» — доля периодов, где знак дифференциала сохранялся.",
    ]
    if not slippage:
        lines.append("ВНИМАНИЕ: слиппедж = 0, окупаемость ЗАНИЖЕНА. На тонких парах "
                     "он кратно превышает комиссии — задай --funding-slippage.")
    lines.append("")
    head = (f"{'пара':<16}{'H → L':<20}{'% годовых':>10}{'% в сутки':>11}"
            f"{'окуп.,ч':>9}{'устойч.':>9}")
    if volumes:
        head += f"{'оборот,$':>12}"
    lines.append(head)
    shown = [r for r in rows
             if r["stability"] is None or r["stability"] >= min_stability][:top]
    for r in shown:
        st = f"{r['stability'] * 100:.0f}%" if r["stability"] is not None else "—"
        be = f"{r['breakeven_h']:.0f}" if r["breakeven_h"] else "—"
        line = (f"{r['symbol']:<16}{r['high'] + ' → ' + r['low']:<20}"
                f"{r['apr']:>10.1f}{r['daily']:>11.3f}{be:>9}{st:>9}")
        if volumes:
            line += f"{_short_money(r['vol']):>12}"
        lines.append(line)

    # Практический вывод: годовые проценты бесполезны, если окупаемость дольше,
    # чем ставка вообще держится. Поэтому отдельно считаем реалистичные связки.
    good = [r for r in shown if r["breakeven_h"] and r["breakeven_h"] <= 48]
    lines.append("")
    lines.append("--- Вывод ---")
    if good:
        best = good[0]
        lines.append(
            f"  Связок с окупаемостью до 2 суток: {len(good)}. Лучшая — "
            f"{best['symbol']} {best['high']}→{best['low']}: "
            f"{best['apr']:.0f}% годовых, издержки отбиваются за "
            f"{best['breakeven_h']:.0f}ч.")
    else:
        lines.append("  Связок с окупаемостью до 2 суток НЕТ: дифференциал ставок "
                     "не перекрывает издержки за разумное время удержания.")
        lines.append("  На текущих тарифах это нежизнеспособно — нужны мейкерские "
                     "комиссии или пары с меньшим слиппеджем.")
    # Предупреждения важнее самих цифр: без них отчёт подталкивает к тому же
    # неликвиду, на котором провалился ценовой арбитраж.
    lines.append("  РИСК ЦЕНЫ: funding капает медленно, а цена двигается быстро. При "
                 f"доходе {shown[0]['daily']:.2f}%/сутки расхождение цен на 2% "
                 f"стирает {2 / max(shown[0]['daily'], 1e-9):.1f} суток дохода — "
                 "поэтому пары обязаны быть ЛИКВИДНЫМИ и тождественными.")
    lines.append("  Это ИСТОРИЯ: ставки меняются, прошлый дифференциал не "
                 "гарантирует будущий. Проверять в dry_run.")
    return "\n".join(lines)


async def collect_volumes(connectors: dict, symbols: list) -> dict:
    """Суточный оборот в USDT по парам: {symbol: {exchange: quoteVolume}}.

    Нужен, чтобы отсеять неликвид: на тонких парах цена бирж расходится, и это
    съедает funding быстрее, чем он накапливается.
    """
    out: dict = {}
    wanted = set(symbols)

    async def one(name, conn):
        client = conn.client
        if not getattr(client, "has", {}).get("fetchTickers"):
            return
        try:
            tickers = await client.fetch_tickers()
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_tickers %s: %s", name, exc)
            return
        raw_to_sym = {m.raw_symbol: s
                      for s, m in getattr(conn, "contracts", {}).items()}
        for raw, t in (tickers or {}).items():
            symbol = raw_to_sym.get(raw, raw)
            if symbol not in wanted or not isinstance(t, dict):
                continue
            vol = t.get("quoteVolume")
            if vol is None and t.get("baseVolume") and t.get("last"):
                vol = float(t["baseVolume"]) * float(t["last"])
            if vol:
                out.setdefault(symbol, {})[name] = float(vol)

    await asyncio.gather(*[one(n, c) for n, c in connectors.items()],
                         return_exceptions=True)
    return out


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
