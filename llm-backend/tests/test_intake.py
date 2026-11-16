"""
test_intake.py
==============
The unauthenticated chatbot-intake routes (intake.py) used by the "request a chatbot" skill.

Coverage
--------
  - No admin key needed; the owner list still needs one
  - Create stores config + owner record, registers downstream, returns links
  - Owner details are never returned by the public routes
  - Validation (email, lengths) and the not-configured case
  - Rollback when the vector index or the downstream registration fails
  - /test and the GET only reach bots created through intake
"""

from unittest.mock import patch

import pytest

from tests.conftest import AUTH_HEADERS

VALID = {
    "username": "jdoe",
    "email": "jdoe@uri.edu",
    "name": "Graduate Admissions Helper",
    "purpose": "Prospective grad students asking about deadlines",
    "system_prompt": "You are the Graduate Admissions helper.",
    "welcome_text": "Ask about applying.",
    "starter_questions": [{"category": "Applying", "questions": ["What are the deadlines?"]}],
    "followups": True,
}


@pytest.fixture(autouse=True)
def intake_config(monkeypatch):
    import config
    monkeypatch.setattr(config, "INTAKE_LLM_MODEL", "gw/llm")
    monkeypatch.setattr(config, "INTAKE_EMBED_MODEL", "gw/embed")
    monkeypatch.setattr(config, "INTAKE_API_KEY", "gw-secret-1234")
    monkeypatch.setattr(config, "PUBLIC_CHAT_URL", "https://chat.example/chat")
    monkeypatch.setattr(config, "PUBLIC_UPLOAD_URL", "https://chat.example/upload")


@pytest.fixture()
def registered():
    """Downstream registration succeeds, and records what it was sent."""
    import manager
    with patch.object(manager, "register_downstream", return_value={"uploader": True, "chat": True}) as reg, \
         patch.object(manager, "unregister_downstream", return_value=None) as unreg:
        reg.unregister = unreg
        yield reg


def create(client, **overrides):
    return client.post("/api/intake/chatbot", json={**VALID, **overrides})   # no auth header


class TestCreate:
    def test_needs_no_admin_key_and_returns_links(self, test_client, registered):
        resp = create(test_client)
        assert resp.status_code == 200, resp.text
        out = resp.json()
        slug = out["slug"]
        assert out["name"] == VALID["name"]
        assert out["chat_page"] == f"https://chat.example/chat/{slug}"
        assert out["uploader_page"] == f"https://chat.example/upload/{slug}"
        assert out["embed_script_url"] == "https://chat.example/chat/embed.js"
        assert f'src="https://chat.example/chat/embed.js"' in out["embed_snippet"]
        assert f'data-workspace="{slug}"' in out["embed_snippet"]

    def test_workspace_uses_server_side_models_and_key(self, test_client, registered):
        slug = create(test_client).json()["slug"]
        ws = test_client.get(f"/api/workspace/{slug}", headers=AUTH_HEADERS).json()
        assert (ws["llm_model"], ws["embed_model"]) == ("gw/llm", "gw/embed")
        assert ws["api_key_hint"].endswith("1234")
        assert ws["system_prompt"] == VALID["system_prompt"]
        assert ws["temperature"] == 0.3
        assert ws["searxng_enabled"] == 0

    def test_web_search_sets_suffix(self, test_client, registered):
        slug = create(test_client, web_search=True).json()["slug"]
        ws = test_client.get(f"/api/workspace/{slug}", headers=AUTH_HEADERS).json()
        assert ws["searxng_enabled"] == 1 and ws["searxng_query_suffix"] == "site:uri.edu"

    def test_owner_recorded_and_listed_for_admins_only(self, test_client, registered):
        slug = create(test_client).json()["slug"]
        assert test_client.get("/api/owners").status_code == 403
        owners = test_client.get("/api/owners", headers=AUTH_HEADERS).json()
        assert [o["slug"] for o in owners] == [slug]
        assert owners[0]["username"] == "jdoe" and owners[0]["email"] == "jdoe@uri.edu"
        assert owners[0]["purpose"] == VALID["purpose"] and owners[0]["created_at"]

    def test_owner_details_never_in_public_responses(self, test_client, registered):
        created = create(test_client)
        slug = created.json()["slug"]
        for body in (created.text, test_client.get(f"/api/intake/chatbot/{slug}").text):
            assert "jdoe" not in body
        ws = test_client.get(f"/api/workspace/{slug}", headers=AUTH_HEADERS).text
        assert "jdoe" not in ws   # kept out of settings.json too

    def test_registers_downstream_with_chat_settings(self, test_client, registered):
        slug = create(test_client).json()["slug"]
        args = registered.call_args.args
        assert args[:2] == (slug, VALID["name"])
        assert args[2] == {
            "welcome_text": "Ask about applying.", "followup_enabled": True, "followup_count": 3,
            "default_questions": [{"category": "Applying", "questions": ["What are the deadlines?"]}],
        }

    def test_falls_back_to_global_settings(self, test_client, registered, monkeypatch):
        import config
        for name in ("INTAKE_LLM_MODEL", "INTAKE_EMBED_MODEL", "INTAKE_API_KEY"):
            monkeypatch.setattr(config, name, "")
        test_client.put("/api/settings", headers=AUTH_HEADERS,
                        json={"llm_model": "g/llm", "embed_model": "g/embed", "api_key": "global-key-9999"})
        slug = create(test_client).json()["slug"]
        ws = test_client.get(f"/api/workspace/{slug}", headers=AUTH_HEADERS).json()
        assert (ws["llm_model"], ws["embed_model"]) == ("g/llm", "g/embed")

    def test_not_configured_is_503_and_creates_nothing(self, test_client, registered, monkeypatch):
        import config
        monkeypatch.setattr(config, "INTAKE_API_KEY", "")
        assert create(test_client).status_code == 503
        assert test_client.get("/api/workspaces", headers=AUTH_HEADERS).json() == []


