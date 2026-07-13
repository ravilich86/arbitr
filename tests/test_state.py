"""Тесты локального хранилища позиций."""

from arb.state import PositionStore


def test_store_add_remove(tmp_path):
    path = tmp_path / "positions.json"
    store = PositionStore(str(path))
    store.add({"id": "p1", "symbol": "BTC/USDT"})
    store.add({"id": "p2", "symbol": "ETH/USDT"})
    assert len(store.all()) == 2

    store.remove("p1")
    assert [r["id"] for r in store.all()] == ["p2"]

    # перечитать с диска
    store2 = PositionStore(str(path))
    assert [r["id"] for r in store2.all()] == ["p2"]


def test_store_ignores_record_without_id(tmp_path):
    store = PositionStore(str(tmp_path / "p.json"))
    store.add({"symbol": "BTC/USDT"})  # нет id
    assert store.all() == []
