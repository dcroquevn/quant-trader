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
import uuid
from pathlib import Path

from app.core.logging import get_logger
from app.notifications.base import Notification, NotificationResult, Notifier

logger = get_logger(__name__)

__all__ = [
    "TelegramNotifier",
    "TELEGRAM_API_BASE",
    "MESSAGE_LIMIT",
    "DOCUMENT_LIMIT_BYTES",
    "CAPTION_LIMIT",
]

TELEGRAM_API_BASE = "https://api.telegram.org"

MESSAGE_LIMIT = 4096
"""Telegram's per-message character limit. Longer messages are rejected outright.

An exit alert listing every failed condition can exceed this, so it is truncated here rather
than discovered as a 400 from the API.
"""

REQUEST_TIMEOUT_SECONDS = 15

DOCUMENT_LIMIT_BYTES = 50 * 1024 * 1024
"""Telegram's per-document ceiling. The digest is ~100KB, so this is a guard, not a constraint."""

CAPTION_LIMIT = 1024
"""Telegram rejects a longer caption outright, so it is clipped before sending."""

DOCUMENT_TIMEOUT_SECONDS = 60
"""An upload needs more headroom than a text message, and the daily job can afford to wait."""


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

    # ------------------------------------------------------------------ #
    # Files
    # ------------------------------------------------------------------ #

    def send_document(
        self, path: "Path", *, caption: str = "", filename: str | None = None
    ) -> NotificationResult:
        """Upload a file to the chat.

        Exists so the daily digest can arrive as something tappable instead of an artifact behind
        a login, an Actions page and a zip. The whole point of the scheduled run is that the user
        does not have to go anywhere, and a download that needs a desktop defeats it.

        Telegram accepts up to 50MB per document; the digest is around 100KB, so the limit is not
        a practical constraint but it is checked rather than assumed, because a rejected upload
        would otherwise look like a network failure.

        multipart/form-data is assembled by hand for the same reason the rest of this module uses
        ``urllib``: one upload does not justify a dependency on the path that has to work
        unattended.
        """
        if not self.configured:
            return NotificationResult.failed(
                self.name, "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not both set."
            )
        if not path.exists():
            return NotificationResult.failed(self.name, f"no such file: {path}")

        payload = path.read_bytes()
        if len(payload) > DOCUMENT_LIMIT_BYTES:
            return NotificationResult.failed(
                self.name,
                f"{path.name} is {len(payload) / 1_048_576:.1f}MB; Telegram's limit is "
                f"{DOCUMENT_LIMIT_BYTES / 1_048_576:.0f}MB.",
            )

        boundary = f"----quanttrader{uuid.uuid4().hex}"
        name = filename or path.name
        # A caption longer than this is rejected outright, so it is clipped here rather than
        # discovered as a 400 that looks like a bad token.
        caption = caption[:CAPTION_LIMIT]

        parts: list[bytes] = []

        def field(key: str, value: str) -> None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )

        field("chat_id", str(self._chat_id))
        if caption:
            field("caption", caption)
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
            f'filename="{name}"\r\nContent-Type: text/html\r\n\r\n'.encode("utf-8")
        )
        parts.append(payload)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        request = urllib.request.Request(
            f"{TELEGRAM_API_BASE}/bot{self._token}/sendDocument",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

        try:
            with urllib.request.urlopen(
                request, timeout=DOCUMENT_TIMEOUT_SECONDS
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            logger.warning("Telegram rejected the document: %s %s", exc.code, detail)
            return NotificationResult.failed(self.name, f"HTTP {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            return NotificationResult.failed(self.name, f"unreachable: {exc.reason}")
        except (TimeoutError, OSError) as exc:
            return NotificationResult.failed(self.name, f"{type(exc).__name__}: {exc}")
        except json.JSONDecodeError as exc:
            return NotificationResult.failed(self.name, f"unparseable response: {exc}")

        if not result.get("ok"):
            return NotificationResult.failed(
                self.name, f"API returned ok=false: {result.get('description', result)}"
            )
        return NotificationResult.sent(
            self.name, f"document {name} ({len(payload):,} bytes)"
        )

    def verify_token(self) -> tuple[bool, str]:
        """Ask Telegram who this bot is. Returns ``(ok, detail)``.

        Separates the two failures that look identical from a failed send: a bad token and a bad
        chat id. ``getMe`` needs only the token, so if it succeeds the token is good and anything
        still failing is about the destination. Without this split, "not delivered" sends the
        user to re-copy a token that was never the problem.
        """
        if not self._token:
            return False, "TELEGRAM_BOT_TOKEN is empty."

        request = urllib.request.Request(f"{TELEGRAM_API_BASE}/bot{self._token}/getMe")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            if exc.code == 404:
                return False, (
                    "HTTP 404: no bot has this token. Almost always a stray character pasted "
                    "with it -- angle brackets, a quote, or a trailing space."
                )
            if exc.code == 401:
                return False, "HTTP 401: the token is wrong or was revoked."
            return False, f"HTTP {exc.code}: {detail}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, f"could not reach Telegram: {exc}"
        except json.JSONDecodeError as exc:
            return False, f"unparseable response: {exc}"

        if not body.get("ok"):
            return False, str(body.get("description", body))
        bot = body.get("result", {})
        return True, f"@{bot.get('username', '?')} ({bot.get('first_name', '?')})"
