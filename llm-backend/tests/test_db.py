"""
test_db.py
==========
Unit tests for db.py — SQLite workspace and settings storage.

All tests operate on the isolated temp database provided by the
`isolate_config` autouse fixture in conftest.py.
"""

import pytest
import db


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class TestSettings:
    def test_get_settings_returns_defaults(self):
        """Fresh database should return all-default settings."""
        s = db.get_settings()
        assert s["llm_model"] == ""
        assert s["temperature"] == pytest.approx(0.7)
        assert s["top_n"] == 5
        assert s["similarity_threshold"] == pytest.approx(0.5)
        assert s["chunk_size"] == 1024
        assert s["chunk_overlap"] == 104
        assert s["max_tokens"] == 1024
        assert s["searxng_enabled"] == 0
        assert s["searxng_num_results"] == 3

    def test_update_single_field(self):
        s = db.update_settings(temperature=0.3)
        assert s["temperature"] == pytest.approx(0.3)
        # Other fields unchanged
        assert s["top_n"] == 5

    def test_update_multiple_fields(self):
        s = db.update_settings(llm_model="openai/gpt-4o", top_n=10, max_tokens=2048)
        assert s["llm_model"] == "openai/gpt-4o"
        assert s["top_n"] == 10
        assert s["max_tokens"] == 2048

    def test_update_settings_ignores_unknown_fields(self):
        """Unknown keys must be silently ignored."""
        s = db.update_settings(nonexistent_field="value", temperature=0.5)
        assert s["temperature"] == pytest.approx(0.5)

    def test_update_settings_all_none_returns_current(self):
        """Calling update_settings with no real values returns current state."""
        db.update_settings(llm_model="openai/gpt-4o")
        s = db.update_settings()   # no kwargs
        assert s["llm_model"] == "openai/gpt-4o"

    def test_update_settings_persists_across_calls(self):
        db.update_settings(system_prompt="Be concise.")
        s = db.get_settings()
        assert s["system_prompt"] == "Be concise."

    def test_update_settings_searxng_fields(self):
        s = db.update_settings(
            searxng_enabled=1,
            searxng_num_results=7,
            searxng_query_suffix="site:uri.edu",
        )
        assert s["searxng_enabled"] == 1
        assert s["searxng_num_results"] == 7
        assert s["searxng_query_suffix"] == "site:uri.edu"

    def test_update_rewrite_fields(self):
        s = db.update_settings(
            rewrite_model="openai/gpt-4o-mini",
            rewrite_prompt="Custom rewrite prompt.",
        )
        assert s["rewrite_model"] == "openai/gpt-4o-mini"
        assert s["rewrite_prompt"] == "Custom rewrite prompt."


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

class TestSlugGeneration:
    def test_slug_is_lowercase(self):
        slug = db._generate_slug("My Workspace")
        assert slug == slug.lower()

    def test_slug_uses_base_from_name(self):
        slug = db._generate_slug("hello world")
        assert slug.startswith("hello-world")

    def test_slug_strips_special_characters(self):
        slug = db._generate_slug("Test! @#$% Workspace")
        # Only alphanumeric, hyphens, and underscores allowed
        import re
        assert re.match(r'^[a-z0-9\-_]+$', slug)

    def test_slug_uniqueness(self):
        """Two calls with the same name should produce different slugs (random suffix)."""
        s1 = db._generate_slug("Same Name")
        s2 = db._generate_slug("Same Name")
        assert s1 != s2

    def test_slug_max_base_length(self):
        long_name = "a" * 100
        slug = db._generate_slug(long_name)
        # Base is capped at 40 chars; full slug includes 17-char suffix (base + "-" + 16)
        assert len(slug) <= 40 + 1 + 16

    def test_slug_empty_name(self):
        """An empty/whitespace name should not crash — suffix alone is returned."""
        slug = db._generate_slug("")
        assert len(slug) > 0


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------

