"""
store.py
========
Uploader state, stored in the RAG backend's S3 state bucket under each
workspace's prefix so any number of uploader servers can share it:

    workspaces/<slug>/uploader/workspace.json          {id, name, owners, created_at}
    workspaces/<slug>/uploader/files.json              [file record, ...]
    workspaces/<slug>/uploader/scrape_jobs.json        [scrape job, ...]
    workspaces/<slug>/uploader/converted/<key>/<name>  each document's DoclingDocument JSON (written by
                                                       docling-pipeline), or a text upload as-is
    workspaces/<slug>/uploader/originals/<key>/<name>  an upload waiting to be converted (deleted afterwards)

Records are plain dicts with ISO-8601 datetimes. Read-modify-write updates use
S3 conditional writes (If-Match on the ETag, If-None-Match on create), the same
scheme as llm-backend/db.py, so concurrent writers can't overwrite each other.

A file record's `id` is the backend doc_id (it changes when a document is
re-embedded, and is None while the document is still converting); `key` is a
stable id that also names its folders under originals/ and converted/.
"""

import json
import random
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

import boto3
from botocore.exceptions import ClientError

from config import AWS_REGION, S3_STATE_BUCKET

_PREFIX = "workspaces/"
_MAX_RETRIES = 10

# shortcut: a job whose run crashed stays "running" until this lease expires; raise it if
# a single job run can legitimately take longer.
RUN_LEASE = timedelta(hours=6)

_client = None


def _s3():
    """Return the shared (thread-safe) boto3 S3 client."""
    global _client
    if _client is None:
        _client = boto3.client("s3", region_name=AWS_REGION or None)
    return _client


def _key(slug: str, name: str) -> str:
    return f"{_PREFIX}{slug}/uploader/{name}"


def _get_json(key: str) -> tuple[Optional[object], Optional[str]]:
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


def _update_json(key: str, fn: Callable, default=None):
    """Atomically apply fn(current) -> new to a JSON object and return new.

    A missing object starts from `default`, or nothing is written and None is
    returned when `default` is None.
    """
    for attempt in range(_MAX_RETRIES):
        if attempt:
            time.sleep(random.uniform(0, min(2.0, 0.05 * 2 ** attempt)))
        current, etag = _get_json(key)
        if current is None:
            if default is None:
                return None
            current = default
        new = fn(current)
        try:
            _put_json(key, new, if_match=etag, if_none_match=etag is None)
            return new
        except ClientError as exc:
            if not _is_write_conflict(exc):
                raise
    raise RuntimeError(f"Too many concurrent updates to s3://{S3_STATE_BUCKET}/{key}")


def _list_keys(prefix: str) -> list[str]:
    keys = []
    for page in _s3().get_paginator("list_objects_v2").paginate(Bucket=S3_STATE_BUCKET, Prefix=prefix):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    return keys


def _delete_keys(keys: list[str]) -> None:
    for i in range(0, len(keys), 1000):
        _s3().delete_objects(
            Bucket=S3_STATE_BUCKET,
            Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True},
        )


def now() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).isoformat()


# --- Workspaces ----------------------------------------------------------------

def list_workspace_slugs() -> list[str]:
    """Every slug under workspaces/ (including ones the uploader holds nothing for)."""
    slugs = []
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_STATE_BUCKET, Prefix=_PREFIX, Delimiter="/"):
        slugs.extend(p["Prefix"][len(_PREFIX):-1] for p in page.get("CommonPrefixes", []))
    return slugs


def get_workspace(slug: str) -> Optional[dict]:
    ws, _ = _get_json(_key(slug, "workspace.json"))
    return ws


def create_workspace(slug: str, name: str, owners: list[str]) -> Optional[dict]:
    """Store a new workspace; None if one with this id already exists."""
    ws = {"id": slug, "name": name, "owners": owners, "created_at": now()}
    try:
        _put_json(_key(slug, "workspace.json"), ws, if_none_match=True)
        return ws
    except ClientError as exc:
        if _is_write_conflict(exc):
            return None
        raise


def update_workspace(slug: str, **fields) -> Optional[dict]:
    return _update_json(_key(slug, "workspace.json"), lambda cur: {**cur, **fields})


def delete_workspace(slug: str) -> None:
    """Remove everything the uploader stores for the workspace, documents included."""
    _delete_keys(_list_keys(_key(slug, "")))


