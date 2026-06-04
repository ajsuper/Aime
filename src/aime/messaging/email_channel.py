"""Email as a ``MessageChannel``, wrapping the existing SMTP sender.

This is the bridge that lets the same messaging abstraction also carry things
like verification codes or notification emails: the recipient is an email
address and ``send`` defers to ``aime.email_send.send_email``. It exists so
choosing "email" as the messaging channel needs no special-casing anywhere
above ``MessageChannel``.
"""

from __future__ import annotations

from .. import email_send
from .base import MessageChannel, MessageSendError


_DEFAULT_SUBJECT = "A message from Aime"


class EmailChannel(MessageChannel):
    name = "email"

    def send(self, recipient: str, text: str, *, subject: str | None = None) -> None:
        try:
            email_send.send_email(recipient, subject or _DEFAULT_SUBJECT, text)
        except email_send.EmailSendError as exc:
            # Re-wrap into the messaging layer's error type so callers only ever
            # have to catch MessageSendError regardless of the active channel.
            raise MessageSendError(str(exc)) from exc
