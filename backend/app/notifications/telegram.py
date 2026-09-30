"""Telegram Bot API delivery.

Chosen because it is free with no card on file, needs no server to receive a webhook, and puts
a message on a phone the user already carries. The cost is one HTTP POST per alert.

Setup, all free:

1. Message ``@BotFather`` on Telegram, send ``/newbot``, and it returns a token.
2. Send your new bot any message, then open
   ``https://api.telegram.org/bot<TOKEN>/getUpdates`` and read ``result[0].message.chat.id``.
3. Put both in ``.env`` as ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``, and set
   ``TELEGRAM_ENABLED=true``. Never in code -- ``.env`` is gitignored, and a bot token in a
   public repository is a bot anyone can send messages as.

What is *not* free and is therefore not here: Telegram's API is free, but it is also an external
service that can rate-limit, change, or be unreachable. Every failure returns a result rather
than raising, and the console notifier remains the default.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from app.core.logging import get_logger
from app.notifications.base import Notification, NotificationResult, Notifier

logger = get_logger(__name__)

__all__ = ["TelegramNotifier", "TELEGRAM_API_BASE", "MESSAGE_LIMIT"]

TELEGRAM_API_BASE = "https://api.telegram.org"

MESSAGE_LIMIT = 4096
"""Telegram's per-message character limit. Longer messages are rejected outright.

An exit alert listing every failed condition can exceed this, so it is truncated here rather
than discovered as a 400 from the API.
"""

REQUEST_TIMEOUT_SECONDS = 15


class TelegramNotifier(Notifier):
    """Sends via the Telegram Bot API.

    Uses ``urllib`` from the standard library rather than ``requests`` or ``httpx``: one POST
    does not justify a dependency, and this keeps the notification path free of anything that
    could fail to install on a machine running the daily check unattended.
    """

    name = "telegram"

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        from app.config import get_settings

        settings = get_settings()
        self._token = token if token is not None else settings.telegram_bot_token
        self._chat_id = chat_id if chat_id is not None else settings.telegram_chat_id

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, notification: Notification) -> NotificationResult:
        if not self.configured:
            return NotificationResult.failed(
                self.name,
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not both set. See "
                "app/notifications/telegram.py for the free setup steps.",
            )

        text = notification.as_text()
        if len(text) > MESSAGE_LIMIT:
            # Truncate rather than let the API reject the whole thing. The subject and the
            # first lines carry the actionable part, so a clipped tail still delivers it.
            keep = MESSAGE_LIMIT - 80
            text = text[:keep] + "\n\n[truncated; run `python -m app watch` for the full text]"

        payload = urllib.parse.urlencode(
            {
                "chat_id": self._chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")

        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Telegram puts a usable reason in the body of a 4xx; the status alone does not
            # distinguish a bad token from a blocked bot.
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            logger.warning("Telegram rejected the message: %s %s", exc.code, detail)
            return NotificationResult.failed(self.name, f"HTTP {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            logger.warning("Telegram unreachable: %s", exc.reason)
            return NotificationResult.failed(self.name, f"unreachable: {exc.reason}")
        except (TimeoutError, OSError) as exc:
            logger.warning("Telegram send failed: %s", exc)
            return NotificationResult.failed(self.name, f"{type(exc).__name__}: {exc}")
        except json.JSONDecodeError as exc:
            return NotificationResult.failed(self.name, f"unparseable response: {exc}")

        if not body.get("ok"):
            return NotificationResult.failed(
                self.name, f"API returned ok=false: {body.get('description', body)}"
            )
        return NotificationResult.sent(self.name, f"message_id {body['result']['message_id']}")

    def describe_setup(self) -> str:
        """What the user has to do, for the CLI to print when it is not configured."""
        if self.configured:
            return "Telegram is configured."
        missing = []
        if not self._token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self._chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        return (
            f"Telegram is not configured: {', '.join(missing)} missing from .env. "
            "Get a token from @BotFather (free), send your bot a message, then read your "
            "chat id from https://api.telegram.org/bot<TOKEN>/getUpdates. "
            "Alerts print to the console until then."
        )
