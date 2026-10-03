"""
test_db.py
==========
Unit tests for db.py — workspace, settings and document storage in S3 (faked in memory).

All tests operate on the isolated temp database provided by the
`isolate_config` autouse fixture in conftest.py.
"""

from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# Document records
# ---------------------------------------------------------------------------

class TestDocRecords:
    def _doc(self, doc_id, chunks=3):
        return {"doc_id": doc_id, "filename": f"{doc_id}.pdf", "chunks_embedded": chunks}

    def test_list_docs_empty_initially(self):
        ws = db.create_workspace(name="Docs")
        assert db.list_docs(ws["slug"]) == []

    def test_add_and_get_doc(self):
        ws = db.create_workspace(name="Docs")
        db.add_doc(ws["slug"], self._doc("a"))
        db.add_doc(ws["slug"], self._doc("b", chunks=7))
        assert [d["doc_id"] for d in db.list_docs(ws["slug"])] == ["a", "b"]
        assert db.get_doc(ws["slug"], "b")["chunks_embedded"] == 7
        assert db.get_doc(ws["slug"], "missing") is None

    def test_remove_doc(self):
        ws = db.create_workspace(name="Docs")
        db.add_doc(ws["slug"], self._doc("a"))
        db.add_doc(ws["slug"], self._doc("b"))
        db.remove_doc(ws["slug"], "a")
        assert [d["doc_id"] for d in db.list_docs(ws["slug"])] == ["b"]

    def test_docs_are_per_workspace(self):
        ws1 = db.create_workspace(name="One")
        ws2 = db.create_workspace(name="Two")
        db.add_doc(ws1["slug"], self._doc("a"))
        assert db.list_docs(ws2["slug"]) == []

    def test_delete_workspace_removes_docs(self):
        ws = db.create_workspace(name="Docs")
        db.add_doc(ws["slug"], self._doc("a"))
        db.delete_workspace(ws["slug"])
        assert db.list_docs(ws["slug"]) == []

    def test_concurrent_write_is_retried_not_lost(self):
        """If another writer changes docs.json between our read and write,
        the conditional write fails and the update is retried on fresh data."""
        ws = db.create_workspace(name="Docs")
        db.add_doc(ws["slug"], self._doc("a"))

        real_put = db._put_json
        raced = {"done": False}

        def racing_put(key, data, **kwargs):
            if not raced["done"]:
                raced["done"] = True
                # Another request adds a record after we read docs.json
                real_put(key, [self._doc("a"), self._doc("other-writer")])
            return real_put(key, data, **kwargs)

        with patch.object(db, "_put_json", side_effect=racing_put):
            db.add_doc(ws["slug"], self._doc("b"))

        assert [d["doc_id"] for d in db.list_docs(ws["slug"])] == ["a", "other-writer", "b"]


# ---------------------------------------------------------------------------
# Query logs
# ---------------------------------------------------------------------------

class _Clock:
    """Patch db's clock so log entries get chosen timestamps."""
    def __init__(self, when):
        self.when = when

    def __enter__(self):
        from datetime import datetime as real_dt
        clock = self

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return clock.when

        self._patch = patch.object(db, "datetime", FakeDT)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def _utc(*args):
    from datetime import datetime, timezone
    return datetime(*args, tzinfo=timezone.utc)


def _questions(page):
    return [e.get("question") or e["turns"][0]["question"] for e in page["entries"]]


