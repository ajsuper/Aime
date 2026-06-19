"""Unit tests for the offloaded web-search agent (``aime.web_search_agent``).

The agent runs Anthropic's server-side web search on a cheap model and returns a
:class:`WebSearchResult`: the digest the conversational model reads *and* the
same citations as structured ``(title, url)`` pairs. That structured list is
what the frontend renders as a source bubble, so these tests pin the contract
that every cited source is harvested and surfaced — without touching the API
(a fake client supplies canned response blocks).
"""

from types import SimpleNamespace

from aime.web_search_agent import WebSearchAgent, WebSearchResult


def _text_block(text, citations=()):
    cites = [SimpleNamespace(url=u, title=t) for (t, u) in citations]
    return SimpleNamespace(type="text", text=text, citations=cites)


def _search_result_block(results):
    items = [
        SimpleNamespace(type="web_search_result", url=u, title=t)
        for (t, u) in results
    ]
    return SimpleNamespace(type="web_search_tool_result", content=items)


def _resp(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=None)


def _agent(responses):
    """A WebSearchAgent whose client replays `responses` one per create() call."""
    agent = WebSearchAgent(model="haiku", tool_version="web_search_20250305")
    it = iter(responses)

    class _Msgs:
        def create(self, **_kw):
            return next(it)

    agent._client = SimpleNamespace(messages=_Msgs())
    return agent


def test_search_returns_digest_and_structured_cited_sources():
    resp = _resp([
        _search_result_block([("Ignored Raw", "https://raw.example/x")]),
        _text_block(
            "Paris is the capital of France.",
            citations=[("Britannica — France", "https://britannica.com/france")],
        ),
    ])
    result = _agent([resp]).search("capital of France")

    assert isinstance(result, WebSearchResult)
    # The model actually cited Britannica — that's what surfaces, not the raw hit.
    assert result.sources == [("Britannica — France", "https://britannica.com/france")]
    # The same source is also embedded in the digest for the model to read.
    assert "Paris is the capital of France." in result.digest
    assert "https://britannica.com/france" in result.digest


def test_search_falls_back_to_raw_results_when_nothing_cited():
    # No citations on the text block — surface where it *looked* instead.
    resp = _resp([
        _search_result_block([
            ("Result One", "https://one.example"),
            ("Result Two", "https://two.example"),
        ]),
        _text_block("Here is a summary with no inline citations."),
    ])
    result = _agent([resp]).search("something")

    assert result.sources == [
        ("Result One", "https://one.example"),
        ("Result Two", "https://two.example"),
    ]


def test_sources_are_deduped_by_url_and_order_preserving():
    resp = _resp([
        _text_block(
            "First.",
            citations=[("A", "https://a.example"), ("B", "https://b.example")],
        ),
        _text_block(
            "Second.",
            # a.example repeats — must not appear twice.
            citations=[("A again", "https://a.example"), ("C", "https://c.example")],
        ),
    ])
    result = _agent([resp]).search("multi")

    assert [u for _, u in result.sources] == [
        "https://a.example", "https://b.example", "https://c.example",
    ]


def test_failure_returns_empty_sources_and_never_raises():
    agent = WebSearchAgent(model="haiku", tool_version="web_search_20250305")

    class _Boom:
        def create(self, **_kw):
            raise RuntimeError("network down")

    agent._client = SimpleNamespace(messages=_Boom())
    result = agent.search("anything")

    assert isinstance(result, WebSearchResult)
    assert result.sources == []
    assert result.digest  # a friendly, relayable line — not an exception
