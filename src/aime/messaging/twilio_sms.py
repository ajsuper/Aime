"""Twilio SMS channel — the production outbound transport.

Like the Telegram channel this is a single HTTPS POST against a REST endpoint,
so it needs no SDK (``requests`` is already a dependency) and nothing to keep
running between sends. Credentials are HTTP basic auth: the account SID as the
username, the auth token as the password.

Setup (once): from the Twilio console copy the Account SID and Auth Token into
``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN``, then set the sender — either a
number you own (``TWILIO_FROM_NUMBER``) or, preferably in production, a
Messaging Service (``TWILIO_MESSAGING_SERVICE_SID``) which handles number pools
and compliance for you. Recipients are stored per account as the messaging
contact and should be in E.164 form (``+15551234567``).

Two things SMS imposes that the other channels don't, both handled here so no
caller has to care: there is no subject line (it is folded into the body, as on
Telegram), and the body has a hard length ceiling — long text is trimmed rather
than rejected outright, since a slightly clipped reminder beats no reminder.
"""

from __future__ import annotations

import os
import re

import requests

from .base import MessageChannel, MessageSendError


_API_BASE = "https://api.twilio.com/2010-04-01"

# Twilio rejects bodies over 1600 characters outright. Trim below that so the
# ellipsis always fits, and because a message this long is already well past
# what anyone wants to read on a phone (each 153-char segment is billed).
_MAX_BODY_CHARS = 1500

# Characters people naturally type into a phone number that Twilio won't take.
_PHONE_SEPARATORS = re.compile(r"[\s()\-.]")


def _normalize_number(raw: str) -> str:
    """Strip the punctuation humans put in phone numbers ("(555) 123-4567").

    Deliberately does *not* invent a country code: guessing one silently texts
    the wrong person. A number that arrives without ``+`` is passed through and
    Twilio's own validation decides, which is why the failure message below
    mentions international format.
    """
    return _PHONE_SEPARATORS.sub("", (raw or "").strip())


class TwilioSMSChannel(MessageChannel):
    name = "sms"

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
        messaging_service_sid: str | None = None,
        *,
        timeout: float = 15.0,
    ):
        env = os.environ.get
        self._account_sid = (account_sid or env("TWILIO_ACCOUNT_SID", "")).strip()
        self._auth_token = (auth_token or env("TWILIO_AUTH_TOKEN", "")).strip()
        self._from_number = _normalize_number(
            from_number if from_number is not None else env("TWILIO_FROM_NUMBER", "")
        )
        self._messaging_service_sid = (
            messaging_service_sid
            if messaging_service_sid is not None
            else env("TWILIO_MESSAGING_SERVICE_SID", "")
        ).strip()
        self._timeout = timeout

    def _sender_field(self) -> tuple[str, str]:
        """The 'who it's from' half of the payload. A Messaging Service wins when
        both are set — it's the production path, and Twilio picks the number."""
        if self._messaging_service_sid:
            return ("MessagingServiceSid", self._messaging_service_sid)
        return ("From", self._from_number)

    def send(self, recipient: str, text: str, *, subject: str | None = None) -> None:
        if not self._account_sid or not self._auth_token:
            raise MessageSendError(
                "Text messaging isn't set up on this server yet. Please ask the "
                "administrator to configure TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN."
            )
        if not self._messaging_service_sid and not self._from_number:
            raise MessageSendError(
                "Text messaging isn't set up on this server yet. Please ask the "
                "administrator to configure TWILIO_FROM_NUMBER (or "
                "TWILIO_MESSAGING_SERVICE_SID)."
            )
        to = _normalize_number(recipient)
        if not to:
            raise MessageSendError(
                "There's no messaging contact on file to send this to yet."
            )

        # SMS has no subject line; keep the information by leading with it.
        body = f"{subject.strip()}\n\n{text}" if subject and subject.strip() else text
        if len(body) > _MAX_BODY_CHARS:
            body = body[: _MAX_BODY_CHARS - 1].rstrip() + "…"

        sender_key, sender_value = self._sender_field()
        url = f"{_API_BASE}/Accounts/{self._account_sid}/Messages.json"
        try:
            resp = requests.post(
                url,
                data={"To": to, "Body": body, sender_key: sender_value},
                auth=(self._account_sid, self._auth_token),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise MessageSendError(
                "We couldn't send that text message right now. Please try again "
                "in a moment."
            ) from exc
        if not resp.ok:
            # Twilio replies with a JSON {code, message, more_info} body on error.
            # A 400 here is nearly always a malformed recipient, so that case gets
            # a hint the user can act on; everything else stays reassuring. The
            # provider detail rides the exception chain into the logs only.
            if resp.status_code == 400:
                friendly = (
                    "We couldn't send that text message — the phone number on "
                    "file doesn't look right. It needs to be in international "
                    "format, like +15551234567."
                )
            else:
                friendly = (
                    "We couldn't send that text message right now. Please try "
                    "again in a moment."
                )
            raise MessageSendError(friendly) from RuntimeError(
                f"twilio Messages.json failed: {resp.status_code} {resp.text}"
            )
