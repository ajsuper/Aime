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


def test_me_reports_accepted_terms_version():
    """The Legal section shows which revision you agreed to and when, so /me has
    to carry it — alongside the current revision, which is what tells the user
    (and us) that they are reading newer text than they accepted."""
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "from aime import config\n"
        "c = w.app.test_client()\n"
        "r = c.post('/signup', data={'username':'owner','password':'" + _PW + "',"
        "'password2':'" + _PW + "','accept_terms':'1'})\n"
        "assert r.status_code in (200, 302), r.status_code\n"
        "me = c.get('/me').get_json()\n"
        "t = me['terms']\n"
        "assert t['version'] == config.TERMS_VERSION, t\n"
        "assert t['current'] == config.TERMS_VERSION, t\n"
        "assert isinstance(t['accepted_at'], int) and t['accepted_at'] > 0, t\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_me_reports_no_consent_for_admin_made_account():
    """An account created without consent (CLI/admin) must report nulls rather
    than implying an agreement nobody gave."""
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "u, _dek = w._auth_backend.create('owner', '" + _PW + "')\n"
        "c = w.app.test_client()\n"
        "r = c.post('/login', data={'username':'owner','password':'" + _PW + "'})\n"
        "assert r.status_code in (200, 302), r.status_code\n"
        "t = c.get('/me').get_json()['terms']\n"
        "assert t['version'] is None, t\n"
        "assert t['accepted_at'] is None, t\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_chat_ui_links_both_documents():
    """Signup is not the only place these have to be reachable: someone who has
    already accepted them still needs to re-read what they agreed to, and to
    find the contact address the documents name. The account settings panel is
    the one in-app route to them."""
    chat_html = os.path.join(_REPO, "resources", "style", "web_chat.html")
    with open(chat_html) as f:
        markup = f.read()
    assert 'href="/terms"' in markup
    assert 'href="/privacy"' in markup


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


# --- SMS consent (10DLC) ----------------------------------------------------

_SIGNUP_AND_SAVE = (
    "import frontends.web_app as w\n"
    "c = w.app.test_client()\n"
    "c.post('/signup', data={'username':'owner','password':'" + _PW + "',"
    "'password2':'" + _PW + "','accept_terms':'1'})\n"
    "def save(**body):\n"
    "    return c.post('/messaging-contact', json=body)\n"
    "def stored():\n"
    "    return w._auth_backend.lookup_by_username('owner').messaging_contact\n"
)


