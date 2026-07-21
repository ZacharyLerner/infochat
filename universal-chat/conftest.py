"""conftest.py — Shared pytest fixtures for the Universal Chat test suite."""
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Patch environment variables BEFORE any app module is imported so that
# config.py sees them.  This avoids "None" values for required URLs.
# ---------------------------------------------------------------------------
os.environ.setdefault("RAG_API_URL", "http://rag-api.test")
os.environ.setdefault("RAG_API_KEY", "test-rag-key")
os.environ.setdefault("LLM_GW_URL", "http://llm-gw.test/v1/chat/completions")
os.environ.setdefault("LLM_GW_KEY", "test-gw-key")

from db import Base, get_db  # noqa: E402  (import after env patch)
from main import app  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory SQLite DB for tests — isolates each test run from the real DB.
#
# SQLite in-memory databases are per-connection, so we pin the engine to a
# single underlying connection using the `connect` event and
# `creator` approach.  This ensures that create_all, the session, and the
# FastAPI dependency override all share the same in-memory database.
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_engine():
    """Create a fresh in-memory SQLite engine with all tables.

    Uses a single persistent connection so that all sessions see the same
    in-memory database (SQLite in-memory DBs are not shared across
    connections by default).
    """
    import sqlite3

    # Create one underlying sqlite3 connection for this test.
    raw_conn = sqlite3.connect(":memory:", check_same_thread=False)

    def creator():
        return raw_conn

    _engine = create_engine("sqlite+pysqlite://", creator=creator)
    Base.metadata.create_all(bind=_engine)
    yield _engine
    Base.metadata.drop_all(bind=_engine)
    _engine.dispose()
    raw_conn.close()


@pytest.fixture()
def db_session(db_engine):
    """Provide a clean SQLAlchemy session for the duration of one test."""
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    with TestingSession() as session:
        yield session


@pytest.fixture()
def client(db_engine):
    """FastAPI TestClient backed by the in-memory DB."""
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)

    def override_get_db():
        with TestingSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
