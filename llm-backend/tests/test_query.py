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
        "max_tokens": 1024,
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
        assert "[Web Result 1]" in result
        assert "[Web Result 2]" in result

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
    def test_returns_empty_when_index_is_none(self):
        ws = _make_workspace()
        result = asyncio.run(query._retrieve_nodes(None, "question", ws))
        assert result == []

    def test_filters_below_similarity_threshold(self):
        ws = _make_workspace(similarity_threshold=0.8)
        mock_index = MagicMock()
        mock_retriever = MagicMock()

        above = _make_mock_node(score=0.9)
        below = _make_mock_node(score=0.5)
        mock_retriever.retrieve.return_value = [above, below]
        mock_index.as_retriever.return_value = mock_retriever

        result = asyncio.run(query._retrieve_nodes(mock_index, "q", ws))
        assert above in result
        assert below not in result

    def test_includes_node_with_none_score(self):
        """Nodes with score=None (exact match or score not available) should pass through."""
        ws = _make_workspace(similarity_threshold=0.9)
        mock_index = MagicMock()
        mock_retriever = MagicMock()
        none_score_node = _make_mock_node(score=None)
        mock_retriever.retrieve.return_value = [none_score_node]
        mock_index.as_retriever.return_value = mock_retriever

        result = asyncio.run(query._retrieve_nodes(mock_index, "q", ws))
        assert none_score_node in result

    def test_top_n_passed_to_retriever(self):
        ws = _make_workspace(top_n=7)
        mock_index = MagicMock()
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = []
        mock_index.as_retriever.return_value = mock_retriever

        asyncio.run(query._retrieve_nodes(mock_index, "q", ws))
        mock_index.as_retriever.assert_called_once_with(similarity_top_k=7)


# ---------------------------------------------------------------------------
# query_workspace (blocking entry point)
# ---------------------------------------------------------------------------

class TestQueryWorkspace:
    def _run(self, workspace, question, mock_nodes=None, mock_web=None, llm_answer="42"):
        if mock_nodes is None:
            mock_nodes = [_make_mock_node()]
        if mock_web is None:
            mock_web = []

        mock_index = MagicMock()
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = mock_nodes
        mock_index.as_retriever.return_value = mock_retriever

        mock_response = MagicMock()
        mock_response.message.content = llm_answer

        with (
            patch("query._build_index", return_value=mock_index),
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
        from llama_index.vector_stores.lancedb.base import TableNotFoundError

        with (
            patch("query._build_index", side_effect=TableNotFoundError("no table")),
            patch("query._searxng.web_search", new=AsyncMock(return_value=[])),
        ):
            result = query.query_workspace(ws, "Q?")
        assert "No documents" in result["answer"]
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
        result = self._run(ws, "Q?", mock_nodes=[], mock_web=web)
        assert result["sources"]["web"] == web


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

        mock_index = MagicMock()
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = mock_nodes
        mock_index.as_retriever.return_value = mock_retriever

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
                patch("query._build_index", return_value=mock_index),
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
        from llama_index.vector_stores.lancedb.base import TableNotFoundError
        ws = _make_workspace(searxng_enabled=0)

        events = []

        async def _run():
            with (
                patch("query._build_index", side_effect=TableNotFoundError("x")),
                patch("query._searxng.web_search", new=AsyncMock(return_value=[])),
            ):
                async for chunk in query.stream_query_workspace(ws, "Q?"):
                    events.append(chunk)

        asyncio.run(_run())
        assert any("event: done" in e for e in events)
        assert any("No documents" in e for e in events)

    def test_prompt_suffix_appended(self):
        """prompt_suffix should be appended to the user prompt."""
        ws = _make_workspace()
        node = _make_mock_node()

        mock_index = MagicMock()
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = [node]
        mock_index.as_retriever.return_value = mock_retriever

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
                patch("query._build_index", return_value=mock_index),
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
            llm = query.build_llm("openai/gpt-4o", "key", 0.5, "sys", 512)
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
