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
import json
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

    def test_served_under_root_path_and_at_root(self, test_client, monkeypatch):
        import config
        monkeypatch.setattr(config, "ROOT_PATH", "/admin")
        page = test_client.get("/admin/workspace/some-slug/query")
        assert page.status_code == 200 and '<base href="/admin/" />' in page.text
        bare = test_client.get("/admin", follow_redirects=False)
        assert bare.status_code == 200 and '<base href="/admin/" />' in bare.text
        assert test_client.get("/admin/app.js").text == Path("public/app.js").read_text()
        # Chat and the uploader call the API without the prefix; the load balancer sends it with
        for path in ("/api/auth/verify", "/admin/api/auth/verify"):
            assert test_client.get(path, headers=AUTH_HEADERS).status_code == 200

    def test_workspace_key_only_opens_its_own_workspace(self, test_client, workspace):
        other = test_client.post("/api/workspace", json={"name": "Other"}, headers=AUTH_HEADERS).json()
        key = workspace["access_key"]
        assert len(key) == len(AUTH_HEADERS["X-API-Key"]) and key != other["access_key"]
        own = {"X-API-Key": key}
        slug = workspace["slug"]
        assert test_client.get(f"/api/docs/{slug}", headers=own).status_code == 200
        assert test_client.get(f"/api/workspace/{slug}", headers=own).status_code == 200
        # Not another workspace, not admin-wide routes, and not changing or deleting itself
        assert test_client.get(f"/api/docs/{other['slug']}", headers=own).status_code == 403
        assert test_client.get("/api/workspaces", headers=own).status_code == 403
        assert test_client.put(f"/api/workspace/{slug}", json={"name": "x"}, headers=own).status_code == 403
        assert test_client.delete(f"/api/workspace/{slug}", headers=own).status_code == 403

    @pytest.mark.parametrize("peer, trusted, expected", [
        ("10.140.2.31", "10.140.2.31,127.0.0.1", 200),     # chat / uploader
        ("127.0.0.1", "10.140.2.31,127.0.0.1", 200),       # the backend host's own checks
        ("131.128.1.77", "10.140.2.31,127.0.0.1", 403),    # anyone through the load balancer
        ("10.140.9.9", "10.140.2.0/28", 403),
        ("10.140.2.5", "10.140.2.0/28", 200),
    ])
    def test_admin_key_header_only_from_trusted_services(self, test_client, monkeypatch, peer, trusted, expected):
        import config, main
        from fastapi.testclient import TestClient
        monkeypatch.setattr(config, "TRUSTED_SERVICE_IPS", trusted)
        client = TestClient(main.app, client=(peer, 50000))
        assert client.get("/api/workspaces", headers=AUTH_HEADERS).status_code == expected

    def test_admin_ui_logs_in_for_a_session_cookie(self, test_client, workspace, monkeypatch):
        import config, main
        from fastapi.testclient import TestClient
        monkeypatch.setattr(config, "TRUSTED_SERVICE_IPS", "10.140.2.31")
        browser = TestClient(main.app, base_url="https://testserver", client=("131.128.1.77", 50000))
        assert browser.get("/api/workspaces", headers=AUTH_HEADERS).status_code == 403
        assert browser.post("/api/auth/login", json={"key": "wrong"}).status_code == 403
        resp = browser.post("/api/auth/login", json={"key": AUTH_HEADERS["X-API-Key"]})
        assert resp.status_code == 200
        cookie = resp.headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ("httponly", "secure", "samesite=strict"))
        assert browser.get("/api/workspaces").status_code == 200        # cookie, no key sent
        # Workspace keys still work from anywhere, on their own workspace
        assert browser.get(f"/api/docs/{workspace['slug']}", cookies={},
                           headers={"X-API-Key": workspace["access_key"]}).status_code == 200
        browser.post("/api/auth/logout")
        assert browser.get("/api/workspaces").status_code == 403

    def test_session_is_signed_and_expires(self, monkeypatch):
        import main
        token = main._new_session()
        assert main._valid_session(token)
        expires, sig = token.split(".")
        assert not main._valid_session(f"{int(expires) + 999}.{sig}")   # tampered expiry
        assert not main._valid_session("garbage") and not main._valid_session(None)
        monkeypatch.setattr(main._time, "time", lambda: int(expires) + 1)
        assert not main._valid_session(token)                            # expired

    def test_startup_gives_old_workspaces_a_key(self, test_client):
        import db, main
        ws = db.create_workspace(name="Old")
        db._update_json(db._ws_key(ws["slug"], "settings.json"),
                        lambda cur: {k: v for k, v in cur.items() if k != "access_key"})
        main._ensure_indexes()
        assert len(db.get_workspace(ws["slug"])["access_key"]) == len(AUTH_HEADERS["X-API-Key"])


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


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