# --- File records ----------------------------------------------------------------

def list_files(slug: str) -> list[dict]:
    files, _ = _get_json(_key(slug, "files.json"))
    return files or []


def update_files(slug: str, fn: Callable[[list], list]) -> list[dict]:
    """Atomically replace the workspace's file records with fn(records)."""
    return _update_json(_key(slug, "files.json"), fn, default=[])


def add_files(slug: str, records: list[dict]) -> None:
    if records:
        update_files(slug, lambda files: files + records)


def update_file(slug: str, key: str, fn: Callable[[dict], dict]) -> Optional[dict]:
    """Atomically replace the record with this stable key by fn(record); None if it's gone."""
    found = []

    def apply(files):
        out = []
        for f in files:
            if f["key"] == key:
                f = fn(f)
                found.append(f)
            out.append(f)
        return out

    update_files(slug, apply)
    return found[0] if found else None


def remove_files(slug: str, ids) -> list[dict]:
    """Drop the records with these doc ids (or stable keys, for documents still converting)
    and their stored objects; return the removed records."""
    ids = set(ids) - {None}  # None is the id of every record still converting
    removed = []

    def drop(files):
        removed[:] = [f for f in files if f["id"] in ids or f["key"] in ids]
        return [f for f in files if f["id"] not in ids and f["key"] not in ids]

    update_files(slug, drop)
    _delete_keys([k for f in removed for k in (f.get("original"), f.get("converted")) if k])
    return removed


def original_key(slug: str, key: str, filename: str) -> str:
    return _key(slug, f"originals/{key}/{filename}")


def converted_key(slug: str, key: str, filename: str) -> str:
    return _key(slug, f"converted/{key}/{filename}")


def put_object(s3_key: str, content: bytes) -> None:
    _s3().put_object(Bucket=S3_STATE_BUCKET, Key=s3_key, Body=content)


def get_object(s3_key: str) -> bytes:
    return _s3().get_object(Bucket=S3_STATE_BUCKET, Key=s3_key)["Body"].read()


def delete_objects(s3_keys: list[str]) -> None:
    _delete_keys([k for k in s3_keys if k])


# --- Scrape jobs -----------------------------------------------------------------

def list_jobs(slug: str) -> list[dict]:
    jobs, _ = _get_json(_key(slug, "scrape_jobs.json"))
    return jobs or []


def get_job(slug: str, job_id: str) -> Optional[dict]:
    return next((j for j in list_jobs(slug) if j["id"] == job_id), None)


def add_job(slug: str, job: dict) -> None:
    _update_json(_key(slug, "scrape_jobs.json"), lambda jobs: jobs + [job], default=[])


def update_job(slug: str, job_id: str, fn: Callable[[dict], dict]) -> Optional[dict]:
    """Atomically replace one job with fn(job); None if it doesn't exist."""
    found = []

    def apply(jobs):
        out = []
        for j in jobs:
            if j["id"] == job_id:
                j = fn(j)
                found.append(j)
            out.append(j)
        return out

    _update_json(_key(slug, "scrape_jobs.json"), apply, default=[])
    return found[0] if found else None


def delete_job(slug: str, job_id: str) -> None:
    _update_json(_key(slug, "scrape_jobs.json"), lambda jobs: [j for j in jobs if j["id"] != job_id])


class JobBusy(Exception):
    """The job is already running on some server."""


def is_running(job: dict) -> bool:
    started = job.get("run_started_at")
    return bool(job.get("is_running")) and bool(started) and (
        datetime.fromisoformat(started) > datetime.fromisoformat(now()) - RUN_LEASE
    )


def claim_job(slug: str, job_id: str) -> Optional[dict]:
    """Mark the job running unless it already is; the conditional write means only one
    server or request can win. Returns the job, None if missing, raises JobBusy."""
    def claim(job):
        if is_running(job):
            raise JobBusy(job_id)
        return {**job, "is_running": True, "run_started_at": now()}

    return update_job(slug, job_id, claim)


def release_job(slug: str, job_id: str, next_scrape_at: Callable[[dict, str], Optional[str]]) -> None:
    """Mark the job finished and schedule its next run."""
    finished = now()
    update_job(slug, job_id, lambda job: {
        **job, "is_running": False, "run_started_at": None,
        "last_scraped_at": finished, "next_scrape_at": next_scrape_at(job, finished),
    })
