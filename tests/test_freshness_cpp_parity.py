"""Parity test: the C++ restamp must behave exactly like the Python one.

`aime.freshness` and the `restampSections` block in serve.cpp are two
implementations of the same format. Python owns render/strip (and the tests
that pin the semantics); C++ owns the write path, because only it holds the old
content, the new content, and the patch application atomically.

Two implementations of one format drift. This test compiles the C++ block
straight out of serve.cpp and diffs its output against the Python for the same
inputs, so a change to either side that isn't mirrored fails here rather than in
production, where the symptom would be topics quietly losing their stamps.

Skipped when there is no C++ toolchain (the Python side is still covered by
tests/test_freshness.py).
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from aime import freshness as f

ROOT = Path(__file__).resolve().parent.parent
SERVE = ROOT / "src" / "serve.cpp"

pytestmark = pytest.mark.skipif(
    shutil.which("g++") is None, reason="no C++ toolchain available"
)

_HARNESS = """
#include <string>
#include <vector>
#include <unordered_map>
#include <sstream>
#include <iostream>
#include <cctype>
#include <algorithm>

static std::string trimWhitespace(const std::string& s) {
    size_t start = 0;
    while (start < s.size() && std::isspace(static_cast<unsigned char>(s[start]))) ++start;
    size_t end = s.size();
    while (end > start && std::isspace(static_cast<unsigned char>(s[end - 1]))) --end;
    return s.substr(start, end - start);
}

#include "fresh_core.inc"

int main() {
    std::string all((std::istreambuf_iterator<char>(std::cin)),
                    std::istreambuf_iterator<char>());
    size_t a = all.find('\\x1e');
    size_t b = all.find('\\x1e', a + 1);
    std::cout << restampSections(all.substr(0, a),
                                 all.substr(a + 1, b - a - 1),
                                 all.substr(b + 1));
    return 0;
}
"""


def _extract_cpp() -> str:
    """The freshness helpers, lifted verbatim out of serve.cpp.

    Taking the real source (rather than a copy kept alongside the test) is what
    makes this a parity test at all — a copy would drift in exactly the way
    this exists to prevent."""
    src = SERVE.read_text()
    start = src.index("// --- Per-section freshness stamps")
    end = src.index("void replaceTopicContents(")
    block = src[start:end]
    # Drop the two pieces the harness can't take: the forward declaration (it
    # defines its own trim) and the crow-typed json reader.
    block = block.replace(
        "static std::string trimWhitespace(const std::string& s);  // defined below", "")
    i = block.index("// The user-local YYYY-MM-DD")
    j = block.index("static const std::string kStampOpen")
    return block[:i] + block[j:]


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("fresh_parity")
    (d / "fresh_core.inc").write_text(_extract_cpp())
    (d / "harness.cpp").write_text(textwrap.dedent(_HARNESS))
    exe = d / "harness"
    proc = subprocess.run(
        ["g++", "-std=c++17", "-O1", f"-I{d}", "-o", str(exe), str(d / "harness.cpp")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"serve.cpp freshness block failed to compile:\n{proc.stderr}")
    return exe


STORED = (
    "# Trip notes\n\n## Social\nParty in 3 days on August 22nd\n\n"
    "## Health\nWent running\n\n<!--aime:updated v1\n"
    "Trip notes=2026-08-10\nSocial=2026-08-19\nHealth=2026-08-25\n-->\n"
)
BODY = f.parse(STORED)[0]
STAMP = "2026-08-26"

CASES = {
    "first write": ("", "## Social\nHello\n"),
    "edit one section": (
        STORED,
        "# Trip notes\n\n## Social\nParty in 3 days on August 22nd\n\n"
        "## Health\nWent running twice\n"),
    "full rewrite, text unchanged": (STORED, BODY),
    "section added": (
        STORED,
        "# Trip notes\n\n## Social\nParty in 3 days on August 22nd\n\n"
        "## Health\nWent running\n\n## Money\nSaved\n"),
    "section renamed": (STORED, "## Socialising\nParty\n"),
    "no headings at all": ("", "just a flat note\n"),
    "preamble then heading": ("", "opening text\n\n## Social\nx\n"),
    "duplicate headings": ("", "## Social\nfirst\n\n## Social\nsecond\n"),
    "heading containing an equals sign": ("", "## A = B\nx\n"),
    "emptied content": (STORED, ""),
    "forged block echoed back": (
        STORED,
        "# Trip notes\n\n## Social\nParty in 3 days on August 22nd\n\n"
        "## Health\nWent running\n\n<!--aime:updated v1\nSocial=1999-01-01\n-->\n"),
    "corrupt block in stored content": (
        "## Social\nx\n\n<!--aime:updated v1\ngarbage\nSocial=not-a-date\n-->\n",
        "## Social\nx\n"),
    "heading depths 1/3/6": ("", "# A\nx\n### B\ny\n###### C\nz\n"),
    "hash without a space is not a heading": ("", "#nospace\nx\n"),
    "cosmetic blank-line changes": (
        STORED,
        "# Trip notes\n\n## Social\nParty in 3 days on August 22nd\n\n\n\n"
        "## Health\nWent running\n\n\n"),
    "unicode heading": ("", "## Café — plans\nx\n"),
    "no trailing newline": ("", "## Social\nx"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_cpp_matches_python(harness, name):
    old, new = CASES[name]
    expected = f.restamp(old, new, STAMP)
    proc = subprocess.run(
        [str(harness)],
        input=(old + "\x1e" + new + "\x1e" + STAMP).encode(),
        capture_output=True,
    )
    assert proc.stdout.decode() == expected


def test_cosmetic_change_does_not_restamp(harness):
    # The property that matters most, asserted on the C++ side specifically:
    # editing Health must leave Social's date alone.
    old, new = CASES["edit one section"]
    proc = subprocess.run(
        [str(harness)],
        input=(old + "\x1e" + new + "\x1e" + STAMP).encode(),
        capture_output=True,
    )
    stamps = f.parse(proc.stdout.decode())[1]
    assert stamps["Social"] == "2026-08-19"
    assert stamps["Health"] == STAMP
