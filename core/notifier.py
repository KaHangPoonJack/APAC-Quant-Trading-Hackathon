"""Outbound notifications (Telegram).

Kept behind a small interface so the recorder/engine depend on `Notifier`, not on
Telegram specifics. Sends use the stdlib (no extra dependency) and never raise —
a notification failure must never break the trading loop.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, text: str) -> bool:
        """Send a plain-text message. Returns True on success, never raises."""
        raise NotImplementedError


class NullNotifier(Notifier):
    """No-op notifier used when notifications are disabled."""

    def send(self, text: str) -> bool:  # noqa: D401
        return False


class TelegramNotifier(Notifier):
    """Telegram sender that never raises and escalates a PERSISTENT outage.

    A single failed send is noise (a timeout, a blip) and is logged at error.
    But a *revoked token or wrong chat_id fails silently forever* — every send
    logs one line and nothing ever draws attention to it. After
    `ESCALATE_AFTER` consecutive failures the notifier logs a distinct,
    actionable CRITICAL line once (latched), and one more when it recovers, so a
    dead notification channel is visible without spamming a line per attempt.
    """

    ESCALATE_AFTER = 3

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = str(chat_id)
        self._timeout = timeout
        self._consecutive_failures = 0
        self._escalated = False

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def degraded(self) -> bool:
        """True once this channel has failed enough to be considered down."""
        return self._escalated

    def _on_success(self) -> None:
        if self._escalated:
            log.warning("Telegram channel recovered after %d consecutive "
                        "failures (chat %s)", self._consecutive_failures,
                        self._chat_id)
        self._consecutive_failures = 0
        self._escalated = False

    def _on_failure(self, reason: object) -> None:
        self._consecutive_failures += 1
        log.error("Telegram send failed (%d consecutive): %s",
                  self._consecutive_failures, reason)
        if self._consecutive_failures >= self.ESCALATE_AFTER and not self._escalated:
            self._escalated = True
            log.critical(
                "Telegram channel for chat %s has failed %d times in a row — "
                "alerts from it are being LOST. Check the bot token, that the "
                "bot was /start-ed in that chat, and the chat_id. Diagnose with "
                "`python -m scripts.test_notification`.",
                self._chat_id, self._consecutive_failures)

    def send(self, text: str) -> bool:
        payload = json.dumps({
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                # An API-level rejection (bad chat_id, bot blocked) is just as
                # much a lost alert as a network failure, so it counts too.
                self._on_failure(f"API returned not-ok: {body}")
                return False
            self._on_success()
            return True
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad. A notification is NEVER worth crashing a
            # trading loop or the supervisor over, and the narrow tuple this
            # replaced (URLError/OSError/ValueError) missed real cases — a
            # placeholder bot token yields a URL with spaces, and http.client
            # raises InvalidURL, which subclasses HTTPException and so escaped.
            # KeyboardInterrupt/SystemExit derive from BaseException, so Ctrl+C
            # still propagates normally.
            self._on_failure(exc)
            return False


class NotifierRegistry:
    """Routes each strategy's messages to its OWN Telegram bot, with a shared
    `system` bot for errors/alerts. `for_strategy(name)` falls back to the system
    bot (then a NullNotifier) when a strategy has no dedicated channel — so a
    missing bot never silences a strategy and never breaks the trading loop."""

    SYSTEM = "system"

    def __init__(self, channels: dict[str, Notifier], system: Notifier):
        self._channels = channels
        self._system = system

    def for_strategy(self, name: str) -> Notifier:
        return self._channels.get(name) or self._system

    @property
    def system(self) -> Notifier:
        return self._system

    def channels(self) -> dict[str, Notifier]:
        """All configured destinations incl. system (for the test script)."""
        out = dict(self._channels)
        out.setdefault(self.SYSTEM, self._system)
        return out


def build_registry(ncfg) -> NotifierRegistry:
    """Build a NotifierRegistry from a NotificationsConfig. Disabled or missing
    tokens degrade to NullNotifier. The `system` bot is the explicit `system`
    channel if present, else the legacy single bot; per-strategy channels route
    by strategy name (the `strategy.name` config key)."""
    if not ncfg.enabled:
        return NotifierRegistry({}, NullNotifier())
    channels: dict[str, Notifier] = {}
    for name, ch in ncfg.channels.items():
        if ch.ready:
            channels[name] = TelegramNotifier(ch.bot_token, ch.chat_id)
    system = channels.pop(NotifierRegistry.SYSTEM, None)
    if system is None and ncfg.telegram_ready:
        system = TelegramNotifier(ncfg.telegram_bot_token, ncfg.telegram_chat_id)
    return NotifierRegistry(channels, system or NullNotifier())


def build_registry_logged(ncfg, logger) -> NotifierRegistry:
    """build_registry + a startup log line naming which bots resolved (secrets
    live in git-ignored settings.local.yaml and don't sync, so a fresh machine
    often has none — log loudly rather than fail silently)."""
    reg = build_registry(ncfg)
    if not ncfg.enabled:
        logger.warning("Notifications: DISABLED (notifications.enabled=false)")
        return reg
    chans = reg.channels()
    strat_active = sorted(k for k, v in chans.items()
                          if k != NotifierRegistry.SYSTEM and isinstance(v, TelegramNotifier))
    sys_on = isinstance(reg.system, TelegramNotifier)
    logger.info("Notifications: system bot=%s | per-strategy bots=[%s]",
                "ACTIVE" if sys_on else "fallback/none",
                ", ".join(strat_active) or "none (routing to system/legacy bot)")
    if not sys_on and not strat_active:
        logger.warning("Notifications: no Telegram bot resolved — every send is a "
                       "no-op. Add tokens under notifications.telegram.channels.* "
                       "in config/settings.local.yaml (git-ignored, per machine).")
    return reg
