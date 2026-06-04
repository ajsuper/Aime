"""Offloaded web search.

The conversational model (Sonnet) calls a single ``WebSearch`` tool with a
natural-language description of what it needs. That request is handed to this
agent, which runs Anthropic's native server-side web search on a cheap Haiku
model, lets Haiku read and reason over the raw results, and returns a dense
text summary plus a deterministically-harvested Sources list.

Why this exists (set by the operator, not the model):
  * Raw web_search results — snippets, fetched HTML, ``encrypted_content``
    blocks — are large, and once they land in the conversation they get
    re-read from the prompt cache on *every* later turn and re-summarised at
    every compaction. Keeping them inside this Haiku sub-call means the
    expensive model's persistent context only ever carries the compact
    summary. The bulk reading happens at ~1/3 the price and never re-bills.
  * The native web_search tool also stays out of the conversational model's
    cached tool schema (one small ``WebSearch`` schema replaces it), and the
    Haiku-model-support question for newer tool versions never arises — this
    agent picks the model and tool version explicitly.

Citations: the native citation machinery (``encrypted_index``) is tied to this
sub-call's request and can't be replayed into the caller's stream, so the
caller won't get native citation blocks. But the *factual* citation data —
source URL and title — is harvested in code from Haiku's response blocks and
appended as a Sources list, so the caller can still attribute and link. AiMe
renders its own chat, so it never depended on native citation rendering.

Failure-tolerant: any error returns a short factual string the caller can
relay, never an exception — a search failure must not break the turn.
"""

import datetime
from typing import Callable

from anthropic import Anthropic


_SYSTEM = (
    "You are a web research assistant for AiMe, a personal assistant. The "
    "request below comes from another AI delegating research to you — it may "
    "bundle several items or sub-questions into one task (e.g. tuition for ten "
    "colleges, specs for five phones). Run as many web searches as you need to "
    "cover EVERY part of the request, read the results, and return ONE dense, "
    "factual summary that answers the whole thing. When the request spans "
    "multiple items, structure the summary so each item is clearly covered (a "
    "labelled line or short section per item) and explicitly note any you "
    "could not find. Preserve specifics — names, dates, numbers, exact quotes "
    "where they matter — and flag disagreements between sources. Do not paste "
    "raw HTML or long passages. Another assistant will relay your summary to "
    "the user, so write findings, not a chat reply, and do not add a sources "
    "list yourself — the system appends one."
)