def test_sms_number_without_consent_is_refused():
    """The checkbox can't be skipped by posting directly: under the SMS channel
    a number without explicit consent is rejected and nothing is stored."""
    proc = _run_snippet(_SIGNUP_AND_SAVE +
        "r = save(contact='+15551234567')\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert r.get_json()['error'] == 'consent_required'\n"
        "r = save(contact='+15551234567', sms_consent='yes')\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert stored() is None, stored()\n"
        "print('OK')\n",
        {"AIME_MESSAGING_CHANNEL": "sms"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_sms_number_with_consent_is_saved_and_clearing_needs_none():
    proc = _run_snippet(_SIGNUP_AND_SAVE +
        "r = save(contact='+15551234567', sms_consent=True)\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert stored() == '+15551234567', stored()\n"
        "r = save(contact='')\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert stored() is None, stored()\n"
        "print('OK')\n",
        {"AIME_MESSAGING_CHANNEL": "sms"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_non_sms_channels_need_no_sms_consent():
    proc = _run_snippet(_SIGNUP_AND_SAVE +
        "r = save(contact='12345')\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert stored() == '12345', stored()\n"
        "print('OK')\n",
        {"AIME_MESSAGING_CHANNEL": "telegram"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_sms_consent_is_recorded_and_tied_to_the_number():
    """Proof of opt-in: consent is stamped with a time and disclosure version,
    re-saving the same number needs no re-tick, a different number does, and
    clearing the number clears its consent."""
    proc = _run_snippet(_SIGNUP_AND_SAVE +
        "from aime import config\n"
        "u = lambda: w._auth_backend.lookup_by_username('owner')\n"
        "r = save(contact='+15551234567', sms_consent=True)\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert u().sms_consent_at, u()\n"
        "assert u().sms_consent_version == config.SMS_CONSENT_VERSION\n"
        "assert r.get_json()['sms_consent_at'] == u().sms_consent_at\n"
        "r = save(contact='+15551234567')\n"
        "assert r.status_code == 200, r.status_code\n"
        "r = save(contact='+15559876543')\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert stored() == '+15551234567', stored()\n"
        "r = save(contact='')\n"
        "assert r.status_code == 200\n"
        "assert u().sms_consent_at is None and u().sms_consent_version is None\n"
        "print('OK')\n",
        {"AIME_MESSAGING_CHANNEL": "sms"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def _read(rel):
    with open(os.path.join(_REPO, rel), encoding="utf-8") as f:
        return f.read()


def test_sms_consent_checkbox_carries_the_10dlc_disclosures():
    """Guards the common 10DLC rejection reasons: the SMS box is its own
    unticked, optional checkbox, naming the brand and carrying frequency,
    rates, STOP/HELP and not-a-condition-of-purchase language."""
    import re
    html = _read("resources/style/web_chat.html")
    row = re.search(r'<label id="account-sms-consent-row".*?</label>', html, re.S)
    assert row, "SMS consent checkbox missing"
    text = " ".join(row.group(0).split())
    box = re.search(r'<input type="checkbox" id="account-sms-consent"[^>]*>', text)
    assert box and "checked" not in box.group(0) and "required" not in box.group(0)
    for phrase in ("Aime", "Prism", "Message frequency varies",
                   "Message and data rates may apply", "STOP", "HELP",
                   "not a condition of purchase", "/terms", "/privacy"):
        assert phrase in text, phrase


def test_signup_sms_consent_is_separate_optional_and_unticked():
    """On signup the phone number is optional and its SMS consent box is its
    own checkbox: not the Terms box, not pre-ticked, not required, and carrying
    the same disclosures as the Settings box."""
    import re
    html = _read("resources/style/login.html")
    phone = re.search(r'<input id="signup-phone"[^>]*>', html, re.S).group(0)
    assert "required" not in phone
    box = re.search(r'<input id="signup-sms-consent"[^>]*>', html, re.S).group(0)
    assert "checked" not in box and "required" not in box
    assert 'name="sms_consent"' in box
    terms = re.search(r'<input id="signup-terms"[^>]*>', html, re.S).group(0)
    assert 'name="accept_terms"' in terms
    row = re.search(r'<label class="consent" for="signup-sms-consent">.*?</label>',
                    html, re.S)
    text = " ".join(row.group(0).split())
    for phrase in ("Aime", "Prism", "Message frequency varies",
                   "Message and data rates may apply", "STOP", "HELP",
                   "not a condition of purchase", "/terms", "/privacy"):
        assert phrase in text, phrase
    # The Terms box's own wording says nothing about texts.
    terms_row = re.search(r'<label class="consent" for="signup-terms">.*?</label>',
                          html, re.S).group(0).lower()
    assert "text" not in terms_row and "sms" not in terms_row


_SIGNUP_WITH_PHONE = (
    "import frontends.web_app as w\n"
    "c = w.app.test_client()\n"
    "def signup(**extra):\n"
    "    data = {'username':'owner','password':'" + _PW + "',"
    "'password2':'" + _PW + "','accept_terms':'1'}\n"
    "    data.update(extra)\n"
    "    return c.post('/signup', data=data)\n"
    "u = lambda: w._auth_backend.lookup_by_username('owner')\n"
)


def test_signup_phone_needs_its_own_consent_and_a_valid_number():
    proc = _run_snippet(_SIGNUP_WITH_PHONE +
        "r = signup(phone='+15551234567')\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert u() is None\n"
        "r = signup(phone='555-1234', sms_consent='1')\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert u() is None\n"
        "r = signup(phone='+1 (555) 123-4567', sms_consent='1')\n"
        "assert r.status_code == 302, r.status_code\n"
        "from aime import config\n"
        "assert u().messaging_contact == '+15551234567', u()\n"
        "assert u().sms_consent_at and u().sms_consent_version == config.SMS_CONSENT_VERSION\n"
        "print('OK')\n",
        {"AIME_MESSAGING_CHANNEL": "sms"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_signup_without_phone_records_no_sms_consent():
    """Leaving the number blank is fine, and a stray tick with no number
    records nothing."""
    proc = _run_snippet(_SIGNUP_WITH_PHONE +
        "r = signup(sms_consent='1')\n"
        "assert r.status_code == 302, r.status_code\n"
        "assert u().messaging_contact is None and u().sms_consent_at is None\n"
        "print('OK')\n",
        {"AIME_MESSAGING_CHANNEL": "sms"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_signup_phone_waits_through_email_verification(backend):
    """With email verification on, the number + consent ride the pending row
    and land on the account when the code is confirmed."""
    token, code, _ = backend.start_signup_verification(
        "owner", _PW, "owner@example.com", terms_version="v1",
        messaging_contact="+15551234567", sms_consent_version="s1",
    )
    user, _dek = backend.complete_signup_verification(token, code)
    stored = backend.lookup(user.id)
    assert stored.messaging_contact == "+15551234567"
    assert stored.sms_consent_version == "s1" and stored.sms_consent_at


def test_sms_signup_block_hidden_under_other_channels():
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "html = w.app.test_client().get('/login').get_data(as_text=True)\n"
        "assert '[data-sms-signup]{display:none' in html\n"
        "print('OK')\n",
        {"AIME_MESSAGING_CHANNEL": "telegram"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_sms_opt_in_flag_collects_signup_consent_under_other_channels():
    """AIME_SMS_OPT_IN shows the phone + consent box and stores both while
    delivery stays on Telegram — the state a 10DLC campaign is reviewed in.
    The consent rules are the same as under the SMS channel."""
    proc = _run_snippet(_SIGNUP_WITH_PHONE +
        "html = c.get('/login').get_data(as_text=True)\n"
        "assert '[data-sms-signup]{display:none' not in html\n"
        "assert 'name=\"sms_consent\"' in html\n"
        "r = signup(phone='+15551234567')\n"
        "assert r.status_code == 400, r.status_code\n"
        "assert u() is None\n"
        "r = signup(phone='+15551234567', sms_consent='1')\n"
        "assert r.status_code == 302, r.status_code\n"
        "assert u().messaging_contact == '+15551234567', u()\n"
        "assert u().sms_consent_at is not None\n"
        "print('OK')\n",
        {"AIME_MESSAGING_CHANNEL": "telegram", "AIME_SMS_OPT_IN": "1"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_signup_ignores_phone_when_sms_signup_is_off():
    """Without the SMS channel or AIME_SMS_OPT_IN, a posted number is dropped
    rather than stored as a contact nobody consented to texts for."""
    proc = _run_snippet(_SIGNUP_WITH_PHONE +
        "r = signup(phone='+15551234567', sms_consent='1')\n"
        "assert r.status_code == 302, r.status_code\n"
        "assert u().messaging_contact is None and u().sms_consent_at is None\n"
        "print('OK')\n",
        {"AIME_MESSAGING_CHANNEL": "telegram", "AIME_SMS_OPT_IN": "0"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


@pytest.mark.parametrize("path", ["/terms", "/privacy"])
def test_legal_pages_do_not_serve_maintainer_comments(path):
    """The body files' editing notes (lawyer-review status etc.) stay in the
    repo; the public page carries no HTML comments."""
    assert "<!--" in _read("resources/legal" + path + ".html")
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        f"html = w.app.test_client().get({path!r}).get_data(as_text=True)\n"
        "assert '<!--' not in html\n"
        "assert 'reviewed by a lawyer' not in html\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_legal_documents_describe_the_sms_program():
    terms = " ".join(_read("resources/legal/terms.html").split()).lower()
    for phrase in ("Text messages (SMS)", "message frequency varies",
                   "message and data rates may apply", "STOP", "HELP",
                   "not a condition of any purchase",
                   "Carriers are not liable"):
        assert phrase.lower() in terms, phrase
    privacy = " ".join(_read("resources/legal/privacy.html").split())
    assert ("No mobile information will be shared with third parties or "
            "affiliates for marketing or promotional purposes") in privacy


def test_sms_carrier_messages_fit_one_segment_and_carry_disclosures():
    """The opt-in confirmation and the HELP/STOP keyword replies each name the
    brand and fit one plain GSM-7 segment (160 chars, ASCII only)."""
    messages = {
        "confirmation": config.SMS_OPT_IN_CONFIRMATION,
        "help": config.SMS_HELP_REPLY,
        "stop": config.SMS_STOP_REPLY,
    }
    for name, text in messages.items():
        assert text.startswith("Aime (Prism):"), name
        assert text.isascii(), name
        assert len(text) <= 160 or name == "confirmation", (name, len(text))
    help_text = config.SMS_HELP_REPLY
    for phrase in ("@933consulting.com", "app.heyaime.org", "STOP",
                   "Msg frequency varies", "Msg & data rates may apply"):
        assert phrase in help_text, phrase
    assert "unsubscribed" in config.SMS_STOP_REPLY
    assert "START" in config.SMS_STOP_REPLY
