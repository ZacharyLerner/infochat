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
    def test_partial_update_persists_and_ignores_unknown_fields(self):
        db.update_settings(temperature=0.3, nonexistent_field="value")
        s = db.get_settings()
        assert s["temperature"] == pytest.approx(0.3)
        assert s["top_n"] == 5          # untouched default
        assert "nonexistent_field" not in s


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------

class TestWorkspaceCRUD:
    def test_slug_is_safe_and_unique(self):
        import re
        s1 = db._generate_slug("Test! @#$% Workspace")
        s2 = db._generate_slug("Test! @#$% Workspace")
        assert re.match(r'^[a-z0-9\-_]+$', s1)
        assert s1.startswith("test-") and s1 != s2

    def test_create_inherits_global_defaults_unless_explicit(self):
        db.update_settings(llm_model="openai/gpt-4o", embed_model="openai/text-embedding-3-small")
        inherited = db.create_workspace(name="Inheritor")
        assert inherited["llm_model"] == "openai/gpt-4o"
        assert inherited["embed_model"] == "openai/text-embedding-3-small"
        explicit = db.create_workspace(name="Override", llm_model="openai/gpt-3.5-turbo")
        assert explicit["llm_model"] == "openai/gpt-3.5-turbo"

    def test_get_list_update(self):
        ws = db.create_workspace(name="Zulu")
        other = db.create_workspace(name="Alpha", temperature=0.8)
        assert db.get_workspace(ws["slug"])["name"] == "Zulu"
        assert db.get_workspace("does-not-exist-xyz") is None
        assert [w["name"] for w in db.list_workspaces()] == ["Alpha", "Zulu"]

        updated = db.update_workspace(ws["slug"], name="New Name", temperature=0.1)
        assert updated["name"] == "New Name"
        assert db.get_workspace(ws["slug"])["temperature"] == pytest.approx(0.1)
        assert db.get_workspace(other["slug"])["temperature"] == pytest.approx(0.8)

    def test_update_workspace_locked_fields_not_accepted(self):
        """embed_model should not be changeable via update_workspace."""
        ws = db.create_workspace(name="Locked", embed_model="model-a")
        db.update_workspace(ws["slug"], embed_model="model-b")
        assert db.get_workspace(ws["slug"])["embed_model"] == "model-a"

    def test_delete_workspace_removes_settings_docs_and_logs(self):
        ws = db.create_workspace(name="Gone")
        db.add_doc(ws["slug"], {"doc_id": "a", "filename": "a.pdf", "chunks_embedded": 1})
        db.append_log(ws["slug"], {"id": "a", "question": "q"})
        assert db.delete_workspace(ws["slug"]) is True
        assert db.get_workspace(ws["slug"]) is None
        assert db.list_docs(ws["slug"]) == []
        assert db.list_logs(ws["slug"])["entries"] == []
        assert db.delete_workspace(ws["slug"]) is False

    def test_delete_workspace_removes_other_services_files(self):
        ws = db.create_workspace(name="Shared")
        keep = db.create_workspace(name="Shared2")
        for slug in (ws["slug"], keep["slug"]):
            db._put_json(db._ws_key(slug, "chat.json"), {"slug": slug})
            db._put_json(db._ws_key(slug, "uploader/originals/k/a.pdf"), {})
        db.delete_workspace(ws["slug"])
        assert db._list_keys(db._ws_key(ws["slug"], "")) == []
        assert len(db._list_keys(db._ws_key(keep["slug"], ""))) == 3


# ---------------------------------------------------------------------------
# Document records
# ---------------------------------------------------------------------------

class TestDocRecords:
    def _doc(self, doc_id, chunks=3):
        return {"doc_id": doc_id, "filename": f"{doc_id}.pdf", "chunks_embedded": chunks}

    def test_add_get_remove(self):
        ws = db.create_workspace(name="Docs")
        assert db.list_docs(ws["slug"]) == []
        db.add_doc(ws["slug"], self._doc("a"))
        db.add_doc(ws["slug"], self._doc("b", chunks=7))
        assert [d["doc_id"] for d in db.list_docs(ws["slug"])] == ["a", "b"]
        assert db.get_doc(ws["slug"], "b")["chunks_embedded"] == 7
        assert db.get_doc(ws["slug"], "missing") is None
        db.remove_doc(ws["slug"], "a")
        assert [d["doc_id"] for d in db.list_docs(ws["slug"])] == ["b"]

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

    def test_gives_up_after_max_retries(self):
        from botocore.exceptions import ClientError
        conflict = ClientError({"Error": {"Code": "PreconditionFailed", "Message": "x"}}, "PutObject")
        ws = db.create_workspace(name="Docs")
        with (
            patch.object(db, "_put_json", side_effect=conflict) as put,
            patch.object(db.time, "sleep"),
        ):
            with pytest.raises(RuntimeError, match="Too many concurrent updates"):
                db.add_doc(ws["slug"], self._doc("a"))
        assert put.call_count == db._MAX_RETRIES


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

    def test_clear_logs(self):
        ws = db.create_workspace(name="Logs")
        db.append_log(ws["slug"], {"id": "a", "question": "q"})
        db.append_chat_turn(ws["slug"], "s1", {"question": "c"})
        db.clear_logs(ws["slug"])
        assert db.list_logs(ws["slug"])["entries"] == []

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

    def test_session_id_cannot_escape_logs_folder(self):
        ws = db.create_workspace(name="Chat")
        db.append_chat_turn(ws["slug"], "../../settings", {"question": "x"})
        assert db.get_settings()["llm_model"] == ""   # global settings untouched
        assert len(db.list_logs(ws["slug"])["entries"]) == 1
