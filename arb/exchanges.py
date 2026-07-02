"""Этап 1 (§3): коннекторы к биржам и сбор USDT-перпов.

Для каждой из 5 бирж:
  - инициализируем ccxt-коннектор (REST + WS через ccxt.pro), с rate limit;
  - загружаем рынки (load_markets);
  - фильтруем только линейные бессрочные фьючерсы (swap, linear, quote=USDT, active);
  - извлекаем метаданные контракта в ContractMeta.

Логика фильтрации вынесена в чистые функции (filter_perp_markets и
market_to_contract), чтобы её можно было тестировать на фикстурах без ccxt/сети.
"""

from __future__ import annotations

from typing import Any, Optional

from .config import Config, ExchangeConfig
from .models import ContractMeta

# Биржи, поддерживаемые на старте (§0). Имена = id ccxt.
SUPPORTED_EXCHANGES = ("binance", "bybit", "okx", "mexc", "bitget", "gate")


# --------------------------------------------------------------------------
#  Нормализация символа
# --------------------------------------------------------------------------
def normalize_symbol(base: str, quote: str = "USDT") -> str:
    """Единый ключ актива: 'BTC/USDT' независимо от биржевой записи (§4).

    ccxt даёт символ перпа как 'BTC/USDT:USDT'; сводим к 'BASE/QUOTE'.
    """
    return f"{base.upper()}/{quote.upper()}"


# --------------------------------------------------------------------------
#  Извлечение метаданных из ccxt market-структуры
# --------------------------------------------------------------------------
def _safe_get(d: dict, *path: str) -> Any:
    """Безопасно достать вложенное значение d[path[0]][path[1]]..."""
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def is_usdt_perp(market: dict) -> bool:
    """Линейный бессрочный фьючерс в USDT, активный (§3)."""
    if not isinstance(market, dict):
        return False
    if not market.get("swap", False):
        return False
    if not market.get("linear", False):
        return False
    if market.get("quote") != "USDT":
        return False
    # settle тоже USDT для линейных USDT-M
    if market.get("settle") not in (None, "USDT"):
        return False
    # active может отсутствовать -> считаем активным, но если явно False — пропускаем
    if market.get("active") is False:
        return False
    return True


# Дальняя граница: значения времени поставки за этим горизонтом считаем «вечными»
# сентинелами перпа (напр. Binance deliveryDate ≈ 4133404800000 = ~2100 год), не делистингом.
_FAR_FUTURE_MS = 2_500_000_000_000  # ~ год 2049; всё дальше — сентинел бессрочного перпа


def extract_delist_time(market: dict) -> Optional[float]:
    """Best-effort извлечение времени делистинга/поставки контракта (unix ms).

    Перпы обычно бессрочны, но при анонсе делистинга биржи проставляют дату
    поставки/делистинга. Поля различаются по биржам, поэтому проверяем несколько
    типичных вариантов. Далёкие сентинелы (год ~2100) игнорируем.
    """
    candidates: list = []
    expiry = market.get("expiry")
    if expiry:
        candidates.append(expiry)
    info = market.get("info") or {}
    if isinstance(info, dict):
        for key in ("delistTime", "deliveryTime", "deliveryDate", "settleTime",
                    "delivery_time", "delist_time"):
            val = info.get(key)
            fv = _to_float(val)
            if fv:
                candidates.append(fv)
    valid = [c for c in candidates if 0 < c < _FAR_FUTURE_MS]
    return min(valid) if valid else None


def market_to_contract(exchange: str, market: dict) -> ContractMeta:
    """Преобразовать ccxt market в ContractMeta (§3)."""
    base = market.get("base", "")
    quote = market.get("quote", "USDT")
    symbol = normalize_symbol(base, quote)
    return ContractMeta(
        exchange=exchange,
        symbol=symbol,
        raw_symbol=market.get("symbol", symbol),
        base=base.upper(),
        quote=quote.upper(),
        tick_size=_to_float(_safe_get(market, "precision", "price")),
        step_size=_to_float(_safe_get(market, "precision", "amount")),
        min_amount=_to_float(_safe_get(market, "limits", "amount", "min")),
        min_notional=_to_float(_safe_get(market, "limits", "cost", "min")),
        max_leverage=_to_float(_safe_get(market, "limits", "leverage", "max")),
        contract_size=_to_float(market.get("contractSize")),
        funding_interval_hours=None,  # заполняется на Этапе 3 (§5) через fetch_funding_rate
        delist_time=extract_delist_time(market),
    )


