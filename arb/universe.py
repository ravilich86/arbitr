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


def _contract_sizes_consistent(contracts: dict[str, ContractMeta], rel_tol: float) -> bool:
    """Проверить, что размеры контракта по биржам согласованы (эвристика коллизий).

    Если у части бирж contract_size не задан — пропускаем проверку (нельзя судить).
    Расходящиеся размеры могут означать разные базовые активы под одним тикером.
    """
    sizes = [c.contract_size for c in contracts.values() if c.contract_size]
    if len(sizes) < 2:
        return True
    lo, hi = min(sizes), max(sizes)
    if lo <= 0:
        return True
    return (hi - lo) / lo <= rel_tol


def build_universe(
    contracts_by_exchange: dict[str, dict[str, ContractMeta]],
    allow_list: list[str] | None = None,
    deny_list: list[str] | None = None,
    min_exchanges: int = 2,
    contract_size_rel_tol: float = 0.0,
    drop_suspicious: bool = True,
) -> UniverseResult:
    """Построить матрицу тождественных пар (§4).

    Args:
        contracts_by_exchange: {биржа -> {нормализованный символ -> ContractMeta}}.
        allow_list: если непусто — оставить только эти символы.
        deny_list: символы, которые нужно исключить (коллизии/спорные).
        min_exchanges: минимум бирж, на которых должен быть актив (по умолчанию 2).
        contract_size_rel_tol: допустимое относительное расхождение contract_size
            между биржами; 0.0 = требуем точного совпадения там, где размер задан.
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
        if not _contract_sizes_consistent(contracts, contract_size_rel_tol):
            reasons.append("расходящийся contract_size между биржами")

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
