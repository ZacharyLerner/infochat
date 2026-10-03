"""
db.py
=====
Workspace settings, global settings and document records, stored as JSON
objects in S3 (config.S3_STATE_BUCKET) so the backend container is stateless:

    settings.json                     global settings
    workspaces/<slug>/settings.json   workspace settings
    workspaces/<slug>/docs.json       embedded-document records
    workspaces/<slug>/logs/<day>/...  query and chat logs (see "Query logs")

Read-modify-write updates use S3 conditional writes (If-Match on the ETag,
retried on conflict), so concurrent requests and multiple containers never
overwrite each other's changes.
"""

import json
import random
import re
import string
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import boto3
from botocore.exceptions import ClientError

import config

SETTINGS_KEY = "settings.json"
_WS_PREFIX = "workspaces/"

# Defaults for every settings field. Stored objects are merged over these, so
# fields added later get a default without any migration.
SETTINGS_DEFAULTS = {
    "llm_model": "",
    "api_key": "",
    "temperature": 0.7,
    "system_prompt": "",
    "top_n": 5,
    "similarity_threshold": 0.5,
    "chunk_size": 1024,
    "chunk_overlap": 104,
    "embed_model": "",
    "embed_api_key": "",
    "max_tokens": 1024,
    "searxng_enabled": 0,
    "searxng_num_results": 3,
    "searxng_query_suffix": "site:uri.edu",
    "rewrite_model": "",
    "rewrite_prompt": "",
}
WORKSPACE_DEFAULTS = {**SETTINGS_DEFAULTS, "searxng_query_suffix": ""}

_SETTINGS_FIELDS = set(SETTINGS_DEFAULTS)
# chunk_size, chunk_overlap and embed_model are locked after creation
_WORKSPACE_MUTABLE_FIELDS = {"name"} | _SETTINGS_FIELDS - {"chunk_size", "chunk_overlap", "embed_model"}

_MAX_RETRIES = 5

_client = None


def _s3():
    """Return the shared (thread-safe) boto3 S3 client."""
    global _client
    if _client is None:
        _client = boto3.client("s3", region_name=config.AWS_REGION or None)
    return _client


# --- JSON object helpers -----------------------------------------------------

def _ws_key(slug: str, name: str) -> str:
    return f"{_WS_PREFIX}{slug}/{name}"


def _get_json(key: str) -> tuple[Optional[object], Optional[str]]:
    """Return (data, etag), or (None, None) if the object doesn't exist."""
    try:
        obj = _s3().get_object(Bucket=config.S3_STATE_BUCKET, Key=key)
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
        Bucket=config.S3_STATE_BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2).encode(),
        ContentType="application/json",
        **kwargs,
    )


def _delete(key: str) -> None:
    _s3().delete_object(Bucket=config.S3_STATE_BUCKET, Key=key)


def _is_write_conflict(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] in ("PreconditionFailed", "ConditionalRequestConflict")


def _update_json(key: str, fn: Callable, default=None, create: bool = True):
    """Atomically apply fn(current) -> new to a JSON object and return new.

    If the object doesn't exist, `default` is used as current when `create`
    is True; otherwise None is returned and nothing is written.
    """
    for _ in range(_MAX_RETRIES):
        current, etag = _get_json(key)
        if current is None:
            if not create:
                return None
            current = default
        new = fn(current)
        try:
            _put_json(key, new, if_match=etag, if_none_match=etag is None)
            return new
        except ClientError as exc:
            if not _is_write_conflict(exc):
                raise
    raise RuntimeError(f"Too many concurrent updates to s3://{config.S3_STATE_BUCKET}/{key}")


def init_db():
    """Nothing to initialise — objects are created on first write."""


# --- Settings ----------------------------------------------------------------

def get_settings() -> dict:
    stored, _ = _get_json(SETTINGS_KEY)
    return {**SETTINGS_DEFAULTS, **(stored or {})}


def update_settings(**fields) -> dict:
    """Update any subset of global settings fields."""
    updates = {k: v for k, v in fields.items() if k in _SETTINGS_FIELDS and v is not None}
    if not updates:
        return get_settings()
    stored = _update_json(SETTINGS_KEY, lambda cur: {**cur, **updates}, default={})
    return {**SETTINGS_DEFAULTS, **stored}


# --- Workspaces --------------------------------------------------------------

def _generate_slug(name: str) -> str:
    """Generate a URL-safe slug from a workspace name."""
    base = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in name.lower())
    base = base.strip("-")[:40]
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
    return f"{base}-{suffix}"


def _list_slugs() -> list[str]:
    slugs = []
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.S3_STATE_BUCKET, Prefix=_WS_PREFIX, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            slugs.append(p["Prefix"][len(_WS_PREFIX):].rstrip("/"))
    return slugs


