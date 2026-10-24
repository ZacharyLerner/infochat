"""conftest.py — Shared pytest fixtures for the Universal Chat test suite."""
import io
import os
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Patch environment variables BEFORE any app module is imported so that
# config.py sees them.  This avoids "None" values for required URLs.
# ---------------------------------------------------------------------------
os.environ.setdefault("RAG_API_URL", "http://rag-api.test")
os.environ.setdefault("RAG_API_KEY", "test-rag-key")
os.environ.setdefault("LLM_GW_URL", "http://llm-gw.test/v1/chat/completions")
os.environ.setdefault("LLM_GW_KEY", "test-gw-key")

import store  # noqa: E402  (import after env patch)
from main import app  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory stand-in for the S3 client used by store.py, including the
# If-Match / If-None-Match conditional writes (same as llm-backend's tests).
# ---------------------------------------------------------------------------

class FakeS3:
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


@pytest.fixture()
def fake_s3(monkeypatch):
    """A fresh in-memory bucket for each test, so tests never touch real S3."""
    s3 = FakeS3()
    monkeypatch.setattr(store, "_s3", lambda: s3)
    return s3


@pytest.fixture()
def client(fake_s3):
    """FastAPI TestClient backed by the in-memory bucket."""
    with TestClient(app) as c:
        yield c
