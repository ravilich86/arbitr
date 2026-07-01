"""Тесты загрузки конфигурации (§1, §11)."""

import os
import textwrap
from pathlib import Path

import pytest

from arb.config import load_config


@pytest.fixture()
def cfg_file(tmp_path: Path) -> Path:
    content = textwrap.dedent(
        """
        dry_run: true
        testnet: false
        exchanges:
          binance:
            enabled: true
            api_key_env: TEST_BINANCE_KEY
            api_secret_env: TEST_BINANCE_SECRET
            taker_fee: 0.0005
            default_type: swap
          bybit:
            enabled: false
            api_key_env: TEST_BYBIT_KEY
            api_secret_env: TEST_BYBIT_SECRET
            taker_fee: 0.00055
        spread:
          min_gross_spread: 0.005
          min_net_spread: 0.002
        sizing:
          leverage: 20
        execution:
          order_type: market
        risk:
          max_concurrent_positions: 1
        allow_list: []
        deny_list:
          - ""
          - SOMEBADTICKER
        logging:
          level: INFO
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_basic_fields(cfg_file: Path):
    cfg = load_config(cfg_file, load_env=False)
    assert cfg.dry_run is True
    assert cfg.testnet is False
    assert cfg.spread["min_gross_spread"] == 0.005
    assert cfg.sizing["leverage"] == 20


def test_secrets_from_env(cfg_file: Path, monkeypatch):
    monkeypatch.setenv("TEST_BINANCE_KEY", "abc")
    monkeypatch.setenv("TEST_BINANCE_SECRET", "xyz")
    cfg = load_config(cfg_file, load_env=False)
    binance = cfg.exchanges["binance"]
    assert binance.api_key == "abc"
    assert binance.api_secret == "xyz"
    assert binance.has_credentials is True


def test_missing_secrets_none(cfg_file: Path, monkeypatch):
    monkeypatch.delenv("TEST_BYBIT_KEY", raising=False)
    cfg = load_config(cfg_file, load_env=False)
    assert cfg.exchanges["bybit"].api_key is None
    assert cfg.exchanges["bybit"].has_credentials is False


def test_enabled_exchanges_filter(cfg_file: Path):
    cfg = load_config(cfg_file, load_env=False)
    assert set(cfg.enabled_exchanges.keys()) == {"binance"}


def test_deny_list_strips_empty(cfg_file: Path):
    cfg = load_config(cfg_file, load_env=False)
    assert cfg.deny_list == ["SOMEBADTICKER"]


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml", load_env=False)