class WebSearchAgent:
    """Runs web search on a cheap model and returns a compact, cited digest.

    Thread-safety: ``search()`` may be called from the controller's tool thread
    across overlapping sessions. The Anthropic client is thread-safe and the
    agent holds no mutable per-call state, so no lock is needed.
    """

    def __init__(
        self,
        *,
        model: str,
        tool_version: str,
        usage_label: str | None = None,
        record_api: Callable | None = None,
        usage_source: str = "interactive",
        # A single request may now bundle many items (the caller is told to
        # batch — "tuition for 10 colleges" in one call), so the search budget
        # has to be generous enough to cover each. Each search is a flat
        # $10/1000 charge, so this also caps per-call cost.
        max_searches: int = 12,
        max_loops: int = 8,
        max_tokens: int = 4096,
    ):
        self._model = model
        self._tool = {
            "type": tool_version,
            "name": "web_search",
            "max_uses": max_searches,
        }
        self._usage_label = usage_label
        # When this search agent serves a background-agent run, its offloaded
        # Haiku searches should count as agent cost, not interactive web_search
        # cost. Threaded onto every usage record (see _record).
        self._usage_source = usage_source or "interactive"
        # Injected so this module doesn't import aime.usage directly (keeps it
        # testable and avoids an import cycle), mirroring ModelRouter.
        self._record_api = record_api
        self._max_loops = max_loops
        self._max_tokens = max_tokens
        self._client = Anthropic(max_retries=2)

    def search(self, request: str, *, session_id: str | None = None) -> str:
        """Run the request and return ``summary + Sources``.

        Always returns a string — on any failure, a short factual line the
        caller can relay rather than an exception.
        """
        request = (request or "").strip()
        if not request:
            return "No search request was provided."
        try:
            return self._run(request, session_id=session_id)
        except Exception as exc:  # never let a search failure break the turn
            return (
                "The web search could not be completed right now "
                f"({type(exc).__name__}). Tell the user the lookup failed and, "
                "if you can, answer from what you already know."
            )

    # --- internals ---

    def _run(self, request: str, *, session_id: str | None) -> str:
        messages: list[dict] = [{"role": "user", "content": request}]
        text_parts: list[str] = []
        sources: list[tuple[str, str]] = []   # (title, url), order-preserving
        result_fallback: list[tuple[str, str]] = []
        seen: set[str] = set()
        seen_fallback: set[str] = set()

        for _ in range(self._max_loops):
            started = datetime.datetime.now()
            resp = self._client.messages.create(
                model=self._model,
                system=_SYSTEM,
                messages=messages,
                tools=[self._tool],
                max_tokens=self._max_tokens,
            )
            self._record(resp, session_id, started)

            last_text = self._harvest(resp, sources, seen, result_fallback, seen_fallback)
            if last_text:
                text_parts.append(last_text)

            # Server-side web search runs its own loop; pause_turn means
            # "re-send and I'll resume". Anything else ends the search.
            if getattr(resp, "stop_reason", None) == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break

        summary = (text_parts[-1] if text_parts else "").strip()
        if not summary:
            summary = "\n\n".join(p for p in text_parts if p).strip()
        if not summary:
            summary = "The search returned no usable results."

        # Prefer the sources Haiku actually cited; fall back to the raw result
        # list only if it cited nothing (so we still surface where it looked).
        chosen = sources or result_fallback[:8]
        if chosen:
            lines = "\n".join(f"- {title} — {url}" for title, url in chosen)
            return f"{summary}\n\nSources:\n{lines}"
        return summary

    @staticmethod
    def _harvest(
        resp,
        sources: list[tuple[str, str]],
        seen: set[str],
        result_fallback: list[tuple[str, str]],
        seen_fallback: set[str],
    ) -> str:
        """Pull the text and citation/source data off one response.

        Returns the concatenated text of this response. Citations (the sources
        the model actually used) go into ``sources``; every returned search
        result goes into ``result_fallback`` for use only if nothing was cited.
        Deterministic — read straight off the response blocks, never trusting
        the model to format URLs in prose.
        """
        text_chunks: list[str] = []
        for block in getattr(resp, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                txt = getattr(block, "text", "") or ""
                if txt:
                    text_chunks.append(txt)
                for cite in (getattr(block, "citations", None) or []):
                    url = getattr(cite, "url", None)
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    sources.append((getattr(cite, "title", None) or url, url))
            elif btype == "web_search_tool_result":
                content = getattr(block, "content", None)
                if not isinstance(content, list):
                    continue  # an error object, not a result list
                for r in content:
                    if getattr(r, "type", None) != "web_search_result":
                        continue
                    url = getattr(r, "url", None)
                    if not url or url in seen_fallback:
                        continue
                    seen_fallback.add(url)
                    result_fallback.append((getattr(r, "title", None) or url, url))
        return "".join(text_chunks)

    def _record(self, resp, session_id: str | None, started: datetime.datetime) -> None:
        if self._record_api is None:
            return
        try:
            self._record_api(
                self._usage_label,
                self._model,
                getattr(resp, "usage", None),
                purpose="web_search",
                session_id=session_id,
                stop_reason=getattr(resp, "stop_reason", None),
                duration_ms=(datetime.datetime.now() - started).total_seconds() * 1000.0,
                # No routed_decision: this is its own purpose, not a routed user
                # turn, so it stays out of the routing tab's turn/misclass counts.
                # The web-search savings view re-prices it on its own.
                source=self._usage_source,
            )
        except Exception:
            pass
