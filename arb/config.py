"""Загрузка конфигурации: config.yaml + секреты из .env (§1, §11).

API-ключи НИКОГДА не хранятся в config.yaml или коде — только имена
переменных окружения, реальные значения подтягиваются из .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv опционален для тестов
    load_dotenv = None


@dataclass
class ExchangeConfig:
    """Настройки одной биржи + разрешённые секреты из окружения."""

    name: str
    enabled: bool
    taker_fee: float
    default_type: str = "swap"
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_password: Optional[str] = None  # passphrase для OKX/Bitget

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)


@dataclass
class Config:
    """Полная конфигурация приложения."""

    dry_run: bool
    testnet: bool
    exchanges: dict[str, ExchangeConfig]
    spread: dict[str, Any]
    sizing: dict[str, Any]
    execution: dict[str, Any]
    risk: dict[str, Any]
    allow_list: list[str] = field(default_factory=list)
    deny_list: list[str] = field(default_factory=list)
    logging: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled_exchanges(self) -> dict[str, ExchangeConfig]:
        return {n: e for n, e in self.exchanges.items() if e.enabled}


def _resolve_secret(env_name: Optional[str]) -> Optional[str]:
    """Достать секрет из окружения по имени переменной."""
    if not env_name:
        return None
    val = os.environ.get(env_name)
    return val or None


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path = ".env",
    load_env: bool = True,
) -> Config:
    """Прочитать config.yaml и подставить секреты из .env.

    Args:
        config_path: путь к YAML-конфигу.
        env_path: путь к .env файлу с секретами.
        load_env: загружать ли .env (в тестах можно отключить).
    """
    if load_env and load_dotenv is not None:
        env_file = Path(env_path)
        if env_file.exists():
            load_dotenv(env_file)

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Не найден файл конфигурации: {path}")

    with path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}

    exchanges: dict[str, ExchangeConfig] = {}
    for name, ex in (data.get("exchanges") or {}).items():
        exchanges[name] = ExchangeConfig(
            name=name,
            enabled=bool(ex.get("enabled", False)),
            taker_fee=float(ex.get("taker_fee", 0.0)),
            default_type=ex.get("default_type", "swap"),
            api_key=_resolve_secret(ex.get("api_key_env")),
            api_secret=_resolve_secret(ex.get("api_secret_env")),
            api_password=_resolve_secret(ex.get("api_password_env")),
        )

    deny = [d for d in (data.get("deny_list") or []) if d]

    return Config(
        dry_run=bool(data.get("dry_run", True)),
        testnet=bool(data.get("testnet", False)),
        exchanges=exchanges,
        spread=data.get("spread", {}) or {},
        sizing=data.get("sizing", {}) or {},
        execution=data.get("execution", {}) or {},
        risk=data.get("risk", {}) or {},
        allow_list=list(data.get("allow_list") or []),
        deny_list=deny,
        logging=data.get("logging", {}) or {},
        raw=data,
    )
