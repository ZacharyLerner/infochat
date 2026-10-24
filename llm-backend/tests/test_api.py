"""
test_api.py
===========
Integration tests for the FastAPI HTTP layer (main.py).

These tests use FastAPI's TestClient with:
  - real routing, request validation, and response serialisation
  - in-memory fake S3 bucket for settings (via conftest.isolate_config)
  - manager calls and S3 Vectors index lifecycle mocked (via conftest.test_client)
  - embedding and LLM calls mocked where needed

Coverage
--------
  - Authentication guard on /api, SPA catch-all (no key, no path traversal)
  - Workspace create rollback, embed-model lock, delete cleanup
  - Embedding upload recorded in docs.json, vectors removed if recording fails
  - Query logging
  - API key masking
"""

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import AUTH_HEADERS


# ---------------------------------------------------------------------------
# Authentication / SPA
# ---------------------------------------------------------------------------

class TestAuth:
    @pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
    def test_api_rejects_missing_or_wrong_key(self, test_client, headers):
        assert test_client.get("/api/workspaces", headers=headers).status_code == 403

    def test_correct_key_accepted(self, test_client):
        resp = test_client.get("/api/auth/verify", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_spa_catchall_needs_no_key_and_blocks_traversal(self, test_client):
        index_html = Path("public/index.html").read_text()
        resp = test_client.get("/workspace/some-slug/query")
        assert resp.status_code == 200
        assert resp.text == index_html
        assert test_client.get("/app.js").text == Path("public/app.js").read_text()
        # A path escaping public/ falls back to index.html instead of leaking source
        traversal = test_client.get("/%2e%2e/main.py")
        assert traversal.text == index_html


# ---------------------------------------------------------------------------
# Workspace lifecycle
# ---------------------------------------------------------------------------

class TestWorkspace:
    def test_create_rolls_back_if_index_cannot_be_made(self, test_client):
        with patch("main.embedding.create_workspace_index", side_effect=RuntimeError("s3v down")):
            resp = test_client.post("/api/workspace", json={"name": "Doomed"}, headers=AUTH_HEADERS)
        assert resp.status_code == 502
        assert test_client.get("/api/workspaces", headers=AUTH_HEADERS).json() == []

    def test_embed_model_locked_after_creation(self, test_client):
        ws = test_client.post(
            "/api/workspace", json={"name": "Locked", "embed_model": "model-a"}, headers=AUTH_HEADERS,
        ).json()
        resp = test_client.put(
            f"/api/workspace/{ws['slug']}",
            json={"name": "Renamed", "embed_model": "model-b"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"
        assert resp.json()["embed_model"] == "model-a"

    def test_delete_removes_index_and_records(self, test_client, workspace):
        import embedding
        slug = workspace["slug"]
        test_client.post(f"/api/docs/{slug}", json={"doc_id": "d1", "filename": "f.txt"}, headers=AUTH_HEADERS)
        with patch("main.query.query_workspace", return_value=_MOCK_QUERY_RESULT):
            test_client.post(f"/api/workspace/{slug}/query", json={"question": "Q?"}, headers=AUTH_HEADERS)

        assert test_client.delete(f"/api/workspace/{slug}", headers=AUTH_HEADERS).status_code == 200
        embedding.delete_workspace_index.assert_called_once_with(slug)
        assert test_client.get(f"/api/workspace/{slug}", headers=AUTH_HEADERS).status_code == 404
        assert test_client.get(f"/api/docs/{slug}", headers=AUTH_HEADERS).json() == []
        assert test_client.get(f"/api/workspace/{slug}/logs", headers=AUTH_HEADERS).json()["entries"] == []


# ---------------------------------------------------------------------------
# Document embedding endpoints
# ---------------------------------------------------------------------------

class TestEmbedEndpoints:
    # doc_id is always a server-generated UUID in production
    FAKE_DOC_ID = "12345678-1234-5678-1234-567812345678"

    def _post_file(self, test_client, slug, name="doc.txt"):
        return test_client.post(
            f"/api/workspace/{slug}/embed",
            files={"file": (name, io.BytesIO(b"content"), "text/plain")},
            headers=AUTH_HEADERS,
        )

    def test_embed_file_records_in_docs(self, test_client, workspace):
        slug = workspace["slug"]
        with patch("main.embedding.embed_workspace_file", return_value=(5, self.FAKE_DOC_ID)):
            resp = self._post_file(test_client, slug)
        assert resp.status_code == 200
        assert resp.json()["chunks_embedded"] == 5
        docs = test_client.get(f"/api/docs/{slug}", headers=AUTH_HEADERS).json()
        assert [(d["doc_id"], d["filename"]) for d in docs] == [(self.FAKE_DOC_ID, "doc.txt")]

    def test_embed_record_failure_removes_vectors(self, test_client, workspace):
        """If docs.json can't be updated, the new vectors are deleted and a retryable 503 is returned."""
        slug = workspace["slug"]
        with (
            patch("main.embedding.embed_workspace_file", return_value=(5, self.FAKE_DOC_ID)),
            patch("main.db.add_doc", side_effect=RuntimeError("Too many concurrent updates")),
            patch("main.embedding.delete_workspace_file", return_value=5) as delete,
        ):
            resp = self._post_file(test_client, slug, "busy.txt")
        assert resp.status_code == 503
        delete.assert_called_once_with(slug, self.FAKE_DOC_ID, 5)
        assert test_client.get(f"/api/docs/{slug}", headers=AUTH_HEADERS).json() == []


# ---------------------------------------------------------------------------
# Query endpoints
# ---------------------------------------------------------------------------

_MOCK_QUERY_RESULT = {
    "answer": "The answer is 42.",
    "sources": {"documents": [{"score": 0.9, "filename": "test.txt", "text": "..."}], "web": []},
    "rewritten_query": None,
}


class TestQueryEndpoints:
    def test_query_answers_and_appends_to_log(self, test_client, workspace):
        slug = workspace["slug"]
        with patch("main.query.query_workspace", return_value=_MOCK_QUERY_RESULT):
            resp = test_client.post(
                f"/api/workspace/{slug}/query",
                json={"question": "Logged question?"},
                headers=AUTH_HEADERS,
            )
        assert resp.json()["answer"] == "The answer is 42."
        logs = test_client.get(f"/api/workspace/{slug}/logs", headers=AUTH_HEADERS).json()["entries"]
        assert logs[0]["question"] == "Logged question?"


# ---------------------------------------------------------------------------
# API key masking
# ---------------------------------------------------------------------------

class TestKeyMasking:
    def test_settings_keys_masked(self, test_client):
        test_client.put("/api/settings", json={"api_key": "sk-global-abcd"}, headers=AUTH_HEADERS)
        body = test_client.get("/api/settings", headers=AUTH_HEADERS).json()
        assert body["api_key"] == ""
        assert body["api_key_hint"] == "…abcd"

    def test_workspace_keys_masked_everywhere_and_kept_on_save(self, test_client):
        import db
        ws = test_client.post(
            "/api/workspace", json={"name": "Masked", "api_key": "sk-ws-1234"}, headers=AUTH_HEADERS,
        ).json()
        assert ws["api_key"] == "" and ws["api_key_hint"] == "…1234"
        assert test_client.get(f"/api/workspace/{ws['slug']}", headers=AUTH_HEADERS).json()["api_key"] == ""
        assert all(w["api_key"] == "" for w in test_client.get("/api/workspaces", headers=AUTH_HEADERS).json())
        # Saving the form without re-entering the key must not wipe it
        test_client.put(f"/api/workspace/{ws['slug']}", json={"temperature": 0.2}, headers=AUTH_HEADERS)
        assert db.get_workspace(ws["slug"])["api_key"] == "sk-ws-1234"