class TestListModels:
    def _gateway(self, seen_keys):
        class FakeGateway:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): pass

            async def get(self, url, headers):
                seen_keys.append(headers["Authorization"])
                class Res:
                    status_code = 200
                    def json(self):
                        return {"data": [{"id": "openai/gpt-4o"}, {"id": "text-embed-3"}]}
                return Res()
        return patch("main._httpx.AsyncClient", FakeGateway)

    def test_blank_key_uses_saved_workspace_then_global_key(self, test_client):
        test_client.put("/api/settings", json={"api_key": "sk-global"}, headers=AUTH_HEADERS)
        ws = test_client.post("/api/workspace", json={"name": "Models", "api_key": "sk-ws"}, headers=AUTH_HEADERS).json()
        seen = []
        with self._gateway(seen):
            res = test_client.post("/api/models", json={"workspace": ws["slug"]}, headers=AUTH_HEADERS)
            test_client.post("/api/models", json={}, headers=AUTH_HEADERS)
            test_client.post("/api/models", json={"api_key": "sk-typed", "workspace": ws["slug"]}, headers=AUTH_HEADERS)
        assert res.json() == {"llm_models": ["openai/gpt-4o"], "embedding_models": ["text-embed-3"]}
        assert seen == ["Bearer sk-ws", "Bearer sk-global", "Bearer sk-typed"]

    def test_unknown_workspace_and_missing_key_are_rejected(self, test_client):
        assert test_client.post("/api/models", json={"workspace": "nope"}, headers=AUTH_HEADERS).status_code == 404
        assert test_client.post("/api/models", json={}, headers=AUTH_HEADERS).status_code == 400


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

class TestActivity:
    def _events(self):
        import activity
        return [json.loads(e["message"]) for e in activity._pending]

    def test_upload_by_workspace_key_logs_prefix_not_key(self, test_client, workspace):
        slug, key = workspace["slug"], workspace["access_key"]
        with patch("main.embedding.embed_workspace_file", return_value=(3, TestEmbedEndpoints.FAKE_DOC_ID)):
            resp = test_client.post(f"/api/workspace/{slug}/embed", files={"file": ("a.txt", io.BytesIO(b"hi"), "text/plain")},
                                    headers={"X-API-Key": key, "X-Infochat-Source": "uploader"})
        assert resp.status_code == 200
        upload = [e for e in self._events() if e["event"] == "upload"]
        assert upload == [{"event": "upload", "workspace": slug, "workspace_name": "Test Workspace",
                           "key_prefix": "openai", "caller": "workspace_key", "source": "uploader",
                           "filename": "a.txt", "doc_id": TestEmbedEndpoints.FAKE_DOC_ID, "chunks": 3, "bytes": 2}]
        import activity
        assert all(key not in e["message"] for e in activity._pending)

    def test_admin_create_and_query_logged(self, test_client, workspace):
        with patch("main.query.query_workspace", return_value=_MOCK_QUERY_RESULT):
            test_client.post(f"/api/workspace/{workspace['slug']}/query", json={"question": "Q?"}, headers=AUTH_HEADERS)
        events = self._events()
        assert [e["event"] for e in events] == ["workspace_create", "query"]
        assert events[1]["caller"] == "admin" and events[1]["source"] == "api" and "duration_ms" in events[1]
        assert "Q?" not in json.dumps(events)  # questions stay in the per-workspace log only

    def test_activity_route_is_admin_only_and_validates_filters(self, test_client, workspace):
        own = {"X-API-Key": workspace["access_key"]}
        assert test_client.get("/api/activity", headers=own).status_code == 403
        assert test_client.get('/api/activity?key_prefix=x" or 1', headers=AUTH_HEADERS).status_code == 400

    def test_activity_route_builds_filtered_queries(self, test_client, workspace):
        import activity
        calls = []

        def fake(query, start, end, limit):
            calls.append(query)
            return [{"key_prefix": "old_prefix", "workspace": "gone", "event": "chat", "count": "2"}] if "stats" in query else []
        with patch.object(activity, "_insights", side_effect=fake):
            resp = test_client.get("/api/activity?key_prefix=openai&event=chat&hours=48", headers=AUTH_HEADERS)
        assert resp.status_code == 200, resp.text
        assert all('filter key_prefix = "openai" and event = "chat"' in q for q in calls) and len(calls) == 2
        body = resp.json()
        assert body["key_prefixes"] == ["old_prefix", "openai"]
        assert body["workspaces"] == [{"slug": workspace["slug"], "name": "Test Workspace", "key_prefix": "openai"}]

    def test_flush_batches_and_keeps_events_when_cloudwatch_fails(self, monkeypatch):
        import activity
        from unittest.mock import MagicMock
        client = MagicMock()
        client.put_log_events.side_effect = [RuntimeError("down"), None, None]
        monkeypatch.setattr(activity, "_logs", lambda: client)
        monkeypatch.setattr(activity, "_MAX_BATCH", 2)
        for i in range(3):
            activity.record("chat", {"slug": f"s{i}", "llm_model": "p/m"})
        activity.flush()                      # CloudWatch down: nothing lost
        assert len(activity._pending) == 3
        activity.flush()                      # back up: sent as batches of 2 and 1
        assert activity._pending == []
        sizes = [len(c.kwargs["logEvents"]) for c in client.put_log_events.call_args_list]
        assert sizes == [2, 2, 1]
