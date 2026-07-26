"""Outbound messaging: a thin, swappable layer for sending short texts to a user.

Public surface:
  * ``MessageChannel`` / ``MessageSendError`` — the transport contract (base.py).
  * ``get_messenger()`` — the configured channel, or None when messaging is off.
  * ``messaging_enabled()`` — whether sending is configured at all.

Everything above this package speaks only ``MessageChannel`` + a recipient
string, so moving from Telegram (prototype) to Twilio SMS (production) is a
matter of pointing ``AIME_MESSAGING_CHANNEL`` at another entry in the registry
below — no caller changes. Recipient strings come from the account record
(``UserRecord.messaging_contact``); this layer never reaches into account
storage itself, which keeps the contact-storage decision easy to revisit.
"""

from __future__ import annotations

import os

from .base import MessageChannel, MessageSendError
from .email_channel import EmailChannel
from .telegram import TelegramChannel
from .twilio_sms import TwilioSMSChannel


# Registry of available channels by their config name. Adding a transport means
# adding a ``MessageChannel`` subclass and one line here.
_CHANNELS: dict[str, type[MessageChannel]] = {
    "telegram": TelegramChannel,
    "email": EmailChannel,
    "sms": TwilioSMSChannel,
    # Alias, so naming the provider in config works as well as naming the medium.
    "twilio": TwilioSMSChannel,
}

_DEFAULT_CHANNEL = "telegram"


def _channel_name() -> str:
    return (os.environ.get("AIME_MESSAGING_CHANNEL") or _DEFAULT_CHANNEL).strip().lower()


def messaging_enabled() -> bool:
    """True unless explicitly disabled via ``AIME_MESSAGING=0``. Note this only
    reflects the on/off switch; an enabled-but-misconfigured channel still
    raises ``MessageSendError`` at send time (with a friendly message)."""
    raw = os.environ.get("AIME_MESSAGING")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_recipient() -> str | None:
    """A single outbound destination read from ``AIME_MESSAGING_CONTACT``.

    A fallback for single-user contexts with no accounts database (the local
    TUI). Multi-user frontends ignore this and pass each user's stored
    ``messaging_contact`` instead. Channel-agnostic: a Telegram chat id, a
    phone number under the SMS channel, an email address under email."""
    return (os.environ.get("AIME_MESSAGING_CONTACT") or "").strip() or None


def active_channel_name() -> str | None:
    """The configured channel's canonical name ("telegram", "sms", "email"), or
    None when messaging is off or misconfigured.

    For UI that has to ask the user for a contact: what a valid contact *looks
    like* is the one channel-specific thing a frontend legitimately needs to
    know. It returns the class's own ``name``, so config aliases (e.g. "twilio")
    normalize to the canonical one."""
    messenger = get_messenger()
    return messenger.name if messenger is not None else None


def get_messenger() -> MessageChannel | None:
    """The configured outbound channel, or None when messaging is disabled or
    the configured channel name is unknown. Callers treat None as 'no messaging
    available' and degrade gracefully."""
    if not messaging_enabled():
        return None
    cls = _CHANNELS.get(_channel_name())
    if cls is None:
        return None
    return cls()


__all__ = [
    "MessageChannel",
    "MessageSendError",
    "get_messenger",
    "messaging_enabled",
    "active_channel_name",
    "env_recipient",
]