class TestWorkspaceCRUD:
    def test_list_workspaces_empty_initially(self):
        assert db.list_workspaces() == []

    def test_create_workspace_returns_dict(self):
        ws = db.create_workspace(name="Alpha")
        assert isinstance(ws, dict)
        assert ws["name"] == "Alpha"
        assert "slug" in ws

    def test_create_workspace_slug_derived_from_name(self):
        ws = db.create_workspace(name="Beta Workspace")
        assert ws["slug"].startswith("beta-workspace")

    def test_create_workspace_applies_explicit_fields(self):
        ws = db.create_workspace(
            name="Gamma",
            temperature=0.2,
            top_n=3,
            chunk_size=512,
            chunk_overlap=50,
            max_tokens=256,
        )
        assert ws["temperature"] == pytest.approx(0.2)
        assert ws["top_n"] == 3
        assert ws["chunk_size"] == 512
        assert ws["chunk_overlap"] == 50
        assert ws["max_tokens"] == 256

    def test_create_workspace_falls_back_to_global_defaults(self):
        """When global llm_model is set, a workspace with blank llm_model should inherit it."""
        db.update_settings(llm_model="openai/gpt-4o", embed_model="openai/text-embedding-3-small")
        ws = db.create_workspace(name="Inheritor")
        assert ws["llm_model"] == "openai/gpt-4o"
        assert ws["embed_model"] == "openai/text-embedding-3-small"

    def test_create_workspace_explicit_overrides_global(self):
        db.update_settings(llm_model="openai/gpt-4o")
        ws = db.create_workspace(name="Override", llm_model="openai/gpt-3.5-turbo")
        assert ws["llm_model"] == "openai/gpt-3.5-turbo"

    def test_get_workspace_returns_correct_row(self):
        ws = db.create_workspace(name="Delta")
        fetched = db.get_workspace(ws["slug"])
        assert fetched["slug"] == ws["slug"]
        assert fetched["name"] == "Delta"

    def test_get_workspace_missing_returns_none(self):
        assert db.get_workspace("does-not-exist-xyz") is None

    def test_list_workspaces_returns_all(self):
        db.create_workspace(name="One")
        db.create_workspace(name="Two")
        db.create_workspace(name="Three")
        ws_list = db.list_workspaces()
        assert len(ws_list) == 3
        names = {w["name"] for w in ws_list}
        assert names == {"One", "Two", "Three"}

    def test_list_workspaces_sorted_by_name(self):
        db.create_workspace(name="Zulu")
        db.create_workspace(name="Alpha")
        db.create_workspace(name="Mango")
        names = [w["name"] for w in db.list_workspaces()]
        assert names == sorted(names)

    def test_update_workspace_name(self):
        ws = db.create_workspace(name="Old Name")
        updated = db.update_workspace(ws["slug"], name="New Name")
        assert updated["name"] == "New Name"

    def test_update_workspace_mutable_fields(self):
        ws = db.create_workspace(name="Mutable")
        updated = db.update_workspace(
            ws["slug"],
            temperature=0.9,
            top_n=8,
            similarity_threshold=0.7,
            max_tokens=512,
            searxng_enabled=1,
            searxng_num_results=5,
            searxng_query_suffix="site:edu",
            rewrite_model="openai/gpt-4o-mini",
            rewrite_prompt="Be brief.",
        )
        assert updated["temperature"] == pytest.approx(0.9)
        assert updated["top_n"] == 8
        assert updated["similarity_threshold"] == pytest.approx(0.7)
        assert updated["max_tokens"] == 512
        assert updated["searxng_enabled"] == 1
        assert updated["searxng_num_results"] == 5
        assert updated["searxng_query_suffix"] == "site:edu"
        assert updated["rewrite_model"] == "openai/gpt-4o-mini"
        assert updated["rewrite_prompt"] == "Be brief."

    def test_update_workspace_locked_fields_not_accepted(self):
        """chunk_size and embed_model should not be changeable via update_workspace."""
        ws = db.create_workspace(name="Locked", chunk_size=512, embed_model="model-a")
        db.update_workspace(ws["slug"], chunk_size=256, embed_model="model-b")
        refetched = db.get_workspace(ws["slug"])
        # Locked fields must remain at creation values
        assert refetched["chunk_size"] == 512
        assert refetched["embed_model"] == "model-a"

    def test_update_workspace_no_fields_returns_current(self):
        ws = db.create_workspace(name="NoOp")
        result = db.update_workspace(ws["slug"])
        assert result["name"] == "NoOp"

    def test_delete_workspace_returns_true(self):
        ws = db.create_workspace(name="ToDelete")
        assert db.delete_workspace(ws["slug"]) is True

    def test_delete_workspace_removes_row(self):
        ws = db.create_workspace(name="Gone")
        db.delete_workspace(ws["slug"])
        assert db.get_workspace(ws["slug"]) is None

    def test_delete_nonexistent_workspace_returns_false(self):
        assert db.delete_workspace("no-such-slug-xyz") is False

    def test_multiple_workspaces_independent(self):
        """Updating one workspace must not affect another."""
        ws1 = db.create_workspace(name="WS1", temperature=0.3)
        ws2 = db.create_workspace(name="WS2", temperature=0.8)
        db.update_workspace(ws1["slug"], temperature=0.1)
        assert db.get_workspace(ws2["slug"])["temperature"] == pytest.approx(0.8)