class TestValidation:
    @pytest.mark.parametrize("overrides", [
        {"email": "not-an-email"},
        {"email": ""},
        {"username": ""},
        {"username": "   "},
        {"name": "x" * 101},          # the uploader refuses names over 100
        {"welcome_text": "x" * 301},  # chat refuses over 300
        {"starter_questions": [{"category": "c" * 41, "questions": ["q"]}]},
        {"starter_questions": [{"category": "ok", "questions": ["q" * 121]}]},
    ])
    def test_rejects_bad_input_before_creating_anything(self, test_client, registered, overrides):
        assert create(test_client, **overrides).status_code == 422
        assert test_client.get("/api/workspaces", headers=AUTH_HEADERS).json() == []
        registered.assert_not_called()

    def test_username_and_email_are_trimmed(self, test_client, registered):
        slug = create(test_client, username="  jdoe  ", email=" jdoe@uri.edu ").json()["slug"]
        owner = test_client.get("/api/owners", headers=AUTH_HEADERS).json()[0]
        assert (owner["slug"], owner["username"], owner["email"]) == (slug, "jdoe", "jdoe@uri.edu")


class TestRollback:
    def test_vector_index_failure_leaves_nothing(self, test_client, registered):
        with patch("main.embedding.create_workspace_index", side_effect=RuntimeError("s3v down")):
            assert create(test_client).status_code == 502
        assert test_client.get("/api/workspaces", headers=AUTH_HEADERS).json() == []
        registered.assert_not_called()

    @pytest.mark.parametrize("result", [
        {"uploader": False, "chat": True},
        {"uploader": True, "chat": False},
    ])
    def test_downstream_failure_rolls_everything_back(self, test_client, registered, result):
        registered.return_value = result
        resp = create(test_client)
        assert resp.status_code == 502
        assert test_client.get("/api/workspaces", headers=AUTH_HEADERS).json() == []
        assert test_client.get("/api/owners", headers=AUTH_HEADERS).json() == []
        registered.unregister.assert_called_once()

    def test_downstream_exception_rolls_back(self, test_client, registered):
        registered.side_effect = RuntimeError("boom")
        assert create(test_client).status_code == 502
        assert test_client.get("/api/workspaces", headers=AUTH_HEADERS).json() == []


class TestScopedAccess:
    def test_get_returns_links_for_intake_bots(self, test_client, registered):
        created = create(test_client).json()
        again = test_client.get(f"/api/intake/chatbot/{created['slug']}")
        assert again.status_code == 200 and again.json() == created

    def test_other_workspaces_are_invisible(self, test_client, workspace):
        # `workspace` was made through the admin API, so it has no owner record
        slug = workspace["slug"]
        assert test_client.get(f"/api/intake/chatbot/{slug}").status_code == 404
        resp = test_client.post(f"/api/intake/chatbot/{slug}/test", json={"question": "hi"})
        assert resp.status_code == 404

    def test_unknown_slug_is_404_and_slashes_never_reach_storage(self, test_client):
        import db
        assert test_client.get("/api/intake/chatbot/nope").status_code == 404
        # An encoded slash never matches the route (the SPA catch-all answers), and the
        # storage layer refuses such slugs anyway.
        assert db.get_owner("a/b") is None and db.get_owner("") is None

    def test_test_route_answers_with_cited_sources(self, test_client, registered):
        slug = create(test_client).json()["slug"]
        fake = {"answer": "Jan 15 [1]", "sources": {"documents": [
            {"title": "deadlines.md", "cited": True}, {"title": "other.md", "cited": False}]}}
        with patch("main.intake.query.query_workspace", return_value=fake) as q:
            resp = test_client.post(f"/api/intake/chatbot/{slug}/test", json={"question": "deadlines?"})
        assert resp.json() == {"answer": "Jan 15 [1]", "cited_sources": ["deadlines.md"]}
        assert q.call_args.args[0]["slug"] == slug

    def test_test_route_does_not_write_query_logs(self, test_client, registered):
        slug = create(test_client).json()["slug"]
        with patch("main.intake.query.query_workspace", return_value={"answer": "a", "sources": {}}):
            test_client.post(f"/api/intake/chatbot/{slug}/test", json={"question": "q"})
        assert test_client.get(f"/api/workspace/{slug}/logs", headers=AUTH_HEADERS).json()["entries"] == []

    def test_deleting_the_workspace_removes_the_owner_record(self, test_client, registered):
        slug = create(test_client).json()["slug"]
        assert test_client.delete(f"/api/workspace/{slug}", headers=AUTH_HEADERS).status_code == 200
        assert test_client.get("/api/owners", headers=AUTH_HEADERS).json() == []
        assert test_client.get(f"/api/intake/chatbot/{slug}").status_code == 404
