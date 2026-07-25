"""Уведомления в Telegram о найденных арбитражах и закрытиях.

Токен и chat_id берутся из .env (как и остальные секреты). Сообщения помечены
именем приложения (app_name), чтобы их можно было отличать от статистики других
ботов в том же чате.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("arb.notifier")

_API = "https://api.telegram.org"


class TelegramNotifier:
    """Отправитель сообщений в Telegram через Bot API."""

    def __init__(
        self,
        token: Optional[str],
        chat_id: Optional[str],
        app_name: str = "Арбитраж-бот",
        enabled: bool = True,
        timeout: float = 10.0,
    ):
        self.token = token
        self.chat_id = chat_id
        self.app_name = app_name
        self.timeout = timeout
        # Реально включён только если задан и токен, и chat_id.
        self.enabled = bool(enabled and token and chat_id)

    async def send(self, text: str) -> bool:
        """Отправить сообщение. Ошибки не роняют бота — только лог."""
        if not self.enabled:
            return False
        try:
            import aiohttp

            url = f"{_API}/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("Telegram %s: %s", resp.status, body[:200])
                        return False
            return True
        except Exception as exc:  # noqa: BLE001 - сеть/таймаут не должны ронять бота
            logger.warning("Telegram: ошибка отправки: %s", exc)
            return False

    # Визуальный разделитель — чтобы в чате было видно начало нового события.
    SEP = "━━━━━━━━━━━━━━━━━━━━"

    # ---- шаблоны сообщений ----
    def _mode(self, dry_run: bool) -> str:
        return "🧪 DRY-RUN (без реальных ордеров)" if dry_run else "🔴 LIVE (реальные деньги)"

    @staticmethod
    def _balances_block(balances: Optional[dict]) -> str:
        """Строки с балансом по каждой бирже."""
        if not balances:
            return ""
        lines = []
        total = 0.0
        for name, val in balances.items():
            if val is None:
                lines.append(f"   {name}: н/д")
            else:
                lines.append(f"   {name}: {val:.2f}")
                total += val
        return "\n💵 Балансы:\n" + "\n".join(lines) + f"\n💰 Итого: {total:.2f} USDT"

    def startup_message(self, exchanges: list, pairs: int, dry_run: bool,
                        balances: Optional[dict] = None) -> str:
        return (
            f"{self.SEP}\n"
            f"🤖 {self.app_name}\n"
            f"🚀 ЗАПУЩЕН\n"
            f"🏦 Биржи: {', '.join(exchanges)}\n"
            f"📚 Пар в работе: {pairs}\n"
            f"{self._mode(dry_run)}"
            f"{self._balances_block(balances)}\n"
            f"{self.SEP}"
        )

    def shutdown_message(self, dry_run: bool, summary: Optional[str] = None,
                         balances: Optional[dict] = None) -> str:
        return (
            f"{self.SEP}\n"
            f"🤖 {self.app_name}\n"
            f"🛑 ОСТАНОВЛЕН"
            + (f"\n📊 {summary}" if summary else "")
            + f"{self._balances_block(balances)}\n"
            f"{self.SEP}"
        )

    def entry_message(self, sig, dry_run: bool) -> str:
        return (
            f"🤖 {self.app_name}\n"
            f"✅ Найден арбитраж\n"
            f"📊 Пара: {sig.symbol}\n"
            f"🔺 Шорт: {sig.exchange_high} @ {sig.bid_high:g}\n"
            f"🔻 Лонг: {sig.exchange_low} @ {sig.ask_low:g}\n"
            f"📈 Спред: {sig.raw_spread * 100:.2f}% "
            f"(чистый {sig.net_spread * 100:.2f}%, комиссии {sig.fee_cost * 100:.2f}%)\n"
            f"{self._mode(dry_run)}"
        )

    def close_message(self, pos, dry_run: bool) -> str:
        pnl = pos.realized_pnl or 0.0
        emoji = "🟢" if pnl >= 0 else "🔻"
        return (
            f"🤖 {self.app_name}\n"
            f"{emoji} Позиция закрыта\n"
            f"📊 Пара: {pos.symbol} ({pos.exchange_high} ↔ {pos.exchange_low})\n"
            f"💰 P&L: {pnl:+.4f} USDT\n"
            f"🏁 Причина: {pos.close_reason}\n"
            f"{self._mode(dry_run)}"
        )


def build_notifier(telegram_cfg: dict) -> Optional[TelegramNotifier]:
    """Собрать нотифаер из секции telegram конфига (с уже разрешёнными секретами)."""
    if not telegram_cfg:
        return None
    return TelegramNotifier(
        token=telegram_cfg.get("token"),
        chat_id=telegram_cfg.get("chat_id"),
        app_name=telegram_cfg.get("app_name", "Арбитраж-бот"),
        enabled=bool(telegram_cfg.get("enabled", False)),
    )
