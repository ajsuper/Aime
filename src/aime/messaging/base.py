"""The channel-agnostic contract for outbound messages.

A ``MessageChannel`` knows how to deliver a short text to one recipient over
one transport (Telegram today; SMS/email tomorrow). Everything above this
interface — the SendMessage tool, the agent SubmitResult ``message_to_user``
field, future verification-code delivery — speaks only ``MessageChannel`` and a
recipient string, so swapping Telegram for a production SMS provider is a change
of *one* concrete class, not of any caller.

The recipient is always an opaque string whose meaning is the channel's own:
a Telegram chat id, a phone number, an email address. Callers pass it straight
through from storage (``UserRecord.messaging_contact``) without interpreting it.
"""

from __future__ import annotations

import abc


class MessageSendError(Exception):
    """Raised when a message can't be delivered — misconfiguration, a missing
    recipient, or a transport failure. Carries a calm, user-facing message so a
    frontend (or the model, when this surfaces as a tool result) can show
    something reassuring rather than a stack trace."""


class MessageChannel(abc.ABC):
    """One outbound transport. Implementations are cheap value objects: they
    read their own credentials from the environment at construction and hold no
    per-recipient state."""

    #: Stable identifier used in config/selection (e.g. "telegram", "sms").
    name: str = "channel"

    @abc.abstractmethod
    def send(self, recipient: str, text: str, *, subject: str | None = None) -> None:
        """Deliver ``text`` to ``recipient``.

        ``subject`` is an optional title for channels that have one (email);
        channels without a subject concept (SMS, Telegram) fold it into the
        body or ignore it. Raises ``MessageSendError`` on any failure.
        """
        raise NotImplementedError
