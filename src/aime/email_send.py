"""Outbound transactional email — used for the 2FA verification codes sent at
signup and the first time an existing account adds an email address.

Reads SMTP credentials from the environment (EMAIL_ADDRESS / EMAIL_PASSWORD)
so deployments can drop them into .env without touching code. Host and port
default to Gmail's SMTP submission endpoint, the most common case; both can
be overridden via SMTP_HOST / SMTP_PORT for any other provider.

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


def send_verification_code(to_email: str, code: str) -> None:
    """Send a one-time 6-digit verification code to `to_email`.

    Raises EmailSendError on any failure — missing config, login refused, or
    network error. The caller is expected to show a soft, user-facing message
    rather than a stack trace.
    """
    address = os.environ.get("EMAIL_ADDRESS", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    if not address or not password:
        raise EmailSendError(
            "Email sending isn't set up on this server yet. Please ask the "
            "administrator to configure EMAIL_ADDRESS and EMAIL_PASSWORD."
        )
    host = os.environ.get("SMTP_HOST", _DEFAULT_SMTP_HOST).strip() or _DEFAULT_SMTP_HOST
    try:
        port = int(os.environ.get("SMTP_PORT", _DEFAULT_SMTP_PORT))
    except ValueError:
        port = _DEFAULT_SMTP_PORT

    msg = EmailMessage()
    msg["Subject"] = "Your Aime verification code"
    msg["From"] = address
    msg["To"] = to_email
    msg.set_content(
        f"Hi there,\n\n"
        f"Your Aime verification code is: {code}\n\n"
        f"It will expire in 10 minutes.\n\n"
        f"If you didn't ask for this, you can safely ignore this email — "
        f"no changes will be made to any account.\n"
    )

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(address, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError(
            "We couldn't send the verification email right now. "
            "Please try again in a moment."
        ) from exc
