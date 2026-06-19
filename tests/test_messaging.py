"""Tests for the outbound messaging layer (``aime.messaging``).

This layer touches the network (Telegram) and SMTP (email), so the contract
that matters most is its *failure* behavior: every transport problem must
surface as a ``MessageSendError`` carrying a calm, user-facing message, while
the noisy provider detail (status codes, response bodies) stays on the
exception chain for logs — never in the message a user or the model sees.

No live network is used: ``requests.post`` and the SMTP sender are stubbed.
"""

import pytest

from aime import messaging
from aime.messaging import (
    MessageSendError,
    get_messenger,
    messaging_enabled,
    env_recipient,
)
from aime.messaging.telegram import TelegramChannel
from aime.messaging.email_channel import EmailChannel
from aime.messaging import telegram as telegram_mod


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, ok=True, status_code=200, text="{\"ok\": true}"):
        self.ok = ok
        self.status_code = status_code
        self.text = text


class _RecordingPost:
    """Stand-in for ``requests.post`` that records its last call."""

    def __init__(self, response=None, raises=None):
        self._response = response or _FakeResponse()
        self._raises = raises
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture
def patched_post(monkeypatch):
    """Install a recording fake over ``telegram.requests.post`` and hand it back
    so a test can assert on what was sent."""
    def _install(response=None, raises=None):
        fake = _RecordingPost(response=response, raises=raises)
        monkeypatch.setattr(telegram_mod.requests, "post", fake)
        return fake
    return _install


# --------------------------------------------------------------------------- #
# TelegramChannel — guard rails (no network reached)
# --------------------------------------------------------------------------- #
def test_telegram_missing_token_is_friendly_and_never_calls_network(patched_post):
    fake = patched_post()
    chan = TelegramChannel(token="")
    with pytest.raises(MessageSendError) as exc:
        chan.send("12345", "hi")
    # Friendly, points the operator at the right env var; no stack-trace leak.
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)
    # Crucially, a misconfigured channel must short-circuit before any POST.
    assert fake.calls == []


def test_telegram_empty_recipient_raises_before_network(patched_post):
    fake = patched_post()
    chan = TelegramChannel(token="t0ken")
    with pytest.raises(MessageSendError):
        chan.send("   ", "hi")
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# TelegramChannel — transport failures surface calmly, detail stays on the chain
# --------------------------------------------------------------------------- #
def test_telegram_request_exception_becomes_friendly_error(patched_post):
    boom = telegram_mod.requests.RequestException("connection reset")
    patched_post(raises=boom)
    chan = TelegramChannel(token="t0ken")
    with pytest.raises(MessageSendError) as exc:
        chan.send("12345", "hi")
    assert "try again" in str(exc.value).lower()
    # The raw transport error is preserved for logs, not shown to the user.
    assert exc.value.__cause__ is boom
    assert "connection reset" not in str(exc.value)


def test_telegram_non_ok_response_does_not_leak_provider_body(patched_post):
    patched_post(response=_FakeResponse(
        ok=False, status_code=403,
        text='{"ok":false,"description":"bot was blocked by the user"}',
    ))
    chan = TelegramChannel(token="t0ken")
    with pytest.raises(MessageSendError) as exc:
        chan.send("12345", "hi")
    user_msg = str(exc.value)
    # The Telegram description must not reach the user-facing message...
    assert "blocked" not in user_msg
    assert "403" not in user_msg
    # ...but it is on the chain for the logs.
    assert "403" in str(exc.value.__cause__)


# --------------------------------------------------------------------------- #
# TelegramChannel — happy path shape
# --------------------------------------------------------------------------- #
def test_telegram_send_posts_expected_payload(patched_post):
    fake = patched_post()
    chan = TelegramChannel(token="abc", timeout=9.0)
    chan.send("  98765  ", "the body")
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://api.telegram.org/botabc/sendMessage"
    assert call["json"]["chat_id"] == "98765"          # recipient trimmed
    assert call["json"]["text"] == "the body"
    assert call["timeout"] == 9.0                        # timeout always set


def test_telegram_subject_is_folded_into_body(patched_post):
    fake = patched_post()
    chan = TelegramChannel(token="abc")
    chan.send("1", "line two", subject="Heads up")
    body = fake.calls[0]["json"]["text"]
    assert body.startswith("Heads up")
    assert "line two" in body


# --------------------------------------------------------------------------- #
# EmailChannel — re-wraps the SMTP error type into the messaging error type
# --------------------------------------------------------------------------- #
def test_email_channel_rewraps_send_error(monkeypatch):
    from aime import email_send

    def _boom(to, subject, body):
        raise email_send.EmailSendError("smtp down")

    monkeypatch.setattr(email_send, "send_email", _boom)
    chan = EmailChannel()
    with pytest.raises(MessageSendError) as exc:
        chan.send("a@b.com", "hi", subject="s")
    # Callers only ever catch MessageSendError, whatever the channel.
    assert isinstance(exc.value.__cause__, email_send.EmailSendError)


def test_email_channel_passes_default_subject(monkeypatch):
    from aime import email_send
    seen = {}

    def _capture(to, subject, body):
        seen.update(to=to, subject=subject, body=body)

    monkeypatch.setattr(email_send, "send_email", _capture)
    EmailChannel().send("a@b.com", "body only")
    assert seen["subject"]  # a non-empty default subject is supplied
    assert seen["to"] == "a@b.com"


# --------------------------------------------------------------------------- #
# Registry / selection
# --------------------------------------------------------------------------- #
def test_messaging_enabled_defaults_on_and_parses_flags(monkeypatch):
    monkeypatch.delenv("AIME_MESSAGING", raising=False)
    assert messaging_enabled() is True
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("AIME_MESSAGING", off)
        assert messaging_enabled() is False
    for on in ("1", "true", "YES", "On"):
        monkeypatch.setenv("AIME_MESSAGING", on)
        assert messaging_enabled() is True


def test_get_messenger_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("AIME_MESSAGING", "0")
    assert get_messenger() is None


def test_get_messenger_unknown_channel_returns_none(monkeypatch):
    monkeypatch.delenv("AIME_MESSAGING", raising=False)
    monkeypatch.setenv("AIME_MESSAGING_CHANNEL", "carrier-pigeon")
    assert get_messenger() is None


def test_get_messenger_default_is_telegram(monkeypatch):
    monkeypatch.delenv("AIME_MESSAGING", raising=False)
    monkeypatch.delenv("AIME_MESSAGING_CHANNEL", raising=False)
    assert isinstance(get_messenger(), TelegramChannel)


def test_get_messenger_selects_email(monkeypatch):
    monkeypatch.delenv("AIME_MESSAGING", raising=False)
    monkeypatch.setenv("AIME_MESSAGING_CHANNEL", "EMAIL")  # case-insensitive
    assert isinstance(get_messenger(), EmailChannel)


def test_env_recipient_parsing(monkeypatch):
    monkeypatch.delenv("AIME_MESSAGING_CONTACT", raising=False)
    assert env_recipient() is None
    monkeypatch.setenv("AIME_MESSAGING_CONTACT", "   ")
    assert env_recipient() is None
    monkeypatch.setenv("AIME_MESSAGING_CONTACT", "  12345  ")
    assert env_recipient() == "12345"
