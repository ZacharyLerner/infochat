"""
test_query.py
=============
Unit tests for query.py — RAG pipeline helpers and the query / streaming functions.

All LLM, retrieval, and network calls are mocked. No real model weights or
network access required.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import query


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workspace(**overrides):
    base = {
        "slug": "test-ws",
        "llm_model": "openai/gpt-4o",
        "api_key": "key",
        "temperature": 0.7,
        "system_prompt": "",
        "top_n": 5,
        "similarity_threshold": 0.5,
        "embed_model": "openai/text-embedding-3-small",
        "embed_api_key": "",
        "searxng_enabled": 0,
        "searxng_num_results": 3,
        "searxng_query_suffix": "",
        "rewrite_model": "",
        "rewrite_prompt": "",
    }
    base.update(overrides)
    return base


def _node(metadata=None, content="chunk text", score=0.9):
    node = MagicMock()
    node.score = score
    node.node.metadata = metadata if metadata is not None else {"filename": "doc.txt"}
    node.node.get_content.return_value = content
    return node


def _streaming_llm(tokens, captured=None):
    """Mock LLM whose astream_chat yields `tokens`; records the messages it was sent."""
    async def _gen():
        for t in tokens:
            r = MagicMock()
            r.delta = t
            yield r

    async def _astream_chat(messages):
        if captured is not None:
            captured.append(list(messages))
        return _gen()

    llm = MagicMock()
    llm.astream_chat = _astream_chat
    return llm


def _collect(agen_factory, nodes, llm=None):
    events = []

    async def _run():
        with (
            patch("query.embedding.retrieve", new=AsyncMock(return_value=nodes)),
            patch("query._searxng.web_search", new=AsyncMock(return_value=[])),
            patch("query.build_llm", return_value=llm),
        ):
            async for chunk in agen_factory():
                events.append(chunk)

    asyncio.run(_run())
    return events


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

class TestCitations:
    def test_passages_numbered_with_title_and_source(self):
        nodes = [
            _node({"title": "URI ITSD Internal - Wi-Fi", "uri": "https://example.edu/wifi",
                   "filename": "wifi.docling.json"}, "Gaming devices text"),
            _node({"filename": "notes.txt"}, "plain text"),
        ]
        ctx = query._build_merged_context(nodes, [])
        assert "[1]\nTitle: URI ITSD Internal - Wi-Fi\nSource: https://example.edu/wifi\nGaming devices text" in ctx
        # Plain uploads fall back to their filename
        assert "[2]\nTitle: notes.txt\nSource: notes.txt\nplain text" in ctx

    def test_web_sources_marked_cited_in_shared_numbering(self):
        nodes = [_node({"title": "Doc"})]
        web = [
            {"title": "Used", "url": "https://used.example", "snippet": "a"},
            {"title": "Unused", "url": "https://unused.example", "snippet": "b"},
        ]
        payload = query._sources_payload(nodes, web, "From the doc [1] and the web [2].")
        assert [(d["n"], d["cited"]) for d in payload["documents"]] == [(1, True)]
        assert [(w["n"], w["cited"], w["url"]) for w in payload["web"]] == [
            (2, True, "https://used.example"), (3, False, "https://unused.example"),
        ]


# ---------------------------------------------------------------------------
# query_workspace (blocking entry point)
# ---------------------------------------------------------------------------

class TestQueryWorkspace:
    def test_returns_answer_and_sources_documents(self):
        nodes = [
            _node({"title": "Wi-Fi", "uri": "https://example.edu/wifi", "filename": "wifi.docling.json",
                   "headings": ["Wi-Fi", "Gaming Devices"], "source_type": "html"}, "content here", 0.85),
            _node({"filename": "notes.txt"}),
        ]
        mock_response = MagicMock()
        mock_response.message.content = "Register the MAC address [1]."
        with (
            patch("query.embedding.retrieve", new=AsyncMock(return_value=nodes)),
            patch("query.build_llm") as mock_llm_fn,
        ):
            mock_llm_fn.return_value.chat.return_value = mock_response
            result = query.query_workspace(_make_workspace(), "How do I connect my Xbox?")

        assert result["answer"] == "Register the MAC address [1]."
        assert result["sources"]["documents"] == [
            {"n": 1, "cited": True, "score": pytest.approx(0.85), "filename": "wifi.docling.json",
             "title": "Wi-Fi", "uri": "https://example.edu/wifi", "headings": ["Wi-Fi", "Gaming Devices"],
             "source_type": "html", "text": "content here"},
            {"n": 2, "cited": False, "score": pytest.approx(0.9), "filename": "notes.txt",
             "title": "notes.txt", "uri": "", "headings": [], "source_type": "", "text": "chunk text"},
        ]
        assert result["sources"]["web"] == []

    def test_no_documents_embedded_returns_message(self):
        with (
            patch("query.embedding.retrieve", new=AsyncMock(return_value=[])),
            patch("query._searxng.web_search", new=AsyncMock(return_value=[])),
        ):
            result = query.query_workspace(_make_workspace(), "Q?")
        assert "No relevant documents" in result["answer"]
        assert result["sources"]["documents"] == []


# ---------------------------------------------------------------------------
# stream_query_workspace (SSE generator)
# ---------------------------------------------------------------------------

class TestStreamQueryWorkspace:
    def test_streams_tokens_then_sources_then_done(self):
        nodes = [_node({"filename": "a.txt"}), _node({"filename": "b.txt"})]
        events = _collect(
            lambda: query.stream_query_workspace(_make_workspace(), "Q?"),
            nodes, _streaming_llm(["Answer ", "[2]", "."]),
        )
        kinds = [e.split("\n", 1)[0] for e in events]
        assert kinds == ["event: token"] * 3 + ["event: sources", "event: done", "event: log"]
        docs = json.loads(events[3].split("data: ", 1)[1].strip())["documents"]
        assert [(d["n"], d["cited"]) for d in docs] == [(1, False), (2, True)]


# ---------------------------------------------------------------------------
# stream_chat_session
# ---------------------------------------------------------------------------

class TestChatSession:
    def test_session_rebuilt_from_history_then_remembers_turns(self):
        sid = "test-session-rebuild"
        ws = _make_workspace()
        captured = []
        history = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        try:
            # First message after a server restart: memory is seeded from browser history
            _collect(lambda: query.stream_chat_session(sid, ws, "follow-up", history=history),
                     [_node()], _streaming_llm(["reply one"], captured))
            # Next message reuses the in-memory session (history not resent)
            _collect(lambda: query.stream_chat_session(sid, ws, "second"),
                     [_node()], _streaming_llm(["reply two"], captured))
        finally:
            query._chat_sessions.pop(sid, None)

        first, second = ([m.content for m in msgs] for msgs in captured)
        assert "earlier question" in first and "earlier answer" in first
        assert {"earlier question", "earlier answer", "follow-up", "reply one"} <= set(second)

    def _empty_retrieval(self, sid, **kw):
        captured = []
        try:
            events = _collect(lambda: query.stream_chat_session(sid, _make_workspace(), "doc block + question", **kw),
                              [], _streaming_llm(["from the attachment"], captured))
        finally:
            query._chat_sessions.pop(sid, None)
        tokens = "".join(e.split("data: ", 1)[1].strip() for e in events if e.startswith("event: token"))
        return tokens, captured

    def test_attachment_answered_even_when_retrieval_finds_nothing(self):
        tokens, captured = self._empty_retrieval("s-attach", has_attachment=True)
        assert tokens == "from the attachment"
        assert any("doc block + question" in m.content for m in captured[0])

    @pytest.mark.parametrize("docs, reply", [
        ([], "No documents have been embedded in this workspace yet."),
        ([{"doc_id": "d1"}], "No relevant information found in this workspace's documents."),
    ])
    def test_empty_retrieval_reply_says_why(self, docs, reply):
        with patch("query.db.list_docs", return_value=docs):
            tokens, captured = self._empty_retrieval("s-empty")
        assert tokens == reply and captured == []
