"""
conftest.py
===========
Shared pytest fixtures for the Infochat test suite.

Every test module that needs a database, the FastAPI app, or a temporary
workspace can import these fixtures directly via pytest's dependency injection.

Strategy
--------
- All tests use an **in-memory fake S3 bucket** for settings and document
  records, so they never touch real AWS state.
- The FastAPI `TestClient` is constructed with `ADMIN_API_KEY` patched to a
  known value so auth tests are deterministic.
- LLM, embedding, and S3 Vectors calls are **mocked** at the module level so
  tests run offline without any network access or installed model weights.
"""

import io
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
# In-memory stand-in for the S3 client used by db.py
# ---------------------------------------------------------------------------

class FakeS3:
    """Implements the subset of the boto3 S3 client that db.py uses,
    including If-Match / If-None-Match conditional writes."""

    def __init__(self):
        self.objects = {}   # key -> (bytes, etag)
        self._version = 0

    @staticmethod
    def _error(code):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": code, "Message": code}}, "op")

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._error("NoSuchKey")
        body, etag = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": etag}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):
        current = self.objects.get(Key)
        if IfNoneMatch == "*" and current is not None:
            raise self._error("PreconditionFailed")
        if IfMatch is not None and (current is None or current[1] != IfMatch):
            raise self._error("PreconditionFailed")
        self._version += 1
        self.objects[Key] = (Body, f'"etag-{self._version}"')
        return {"ETag": self.objects[Key][1]}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        return {}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)
        return {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        fake = self

        class _Paginator:
            def paginate(self, Bucket, Prefix="", Delimiter=None):
                if Delimiter is None:
                    keys = sorted(k for k in fake.objects if k.startswith(Prefix))
                    yield {"Contents": [{"Key": k} for k in keys]}
                    return
                prefixes = sorted({
                    Prefix + k[len(Prefix):].split(Delimiter, 1)[0] + Delimiter
                    for k in fake.objects
                    if k.startswith(Prefix) and Delimiter in k[len(Prefix):]
                })
                yield {"CommonPrefixes": [{"Prefix": p} for p in prefixes]}

        return _Paginator()


# ---------------------------------------------------------------------------
# Isolated config / DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """
    Point config.APP_API_KEY and the db.py S3 client at test-only stand-ins
    so tests never affect production data.

    `autouse=True` means this fixture is applied to *every* test automatically.
    """

    monkeypatch.setenv("ADMIN_API_KEY", TEST_API_KEY)

    import config
    monkeypatch.setattr(config, "APP_API_KEY", TEST_API_KEY)

    # Settings and document records go to an in-memory fake bucket
    import db
    fake_s3 = FakeS3()
    monkeypatch.setattr(db, "_s3", lambda: fake_s3)


    yield


@pytest.fixture()
def test_client(isolate_config):
    """
    A FastAPI TestClient wired to the real application with:
      - auth key = TEST_API_KEY
      - manager calls mocked (no outbound HTTP to file/chat servers)
      - S3 Vectors index lifecycle mocked (no AWS calls)
    """
    import embedding
    import manager
    with (
        patch.object(embedding, "create_workspace_index", return_value=None),
        patch.object(embedding, "ensure_workspace_index", return_value=None),
        patch.object(embedding, "delete_workspace_index", return_value=None),
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
        "/api/workspace",
        json={"name": "Test Workspace", "llm_model": "openai/gpt-4o-mini"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()
