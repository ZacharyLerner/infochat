"""Tests for the workspace CRUD API routes in main.py."""
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_ws(client, slug="demo", name="Demo WS", **kwargs):
    """Helper: POST /api/workspaces and return the response."""
    payload = {"slug": slug, "name": name, **kwargs}
    return client.post("/api/workspaces", json=payload)


# ---------------------------------------------------------------------------
# POST /api/workspaces
# ---------------------------------------------------------------------------

class TestCreateWorkspace:
    def test_create_success(self, client):
        resp = _create_ws(client, slug="test", name="Test Workspace")
        assert resp.status_code == 201
        data = resp.json()
        assert data["slug"] == "test"
        assert data["name"] == "Test Workspace"
        assert data["followup_enabled"] is False
        assert data["followup_count"] == 3
        assert data["email_enabled"] is False
        assert data["welcome_text"] == "Send a message to get started."
        assert data["default_questions"] == []

    def test_create_with_all_fields(self, client):
        resp = _create_ws(
            client,
            slug="admissions",
            name="Admissions Office",
            welcome_text="How can I help?",
            followup_enabled=True,
            followup_count=4,
            email_enabled=True,
            default_questions=[
                {"category": "Help", "questions": ["What can you do?"]}
            ],
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["followup_enabled"] is True
        assert data["followup_count"] == 4
        assert data["email_enabled"] is True
        assert len(data["default_questions"]) == 1
        assert data["default_questions"][0]["category"] == "Help"

    def test_create_duplicate_slug_returns_409(self, client):
        _create_ws(client, slug="dup")
        resp = _create_ws(client, slug="dup")
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    def test_create_missing_slug_returns_422(self, client):
        resp = client.post("/api/workspaces", json={"name": "No Slug"})
        assert resp.status_code == 422

    def test_create_missing_name_returns_422(self, client):
        resp = client.post("/api/workspaces", json={"slug": "no-name"})
        assert resp.status_code == 422

    def test_create_followup_count_out_of_range_returns_422(self, client):
        resp = _create_ws(client, slug="bad", name="Bad", followup_count=0)
        assert resp.status_code == 422

    def test_create_followup_count_too_high_returns_422(self, client):
        resp = _create_ws(client, slug="bad2", name="Bad2", followup_count=10)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/workspaces
# ---------------------------------------------------------------------------

class TestListWorkspaces:
    def test_empty_list(self, client):
        resp = client.get("/api/workspaces")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_after_creates(self, client):
        _create_ws(client, slug="ws1", name="WS One")
        _create_ws(client, slug="ws2", name="WS Two")
        resp = client.get("/api/workspaces")
        assert resp.status_code == 200
        slugs = [w["slug"] for w in resp.json()]
        assert "ws1" in slugs
        assert "ws2" in slugs

    def test_list_count(self, client):
        for i in range(3):
            _create_ws(client, slug=f"ws-{i}", name=f"WS {i}")
        resp = client.get("/api/workspaces")
        assert len(resp.json()) == 3


# ---------------------------------------------------------------------------
# GET /api/workspaces/{slug}
# ---------------------------------------------------------------------------

class TestGetWorkspace:
    def test_get_existing(self, client):
        _create_ws(client, slug="finance", name="Finance Office")
        resp = client.get("/api/workspaces/finance")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Finance Office"

    def test_get_nonexistent_returns_404(self, client):
        resp = client.get("/api/workspaces/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_get_returns_full_fields(self, client):
        _create_ws(
            client,
            slug="full",
            name="Full WS",
            followup_enabled=True,
            followup_count=2,
            email_enabled=True,
        )
        resp = client.get("/api/workspaces/full")
        data = resp.json()
        assert data["followup_enabled"] is True
        assert data["followup_count"] == 2
        assert data["email_enabled"] is True


# ---------------------------------------------------------------------------
# PUT /api/workspaces/{slug}
# ---------------------------------------------------------------------------

class TestUpdateWorkspace:
    def test_update_name(self, client):
        _create_ws(client, slug="upd", name="Old Name")
        resp = client.put(
            "/api/workspaces/upd",
            json={"name": "New Name", "followup_count": 3},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    def test_update_persists(self, client):
        _create_ws(client, slug="persist", name="Orig")
        client.put("/api/workspaces/persist", json={"name": "Updated", "followup_count": 2})
        resp = client.get("/api/workspaces/persist")
        assert resp.json()["name"] == "Updated"
        assert resp.json()["followup_count"] == 2

    def test_update_nonexistent_returns_404(self, client):
        resp = client.put(
            "/api/workspaces/ghost",
            json={"name": "Ghost", "followup_count": 3},
        )
        assert resp.status_code == 404

    def test_update_email_enabled(self, client):
        _create_ws(client, slug="email-ws", name="Email WS")
        resp = client.put(
            "/api/workspaces/email-ws",
            json={"name": "Email WS", "email_enabled": True, "followup_count": 3},
        )
        assert resp.status_code == 200
        assert resp.json()["email_enabled"] is True

    def test_update_default_questions(self, client):
        _create_ws(client, slug="dq", name="DQ")
        resp = client.put(
            "/api/workspaces/dq",
            json={
                "name": "DQ",
                "followup_count": 3,
                "default_questions": [
                    {"category": "FAQ", "questions": ["Q1?", "Q2?"]}
                ],
            },
        )
        assert resp.status_code == 200
        qs = resp.json()["default_questions"]
        assert len(qs) == 1
        assert qs[0]["category"] == "FAQ"
        assert "Q1?" in qs[0]["questions"]

    def test_update_invalid_followup_count_returns_422(self, client):
        _create_ws(client, slug="inv", name="Inv")
        resp = client.put(
            "/api/workspaces/inv",
            json={"name": "Inv", "followup_count": 99},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/workspaces/{slug}
# ---------------------------------------------------------------------------

class TestDeleteWorkspace:
    def test_delete_existing(self, client):
        _create_ws(client, slug="del", name="Delete Me")
        resp = client.delete("/api/workspaces/del")
        assert resp.status_code == 204

    def test_delete_removes_from_list(self, client):
        _create_ws(client, slug="gone", name="Gone")
        client.delete("/api/workspaces/gone")
        resp = client.get("/api/workspaces")
        slugs = [w["slug"] for w in resp.json()]
        assert "gone" not in slugs

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete("/api/workspaces/nope")
        assert resp.status_code == 404

    def test_delete_then_get_returns_404(self, client):
        _create_ws(client, slug="vanish", name="Vanish")
        client.delete("/api/workspaces/vanish")
        resp = client.get("/api/workspaces/vanish")
        assert resp.status_code == 404

    def test_can_recreate_after_delete(self, client):
        _create_ws(client, slug="recycle", name="Recycle")
        client.delete("/api/workspaces/recycle")
        resp = _create_ws(client, slug="recycle", name="Recycled")
        assert resp.status_code == 201
        assert resp.json()["name"] == "Recycled"


# ---------------------------------------------------------------------------
# GET /{slug}  — chat page rendering
# ---------------------------------------------------------------------------

class TestChatPage:
    def test_chat_page_exists(self, client):
        _create_ws(client, slug="myws", name="My WS")
        resp = client.get("/myws")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_chat_page_not_found(self, client):
        resp = client.get("/no-such-workspace")
        assert resp.status_code == 404

    def test_chat_page_contains_slug(self, client):
        _create_ws(client, slug="library", name="Library")
        resp = client.get("/library")
        assert resp.status_code == 200
        # The template should render the workspace name or slug somewhere
        assert "library" in resp.text.lower() or "Library" in resp.text
