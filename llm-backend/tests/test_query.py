"""
test_query.py
=============
Unit tests for query.py — RAG pipeline helpers and the query / streaming functions.

All LLM, LanceDB, and network calls are mocked. No real model weights or
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


def _make_mock_node(score=0.9, filename="doc.txt", content="chunk text"):
    node = MagicMock()
    node.score = score
    node.node.metadata = {"filename": filename}
    node.node.get_content.return_value = content
    return node


# ---------------------------------------------------------------------------
# _safe_embed_query
# ---------------------------------------------------------------------------

class TestSafeEmbedQuery:
    def test_short_query_unchanged(self):
        q = "short query"
        assert query._safe_embed_query(q) == q

    def test_long_query_truncated_to_6000(self):
        q = "x" * 9000
        result = query._safe_embed_query(q)
        assert len(result) == 6000

    def test_exactly_6000_chars_unchanged(self):
        q = "a" * 6000
        assert query._safe_embed_query(q) == q


# ---------------------------------------------------------------------------
# _build_merged_context
# ---------------------------------------------------------------------------

class TestBuildMergedContext:
    def test_empty_nodes_and_web_returns_empty_string(self):
        assert query._build_merged_context([], []) == ""

    def test_nodes_only_includes_document_context(self):
        nodes = [_make_mock_node(content="node content")]
        result = query._build_merged_context(nodes, [])
        assert "--- Document Context ---" in result
        assert "node content" in result
        assert "--- Web Search Results ---" not in result

    def test_web_results_only_includes_web_section(self):
        web = [{"title": "Page", "url": "https://example.com", "snippet": "excerpt"}]
        result = query._build_merged_context([], web)
        assert "--- Web Search Results ---" in result
        assert "https://example.com" in result
        assert "--- Document Context ---" not in result

    def test_both_sources_present(self):
        nodes = [_make_mock_node(content="doc chunk")]
        web = [{"title": "W", "url": "https://w.com", "snippet": "snip"}]
        result = query._build_merged_context(nodes, web)
        assert "--- Document Context ---" in result
        assert "--- Web Search Results ---" in result

    def test_web_result_format_includes_all_fields(self):
        web = [{"title": "URI Homepage", "url": "https://uri.edu", "snippet": "University of Rhode Island"}]
        result = query._build_merged_context([], web)
        assert "URI Homepage" in result
        assert "https://uri.edu" in result
        assert "University of Rhode Island" in result

    def test_multiple_web_results_numbered(self):
        web = [
            {"title": "A", "url": "https://a.com", "snippet": "aa"},
            {"title": "B", "url": "https://b.com", "snippet": "bb"},
        ]
        result = query._build_merged_context([], web)
        assert "[1]\nTitle: A\nSource: https://a.com\naa" in result
        assert "[2]\nTitle: B\nSource: https://b.com\nbb" in result

    def test_web_numbering_continues_after_documents(self):
        nodes = [_make_mock_node(content="doc one"), _make_mock_node(content="doc two")]
        web = [{"title": "W", "url": "https://w.com", "snippet": "web text"}]
        result = query._build_merged_context(nodes, web)
        assert "[1]\n" in result and "[2]\n" in result
        assert "[3]\nTitle: W\nSource: https://w.com\nweb text" in result

    def test_multiple_nodes_all_included(self):
        nodes = [
            _make_mock_node(content="first chunk"),
            _make_mock_node(content="second chunk"),
        ]
        result = query._build_merged_context(nodes, [])
        assert "first chunk" in result
        assert "second chunk" in result


# ---------------------------------------------------------------------------
# _rewrite_if_enabled
# ---------------------------------------------------------------------------

class TestNumberedPassages:
    @staticmethod
    def _node(metadata, content="chunk text", score=0.9):
        node = MagicMock()
        node.score = score
        node.node.metadata = metadata
        node.node.get_content.return_value = content
        return node

    def test_passages_numbered_with_title_and_source(self):
        nodes = [
            self._node({"title": "URI ITSD Internal - Wi-Fi", "uri": "https://example.edu/wifi",
                        "filename": "wifi.docling.json"}, "Gaming devices text"),
            self._node({"filename": "notes.txt"}, "plain text"),
        ]
        ctx = query._build_merged_context(nodes, [])
        assert "[1]\nTitle: URI ITSD Internal - Wi-Fi\nSource: https://example.edu/wifi\nGaming devices text" in ctx
        # Plain uploads fall back to their filename
        assert "[2]\nTitle: notes.txt\nSource: notes.txt\nplain text" in ctx

    def test_source_falls_back_to_original_file_name(self):
        ctx = query._build_merged_context(
            [self._node({"title": "Quiz", "uri": "", "source": "Quiz.docx", "filename": "Quiz.docling.json"})], [])
        assert "Source: Quiz.docx" in ctx

    def test_doc_sources_mark_cited_passages(self):
        nodes = [
            self._node({"title": "Wi-Fi", "uri": "https://example.edu/wifi",
                        "headings": ["Wi-Fi", "Gaming Devices"], "source_type": "html"}),
            self._node({"filename": "notes.txt"}),
            self._node({"title": "Zoom"}),
        ]
        sources = query._doc_sources(nodes, "Register the MAC address [1]. See also [3][7].")
        assert [(s["n"], s["cited"]) for s in sources] == [(1, True), (2, False), (3, True)]
        assert sources[0]["uri"] == "https://example.edu/wifi"
        assert sources[0]["headings"] == ["Wi-Fi", "Gaming Devices"]
        assert sources[0]["source_type"] == "html"
        assert sources[1]["title"] == "notes.txt"
        assert sources[1]["uri"] == "" and sources[1]["headings"] == []

    def test_citation_instructions_sent_even_with_custom_system_prompt(self):
        from prompts import CITATION_INSTRUCTIONS
        ws = _make_workspace(system_prompt="You are the URI IT Service Desk assistant.")
        mock_response = MagicMock()
        mock_response.message.content = "Use rhodywifi [1]."
        with (
            patch("query.embedding.retrieve", new=AsyncMock(return_value=[_make_mock_node()])),
            patch("query.build_llm") as mock_llm_fn,
        ):
            mock_llm_fn.return_value.chat.return_value = mock_response
            result = query.query_workspace(ws, "How do I connect my Xbox?")
        messages = mock_llm_fn.return_value.chat.call_args.args[0]
        assert CITATION_INSTRUCTIONS in messages[-1].content
        assert result["sources"]["documents"][0]["cited"] is True

    def test_citation_instructions_cover_web_only_context(self):
        from prompts import CITATION_INSTRUCTIONS
        assert query._context_instructions([], [{"url": "https://u"}]) == " " + CITATION_INSTRUCTIONS
        assert query._context_instructions([], []) == ""

    def test_web_sources_marked_cited_in_shared_numbering(self):
        nodes = [self._node({"title": "Doc"})]
        web = [
            {"title": "Used", "url": "https://used.example", "snippet": "a"},
            {"title": "Unused", "url": "https://unused.example", "snippet": "b"},
        ]
        payload = query._sources_payload(nodes, web, "From the doc [1] and the web [2].")
        assert [(d["n"], d["cited"]) for d in payload["documents"]] == [(1, True)]
        assert [(w["n"], w["cited"], w["url"]) for w in payload["web"]] == [
            (2, True, "https://used.example"), (3, False, "https://unused.example"),
        ]


class TestRewriteIfEnabled:
    def test_no_rewrite_model_returns_original(self):
        ws = _make_workspace(rewrite_model="")
        result_query, rewritten = asyncio.run(query._rewrite_if_enabled("my question", ws))
        assert result_query == "my question"
        assert rewritten is None

    def test_rewrite_model_set_calls_rewriter(self):
        ws = _make_workspace(rewrite_model="openai/gpt-4o-mini")
        with patch("query._rewriter.rewrite_query", new=AsyncMock(return_value="better query")):
            result_query, rewritten = asyncio.run(
                query._rewrite_if_enabled("my question", ws)
            )
        assert result_query == "better query"
        assert rewritten == "better query"

    def test_unchanged_query_returns_none_for_rewritten(self):
        ws = _make_workspace(rewrite_model="openai/gpt-4o-mini")
        with patch("query._rewriter.rewrite_query", new=AsyncMock(return_value="my question")):
            result_query, rewritten = asyncio.run(
                query._rewrite_if_enabled("my question", ws)
            )
        assert result_query == "my question"
        assert rewritten is None   # no change → None


# ---------------------------------------------------------------------------
# _retrieve_nodes
# ---------------------------------------------------------------------------

class TestRetrieveNodes:
    def _retrieve(self, ws, nodes):
        mock = AsyncMock(return_value=nodes)
        with patch("query.embedding.retrieve", new=mock):
            return asyncio.run(query._retrieve_nodes("q", ws)), mock

    def test_returns_empty_when_nothing_retrieved(self):
        result, _ = self._retrieve(_make_workspace(), [])
        assert result == []

    def test_filters_below_similarity_threshold(self):
        above = _make_mock_node(score=0.9)
        below = _make_mock_node(score=0.5)
        result, _ = self._retrieve(_make_workspace(similarity_threshold=0.8), [above, below])
        assert above in result
        assert below not in result

    def test_includes_node_with_none_score(self):
        """Nodes with score=None (exact match or score not available) should pass through."""
        none_score_node = _make_mock_node(score=None)
        result, _ = self._retrieve(_make_workspace(similarity_threshold=0.9), [none_score_node])
        assert none_score_node in result

    def test_top_n_passed_to_retriever(self):
        ws = _make_workspace(top_n=7)
        _, mock = self._retrieve(ws, [])
        mock.assert_awaited_once_with(ws, "q", 7)


# ---------------------------------------------------------------------------
# query_workspace (blocking entry point)
# ---------------------------------------------------------------------------

class TestQueryWorkspace:
    def _run(self, workspace, question, mock_nodes=None, mock_web=None, llm_answer="42"):
        if mock_nodes is None:
            mock_nodes = [_make_mock_node()]
        if mock_web is None:
            mock_web = []

        mock_response = MagicMock()
        mock_response.message.content = llm_answer

        with (
            patch("query.embedding.retrieve", new=AsyncMock(return_value=mock_nodes)),
            patch("query._searxng.web_search", new=AsyncMock(return_value=mock_web)),
            patch("query.build_llm") as mock_llm_fn,
        ):
            mock_llm = MagicMock()
            mock_llm.chat.return_value = mock_response
            mock_llm_fn.return_value = mock_llm
            return query.query_workspace(workspace, question)

    def test_returns_answer_and_sources(self):
        ws = _make_workspace()
        result = self._run(ws, "What is 6 × 7?", llm_answer="42")
        assert result["answer"] == "42"
        assert "sources" in result
        assert "documents" in result["sources"]

    def test_no_documents_embedded_returns_message(self):
        ws = _make_workspace()

        with (
            patch("query.embedding.retrieve", new=AsyncMock(return_value=[])),
            patch("query._searxng.web_search", new=AsyncMock(return_value=[])),
        ):
            result = query.query_workspace(ws, "Q?")
        assert "No relevant documents" in result["answer"]
        assert result["sources"]["documents"] == []

    def test_no_relevant_docs_above_threshold(self):
        ws = _make_workspace(similarity_threshold=0.99)
        # All nodes have low score — filtered out by threshold
        low_score_node = _make_mock_node(score=0.1)
        result = self._run(ws, "Q?", mock_nodes=[low_score_node])
        assert "No relevant" in result["answer"]

    def test_sources_structure(self):
        ws = _make_workspace()
        node = _make_mock_node(score=0.85, filename="file.pdf", content="content here")
        result = self._run(ws, "Q?", mock_nodes=[node])
        src = result["sources"]["documents"][0]
        assert src["score"] == pytest.approx(0.85)
        assert src["filename"] == "file.pdf"
        assert "content here"[:200] in src["text"]

    def test_rewritten_query_included_in_result(self):
        ws = _make_workspace(rewrite_model="openai/gpt-4o-mini")
        with (
            patch("query._rewriter.rewrite_query", new=AsyncMock(return_value="rewritten q")),
        ):
            result = self._run(ws, "original q")
        assert result.get("rewritten_query") == "rewritten q"

    def test_web_search_results_included(self):
        ws = _make_workspace(searxng_enabled=1)
        web = [{"title": "T", "url": "https://u.com", "snippet": "s"}]
        result = self._run(ws, "Q?", mock_nodes=[], mock_web=web, llm_answer="See [1].")
        assert result["sources"]["web"] == [{**web[0], "n": 1, "cited": True}]


# ---------------------------------------------------------------------------
# stream_query_workspace (SSE generator)
# ---------------------------------------------------------------------------

class TestStreamQueryWorkspace:
    def _collect_stream(self, workspace, question, mock_nodes=None, mock_web=None, tokens=None):
        if mock_nodes is None:
            mock_nodes = [_make_mock_node()]
        if mock_web is None:
            mock_web = []
        if tokens is None:
            tokens = ["Hello", " world"]

        # query.py does: response_gen = await llm.astream_chat(messages)
        # So astream_chat must be a coroutine that returns an async iterable.
        _tokens = list(tokens)

        async def _gen():
            for t in _tokens:
                r = MagicMock()
                r.delta = t
                yield r

        async def _fake_astream_chat(messages):
            return _gen()

        mock_llm = MagicMock()
        mock_llm.astream_chat = _fake_astream_chat

        events = []

        async def _run():
            with (
                patch("query.embedding.retrieve", new=AsyncMock(return_value=mock_nodes)),
                patch("query._searxng.web_search", new=AsyncMock(return_value=mock_web)),
                patch("query.build_llm", return_value=mock_llm),
            ):
                async for chunk in query.stream_query_workspace(workspace, question):
                    events.append(chunk)

        asyncio.run(_run())
        return events

    def test_emits_token_events(self):
        ws = _make_workspace()
        events = self._collect_stream(ws, "Q?", tokens=["tok1", "tok2"])
        token_events = [e for e in events if e.startswith("event: token")]
        assert len(token_events) == 2

    def test_always_emits_done_event(self):
        ws = _make_workspace()
        events = self._collect_stream(ws, "Q?")
        assert any("event: done" in e for e in events)

    def test_emits_sources_event(self):
        ws = _make_workspace()
        events = self._collect_stream(ws, "Q?")
        assert any("event: sources" in e for e in events)

    def test_sources_event_marks_cited_passages(self):
        ws = _make_workspace()
        nodes = [_make_mock_node(filename="a.txt"), _make_mock_node(filename="b.txt")]
        events = self._collect_stream(ws, "Q?", mock_nodes=nodes, tokens=["Answer ", "[2]", "."])
        sources_event = next(e for e in events if e.startswith("event: sources"))
        docs = json.loads(sources_event.split("data: ", 1)[1].strip())["documents"]
        assert [(d["n"], d["cited"]) for d in docs] == [(1, False), (2, True)]

    def test_emits_log_event(self):
        ws = _make_workspace()
        events = self._collect_stream(ws, "Q?")
        assert any("event: log" in e for e in events)

    def test_log_event_not_forwarded_in_main(self):
        """The log event is intercepted by main.py and never in the public stream."""
        # Verify that stream_query_workspace emits it so main.py CAN intercept it
        ws = _make_workspace()
        events = self._collect_stream(ws, "Q?")
        log_events = [e for e in events if e.startswith("event: log")]
        assert len(log_events) == 1
        entry = json.loads(log_events[0].split("data: ", 1)[1].strip())
        assert "question" in entry
        assert "answer" in entry

    def test_no_documents_emits_token_and_done(self):
        ws = _make_workspace(searxng_enabled=0)

        events = []

        async def _run():
            with (
                patch("query.embedding.retrieve", new=AsyncMock(return_value=[])),
                patch("query._searxng.web_search", new=AsyncMock(return_value=[])),
            ):
                async for chunk in query.stream_query_workspace(ws, "Q?"):
                    events.append(chunk)

        asyncio.run(_run())
        assert any("event: done" in e for e in events)
        assert any("No relevant documents" in e for e in events)

    def test_prompt_suffix_appended(self):
        """prompt_suffix should be appended to the user prompt."""
        ws = _make_workspace()
        node = _make_mock_node()

        # Use a mutable container so the nested async generator can write to it.
        captured = {"messages": []}

        async def _inner_gen(msgs):
            r = MagicMock()
            r.delta = "answer"
            yield r

        async def _fake_astream_chat(messages):
            captured["messages"] = list(messages)
            return _inner_gen(messages)

        mock_llm = MagicMock()
        mock_llm.astream_chat = _fake_astream_chat

        async def _run():
            with (
                patch("query.embedding.retrieve", new=AsyncMock(return_value=[node])),
                patch("query._searxng.web_search", new=AsyncMock(return_value=[])),
                patch("query.build_llm", return_value=mock_llm),
            ):
                async for _ in query.stream_query_workspace(ws, "Q?", prompt_suffix=" [SUFFIX]"):
                    pass

        asyncio.run(_run())
        assert len(captured["messages"]) > 0, "No messages were captured — LLM was not called"
        user_msg = next(m for m in captured["messages"] if m.role.value == "user")
        assert "[SUFFIX]" in user_msg.content

    def test_newlines_in_tokens_escaped(self):
        """Newlines in token data must be escaped to \\n in SSE."""
        ws = _make_workspace()
        events = self._collect_stream(ws, "Q?", tokens=["line1\nline2"])
        token_events = [e for e in events if e.startswith("event: token")]
        # Raw newline must NOT appear inside the data line
        for ev in token_events:
            data_line = [l for l in ev.split("\n") if l.startswith("data:")][0]
            assert "\n" not in data_line.replace("\\n", "")


# ---------------------------------------------------------------------------
# build_llm
# ---------------------------------------------------------------------------

class TestBuildLLM:
    def test_returns_gateway_litellm_instance(self):
        with patch("query.LiteLLM.__init__", return_value=None):
            llm = query.build_llm("openai/gpt-4o", "key", 0.5, "sys")
            assert isinstance(llm, query._GatewayLiteLLM)

    def test_metadata_context_window_overridden(self):
        """_GatewayLiteLLM.metadata must return our fixed large context window."""
        llm = query._GatewayLiteLLM.__new__(query._GatewayLiteLLM)
        # Patch super().metadata to return a minimal object
        base_meta = MagicMock()
        base_meta.num_output = 512
        base_meta.is_chat_model = True
        base_meta.is_function_calling_model = False
        base_meta.model_name = "test-model"
        with patch.object(query.LiteLLM, "metadata", new_callable=lambda: property(lambda self: base_meta)):
            meta = llm.metadata
        assert meta.context_window == query._CONTEXT_WINDOW
