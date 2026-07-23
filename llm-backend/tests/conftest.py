"""
conftest.py
===========
Shared pytest fixtures for the RhodyRAG test suite.

Every test module that needs a database, the FastAPI app, or a temporary
workspace can import these fixtures directly via pytest's dependency injection.

Strategy
--------
- All tests use an **in-memory / temp-file SQLite database** so they never
  touch the production `settings.db`.
- The FastAPI `TestClient` is constructed with `ADMIN_API_KEY` patched to a
  known value so auth tests are deterministic.
- LLM, embedding, and LanceDB calls are **mocked** at the module level so
  tests run offline without any network access or installed model weights.
"""

import os
import tempfile
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Test API key used across the entire suite
# ---------------------------------------------------------------------------
TEST_API_KEY = "test-secret-key"
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


# ---------------------------------------------------------------------------
# Isolated config / DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """
    Point config.APP_API_KEY, config.DB_PATH, config.LANCEDB_DIR, and
    main.LOGS_DIR at temporary locations so tests never affect production data.

    `autouse=True` means this fixture is applied to *every* test automatically.
    """
    db_file = str(tmp_path / "test_settings.db")
    lancedb_dir = str(tmp_path / "lancedb")
    logs_dir = tmp_path / "logs"
    os.makedirs(lancedb_dir, exist_ok=True)
    os.makedirs(str(logs_dir), exist_ok=True)

    monkeypatch.setenv("ADMIN_API_KEY", TEST_API_KEY)

    import config
    monkeypatch.setattr(config, "APP_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "LANCEDB_DIR", lancedb_dir)

    # Re-initialise the database against the temp file
    import db
    import sqlite3

    def _test_connect():
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    monkeypatch.setattr(db, "_connect", _test_connect)
    db.init_db()

    # Redirect the log directory used by main.py to the temp path.
    # main.py uses a module-level Path("logs") constant; patch it after import.
    import main
    from pathlib import Path
    monkeypatch.setattr(main, "LOGS_DIR", logs_dir)
    # Also redirect the docs.json flat file
    monkeypatch.setattr(main, "DOCS_FILE", tmp_path / "docs.json")

    yield


@pytest.fixture()
def test_client(isolate_config):
    """
    A FastAPI TestClient wired to the real application with:
      - auth key = TEST_API_KEY
      - manager calls mocked (no outbound HTTP to file/chat servers)
    """
    import manager
    with (
        patch.object(manager, "on_workspace_created", return_value=None),
        patch.object(manager, "on_workspace_renamed", return_value=None),
        patch.object(manager, "on_workspace_deleted", return_value=None),
    ):
        # Import app *after* patching so the lifespan uses the temp DB
        from fastapi.testclient import TestClient
        import main
        with TestClient(main.app) as client:
            yield client


@pytest.fixture()
def workspace(test_client):
    """
    Create a minimal workspace via the API and return the response JSON.
    Useful as a dependency for tests that need an existing workspace.
    """
    resp = test_client.post(
        "/workspace",
        json={"name": "Test Workspace", "llm_model": "openai/gpt-4o-mini"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()
