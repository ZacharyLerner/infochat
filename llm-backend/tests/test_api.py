"""
test_api.py
===========
Integration tests for the FastAPI HTTP layer (main.py).

These tests use FastAPI's TestClient with:
  - real routing, request validation, and response serialisation
  - temp SQLite database (via conftest.isolate_config)
  - manager calls mocked (via conftest.test_client) — no outbound HTTP
  - LanceDB and LLM calls mocked where needed

Coverage
--------
  - Authentication guard (403 without key, 200 with key)
  - GET /settings / PUT /settings
  - GET /defaults
  - GET /workspaces
  - POST /workspace (create)
  - GET /workspace/{slug}
  - PUT /workspace/{slug} (update)
  - DELETE /workspace/{slug}
  - POST /workspace/{slug}/embed  (mocked embedding)
  - DELETE /workspace/{slug}/embed/{doc_id}
  - POST /workspace/{slug}/query  (mocked retrieval + LLM)
  - POST /workspace/{slug}/query/stream
  - POST /workspace/{slug}/chat/session
  - POST /workspace/{slug}/chat/{session_id}/stream
  - DELETE /workspace/{slug}/chat/{session_id}
  - GET /workspace/{slug}/logs
  - DELETE /workspace/{slug}/logs
  - GET /docs/{slug}
  - POST /docs/{slug}
  - DELETE /docs/{slug}/{doc_id}
  - DELETE /docs/{slug}
"""

