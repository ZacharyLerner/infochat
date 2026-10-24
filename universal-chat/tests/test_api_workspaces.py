"""Tests for the workspace settings API (main.py) and its S3 store (store.py)."""
import store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_ws(client, slug="demo", name="Demo WS", **kwargs):
    """Helper: POST /api/workspaces and return the response."""
    payload = {"slug": slug, "name": name, **kwargs}
    return client.post("/api/workspaces", json=payload)


# ---------------------------------------------------------------------------
# Workspace CRUD routes
# ---------------------------------------------------------------------------

class TestWorkspaceApi:
    def test_create_returns_201_with_defaults(self, client):
        resp = _create_ws(client, slug="test", name="Test Workspace")
        assert resp.status_code == 201
        assert resp.json() == {
            "slug": "test",
            "name": "Test Workspace",
            "welcome_text": "Send a message to get started.",
            "followup_enabled": False,
            "followup_count": 3,
            "email_enabled": False,
            "default_questions": [],
        }

    def test_create_duplicate_slug_returns_409(self, client):
        _create_ws(client, slug="dup")
        resp = _create_ws(client, slug="dup")
        assert resp.status_code == 409

    def test_get_and_missing_returns_404(self, client):
        _create_ws(client, slug="finance", name="Finance Office")
        assert client.get("/api/workspaces/finance").json()["name"] == "Finance Office"
        assert client.get("/api/workspaces/nonexistent").status_code == 404

    def test_list(self, client):
        assert client.get("/api/workspaces").json() == []
        _create_ws(client, slug="ws1", name="WS One")
        _create_ws(client, slug="ws2", name="WS Two")
        assert sorted(w["slug"] for w in client.get("/api/workspaces").json()) == ["ws1", "ws2"]

    def test_update_persists(self, client):
        _create_ws(client, slug="persist", name="Orig")
        resp = client.put("/api/workspaces/persist", json={
            "name": "Updated",
            "followup_count": 2,
            "default_questions": [{"category": "FAQ", "questions": ["Q1?"]}],
        })
        assert resp.status_code == 200
        data = client.get("/api/workspaces/persist").json()
        assert data["name"] == "Updated"
        assert data["followup_count"] == 2
        assert data["default_questions"] == [{"category": "FAQ", "questions": ["Q1?"]}]

    def test_update_missing_returns_404(self, client):
        resp = client.put("/api/workspaces/ghost", json={"name": "Ghost", "followup_count": 3})
        assert resp.status_code == 404

    def test_delete_and_missing_returns_404(self, client):
        _create_ws(client, slug="del", name="Delete Me")
        assert client.delete("/api/workspaces/del").status_code == 204
        assert client.get("/api/workspaces/del").status_code == 404
        assert client.delete("/api/workspaces/del").status_code == 404

    def test_chat_page_requires_workspace(self, client):
        _create_ws(client, slug="myws", name="My WS")
        assert client.get("/myws").status_code == 200
        assert client.get("/no-such-workspace").status_code == 404


# ---------------------------------------------------------------------------
# store.py — S3 behaviour the API relies on
# ---------------------------------------------------------------------------

class TestStore:
    def test_create_conflict_returns_false(self, fake_s3):
        assert store.create_workspace({"slug": "a", "name": "A"}) is True
        assert store.create_workspace({"slug": "a", "name": "B"}) is False
        assert store.get_workspace("a")["name"] == "A"

    def test_update_missing_returns_none(self, fake_s3):
        assert store.update_workspace("ghost", {"name": "X"}) is None
        assert "workspaces/ghost/chat.json" not in fake_s3.objects

    def test_list_only_includes_slugs_with_chat_json(self, fake_s3):
        store.create_workspace({"slug": "chat", "name": "Chat"})
        # llm-backend's own files share the workspaces/<slug>/ prefix
        fake_s3.put_object(Bucket="b", Key="workspaces/backend-only/workspace.json", Body=b"{}")
        assert [ws["slug"] for ws in store.list_workspaces()] == ["chat"]
