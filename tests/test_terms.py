"""Tests for Terms-of-Service consent at signup and the public legal pages.

The consent checkbox on the signup form is only a browser convenience — the
gate that matters is server-side, so most of these drive POST /signup directly
(a form post with no `accept_terms`, exactly what a scripted signup would send).
The web-layer tests run in clean subprocesses, matching test_billing.py: each
needs its own AIME_ALLOW_SIGNUP env without leaking it across the suite.
"""

import os
import subprocess
import sys
import tempfile

import pytest

from aime import config
from aime.auth import LocalAuthBackend

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PW = "Sufficiently-long-pw-1"


@pytest.fixture
def backend(tmp_path):
    return LocalAuthBackend(os.path.join(str(tmp_path), "auth", "auth.sql"))


def _run_snippet(snippet, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    env["AIME_DATABASE_DIR"] = tempfile.mkdtemp()
    env.setdefault("AIME_ALLOW_SIGNUP", "1")
    full = "import sys; sys.path.insert(0, 'src')\n" + snippet
    return subprocess.run([sys.executable, "-c", full], cwd=_REPO,
                          capture_output=True, text=True, env=env)


# --- the signup gate --------------------------------------------------------

def test_signup_without_consent_creates_no_account():
    """A POST that omits accept_terms is refused, and leaves no account behind —
    the checkbox can't be skipped by posting the form directly."""
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "r = c.post('/signup', data={'username':'owner','password':'" + _PW + "',"
        "'password2':'" + _PW + "'})\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert b'Terms of Service' in r.data, r.data[:400]\n"
        "assert w._auth_backend.lookup_by_username('owner') is None\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_signup_rejects_unchecked_box_value():
    """An unchecked box sends nothing; a tampered value that isn't '1' is not
    consent either."""
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "r = c.post('/signup', data={'username':'owner','password':'" + _PW + "',"
        "'password2':'" + _PW + "','accept_terms':'0'})\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert w._auth_backend.lookup_by_username('owner') is None\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_signup_with_consent_records_version_and_time():
    """Consent creates the account and stamps *which* revision was agreed to."""
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "from aime import config\n"
        "c = w.app.test_client()\n"
        "r = c.post('/signup', data={'username':'owner','password':'" + _PW + "',"
        "'password2':'" + _PW + "','accept_terms':'1'})\n"
        "assert r.status_code in (200, 302), r.status_code\n"
        "u = w._auth_backend.lookup_by_username('owner')\n"
        "assert u is not None\n"
        "assert u.terms_version == config.TERMS_VERSION, u.terms_version\n"
        "assert u.terms_accepted_at and u.terms_accepted_at > 0, u.terms_accepted_at\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


# --- the public legal pages -------------------------------------------------

@pytest.mark.parametrize("path,heading", [
    ("/terms", b"Terms of Service"),
    ("/privacy", b"Privacy Policy"),
])
def test_legal_pages_are_public(path, heading):
    """Both documents must render without a session — the signup form links to
    them, so they have to be readable before an account exists."""
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "from aime import config\n"
        "c = w.app.test_client()\n"
        f"r = c.get({path!r})\n"
        "assert r.status_code == 200, r.status_code\n"
        f"assert {heading!r} in r.data, r.data[:400]\n"
        # The version is substituted, not left as the raw placeholder.
        "assert b'__TERMS_VERSION__' not in r.data\n"
        "assert config.TERMS_VERSION.encode() in r.data\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_signup_form_links_both_documents():
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "r = c.get('/login')\n"
        "assert b'name=\"accept_terms\"' in r.data\n"
        "assert b'href=\"/terms\"' in r.data\n"
        "assert b'href=\"/privacy\"' in r.data\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


# --- persistence ------------------------------------------------------------

def test_direct_create_records_consent(backend):
    user, _dek = backend.create("alice", _PW, terms_version="2026-07-22")
    assert user.terms_version == "2026-07-22"
    assert user.terms_accepted_at > 0
    # And it survives a round-trip through the canonical projection.
    fetched = backend.lookup(user.id)
    assert fetched.terms_version == "2026-07-22"
    assert fetched.terms_accepted_at == user.terms_accepted_at


def test_create_without_consent_records_nothing(backend):
    """CLI/admin-created accounts pass no version; we record no consent rather
    than inventing one."""
    user, _dek = backend.create("alice", _PW)
    assert user.terms_version is None
    assert user.terms_accepted_at is None
    assert backend.lookup(user.id).terms_accepted_at is None


def test_consent_survives_email_verification(backend):
    """The users row only appears once the emailed code is confirmed, so the
    agreed revision has to ride along on the pending verification row."""
    token, code, _email = backend.start_signup_verification(
        "alice", _PW, "alice@example.com", terms_version="2026-07-22",
    )
    user, _dek = backend.complete_signup_verification(token, code)
    assert user.terms_version == "2026-07-22"
    assert user.terms_accepted_at > 0
    assert backend.lookup(user.id).terms_version == "2026-07-22"


def test_terms_version_is_configured(backend):
    """A blank version would record consent to nothing identifiable."""
    assert config.TERMS_VERSION