def list_workspaces() -> list:
    slugs = _list_slugs()
    if not slugs:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(slugs))) as pool:
        workspaces = [ws for ws in pool.map(get_workspace, slugs) if ws is not None]
    return sorted(workspaces, key=lambda ws: ws["name"])


def create_workspace(
    name: str,
    llm_model: str = "",
    api_key: str = "",
    temperature: float = 0.7,
    system_prompt: str = "",
    top_n: int = 5,
    similarity_threshold: float = 0.5,
    chunk_size: int = 1024,
    chunk_overlap: int = 104,
    embed_model: str = "",
    embed_api_key: str = "",
    max_tokens: int = 1024,
    searxng_enabled: int = 0,
    searxng_num_results: int = 3,
    searxng_query_suffix: str = "",
    rewrite_model: str = "",
    rewrite_prompt: str = "",
) -> dict:
    """Create a new workspace. Falls back to global defaults for blank fields."""
    defaults = get_settings()
    ws = {
        "slug": _generate_slug(name),
        "name": name,
        "llm_model": llm_model or defaults["llm_model"],
        "api_key": api_key or defaults["api_key"],
        "temperature": temperature if temperature is not None else defaults["temperature"],
        "system_prompt": system_prompt or defaults["system_prompt"],
        "top_n": top_n if top_n is not None else defaults["top_n"],
        "similarity_threshold": similarity_threshold if similarity_threshold is not None else defaults["similarity_threshold"],
        "chunk_size": chunk_size if chunk_size is not None else defaults["chunk_size"],
        "chunk_overlap": chunk_overlap if chunk_overlap is not None else defaults["chunk_overlap"],
        "embed_model": embed_model or defaults["embed_model"],
        "embed_api_key": embed_api_key if embed_api_key is not None else defaults["embed_api_key"],
        "max_tokens": max_tokens if max_tokens is not None else defaults["max_tokens"],
        "searxng_enabled": searxng_enabled if searxng_enabled is not None else defaults["searxng_enabled"],
        "searxng_num_results": searxng_num_results if searxng_num_results is not None else defaults["searxng_num_results"],
        "searxng_query_suffix": searxng_query_suffix if searxng_query_suffix is not None else defaults["searxng_query_suffix"],
        "rewrite_model": rewrite_model or defaults["rewrite_model"],
        "rewrite_prompt": rewrite_prompt or defaults["rewrite_prompt"],
    }
    _put_json(_ws_key(ws["slug"], "settings.json"), ws, if_none_match=True)
    return ws


def get_workspace(slug: str) -> Optional[dict]:
    """Return a workspace by slug, or None if it doesn't exist."""
    if not slug or "/" in slug:
        return None
    stored, _ = _get_json(_ws_key(slug, "settings.json"))
    if stored is None:
        return None
    return {**WORKSPACE_DEFAULTS, **stored, "slug": slug}


def update_workspace(slug: str, **fields) -> Optional[dict]:
    """Update any subset of mutable settings fields. Returns the updated workspace."""
    updates = {k: v for k, v in fields.items() if k in _WORKSPACE_MUTABLE_FIELDS and v is not None}
    if not updates:
        return get_workspace(slug)
    stored = _update_json(_ws_key(slug, "settings.json"), lambda cur: {**cur, **updates}, create=False)
    if stored is None:
        return None
    return {**WORKSPACE_DEFAULTS, **stored, "slug": slug}


def delete_workspace(slug: str) -> bool:
    """Delete a workspace's settings, document records and logs. Returns True if it existed."""
    existed = get_workspace(slug) is not None
    clear_logs(slug)
    _delete(_ws_key(slug, "docs.json"))
    _delete(_ws_key(slug, "settings.json"))
    return existed


# --- Document records --------------------------------------------------------

def list_docs(slug: str) -> list:
    docs, _ = _get_json(_ws_key(slug, "docs.json"))
    return docs or []


def get_doc(slug: str, doc_id: str) -> Optional[dict]:
    return next((d for d in list_docs(slug) if d.get("doc_id") == doc_id), None)


def add_doc(slug: str, record: dict) -> None:
    _update_json(_ws_key(slug, "docs.json"), lambda docs: docs + [record], default=[])


def remove_doc(slug: str, doc_id: str) -> None:
    _update_json(
        _ws_key(slug, "docs.json"),
        lambda docs: [d for d in docs if d.get("doc_id") != doc_id],
        create=False,
    )


def clear_docs(slug: str) -> None:
    _delete(_ws_key(slug, "docs.json"))


