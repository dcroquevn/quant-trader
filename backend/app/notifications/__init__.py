"""Getting a message to the user, and recording whether it arrived.

This package exists because Phase 6's only output is a notification. Everything else in the
project produces a number a reader can check; this produces a message that either reached a
phone or did not, and the difference is invisible unless it is recorded.

Two channels, both free:

* :class:`~app.notifications.telegram.TelegramNotifier` -- the Telegram Bot API. Free with no
  quota that a personal alert volume could reach. Needs a bot token from @BotFather and a chat
  id, both free, both in ``.env`` and never in code.
* :class:`~app.notifications.base.ConsoleNotifier` -- prints. The default, because a project
  that must run at zero cost cannot require the user to have configured anything before it
  works, and a printed alert is honest about being a printed alert.

The default is Console, not Telegram. An unconfigured Telegram notifier that silently swallows
messages would be the worst outcome here: the user would believe they were being watched.
"""

from app.notifications.base import (
    ConsoleNotifier,
    Notification,
    NotificationResult,
    Notifier,
    NullNotifier,
    build_notifier,
)
from app.notifications.telegram import TelegramNotifier

__all__ = [
    "Notification",
    "NotificationResult",
    "Notifier",
    "ConsoleNotifier",
    "NullNotifier",
    "TelegramNotifier",
    "build_notifier",
]
