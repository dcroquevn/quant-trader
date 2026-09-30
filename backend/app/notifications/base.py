"""The notifier contract, and the two implementations that need no configuration.

Design decision worth stating: :meth:`Notifier.send` returns a result instead of raising. A
notification failing is an expected operational state, not a bug -- the network is down, the
token expired, the user blocked the bot -- and the caller's correct response is to record the
failure and carry on watching, not to abort the run. Raising would mean one unreachable channel
stopped the whole daily check.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "Notification",
    "NotificationResult",
    "Notifier",
    "ConsoleNotifier",
    "NullNotifier",
    "build_notifier",
]


@dataclass(frozen=True, slots=True)
class Notification:
    """One message, with the key that stops it being sent twice.

    ``dedupe_key`` is the caller's promise about identity: two notifications with the same key
    are the same notification. For an exit signal it is the holding plus the bar date, so a
    watch that runs every morning does not re-send Monday's signal on Tuesday. An alert that
    repeats is an alert that gets muted, and a muted alert is worse than none.
    """

    kind: str
    subject: str
    body: str
    dedupe_key: str
    symbol: str = ""
    holding_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_text(self) -> str:
        """Plain text, ASCII only.

        ASCII because this has printed to a Windows cp1252 console before and an em dash
        raised UnicodeEncodeError mid-alert. A notification that crashes the notifier is a
        notification nobody gets.
        """
        return f"{self.subject}\n\n{self.body}"


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """Whether the message went out, and why not if it did not."""

    delivered: bool
    channel: str
    detail: str = ""
    sent_at: datetime | None = None

    @classmethod
    def sent(cls, channel: str, detail: str = "") -> "NotificationResult":
        return cls(True, channel, detail, datetime.now(timezone.utc))

    @classmethod
    def failed(cls, channel: str, detail: str) -> "NotificationResult":
        return cls(False, channel, detail, None)


class Notifier(abc.ABC):
    """Somewhere a message can be sent."""

    name: str = "notifier"

    @property
    def configured(self) -> bool:
        """Whether this channel can actually deliver right now.

        Checked before a run so the user is told "Telegram is not configured, alerts will
        print to the console" up front, rather than discovering it when an exit signal they
        needed went nowhere.
        """
        return True

    @abc.abstractmethod
    def send(self, notification: Notification) -> NotificationResult:
        """Deliver, and say what happened. Must not raise for a delivery failure."""


class ConsoleNotifier(Notifier):
    """Prints. The default, and honest about what it is.

    It is the default because this project has to work with nothing configured, and because a
    Telegram notifier with no token would appear to work while delivering nothing.
    """

    name = "console"

    def send(self, notification: Notification) -> NotificationResult:
        print()
        print("=" * 72)
        print(notification.as_text())
        print("=" * 72)
        return NotificationResult.sent(self.name, "printed to stdout")


class NullNotifier(Notifier):
    """Discards everything, and says so.

    For tests and dry runs. It reports ``delivered=False`` rather than True, so a test that
    asserts a message was delivered cannot pass by accident against this.
    """

    name = "null"

    @property
    def configured(self) -> bool:
        return False

    def send(self, notification: Notification) -> NotificationResult:
        logger.debug("Discarded notification %s", notification.dedupe_key)
        return NotificationResult.failed(self.name, "notifications are disabled")


def build_notifier(channel: str | None = None) -> Notifier:
    """The notifier the settings ask for, falling back to the console.

    A requested-but-unconfigured Telegram gets the console instead, with a warning. The
    alternative -- returning the unconfigured Telegram notifier -- would mean the user believes
    alerts are going to their phone while every one fails.
    """
    from app.config import get_settings
    from app.notifications.telegram import TelegramNotifier

    settings = get_settings()
    wanted = (channel or ("telegram" if settings.telegram_enabled else "console")).lower()

    if wanted == "null":
        return NullNotifier()
    if wanted == "telegram":
        telegram = TelegramNotifier()
        if telegram.configured:
            return telegram
        logger.warning(
            "Telegram was requested but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not set; "
            "alerts will print to the console instead."
        )
        return ConsoleNotifier()
    return ConsoleNotifier()
