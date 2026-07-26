"""Tests for the self-hosted webfonts.

Fraunces and Hanken Grotesk used to be pulled from Google Fonts, which the app's
own CSP blocked (`style-src`/`font-src` are `'self'`), so every page silently
fell back to system faces. They are served from our origin now. These tests pin
the two ways that can regress: a page reintroducing a Google <link>, and the
/fonts route failing to serve a file some @font-face references.
"""

import os
import glob
import re
import subprocess
import sys
import tempfile

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STYLE_DIR = os.path.join(_REPO, "resources", "style")
_FONTS_DIR = os.path.join(_STYLE_DIR, "fonts")


def _run_snippet(snippet):
    env = dict(os.environ)
    env["AIME_DATABASE_DIR"] = tempfile.mkdtemp()
    env.setdefault("AIME_ALLOW_SIGNUP", "1")
    full = "import sys; sys.path.insert(0, 'src')\n" + snippet
    return subprocess.run([sys.executable, "-c", full], cwd=_REPO,
                          capture_output=True, text=True, env=env)


def _pages():
    return sorted(glob.glob(os.path.join(_STYLE_DIR, "*.html")))


# --- the markup side --------------------------------------------------------

def test_no_page_loads_google_fonts():
    """A Google Fonts <link> would be blocked by our CSP and fall back silently
    — the failure mode is invisible, so it has to be caught here."""
    offenders = [
        os.path.basename(p) for p in _pages()
        if "fonts.googleapis.com" in open(p, encoding="utf-8").read()
        or "fonts.gstatic.com" in open(p, encoding="utf-8").read()
    ]
    assert offenders == [], offenders


def test_every_page_links_the_local_stylesheet():
    missing = [
        os.path.basename(p) for p in _pages()
        if '/fonts/fonts.css' not in open(p, encoding="utf-8").read()
    ]
    assert missing == [], missing


# --- the files themselves ---------------------------------------------------

def _declared_font_files():
    css = open(os.path.join(_FONTS_DIR, "fonts.css"), encoding="utf-8").read()
    return re.findall(r"url\('/fonts/([^']+)'\)", css)


def test_stylesheet_declares_both_families():
    css = open(os.path.join(_FONTS_DIR, "fonts.css"), encoding="utf-8").read()
    assert "font-family: 'Fraunces'" in css
    assert "font-family: 'Hanken Grotesk'" in css
    # Every face must keep a unicode-range: that's what stops a browser
    # downloading all four subsets for a page of plain English.
    assert css.count("@font-face") == css.count("unicode-range:")


def test_declared_files_exist_and_are_woff2():
    declared = _declared_font_files()
    assert declared, "stylesheet declares no font files"
    for name in declared:
        path = os.path.join(_FONTS_DIR, name)
        assert os.path.exists(path), name
        with open(path, "rb") as f:
            assert f.read(4) == b"wOF2", f"{name} is not a woff2 file"


def test_no_orphan_font_files():
    """Every shipped .woff2 is referenced; nothing dead is being served."""
    on_disk = {os.path.basename(p)
               for p in glob.glob(os.path.join(_FONTS_DIR, "*.woff2"))}
    assert on_disk == set(_declared_font_files())


def test_weight_ranges_cover_every_weight_used():
    """Both families are variable fonts declared with a weight *range*. If a
    page asks for a weight outside it, that text renders at the clamped weight
    — so the ranges have to cover what the CSS actually uses."""
    css = open(os.path.join(_FONTS_DIR, "fonts.css"), encoding="utf-8").read()
    ranges = {}
    for fam, lo, hi in re.findall(
        r"font-family: '([^']+)';\s*font-style: normal;\s*"
        r"font-weight: (\d+) (\d+);", css
    ):
        ranges[fam] = (int(lo), int(hi))
    assert set(ranges) == {"Fraunces", "Hanken Grotesk"}, ranges

    # Weights actually requested across every page, split by which family the
    # rule selects (--font-display is Fraunces, everything else falls to body).
    display_weights, body_weights = set(), set()
    for p in _pages():
        text = open(p, encoding="utf-8").read()
        for rule in re.findall(r"\{[^{}]*\}", text):
            m = re.search(r"font-weight:\s*(\d+)", rule)
            if not m:
                continue
            w = int(m.group(1))
            if "var(--font-display)" in rule:
                display_weights.add(w)
            else:
                body_weights.add(w)

    lo, hi = ranges["Fraunces"]
    outside = {w for w in display_weights if not lo <= w <= hi}
    assert not outside, f"Fraunces used at {outside}, declared {lo}-{hi}"

    lo, hi = ranges["Hanken Grotesk"]
    outside = {w for w in body_weights if not lo <= w <= hi}
    assert not outside, f"Hanken Grotesk used at {outside}, declared {lo}-{hi}"


# --- the route --------------------------------------------------------------

def test_font_assets_serve_unauthenticated():
    """The login page needs them before a session exists, so /fonts is public.
    Each declared file must actually serve — a 404 here is an invisible
    fallback to system fonts in the browser."""
    declared = _declared_font_files()
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "r = c.get('/fonts/fonts.css')\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert 'text/css' in r.headers['Content-Type'], r.headers['Content-Type']\n"
        f"for name in {declared!r}:\n"
        "    r = c.get('/fonts/' + name)\n"
        "    assert r.status_code == 200, (name, r.status_code)\n"
        "    assert r.headers['Content-Type'] == 'font/woff2', (name, r.headers['Content-Type'])\n"
        "    assert r.data[:4] == b'wOF2', name\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_font_route_rejects_traversal_and_other_extensions():
    """The route takes a wildcard path, so it must not become a file-read of
    anything outside the fonts directory."""
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "for bad in ['../../../etc/passwd', '../legal/terms.html',\n"
        "            '../web_chat.html', '../login.html', 'secret.txt',\n"
        "            'fonts.css/../../login.html']:\n"
        "    r = c.get('/fonts/' + bad)\n"
        "    assert r.status_code == 404, (bad, r.status_code)\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout


def test_font_assets_are_cached_hard():
    proc = _run_snippet(
        "import frontends.web_app as w\n"
        "c = w.app.test_client()\n"
        "r = c.get('/fonts/fonts.css')\n"
        "assert 'max-age=31536000' in r.headers.get('Cache-Control', ''), "
        "r.headers.get('Cache-Control')\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OK" in proc.stdout
