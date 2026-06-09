"""Outbound transactional email — used for the 2FA verification codes sent at
signup and the first time an existing account adds an email address.

Reads SMTP settings from the environment so deployments can drop them into
.env without touching code:

  SMTP_HOST      server hostname (default: smtp.gmail.com)
  SMTP_PORT      submission port  (default: 587, STARTTLS)
  SMTP_USERNAME  login username   (default: EMAIL_ADDRESS)
  SMTP_PASSWORD  login password   (default: EMAIL_PASSWORD)
  SMTP_FROM      From address     (default: SMTP_USERNAME / EMAIL_ADDRESS)

The legacy EMAIL_ADDRESS / EMAIL_PASSWORD pair still works on its own — it
maps onto SMTP_USERNAME / SMTP_PASSWORD (and the From address) so existing
single-account Gmail setups keep running unchanged. The split lets more
involved setups use a relay whose login username differs from the visible
From address (e.g. SendGrid's "apikey" user, or a shared sending domain).

This module never logs the recipient address or the code body.
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
from email.message import EmailMessage


# Sensible defaults for the most common case; override per-provider via env.
_DEFAULT_SMTP_HOST = "smtp.gmail.com"
_DEFAULT_SMTP_PORT = 587

# Loose but practical email syntax check. We don't try to do RFC 5321 parsing —
# the only goal is to catch obvious typos before bothering the SMTP server.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class EmailSendError(Exception):
    """Surfaced to the frontend so it can show a calm, friendly message
    when SMTP is misconfigured or temporarily unreachable."""


def is_valid_email(email: str) -> bool:
    return isinstance(email, str) and bool(_EMAIL_RE.match(email.strip()))


def send_email(to_email: str, subject: str, body: str) -> None:
    """Send a plain-text email to `to_email` over the configured SMTP account.

    The transport half of email sending, factored out so any caller (2FA codes,
    the messaging layer's EmailChannel, future notifications) can reuse it.
    Raises EmailSendError on any failure — missing config, login refused, or
    network error — so the caller can show a soft, user-facing message rather
    than a stack trace.
    """
    # SMTP_USERNAME / SMTP_PASSWORD are the login credentials; EMAIL_ADDRESS /
    # EMAIL_PASSWORD are honoured as fallbacks for existing single-account setups.
    username = (
        os.environ.get("SMTP_USERNAME", "").strip()
        or os.environ.get("EMAIL_ADDRESS", "").strip()
    )
    password = (
        os.environ.get("SMTP_PASSWORD", "").strip()
        or os.environ.get("EMAIL_PASSWORD", "").strip()
    )
    if not username or not password:
        raise EmailSendError(
            "Email sending isn't set up on this server yet. Please ask the "
            "administrator to configure the SMTP credentials."
        )
    # The visible From address. Defaults to the login username, which is the
    # right answer for a plain Gmail account where the two are the same.
    from_address = (
        os.environ.get("SMTP_FROM", "").strip()
        or os.environ.get("EMAIL_ADDRESS", "").strip()
        or username
    )
    host = os.environ.get("SMTP_HOST", _DEFAULT_SMTP_HOST).strip() or _DEFAULT_SMTP_HOST
    try:
        port = int(os.environ.get("SMTP_PORT", _DEFAULT_SMTP_PORT))
    except ValueError:
        port = _DEFAULT_SMTP_PORT

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_email
    msg.set_content(body)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError(
            "We couldn't send the email right now. Please try again in a moment."
        ) from exc


def send_verification_code(to_email: str, code: str) -> None:
    """Send a one-time 6-digit verification code to `to_email`.

    Raises EmailSendError on any failure — missing config, login refused, or
    network error. The caller is expected to show a soft, user-facing message
    rather than a stack trace.
    """
    send_email(
        to_email,
        "Your Aime verification code",
        f"Hi there,\n\n"
        f"Your Aime verification code is: {code}\n\n"
        f"It will expire in 10 minutes.\n\n"
        f"If you didn't ask for this, you can safely ignore this email — "
        f"no changes will be made to any account.\n",
    )
