"""Этап 2 (§4): построение списка тождественных пар.

Из метаданных контрактов по каждой бирже (Этап 1) строим матрицу
«актив -> на каких биржах есть» и оставляем только активы, присутствующие
минимум на 2 из 5 бирж. Это пул кандидатов для арбитража.

Защита от ложных совпадений (коллизии тикеров, §4):
  - deny_list — жёстко исключить спорные тикеры;
  - allow_list — если задан, отслеживать ТОЛЬКО его;
  - эвристика contract_size — предупреждать, когда один и тот же тикер на
    разных биржах имеет сильно расходящийся размер контракта (возможно,
    это разные активы). Такие пары по умолчанию помечаются подозрительными
    и НЕ попадают в кандидаты (лучше пропустить, чем открыть арбитраж между
    двумя разными активами).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import Candidate, ContractMeta

logger = logging.getLogger(__name__)


@dataclass
class UniverseResult:
    """Результат построения вселенной кандидатов."""

    candidates: dict[str, Candidate] = field(default_factory=dict)
    suspicious: dict[str, list[str]] = field(default_factory=dict)  # symbol -> причины
    single_exchange: list[str] = field(default_factory=list)        # только 1 биржа

    @property
    def symbols(self) -> list[str]:
        return sorted(self.candidates.keys())


def _contract_size_units_mismatch(
    contracts: dict[str, ContractMeta], max_ratio: float
) -> bool:
    """Признак РАЗНЫХ ЕДИНИЦ контракта под одним тикером (эвристика коллизий).

    Важно: небольшие расхождения contract_size между биржами — норма для линейных
    USDT-перпов и НЕ означают коллизию. Настоящий тревожный признак — когда размеры
    отличаются в разы (напр. 1 против 1000): тогда «цены» на биржах несопоставимы,
    и прямое сравнение спреда бессмысленно.

    Возвращает True (коллизия/разные единицы), только если отношение
    max/min размеров контракта превышает max_ratio. Если у части бирж размер
    не задан — по этим биржам не судим.
    """
    sizes = [c.contract_size for c in contracts.values() if c.contract_size]
    if len(sizes) < 2:
        return False
    lo, hi = min(sizes), max(sizes)
    if lo <= 0:
        return False
    return (hi / lo) > max_ratio


def build_universe(
    contracts_by_exchange: dict[str, dict[str, ContractMeta]],
    allow_list: list[str] | None = None,
    deny_list: list[str] | None = None,
    min_exchanges: int = 2,
    max_contract_size_ratio: float | None = None,
    drop_suspicious: bool = True,
) -> UniverseResult:
    """Построить матрицу тождественных пар (§4).

    Args:
        contracts_by_exchange: {биржа -> {нормализованный символ -> ContractMeta}}.
        allow_list: если непусто — оставить только эти символы.
        deny_list: символы, которые нужно исключить (коллизии/спорные).
        min_exchanges: минимум бирж, на которых должен быть актив (по умолчанию 2).
        max_contract_size_ratio: во сколько раз может отличаться размер контракта
            между биржами, прежде чем пара считается коллизией. По умолчанию None
            (проверка ВЫКЛЮЧЕНА): contractSize — это множитель контракта, он законно
            отличается между биржами на порядки (напр. MEXC = 0.0001, Binance = 1) и
            НЕ влияет на цену (ccxt нормализует котировки к цене за 1 базовую единицу).
            Поэтому по contractSize коллизии не ловим — их отсекает ценовой sanity-cap
            сканера (max_gross_spread) и deny_list. Значение задаётся лишь при явной
            необходимости.
        drop_suspicious: выкидывать подозрительные пары из кандидатов.
    """
    allow = {s.upper() for s in (allow_list or [])}
    deny = {s.upper() for s in (deny_list or [])}

    # 1. Собрать матрицу symbol -> {exchange -> meta}
    matrix: dict[str, dict[str, ContractMeta]] = {}
    for exchange, contracts in contracts_by_exchange.items():
        for symbol, meta in contracts.items():
            key = symbol.upper()
            if key in deny:
                continue
            if allow and key not in allow:
                continue
            matrix.setdefault(key, {})[exchange] = meta

    result = UniverseResult()

    # 2. Отфильтровать по числу бирж и проверить на коллизии
    for symbol, contracts in matrix.items():
        if len(contracts) < min_exchanges:
            if len(contracts) == 1:
                result.single_exchange.append(symbol)
            continue

        reasons: list[str] = []
        if (max_contract_size_ratio is not None
                and _contract_size_units_mismatch(contracts, max_contract_size_ratio)):
            reasons.append(
                f"размер контракта различается >{max_contract_size_ratio:g}x "
                "(вероятно разные единицы/токены)"
            )

        if reasons:
            result.suspicious[symbol] = reasons
            logger.warning(
                "Подозрительная пара %s (%s): %s",
                symbol, ", ".join(contracts.keys()), "; ".join(reasons),
            )
            if drop_suspicious:
                continue

        result.candidates[symbol] = Candidate(symbol=symbol, contracts=dict(contracts))

    result.single_exchange.sort()
    return result
