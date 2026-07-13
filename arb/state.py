"""Локальное хранилище открытых позиций (аудит и восстановление после рестарта).

Аккаунты выделены под бота и вручную не торгуются, поэтому источник истины —
биржа. Этот стор — вспомогательный: он фиксирует, какие пары ОТКРЫЛ бот, чтобы
после перезапуска понимать намеренные связки и вести аудит. Для аварийного
закрытия он не обязателен (там читаем позиции прямо с бирж).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("arb.state")


class PositionStore:
    """Простой JSON-стор открытых арбитражных позиций (список записей)."""

    def __init__(self, path: str = "data/positions.json"):
        self.path = Path(path)
        self._items: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = {r["id"]: r for r in data if "id" in r}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось прочитать %s: %s", self.path, exc)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(list(self._items.values()), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось записать %s: %s", self.path, exc)

    def add(self, record: dict[str, Any]) -> None:
        rid = record.get("id")
        if rid is None:
            return
        self._items[rid] = record
        self._save()

    def remove(self, position_id: str) -> None:
        if position_id in self._items:
            del self._items[position_id]
            self._save()

    def all(self) -> list[dict]:
        return list(self._items.values())