# --- Query logs --------------------------------------------------------------
# Logs are grouped into one folder per UTC day, and every file name starts
# with the entry's start time, so the newest entries can be found and paged
# by listing keys alone — only the entries actually shown are read:
#
#   logs/2026-10-03/T195534996461Z-<id>.json           single-shot query
#   logs/2026-10-03/T201500000000Z-session-<id>.json   chat conversation (all turns)
#   logs/sessions/<id>.json                            pointer: session id -> its file
#
# A conversation stays in the folder of the day it started.

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _logs_prefix(slug: str) -> str:
    return _ws_key(slug, "logs/")


def _log_name(ts: datetime, suffix: str) -> str:
    """Key relative to the logs folder, e.g. '2026-10-03/T195534996461Z-<suffix>.json'."""
    return f"{ts:%Y-%m-%d}/T{ts:%H%M%S%f}Z-{suffix}.json"


def _log_time(name: str) -> Optional[datetime]:
    """Start time encoded in a relative log key, or None if it isn't a log entry."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})/T(\d{12})Z-", name)
    if not m:
        return None
    return datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S%f").replace(tzinfo=timezone.utc)


def _list_keys(prefix: str) -> list[str]:
    keys = []
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.S3_STATE_BUCKET, Prefix=prefix):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    return keys


def append_log(slug: str, entry: dict) -> None:
    entry_id = re.sub(r"[^A-Za-z0-9-]", "", str(entry.get("id") or "")) or uuid.uuid4().hex
    name = _log_name(datetime.now(timezone.utc), entry_id)
    _put_json(_logs_prefix(slug) + name, entry, if_none_match=True)


def append_chat_turn(slug: str, session_id: str, turn: dict) -> None:
    """Append one question/answer turn to the conversation's log entry."""
    safe_id = re.sub(r"[^A-Za-z0-9-]", "", session_id)[:64]
    now = datetime.now(timezone.utc)
    ts = turn.get("timestamp") or now.isoformat()

    # Find (or create) the conversation's file via its pointer
    pointer_key = _logs_prefix(slug) + f"sessions/{safe_id}.json"
    pointer, _ = _get_json(pointer_key)
    if pointer is None:
        pointer = {"key": _log_name(now, f"session-{safe_id}")}
        try:
            _put_json(pointer_key, pointer, if_none_match=True)
        except ClientError as exc:
            if not _is_write_conflict(exc):
                raise
            pointer, _ = _get_json(pointer_key)   # another request created it first

    new_entry = {
        "id": safe_id,
        "chat_session": True,
        "session_id": safe_id,
        "timestamp": ts,          # when the conversation started
        "updated_at": ts,
        "turns": [],
    }
    _update_json(
        _logs_prefix(slug) + pointer["key"],
        lambda cur: {**cur, "updated_at": ts, "turns": cur["turns"] + [turn]},
        default=new_entry,
    )


def list_logs(
    slug: str,
    limit: int = 20,
    before: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> dict:
    """Return one page of log entries, newest first.

    `before` is the `next` cursor from the previous page. `start`/`end`
    (timezone-aware) keep only entries that started in [start, end).
    Returns {"entries": [...], "next": cursor or None}.
    """
    prefix = _logs_prefix(slug)
    paginator = _s3().get_paginator("list_objects_v2")
    days = sorted(
        (p["Prefix"][len(prefix):].rstrip("/")
         for page in paginator.paginate(Bucket=config.S3_STATE_BUCKET, Prefix=prefix, Delimiter="/")
         for p in page.get("CommonPrefixes", [])),
        reverse=True,
    )
    days = [d for d in days if _DAY_RE.match(d)]
    if start:
        days = [d for d in days if d >= f"{start.astimezone(timezone.utc):%Y-%m-%d}"]
    if end:
        days = [d for d in days if d <= f"{end.astimezone(timezone.utc):%Y-%m-%d}"]
    if before:
        days = [d for d in days if d <= before[:10]]

    # Collect one more key than needed to know whether another page exists
    selected = []
    for day in days:
        names = sorted((k[len(prefix):] for k in _list_keys(f"{prefix}{day}/")), reverse=True)
        for name in names:
            t = _log_time(name)
            if t is None or (before and name >= before):
                continue
            if (start and t < start) or (end and t >= end):
                continue
            selected.append(name)
        if len(selected) > limit:
            break

    page, more = selected[:limit], len(selected) > limit
    if not page:
        return {"entries": [], "next": None}
    with ThreadPoolExecutor(max_workers=min(16, len(page))) as pool:
        entries = list(pool.map(lambda n: _get_json(prefix + n)[0], page))
    return {
        "entries": [e for e in entries if e is not None],
        "next": page[-1] if more else None,
    }


def clear_logs(slug: str) -> None:
    keys = _list_keys(_logs_prefix(slug))
    for i in range(0, len(keys), 1000):
        _s3().delete_objects(
            Bucket=config.S3_STATE_BUCKET,
            Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True},
        )
