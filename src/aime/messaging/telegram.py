"""Telegram Bot API channel — the prototype's default outbound transport.

Sending is a single HTTPS POST to the Bot API's ``sendMessage`` method, so this
needs no SDK (it reuses ``requests``, already a dependency) and costs nothing
per message. Two-way is a future bonus: the same bot can receive replies via
getUpdates/webhooks, which a messaging-based frontend could build on.

Setup (once): talk to @BotFather, ``/newbot``, copy the token into
``TELEGRAM_BOT_TOKEN``. Message your new bot once, then read your numeric chat
id from ``https://api.telegram.org/bot<token>/getUpdates`` and store it as the
account's messaging contact.
"""

from __future__ import annotations

import os

import requests

from .base import MessageChannel, MessageSendError


_API_BASE = "https://api.telegram.org"


class TelegramChannel(MessageChannel):
    name = "telegram"

    def __init__(self, token: str | None = None, *, timeout: float = 15.0):
        self._token = (token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self._timeout = timeout

    def send(self, recipient: str, text: str, *, subject: str | None = None) -> None:
        if not self._token:
            raise MessageSendError(
                "Messaging isn't set up on this server yet. Please ask the "
                "administrator to configure TELEGRAM_BOT_TOKEN."
            )
        if not (recipient or "").strip():
            raise MessageSendError(
                "There's no messaging contact on file to send this to yet."
            )
        # Telegram has no subject line; prepend it as a bold-ish first line so
        # the information isn't lost when a caller supplies one.
        body = f"{subject.strip()}\n\n{text}" if subject and subject.strip() else text
        url = f"{_API_BASE}/bot{self._token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": recipient.strip(), "text": body},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise MessageSendError(
                "We couldn't send that message right now. Please try again in a moment."
            ) from exc
        if not resp.ok:
            # The Bot API returns a JSON {ok, description} body on error; surface
            # the description to logs via the exception chain, not to the user.
            raise MessageSendError(
                "We couldn't send that message right now. Please try again in a moment."
            ) from RuntimeError(f"telegram sendMessage failed: {resp.status_code} {resp.text}")
