"""
store.py
========
Chat UI settings for each workspace, stored as JSON in the RAG backend's S3
state bucket next to the backend's own workspace files:

    workspaces/<slug>/chat.json

Any number of chat servers can share it. Writes use S3 conditional writes
(If-None-Match on create, If-Match on the ETag for updates), the same scheme
as llm-backend/db.py, so concurrent saves can't overwrite each other.
"""

import json
import random
import time
from typing import Callable, Optional

import boto3
from botocore.exceptions import ClientError

from config import AWS_REGION, S3_STATE_BUCKET

_PREFIX = "workspaces/"
_MAX_RETRIES = 10

_client = None


def _s3():
    """Return the shared (thread-safe) boto3 S3 client."""
    global _client
    if _client is None:
        _client = boto3.client("s3", region_name=AWS_REGION or None)
    return _client


def _key(slug: str) -> str:
    return f"{_PREFIX}{slug}/chat.json"


def _get_json(key: str) -> tuple[Optional[dict], Optional[str]]:
    """Return (data, etag), or (None, None) if the object doesn't exist."""
    try:
        obj = _s3().get_object(Bucket=S3_STATE_BUCKET, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None, None
        raise
    return json.loads(obj["Body"].read()), obj["ETag"]


def _put_json(key: str, data, if_match: Optional[str] = None, if_none_match: bool = False) -> None:
    kwargs = {}
    if if_match:
        kwargs["IfMatch"] = if_match
    if if_none_match:
        kwargs["IfNoneMatch"] = "*"
    _s3().put_object(
        Bucket=S3_STATE_BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2).encode(),
        ContentType="application/json",
        **kwargs,
    )


def _is_write_conflict(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] in ("PreconditionFailed", "ConditionalRequestConflict")


def _update_json(key: str, fn: Callable) -> Optional[dict]:
    """Atomically apply fn(current) -> new to an existing object; None if it doesn't exist."""
    for attempt in range(_MAX_RETRIES):
        if attempt:
            time.sleep(random.uniform(0, min(2.0, 0.05 * 2 ** attempt)))
        current, etag = _get_json(key)
        if current is None:
            return None
        new = fn(current)
        try:
            _put_json(key, new, if_match=etag)
            return new
        except ClientError as exc:
            if not _is_write_conflict(exc):
                raise
    raise RuntimeError(f"Too many concurrent updates to s3://{S3_STATE_BUCKET}/{key}")


# --- Workspaces ----------------------------------------------------------------

def list_workspaces() -> list[dict]:
    slugs = []
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_STATE_BUCKET, Prefix=_PREFIX, Delimiter="/"):
        slugs.extend(p["Prefix"][len(_PREFIX):-1] for p in page.get("CommonPrefixes", []))
    return [ws for ws in map(get_workspace, slugs) if ws is not None]


def get_workspace(slug: str) -> Optional[dict]:
    ws, _ = _get_json(_key(slug))
    return ws


def create_workspace(ws: dict) -> bool:
    """Store a new workspace; False if one with this slug already exists."""
    try:
        _put_json(_key(ws["slug"]), ws, if_none_match=True)
        return True
    except ClientError as exc:
        if _is_write_conflict(exc):
            return False
        raise


def update_workspace(slug: str, fields: dict) -> Optional[dict]:
    return _update_json(_key(slug), lambda cur: {**cur, **fields})


def delete_workspace(slug: str) -> bool:
    if get_workspace(slug) is None:
        return False
    _s3().delete_object(Bucket=S3_STATE_BUCKET, Key=_key(slug))
    return True