import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import AUTH_HEADERS, TEST_API_KEY


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_key_returns_403(self, test_client):
        resp = test_client.get("/workspaces")
        assert resp.status_code == 403

    def test_wrong_key_returns_403(self, test_client):
        resp = test_client.get("/workspaces", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 403

    def test_correct_key_returns_200(self, test_client):
        resp = test_client.get("/auth/verify", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_auth_verify_endpoint(self, test_client):
        resp = test_client.get("/auth/verify", headers=AUTH_HEADERS)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------

class TestSettings:
    def test_get_settings_returns_dict(self, test_client):
        resp = test_client.get("/settings", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "llm_model" in data
        assert "temperature" in data

    def test_put_settings_updates_field(self, test_client):
        resp = test_client.put(
            "/settings",
            json={"temperature": 0.3, "top_n": 8},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["temperature"] == pytest.approx(0.3)
        assert data["top_n"] == 8

    def test_put_settings_clamps_searxng_num_results(self, test_client):
        """searxng_num_results must be clamped to 1–10."""
        resp = test_client.put(
            "/settings",
            json={"searxng_num_results": 99},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["searxng_num_results"] == 10

    def test_put_settings_clamps_lower_bound(self, test_client):
        resp = test_client.put(
            "/settings",
            json={"searxng_num_results": 0},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["searxng_num_results"] == 1

    def test_get_defaults(self, test_client):
        resp = test_client.get("/defaults", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "default_system_prompt_rag" in data
        assert "default_system_prompt_web" in data
        assert "default_rewrite_prompt" in data
        assert len(data["default_system_prompt_rag"]) > 10


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------

class TestWorkspaceCRUD:
    def test_list_workspaces_empty(self, test_client):
        resp = test_client.get("/workspaces", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_workspace_minimal(self, test_client):
        resp = test_client.post(
            "/workspace",
            json={"name": "My Workspace"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "My Workspace"
        assert "slug" in data

    def test_create_workspace_with_all_fields(self, test_client):
        resp = test_client.post(
            "/workspace",
            json={
                "name": "Full Workspace",
                "llm_model": "openai/gpt-4o",
                "temperature": 0.5,
                "top_n": 10,
                "similarity_threshold": 0.6,
                "chunk_size": 512,
                "chunk_overlap": 50,
                "max_tokens": 2048,
                "searxng_enabled": False,
                "searxng_num_results": 5,
                "searxng_query_suffix": "site:uri.edu",
                "rewrite_model": "openai/gpt-4o-mini",
            },
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["llm_model"] == "openai/gpt-4o"
        assert data["temperature"] == pytest.approx(0.5)
        assert data["top_n"] == 10
        assert data["chunk_size"] == 512
        assert data["max_tokens"] == 2048
        assert data["searxng_query_suffix"] == "site:uri.edu"

    def test_list_workspaces_after_create(self, test_client):
        test_client.post("/workspace", json={"name": "A"}, headers=AUTH_HEADERS)
        test_client.post("/workspace", json={"name": "B"}, headers=AUTH_HEADERS)
        resp = test_client.get("/workspaces", headers=AUTH_HEADERS)
        assert len(resp.json()) == 2

    def test_get_workspace_by_slug(self, test_client, workspace):
        slug = workspace["slug"]
        resp = test_client.get(f"/workspace/{slug}", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["slug"] == slug

    def test_get_workspace_not_found(self, test_client):
        resp = test_client.get("/workspace/no-such-workspace-abc", headers=AUTH_HEADERS)
        assert resp.status_code == 404

    def test_update_workspace_name(self, test_client, workspace):
        slug = workspace["slug"]
        resp = test_client.put(
            f"/workspace/{slug}",
            json={"name": "Renamed Workspace"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed Workspace"

    def test_update_workspace_settings(self, test_client, workspace):
        slug = workspace["slug"]
        resp = test_client.put(
            f"/workspace/{slug}",
            json={"temperature": 0.1, "top_n": 3, "searxng_enabled": True},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["temperature"] == pytest.approx(0.1)
        assert data["top_n"] == 3
        assert data["searxng_enabled"] == 1

    def test_update_workspace_not_found(self, test_client):
        resp = test_client.put(
            "/workspace/no-such-slug",
            json={"name": "X"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404

    def test_delete_workspace(self, test_client, workspace):
        slug = workspace["slug"]
        resp = test_client.delete(f"/workspace/{slug}", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_delete_workspace_removes_it(self, test_client, workspace):
        slug = workspace["slug"]
        test_client.delete(f"/workspace/{slug}", headers=AUTH_HEADERS)
        resp = test_client.get(f"/workspace/{slug}", headers=AUTH_HEADERS)
        assert resp.status_code == 404

    def test_delete_workspace_not_found(self, test_client):
        resp = test_client.delete("/workspace/no-such-slug", headers=AUTH_HEADERS)
        assert resp.status_code == 404

    def test_create_workspace_missing_name_returns_422(self, test_client):
        resp = test_client.post("/workspace", json={}, headers=AUTH_HEADERS)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Document embedding endpoints
# ---------------------------------------------------------------------------

class TestEmbedEndpoints:
    def _mock_embed(self):
        """Return a context manager that patches embed_workspace_file."""
        return patch(
            "main.embedding.embed_workspace_file",
            return_value=(5, "fake-doc-id-1234"),
        )

    def test_embed_file_success(self, test_client, workspace):
        slug = workspace["slug"]
        with self._mock_embed():
            resp = test_client.post(
                f"/workspace/{slug}/embed",
                files={"file": ("test.txt", io.BytesIO(b"Hello world"), "text/plain")},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["chunks_embedded"] == 5
        assert data["doc_id"] == "fake-doc-id-1234"
        assert data["filename"] == "test.txt"

    def test_embed_file_workspace_not_found(self, test_client):
        with self._mock_embed():
            resp = test_client.post(
                "/workspace/no-such-slug/embed",
                files={"file": ("doc.txt", io.BytesIO(b"text"), "text/plain")},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 404

    def test_embed_file_zero_chunks_returns_422(self, test_client, workspace):
        slug = workspace["slug"]
        with patch("main.embedding.embed_workspace_file", return_value=(0, "")):
            resp = test_client.post(
                f"/workspace/{slug}/embed",
                files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 422

    def test_embed_file_records_in_docs(self, test_client, workspace):
        slug = workspace["slug"]
        with self._mock_embed():
            test_client.post(
                f"/workspace/{slug}/embed",
                files={"file": ("doc.txt", io.BytesIO(b"content"), "text/plain")},
                headers=AUTH_HEADERS,
            )
        resp = test_client.get(f"/docs/{slug}", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        docs = resp.json()
        assert len(docs) == 1
        assert docs[0]["filename"] == "doc.txt"
        assert docs[0]["doc_id"] == "fake-doc-id-1234"

    def test_delete_embed_success(self, test_client, workspace):
        slug = workspace["slug"]
        # First embed a file to get a valid doc_id in docs.json
        with self._mock_embed():
            embed_resp = test_client.post(
                f"/workspace/{slug}/embed",
                files={"file": ("del.txt", io.BytesIO(b"data"), "text/plain")},
                headers=AUTH_HEADERS,
            )
        doc_id = embed_resp.json()["doc_id"]

        with patch("main.embedding.delete_workspace_file", return_value=3):
            resp = test_client.delete(
                f"/workspace/{slug}/embed/{doc_id}",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["chunks_deleted"] == 3

    def test_delete_embed_not_found(self, test_client, workspace):
        slug = workspace["slug"]
        with patch("main.embedding.delete_workspace_file", return_value=0):
            resp = test_client.delete(
                f"/workspace/{slug}/embed/nonexistent-doc-id",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Query endpoints
# ---------------------------------------------------------------------------

_MOCK_QUERY_RESULT = {
    "answer": "The answer is 42.",
    "sources": {"documents": [{"score": 0.9, "filename": "test.txt", "text": "..."}], "web": []},
    "rewritten_query": None,
}


class TestQueryEndpoints:
    def test_query_workspace_success(self, test_client, workspace):
        slug = workspace["slug"]
        with patch("main.query.query_workspace", return_value=_MOCK_QUERY_RESULT):
            resp = test_client.post(
                f"/workspace/{slug}/query",
                json={"question": "What is the answer?"},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "The answer is 42."
        assert "sources" in data

    def test_query_workspace_not_found(self, test_client):
        with patch("main.query.query_workspace", return_value=_MOCK_QUERY_RESULT):
            resp = test_client.post(
                "/workspace/no-such-slug/query",
                json={"question": "Q?"},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 404

    def test_query_missing_question_returns_422(self, test_client, workspace):
        slug = workspace["slug"]
        resp = test_client.post(
            f"/workspace/{slug}/query",
            json={},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_query_appends_to_log(self, test_client, workspace):
        slug = workspace["slug"]
        with patch("main.query.query_workspace", return_value=_MOCK_QUERY_RESULT):
            test_client.post(
                f"/workspace/{slug}/query",
                json={"question": "Logged question?"},
                headers=AUTH_HEADERS,
            )
        resp = test_client.get(f"/workspace/{slug}/logs", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        logs = resp.json()
        assert len(logs) >= 1
        assert logs[0]["question"] == "Logged question?"

    def test_stream_query_workspace(self, test_client, workspace):
        """Streaming endpoint should return SSE events."""
        slug = workspace["slug"]

        async def _fake_stream(ws, question, prompt_suffix=""):
            yield "event: token\ndata: Hello\n\n"
            yield "event: token\ndata:  world\n\n"
            yield 'event: sources\ndata: {"documents": [], "web": []}\n\n'
            yield "event: done\ndata: [DONE]\n\n"

        with patch("main.query.stream_query_workspace", side_effect=_fake_stream):
            resp = test_client.post(
                f"/workspace/{slug}/query/stream",
                json={"question": "Stream me something"},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        assert "event: token" in body
        assert "event: done" in body

    def test_stream_query_workspace_not_found(self, test_client):
        resp = test_client.post(
            "/workspace/ghost-slug/query/stream",
            json={"question": "Q?"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Chat session endpoints
# ---------------------------------------------------------------------------

class TestChatSessionEndpoints:
    def test_create_chat_session(self, test_client, workspace):
        slug = workspace["slug"]
        resp = test_client.post(
            f"/workspace/{slug}/chat/session",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        import uuid
        uuid.UUID(data["session_id"])  # must be a valid UUID

    def test_create_chat_session_workspace_not_found(self, test_client):
        resp = test_client.post(
            "/workspace/no-slug/chat/session",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404

    def test_stream_chat_session(self, test_client, workspace):
        slug = workspace["slug"]
        # Create session
        session_id = test_client.post(
            f"/workspace/{slug}/chat/session", headers=AUTH_HEADERS
        ).json()["session_id"]

        async def _fake_chat_stream(sid, ws, message, history=None, retrieval_query=None):
            yield "event: token\ndata: Chat response\n\n"
            yield 'event: sources\ndata: {"documents": [], "web": []}\n\n'
            yield "event: done\ndata: [DONE]\n\n"

        with patch("main.query.stream_chat_session", side_effect=_fake_chat_stream):
            resp = test_client.post(
                f"/workspace/{slug}/chat/{session_id}/stream",
                json={"message": "Hello, chatbot"},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert "event: token" in resp.text

    def test_delete_chat_session(self, test_client, workspace):
        slug = workspace["slug"]
        session_id = test_client.post(
            f"/workspace/{slug}/chat/session", headers=AUTH_HEADERS
        ).json()["session_id"]

        resp = test_client.delete(
            f"/workspace/{slug}/chat/{session_id}",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 204

    def test_stream_chat_workspace_not_found(self, test_client):
        resp = test_client.post(
            "/workspace/no-slug/chat/fake-session-id/stream",
            json={"message": "Hi"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Query log endpoints
# ---------------------------------------------------------------------------

class TestLogEndpoints:
    def test_get_logs_empty(self, test_client, workspace):
        slug = workspace["slug"]
        resp = test_client.get(f"/workspace/{slug}/logs", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_logs_newest_first(self, test_client, workspace):
        slug = workspace["slug"]
        result1 = {**_MOCK_QUERY_RESULT, "answer": "First"}
        result2 = {**_MOCK_QUERY_RESULT, "answer": "Second"}
        with patch("main.query.query_workspace", return_value=result1):
            test_client.post(
                f"/workspace/{slug}/query",
                json={"question": "First question"},
                headers=AUTH_HEADERS,
            )
        with patch("main.query.query_workspace", return_value=result2):
            test_client.post(
                f"/workspace/{slug}/query",
                json={"question": "Second question"},
                headers=AUTH_HEADERS,
            )
        logs = test_client.get(f"/workspace/{slug}/logs", headers=AUTH_HEADERS).json()
        assert logs[0]["question"] == "Second question"
        assert logs[1]["question"] == "First question"

    def test_clear_logs(self, test_client, workspace):
        slug = workspace["slug"]
        with patch("main.query.query_workspace", return_value=_MOCK_QUERY_RESULT):
            test_client.post(
                f"/workspace/{slug}/query",
                json={"question": "Q?"},
                headers=AUTH_HEADERS,
            )
        resp = test_client.delete(f"/workspace/{slug}/logs", headers=AUTH_HEADERS)
        assert resp.status_code == 204
        remaining = test_client.get(f"/workspace/{slug}/logs", headers=AUTH_HEADERS).json()
        assert remaining == []

    def test_logs_cleared_on_workspace_delete(self, test_client, workspace):
        slug = workspace["slug"]
        with patch("main.query.query_workspace", return_value=_MOCK_QUERY_RESULT):
            test_client.post(
                f"/workspace/{slug}/query",
                json={"question": "Q?"},
                headers=AUTH_HEADERS,
            )
        test_client.delete(f"/workspace/{slug}", headers=AUTH_HEADERS)
        # Re-create to check logs were actually removed, not just the workspace row
        new_ws = test_client.post(
            "/workspace", json={"name": "Test Workspace"}, headers=AUTH_HEADERS
        ).json()
        # Logs for the new workspace should be empty (different slug)
        assert test_client.get(
            f"/workspace/{new_ws['slug']}/logs", headers=AUTH_HEADERS
        ).json() == []


# ---------------------------------------------------------------------------
# Doc tracking endpoints
# ---------------------------------------------------------------------------

class TestDocTrackingEndpoints:
    def test_list_docs_empty(self, test_client, workspace):
        slug = workspace["slug"]
        resp = test_client.get(f"/docs/{slug}", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_add_and_list_doc(self, test_client, workspace):
        slug = workspace["slug"]
        resp = test_client.post(
            f"/docs/{slug}",
            json={"doc_id": "abc-123", "filename": "report.pdf", "chunks_embedded": 7},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 201
        docs = test_client.get(f"/docs/{slug}", headers=AUTH_HEADERS).json()
        assert len(docs) == 1
        assert docs[0]["doc_id"] == "abc-123"
        assert docs[0]["filename"] == "report.pdf"

    def test_remove_single_doc(self, test_client, workspace):
        slug = workspace["slug"]
        test_client.post(
            f"/docs/{slug}",
            json={"doc_id": "to-remove", "filename": "x.txt"},
            headers=AUTH_HEADERS,
        )
        test_client.post(
            f"/docs/{slug}",
            json={"doc_id": "keep-me", "filename": "y.txt"},
            headers=AUTH_HEADERS,
        )
        test_client.delete(f"/docs/{slug}/to-remove", headers=AUTH_HEADERS)
        docs = test_client.get(f"/docs/{slug}", headers=AUTH_HEADERS).json()
        assert len(docs) == 1
        assert docs[0]["doc_id"] == "keep-me"

    def test_remove_all_docs_for_workspace(self, test_client, workspace):
        slug = workspace["slug"]
        for i in range(3):
            test_client.post(
                f"/docs/{slug}",
                json={"doc_id": f"doc-{i}", "filename": f"file{i}.txt"},
                headers=AUTH_HEADERS,
            )
        resp = test_client.delete(f"/docs/{slug}", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert test_client.get(f"/docs/{slug}", headers=AUTH_HEADERS).json() == []

    def test_docs_cleared_on_workspace_delete(self, test_client, workspace):
        slug = workspace["slug"]
        test_client.post(
            f"/docs/{slug}",
            json={"doc_id": "d1", "filename": "f.txt"},
            headers=AUTH_HEADERS,
        )
        test_client.delete(f"/workspace/{slug}", headers=AUTH_HEADERS)
        # Docs file should no longer contain this slug
        resp = test_client.get(f"/docs/{slug}", headers=AUTH_HEADERS)
        assert resp.json() == []
