"""
test_searxng_rewriter_manager.py
=================================
Unit tests for three standalone utility modules:
  - searxng.py   — async SearXNG wrapper
  - rewriter.py  — query rewriting helper
  - manager.py   — downstream workspace event propagation

All external HTTP calls are mocked with httpx mock responses or patch.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import searxng as searxng_mod
import rewriter as rewriter_mod
import manager as manager_mod


# ===========================================================================
# searxng.web_search
# ===========================================================================

class TestWebSearch:
    def _search(self, get, num_results=5):
        async def _run():
            with patch("searxng.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = get
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                return await searxng_mod.web_search("q", num_results=num_results)
        return asyncio.run(_run())

    def test_successful_search_maps_and_limits_results(self):
        raw = [{"title": f"R{i}", "url": f"https://r{i}.com", "content": f"text {i}"} for i in range(5)]
        resp = MagicMock()
        resp.json.return_value = {"results": raw}
        results = self._search(AsyncMock(return_value=resp), num_results=3)
        assert results == [
            {"title": f"R{i}", "url": f"https://r{i}.com", "snippet": f"text {i}"} for i in range(3)
        ]

    def test_failure_returns_empty_list(self):
        assert self._search(AsyncMock(side_effect=httpx.TimeoutException("timeout"))) == []


# ===========================================================================
# rewriter.rewrite_query
# ===========================================================================

class TestRewriteQuery:
    def _rewrite(self, llm):
        # build_llm is imported inside the rewriter from query, so patch it there
        with patch("query.build_llm", return_value=llm):
            return asyncio.run(
                rewriter_mod.rewrite_query("original q", rewrite_model="openai/gpt-4o-mini", api_key="key")
            )

    def test_successful_rewrite(self):
        llm = MagicMock()
        llm.chat.return_value.message.content = "improved search query"
        assert self._rewrite(llm) == "improved search query"

    def test_llm_exception_returns_original(self):
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError("LLM failure")
        assert self._rewrite(llm) == "original q"


# ===========================================================================
# manager — workspace propagation
# ===========================================================================

class TestManager:
    def _join_background(self, timeout=2.0):
        """Wait for all daemon threads spawned by manager to finish."""
        for t in threading.enumerate():
            if t.daemon and t != threading.current_thread():
                t.join(timeout=timeout)

    def test_events_call_downstream_paths_and_never_raise(self):
        calls = []

        def failing(method):
            def _fn(url, **kwargs):
                calls.append((method, url, kwargs.get("json")))
                raise httpx.ConnectError("server down")
            return _fn

        with (
            patch("manager.httpx.post", side_effect=failing("POST")),
            patch("manager.httpx.patch", side_effect=failing("PATCH")),
            patch("manager.httpx.put", side_effect=failing("PUT")),
            patch("manager.httpx.delete", side_effect=failing("DELETE")),
        ):
            manager_mod.on_workspace_created("ws-slug", "WS Name")
            self._join_background()
            manager_mod.on_workspace_renamed("ws-slug", "Renamed")
            self._join_background()
            manager_mod.on_workspace_deleted("ws-slug")
            self._join_background()

        file_base, chat_base = manager_mod._FILE_BASE, manager_mod._CHAT_BASE
        assert [(m, url) for m, url, _ in calls] == [
            ("POST", f"{file_base}/api/v1/workspaces/db"),
            ("POST", f"{chat_base}/api/workspaces"),
            ("PATCH", f"{file_base}/api/v1/workspaces/ws-slug"),
            ("PUT", f"{chat_base}/api/workspaces/ws-slug"),
            ("DELETE", f"{file_base}/api/v1/workspaces/ws-slug"),
            ("DELETE", f"{chat_base}/api/workspaces/ws-slug"),
        ]
        assert calls[0][2] == {"id": "ws-slug", "name": "WS Name"}
        assert calls[1][2]["slug"] == "ws-slug" and calls[1][2]["name"] == "WS Name"
        assert calls[2][2] == calls[3][2] == {"name": "Renamed"}
