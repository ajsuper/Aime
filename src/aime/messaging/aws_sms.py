"""AWS End User Messaging SMS channel — the production outbound transport.

Sends through the ``pinpoint-sms-voice-v2`` API (``SendTextMessage``), the API
behind AWS End User Messaging SMS. Credentials come from boto3's standard chain
(the instance/task IAM role in production, ``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` or a profile elsewhere), so there is no secret of our
own to configure. The role needs ``sms-voice:SendTextMessage``.

Setup (once): in the AWS End User Messaging console register the brand and
10DLC campaign, attach the number, then set ``AWS_SMS_ORIGINATION_IDENTITY`` to
that phone number (E.164), its ID/ARN, or a pool ID/ARN. Optionally set
``AWS_SMS_CONFIGURATION_SET`` for delivery events, and ``AWS_SMS_REGION`` if
the number lives outside the default region. Recipients are stored per account
as the messaging contact and should be in E.164 form (``+15551234567``).

STOP/HELP keyword replies and the opt-out list are handled by AWS on the
number itself; a send to an opted-out recipient is refused and surfaces here
as a friendly error rather than a silent drop.

Two things SMS imposes that the other channels don't, both handled here so no
caller has to care: there is no subject line (it is folded into the body, as on
Telegram), and the body has a hard length ceiling — long text is trimmed rather
than rejected outright, since a slightly clipped reminder beats no reminder.
"""

from __future__ import annotations

import os
import re

from .base import MessageChannel, MessageSendError


# SendTextMessage rejects bodies over 1600 characters. Trim below that so the
# ellipsis always fits, and because a message this long is already well past
# what anyone wants to read on a phone (each 153-char segment is billed).
_MAX_BODY_CHARS = 1500

# Characters people naturally type into a phone number that the API won't take.
_PHONE_SEPARATORS = re.compile(r"[\s()\-.]")

_RETRY_LATER = (
    "We couldn't send that text message right now. Please try again in a moment."
)


def _normalize_number(raw: str) -> str:
    """Strip the punctuation humans put in phone numbers ("(555) 123-4567").

    Deliberately does *not* invent a country code: guessing one silently texts
    the wrong person. A number that arrives without ``+`` is passed through and
    AWS's own validation decides, which is why the failure message below
    mentions international format.
    """
    return _PHONE_SEPARATORS.sub("", (raw or "").strip())


_E164 = re.compile(r"\+[1-9]\d{7,14}")


def e164_or_none(raw: str) -> str | None:
    """The number in E.164 form ("+15551234567") if it is one once the usual
    punctuation is stripped, else None. For checking a number when it's
    entered (signup), so a typo is caught on the form rather than by a failed
    send later. Same no-guessing rule as _normalize_number: no ``+``, no
    match."""
    number = _normalize_number(raw)
    return number if _E164.fullmatch(number) else None


class AwsSMSChannel(MessageChannel):
    name = "sms"

    def __init__(
        self,
        origination_identity: str | None = None,
        configuration_set: str | None = None,
        region: str | None = None,
        *,
        client=None,
    ):
        env = os.environ.get
        self._origination = (
            origination_identity
            if origination_identity is not None
            else env("AWS_SMS_ORIGINATION_IDENTITY", "")
        ).strip()
        self._configuration_set = (
            configuration_set
            if configuration_set is not None
            else env("AWS_SMS_CONFIGURATION_SET", "")
        ).strip()
        self._region = (region or env("AWS_SMS_REGION", "")).strip() or None
        # Built lazily on first send, so selecting the channel never needs
        # boto3 or AWS config — only actually sending does.
        self._client = client

    def _sms_client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("pinpoint-sms-voice-v2", region_name=self._region)
        return self._client

    def send(self, recipient: str, text: str, *, subject: str | None = None) -> None:
        if not self._origination:
            raise MessageSendError(
                "Text messaging isn't set up on this server yet. Please ask the "
                "administrator to configure AWS_SMS_ORIGINATION_IDENTITY."
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

        params = {
            "DestinationPhoneNumber": to,
            "OriginationIdentity": self._origination,
            "MessageBody": body,
            "MessageType": "TRANSACTIONAL",
        }
        if self._configuration_set:
            params["ConfigurationSetName"] = self._configuration_set

        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._sms_client().send_text_message(**params)
        except ClientError as exc:
            # The provider detail rides the exception chain into the logs only;
            # the user gets something calm, and actionable where it can be.
            code = exc.response.get("Error", {}).get("Code", "")
            reason = exc.response.get("Reason", "")
            if reason == "DESTINATION_PHONE_NUMBER_OPTED_OUT":
                friendly = (
                    "We couldn't send that text message — this number has opted "
                    "out of texts from Aime (by replying STOP)."
                )
            elif code == "ValidationException":
                friendly = (
                    "We couldn't send that text message — the phone number on "
                    "file doesn't look right. It needs to be in international "
                    "format, like +15551234567."
                )
            else:
                friendly = _RETRY_LATER
            raise MessageSendError(friendly) from exc
        except BotoCoreError as exc:
            # Missing credentials/region, connection failures, timeouts.
            raise MessageSendError(_RETRY_LATER) from exc