def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def filter_perp_markets(exchange: str, markets: dict[str, dict]) -> dict[str, ContractMeta]:
    """Отфильтровать USDT-перпы и вернуть {нормализованный символ -> ContractMeta}.

    При коллизии нормализованного символа внутри одной биржи (редко, но бывают
    дубликаты записей) оставляем первый и не перезаписываем.
    """
    result: dict[str, ContractMeta] = {}
    for market in markets.values():
        if not is_usdt_perp(market):
            continue
        contract = market_to_contract(exchange, market)
        result.setdefault(contract.symbol, contract)
    return result


# --------------------------------------------------------------------------
#  Асинхронный коннектор
# --------------------------------------------------------------------------
class ExchangeConnector:
    """Обёртка над ccxt-клиентом одной биржи.

    Клиент можно передать извне (для тестов — мок), либо он создаётся из
    ExchangeConfig через build_ccxt_client().
    """

    def __init__(self, name: str, client: Any, cfg: Optional[ExchangeConfig] = None):
        self.name = name
        self.client = client
        self.cfg = cfg
        self.contracts: dict[str, ContractMeta] = {}

    async def load_perp_contracts(self, reload: bool = False) -> dict[str, ContractMeta]:
        """Загрузить рынки и собрать USDT-перпы (§3)."""
        markets = await self.client.load_markets(reload)
        # ccxt.load_markets может вернуть dict или обновить self.client.markets
        if not isinstance(markets, dict):
            markets = getattr(self.client, "markets", {}) or {}
        self.contracts = filter_perp_markets(self.name, markets)
        return self.contracts

    async def close(self) -> None:
        """Закрыть соединение (важно для async ccxt / WS)."""
        close = getattr(self.client, "close", None)
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result


def build_ccxt_client(cfg: ExchangeConfig, testnet: bool = False) -> Any:
    """Создать ccxt.pro (или async) клиент из конфига биржи (§1).

    Импорт ccxt внутри функции, чтобы тесты не требовали установленного ccxt.
    """
    try:
        import ccxt.pro as ccxt_mod  # type: ignore
    except Exception:  # pragma: no cover
        import ccxt.async_support as ccxt_mod  # type: ignore

    if cfg.name not in SUPPORTED_EXCHANGES:
        raise ValueError(f"Биржа не поддерживается: {cfg.name}")

    klass = getattr(ccxt_mod, cfg.name, None)
    if klass is None:  # pragma: no cover
        raise ValueError(f"ccxt не знает биржу: {cfg.name}")

    params: dict[str, Any] = {
        "enableRateLimit": True,  # обработка лимитов запросов (§3)
        "options": {"defaultType": cfg.default_type},
    }
    if cfg.api_key and cfg.api_secret:
        params["apiKey"] = cfg.api_key
        params["secret"] = cfg.api_secret
    if cfg.api_password:
        params["password"] = cfg.api_password  # passphrase для OKX/Bitget

    client = klass(params)
    if testnet:
        try:
            client.set_sandbox_mode(True)
        except Exception:  # pragma: no cover - не все биржи поддерживают
            pass
    return client


def create_connectors(config: Config) -> dict[str, ExchangeConnector]:
    """Создать коннекторы для всех включённых бирж (§3).

    Требует установленного ccxt (боевой путь). В тестах используем
    ExchangeConnector напрямую с мок-клиентом.
    """
    connectors: dict[str, ExchangeConnector] = {}
    for name, ex_cfg in config.enabled_exchanges.items():
        client = build_ccxt_client(ex_cfg, testnet=config.testnet)
        connectors[name] = ExchangeConnector(name, client, ex_cfg)
    return connectors
