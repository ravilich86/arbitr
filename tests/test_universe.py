"""Тесты Этапа 2 (§4): матрица тождественных пар."""

from arb.exchanges import filter_perp_markets
from arb.models import ContractMeta
from arb.universe import build_universe
from tests.fixtures import binance_markets, bybit_markets


def _contracts():
    return {
        "binance": filter_perp_markets("binance", binance_markets()),
        "bybit": filter_perp_markets("bybit", bybit_markets()),
    }


def test_candidates_require_two_exchanges():
    res = build_universe(_contracts())
    # BTC и ETH есть на обеих; SOL только на bybit -> не кандидат
    assert res.symbols == ["BTC/USDT", "ETH/USDT"]
    assert "SOL/USDT" in res.single_exchange


def test_candidate_holds_contracts_per_exchange():
    res = build_universe(_contracts())
    btc = res.candidates["BTC/USDT"]
    assert set(btc.exchanges) == {"binance", "bybit"}
    assert btc.contracts["binance"].base == "BTC"


def test_deny_list_excludes():
    res = build_universe(_contracts(), deny_list=["BTC/USDT"])
    assert "BTC/USDT" not in res.candidates
    assert "ETH/USDT" in res.candidates


def test_allow_list_restricts():
    res = build_universe(_contracts(), allow_list=["ETH/USDT"])
    assert res.symbols == ["ETH/USDT"]


def test_min_exchanges_param():
    res = build_universe(_contracts(), min_exchanges=1)
    assert "SOL/USDT" in res.candidates


def _pair_with_sizes(size_a, size_b):
    return {
        "binance": {
            "FOO/USDT": ContractMeta("binance", "FOO/USDT", "FOO/USDT:USDT", "FOO",
                                     "USDT", contract_size=size_a),
        },
        "bybit": {
            "FOO/USDT": ContractMeta("bybit", "FOO/USDT", "FOO/USDT:USDT", "FOO",
                                     "USDT", contract_size=size_b),
        },
    }


def test_contract_size_check_disabled_by_default():
    # По умолчанию проверка по contractSize ВЫКЛ: разный множитель (напр. MEXC 0.0001
    # против Binance 1 = 10000x) — это норма, пара остаётся кандидатом
    res = build_universe(_pair_with_sizes(1.0, 10000.0))
    assert "FOO/USDT" in res.candidates
    assert "FOO/USDT" not in res.suspicious


def test_contract_size_units_mismatch_when_explicitly_enabled():
    # Если явно задать порог — большое расхождение считается коллизией
    res = build_universe(_pair_with_sizes(1.0, 100.0), max_contract_size_ratio=50.0)
    assert "FOO/USDT" not in res.candidates
    assert "FOO/USDT" in res.suspicious


def test_small_difference_kept_with_threshold():
    # 10x < 50x -> в пределах порога, пара остаётся
    res = build_universe(_pair_with_sizes(1.0, 10.0), max_contract_size_ratio=50.0)
    assert "FOO/USDT" in res.candidates