class TestLogs:
    def test_empty(self):
        ws = db.create_workspace(name="Logs")
        assert db.list_logs(ws["slug"]) == {"entries": [], "next": None}

    def test_logs_returned_newest_first(self):
        ws = db.create_workspace(name="Logs")
        for i in range(3):
            db.append_log(ws["slug"], {"id": f"e{i}", "question": f"q{i}"})
        assert _questions(db.list_logs(ws["slug"])) == ["q2", "q1", "q0"]

    def test_paging_across_days(self):
        ws = db.create_workspace(name="Logs")
        for day in (1, 2, 3):
            for hour in (9, 10):
                with _Clock(_utc(2026, 1, day, hour)):
                    db.append_log(ws["slug"], {"id": f"d{day}h{hour}", "question": f"{day}-{hour}"})
        p1 = db.list_logs(ws["slug"], limit=4)
        assert _questions(p1) == ["3-10", "3-9", "2-10", "2-9"]
        p2 = db.list_logs(ws["slug"], limit=4, before=p1["next"])
        assert _questions(p2) == ["1-10", "1-9"]
        assert p2["next"] is None

    def test_exact_page_has_no_next(self):
        ws = db.create_workspace(name="Logs")
        for i in range(2):
            db.append_log(ws["slug"], {"id": f"e{i}", "question": f"q{i}"})
        assert db.list_logs(ws["slug"], limit=2)["next"] is None

    def test_date_filter(self):
        ws = db.create_workspace(name="Logs")
        for day, hour in ((1, 23), (2, 1), (2, 23), (3, 1)):
            with _Clock(_utc(2026, 1, day, hour)):
                db.append_log(ws["slug"], {"id": f"d{day}h{hour}", "question": f"{day}-{hour}"})
        # A local day in UTC-5 spans 05:00 UTC to 05:00 UTC the next day
        page = db.list_logs(ws["slug"], start=_utc(2026, 1, 2, 5), end=_utc(2026, 1, 3, 5))
        assert _questions(page) == ["3-1", "2-23"]

    def test_logs_are_per_workspace(self):
        ws1 = db.create_workspace(name="One")
        ws2 = db.create_workspace(name="Two")
        db.append_log(ws1["slug"], {"id": "a", "question": "q"})
        assert db.list_logs(ws2["slug"])["entries"] == []

    def test_clear_logs(self):
        ws = db.create_workspace(name="Logs")
        db.append_log(ws["slug"], {"id": "a", "question": "q"})
        db.append_chat_turn(ws["slug"], "s1", {"question": "c"})
        db.clear_logs(ws["slug"])
        assert db.list_logs(ws["slug"])["entries"] == []

    def test_delete_workspace_removes_logs(self):
        ws = db.create_workspace(name="Logs")
        db.append_log(ws["slug"], {"id": "a", "question": "q"})
        db.delete_workspace(ws["slug"])
        assert db.list_logs(ws["slug"])["entries"] == []

    def test_logs_do_not_appear_as_workspaces(self):
        ws = db.create_workspace(name="Logs")
        db.append_log(ws["slug"], {"id": "a", "question": "q"})
        assert [w["slug"] for w in db.list_workspaces()] == [ws["slug"]]

    def test_chat_turns_grouped_into_one_entry(self):
        ws = db.create_workspace(name="Chat")
        sid = "8f14e45f-ceea-467f-a0e6-1b0e2b6e3a11"
        db.append_chat_turn(ws["slug"], sid, {"timestamp": "2026-01-01T00:00:00Z", "question": "q1", "answer": "a1"})
        db.append_chat_turn(ws["slug"], sid, {"timestamp": "2026-01-01T00:01:00Z", "question": "q2", "answer": "a2"})
        entries = db.list_logs(ws["slug"])["entries"]
        assert len(entries) == 1
        assert entries[0]["session_id"] == sid
        assert [t["question"] for t in entries[0]["turns"]] == ["q1", "q2"]
        assert entries[0]["timestamp"] == "2026-01-01T00:00:00Z"
        assert entries[0]["updated_at"] == "2026-01-01T00:01:00Z"

    def test_conversation_continuing_next_day_stays_one_entry(self):
        ws = db.create_workspace(name="Chat")
        with _Clock(_utc(2026, 1, 1, 23, 59)):
            db.append_chat_turn(ws["slug"], "s1", {"question": "late"})
        with _Clock(_utc(2026, 1, 2, 0, 5)):
            db.append_chat_turn(ws["slug"], "s1", {"question": "after midnight"})
        entries = db.list_logs(ws["slug"])["entries"]
        assert len(entries) == 1
        assert [t["question"] for t in entries[0]["turns"]] == ["late", "after midnight"]

    def test_session_id_cannot_escape_logs_folder(self):
        ws = db.create_workspace(name="Chat")
        db.append_chat_turn(ws["slug"], "../../settings", {"question": "x"})
        assert db.get_settings()["llm_model"] == ""   # global settings untouched
        assert len(db.list_logs(ws["slug"])["entries"]) == 1
