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


def test_contract_size_collision_drops_pair():
    # Один и тот же тикер FOO, но размеры контракта расходятся -> коллизия
    contracts = {
        "binance": {
            "FOO/USDT": ContractMeta("binance", "FOO/USDT", "FOO/USDT:USDT", "FOO",
                                     "USDT", contract_size=1.0),
        },
        "bybit": {
            "FOO/USDT": ContractMeta("bybit", "FOO/USDT", "FOO/USDT:USDT", "FOO",
                                     "USDT", contract_size=100.0),
        },
    }
    res = build_universe(contracts, contract_size_rel_tol=0.0)
    assert "FOO/USDT" not in res.candidates
    assert "FOO/USDT" in res.suspicious


def test_contract_size_consistent_kept():
    contracts = {
        "binance": {
            "FOO/USDT": ContractMeta("binance", "FOO/USDT", "FOO/USDT:USDT", "FOO",
                                     "USDT", contract_size=1.0),
        },
        "bybit": {
            "FOO/USDT": ContractMeta("bybit", "FOO/USDT", "FOO/USDT:USDT", "FOO",
                                     "USDT", contract_size=1.0),
        },
    }
    res = build_universe(contracts)
    assert "FOO/USDT" in res.candidates
