"""
test_searxng_rewriter_manager.py
=================================
Unit tests for three standalone utility modules:
  - searxng.py   — async SearXNG wrapper
  - rewriter.py  — query rewriting helper
  - manager.py   — downstream workspace event propagation
  - prompts.py   — default prompt constants (smoke tests)

All external HTTP calls are mocked with httpx mock responses or patch.
"""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import httpx

import searxng as searxng_mod
import rewriter as rewriter_mod
import manager as manager_mod
import prompts


# ===========================================================================
# searxng.web_search
# ===========================================================================

class TestWebSearch:
    def _make_response(self, results: list, status_code: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = {"results": results}
        resp.raise_for_status = MagicMock()
        if status_code >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "error", request=MagicMock(), response=resp
            )
        return resp

    def _run(self, coro):
        return asyncio.run(coro)

    def test_empty_query_returns_empty_list(self):
        result = self._run(searxng_mod.web_search(""))
        assert result == []

    def test_whitespace_query_returns_empty_list(self):
        result = self._run(searxng_mod.web_search("   "))
        assert result == []

    def test_successful_search_returns_results(self):
        raw = [
            {"title": "URI", "url": "https://uri.edu", "content": "University of Rhode Island"},
            {"title": "Google", "url": "https://google.com", "content": "Search engine"},
        ]
        mock_resp = self._make_response(raw)

        async def _run():
            with patch("searxng.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                return await searxng_mod.web_search("university of rhode island", num_results=5)

        results = asyncio.run(_run())
        assert len(results) == 2
        assert results[0]["url"] == "https://uri.edu"
        assert results[0]["title"] == "URI"
        assert results[0]["snippet"] == "University of Rhode Island"

    def test_num_results_limit_respected(self):
        raw = [{"title": f"R{i}", "url": f"https://r{i}.com", "content": "x"} for i in range(10)]
        mock_resp = self._make_response(raw)

        async def _run():
            with patch("searxng.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                return await searxng_mod.web_search("q", num_results=3)

        results = asyncio.run(_run())
        assert len(results) == 3

    def test_timeout_returns_empty_list(self):
        async def _run():
            with patch("searxng.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                return await searxng_mod.web_search("q")

        results = asyncio.run(_run())
        assert results == []

    def test_http_error_returns_empty_list(self):
        mock_resp = self._make_response([], status_code=500)

        async def _run():
            with patch("searxng.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                return await searxng_mod.web_search("q")

        results = asyncio.run(_run())
        assert results == []

    def test_generic_exception_returns_empty_list(self):
        async def _run():
            with patch("searxng.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(side_effect=RuntimeError("boom"))
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                return await searxng_mod.web_search("q")

        results = asyncio.run(_run())
        assert results == []

    def test_missing_results_key_returns_empty_list(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}  # no 'results' key
        mock_resp.raise_for_status = MagicMock()

        async def _run():
            with patch("searxng.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                return await searxng_mod.web_search("q")

        results = asyncio.run(_run())
        assert results == []

    def test_result_without_url_excluded(self):
        raw = [
            {"title": "No URL", "content": "data"},       # no url — excluded
            {"title": "Has URL", "url": "https://x.com", "content": "data"},
        ]
        mock_resp = self._make_response(raw)

        async def _run():
            with patch("searxng.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                return await searxng_mod.web_search("q", num_results=5)

        results = asyncio.run(_run())
        assert len(results) == 1
        assert results[0]["url"] == "https://x.com"

    def test_snippet_falls_back_to_content_then_empty(self):
        raw = [
            {"title": "A", "url": "https://a.com", "content": "content value"},
            {"title": "B", "url": "https://b.com", "snippet": "snippet value"},
            {"title": "C", "url": "https://c.com"},   # neither
        ]
        mock_resp = self._make_response(raw)

        async def _run():
            with patch("searxng.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                return await searxng_mod.web_search("q", num_results=5)

        results = asyncio.run(_run())
        assert results[0]["snippet"] == "content value"
        assert results[1]["snippet"] == "snippet value"
        assert results[2]["snippet"] == ""


# ===========================================================================
# rewriter.rewrite_query
# ===========================================================================

class TestRewriteQuery:
    def test_empty_model_returns_original(self):
        result = asyncio.run(
            rewriter_mod.rewrite_query("my question", rewrite_model="", api_key="")
        )
        assert result == "my question"

    def test_whitespace_model_returns_original(self):
        result = asyncio.run(
            rewriter_mod.rewrite_query("q", rewrite_model="   ", api_key="")
        )
        assert result == "q"

    def test_successful_rewrite(self):
        mock_response = MagicMock()
        mock_response.message.content = "improved search query"

        mock_llm = MagicMock()
        mock_llm.chat.return_value = mock_response

        # build_llm is imported inside _sync_rewrite via "from query import build_llm"
        # so we must patch it at the query module level.
        with patch("query.build_llm", return_value=mock_llm):
            result = asyncio.run(
                rewriter_mod.rewrite_query(
                    "original q",
                    rewrite_model="openai/gpt-4o-mini",
                    api_key="key",
                )
            )
        assert result == "improved search query"

    def test_empty_llm_response_returns_original(self):
        mock_response = MagicMock()
        mock_response.message.content = ""

        mock_llm = MagicMock()
        mock_llm.chat.return_value = mock_response

        with patch("query.build_llm", return_value=mock_llm):
            result = asyncio.run(
                rewriter_mod.rewrite_query("original q", rewrite_model="openai/gpt-4o-mini", api_key="")
            )
        assert result == "original q"

    def test_multiline_response_returns_original(self):
        """Model went off-script and returned multiple lines → fall back to original."""
        mock_response = MagicMock()
        mock_response.message.content = "line1\nline2"

        mock_llm = MagicMock()
        mock_llm.chat.return_value = mock_response

        with patch("query.build_llm", return_value=mock_llm):
            result = asyncio.run(
                rewriter_mod.rewrite_query("q", rewrite_model="openai/gpt-4o-mini", api_key="")
            )
        assert result == "q"

    def test_llm_exception_returns_original(self):
        mock_llm = MagicMock()
        mock_llm.chat.side_effect = RuntimeError("LLM failure")

        with patch("query.build_llm", return_value=mock_llm):
            result = asyncio.run(
                rewriter_mod.rewrite_query("q", rewrite_model="openai/gpt-4o-mini", api_key="")
            )
        assert result == "q"

    def test_uses_default_prompt_when_blank(self):
        mock_response = MagicMock()
        mock_response.message.content = "rewritten"

        mock_llm = MagicMock()
        mock_llm.chat.return_value = mock_response

        with patch("query.build_llm", return_value=mock_llm):
            asyncio.run(
                rewriter_mod.rewrite_query(
                    "q",
                    rewrite_model="openai/gpt-4o-mini",
                    api_key="",
                    rewrite_prompt="",   # blank → default used
                )
            )
        # The system message content should be the default prompt
        call_messages = mock_llm.chat.call_args[0][0]
        system_msg = call_messages[0]
        assert prompts.DEFAULT_REWRITE_PROMPT in system_msg.content

    def test_custom_prompt_used_when_provided(self):
        mock_response = MagicMock()
        mock_response.message.content = "rewritten"
        mock_llm = MagicMock()
        mock_llm.chat.return_value = mock_response

        with patch("query.build_llm", return_value=mock_llm):
            asyncio.run(
                rewriter_mod.rewrite_query(
                    "q",
                    rewrite_model="openai/gpt-4o-mini",
                    api_key="",
                    rewrite_prompt="Custom system prompt.",
                )
            )
        call_messages = mock_llm.chat.call_args[0][0]
        system_msg = call_messages[0]
        assert "Custom system prompt." in system_msg.content

    def test_history_injected_into_messages(self):
        mock_response = MagicMock()
        mock_response.message.content = "rewritten"
        mock_llm = MagicMock()
        mock_llm.chat.return_value = mock_response

        history = [
            {"role": "user", "content": "previous user msg"},
            {"role": "assistant", "content": "previous assistant msg"},
        ]

        with patch("query.build_llm", return_value=mock_llm):
            asyncio.run(
                rewriter_mod.rewrite_query(
                    "follow-up q",
                    rewrite_model="openai/gpt-4o-mini",
                    api_key="",
                    history=history,
                )
            )
        call_messages = mock_llm.chat.call_args[0][0]
        contents = [m.content for m in call_messages]
        assert "previous user msg" in contents
        assert "previous assistant msg" in contents


# ===========================================================================
# manager — workspace propagation
# ===========================================================================

class TestManager:
    def _join_background(self, timeout=2.0):
        """Wait for all daemon threads spawned by manager to finish."""
        for t in threading.enumerate():
            if t.daemon and t != threading.current_thread():
                t.join(timeout=timeout)

    def test_on_workspace_created_calls_both_servers(self):
        with (
            patch.object(manager_mod, "_post") as mock_post,
        ):
            mock_post.return_value = {"ok": True}
            manager_mod.on_workspace_created("test-slug", "Test WS")
            self._join_background()
            assert mock_post.call_count == 2

    def test_on_workspace_created_sends_correct_payloads(self):
        calls_made = []

        def capture_post(base, path, headers, payload):
            calls_made.append((base, path, payload))
            return {"ok": True}

        with patch.object(manager_mod, "_post", side_effect=capture_post):
            manager_mod.on_workspace_created("ws-slug", "WS Name")
            self._join_background()

        # File server call
        file_call = next(c for c in calls_made if "/api/v1/workspaces" in c[1])
        assert file_call[2]["id"] == "ws-slug"
        assert file_call[2]["name"] == "WS Name"

        # Chat server call
        chat_call = next(c for c in calls_made if c[1] == "/api/workspaces")
        assert chat_call[2]["slug"] == "ws-slug"
        assert chat_call[2]["name"] == "WS Name"

    def test_on_workspace_renamed_calls_patch_and_put(self):
        with (
            patch.object(manager_mod, "_patch") as mock_patch,
            patch.object(manager_mod, "_put") as mock_put,
        ):
            mock_patch.return_value = {}
            mock_put.return_value = {}
            manager_mod.on_workspace_renamed("ws-slug", "New Name")
            self._join_background()
            mock_patch.assert_called_once()
            mock_put.assert_called_once()

    def test_on_workspace_renamed_sends_new_name(self):
        patched_calls = []
        put_calls = []

        def cap_patch(base, path, headers, payload):
            patched_calls.append(payload)
            return {}

        def cap_put(base, path, headers, payload):
            put_calls.append(payload)
            return {}

        with (
            patch.object(manager_mod, "_patch", side_effect=cap_patch),
            patch.object(manager_mod, "_put", side_effect=cap_put),
        ):
            manager_mod.on_workspace_renamed("slug", "Renamed WS")
            self._join_background()

        assert patched_calls[0]["name"] == "Renamed WS"
        assert put_calls[0]["name"] == "Renamed WS"

    def test_on_workspace_deleted_calls_delete_twice(self):
        with patch.object(manager_mod, "_delete") as mock_del:
            mock_del.return_value = True
            manager_mod.on_workspace_deleted("del-slug")
            self._join_background()
            assert mock_del.call_count == 2

    def test_propagation_failure_does_not_raise(self):
        """Manager failures must be silent — never propagate to the caller."""
        with patch.object(manager_mod, "_post", return_value=None):
            # Should not raise
            manager_mod.on_workspace_created("slug", "name")
            self._join_background()

    def test_delete_accepts_404_as_success(self):
        """HTTP 404 on delete means workspace never existed there — acceptable."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock()

        with patch("manager.httpx.delete", return_value=mock_resp):
            result = manager_mod._delete("http://server", "/api/workspaces/slug", {})
        assert result is True

    def test_http_error_on_post_returns_none(self):
        with patch("manager.httpx.post", side_effect=RuntimeError("network error")):
            result = manager_mod._post("http://server", "/path", {}, {})
        assert result is None

    def test_fire_and_forget_runs_in_background_thread(self):
        called_in_thread = []

        def _fn():
            called_in_thread.append(threading.current_thread().name)

        manager_mod._run_in_background(_fn)
        self._join_background()
        assert len(called_in_thread) == 1
        assert called_in_thread[0] != threading.current_thread().name


# ===========================================================================
# prompts — smoke tests
# ===========================================================================

class TestPrompts:
    def test_default_rag_prompt_not_empty(self):
        assert len(prompts.DEFAULT_SYSTEM_PROMPT_RAG) > 20

    def test_default_web_prompt_not_empty(self):
        assert len(prompts.DEFAULT_SYSTEM_PROMPT_WEB) > 20

    def test_default_rewrite_prompt_not_empty(self):
        assert len(prompts.DEFAULT_REWRITE_PROMPT) > 20

    def test_rag_prompt_mentions_context(self):
        assert "context" in prompts.DEFAULT_SYSTEM_PROMPT_RAG.lower()

    def test_web_prompt_mentions_url(self):
        assert "url" in prompts.DEFAULT_SYSTEM_PROMPT_WEB.lower()

    def test_rewrite_prompt_output_only_instruction(self):
        assert "ONLY" in prompts.DEFAULT_REWRITE_PROMPT

    def test_all_prompts_are_strings(self):
        assert isinstance(prompts.DEFAULT_SYSTEM_PROMPT_RAG, str)
        assert isinstance(prompts.DEFAULT_SYSTEM_PROMPT_WEB, str)
        assert isinstance(prompts.DEFAULT_REWRITE_PROMPT, str)
