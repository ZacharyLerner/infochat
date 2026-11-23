"""
activity.py
===========
Activity log for every workspace: chats, queries, uploads, deletes and settings
changes, sent to CloudWatch Logs (config.ACTIVITY_LOG_GROUP) and read back for
the admin page's Activity area with Logs Insights.

Each event is one JSON log line:

    {"event": "chat", "workspace": "<slug>", "workspace_name": "...",
     "key_prefix": "its_rhodyrag_prod", "caller": "admin|workspace_key|intake",
     "source": "chat|uploader|admin-ui|api|intake", ...event details}

`key_prefix` is the namespace of the workspace's LLM gateway key (the part before
"/" in its model names), so usage can be split per gateway key without the key
ever being logged.

Recording never blocks a request: record() only appends to an in-memory queue,
and a background thread sends it to CloudWatch in batches every few seconds
(PutLogEvents takes up to 10,000 events / 1 MB per call). If CloudWatch is
unreachable, unsent events are kept and retried, up to _MAX_PENDING.
"""

import json
import logging
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import boto3
from botocore.exceptions import ClientError

import config

logger = logging.getLogger(__name__)

EVENTS = ("chat", "query", "upload", "delete_document", "workspace_create",
          "workspace_update", "workspace_delete", "settings_update")

_FLUSH_SECONDS = 5
_MAX_BATCH = 5000            # well under PutLogEvents' 10,000 events / 1 MB
_MAX_PENDING = 50_000        # drop the oldest beyond this if CloudWatch stays down
_STREAM = f"{socket.gethostname()}-{os.getpid()}"

_lock = threading.Lock()
_pending: list[dict] = []
_wake = threading.Event()
_thread: Optional[threading.Thread] = None
_stream_ready = False
_client = None


def _logs():
    global _client
    if _client is None:
        _client = boto3.client("logs", region_name=config.AWS_REGION or None)
    return _client


def key_prefix(ws: Optional[dict]) -> str:
    """Gateway key namespace of a workspace, from its model names ("its_rhodyrag_prod/...")."""
    for field in ("llm_model", "embed_model"):
        model = (ws or {}).get(field) or ""
        if "/" in model:
            return model.split("/", 1)[0]
    return ""


def record(event: str, ws: Optional[dict] = None, request=None, **detail) -> None:
    """Queue one activity event. Never raises and never waits on the network."""
    if not config.ACTIVITY_LOG_GROUP:
        return
    try:
        entry = {"event": event}
        if ws:
            entry.update(workspace=ws.get("slug", ""), workspace_name=ws.get("name", ""), key_prefix=key_prefix(ws))
        if request is not None:
            entry["caller"] = getattr(request.state, "caller", "admin")
            entry["source"] = (request.headers.get("X-Infochat-Source") or "api")[:40]
        entry.update({k: v for k, v in detail.items() if v is not None})
        with _lock:
            _pending.append({"timestamp": int(time.time() * 1000), "message": json.dumps(entry, default=str)})
            if len(_pending) > _MAX_PENDING:
                del _pending[: len(_pending) - _MAX_PENDING]
        _start()
    except Exception as exc:
        logger.warning("activity: could not record %s: %s", event, exc)


def _start() -> None:
    global _thread
    if _thread is None:
        with _lock:
            if _thread is None:
                _thread = threading.Thread(target=_run, name="activity-log", daemon=True)
                _thread.start()


def _run() -> None:
    while True:
        _wake.wait(_FLUSH_SECONDS)
        _wake.clear()
        flush()


def _ensure_stream() -> None:
    global _stream_ready
    if _stream_ready:
        return
    try:
        _logs().create_log_group(logGroupName=config.ACTIVITY_LOG_GROUP)
    except ClientError as exc:
        # Already there is fine; so is no permission to create it if it was made by hand.
        if exc.response["Error"]["Code"] not in ("ResourceAlreadyExistsException", "AccessDeniedException"):
            raise
    try:
        _logs().create_log_stream(logGroupName=config.ACTIVITY_LOG_GROUP, logStreamName=_STREAM)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    _stream_ready = True


def flush() -> None:
    """Send everything queued so far. Called by the background thread and at shutdown."""
    while True:
        with _lock:
            batch = _pending[:_MAX_BATCH]
        if not batch:
            return
        try:
            _ensure_stream()
            _logs().put_log_events(logGroupName=config.ACTIVITY_LOG_GROUP, logStreamName=_STREAM, logEvents=batch)
        except Exception as exc:
            logger.warning("activity: could not send %d events to CloudWatch (will retry): %s", len(batch), exc)
            return
        with _lock:
            # Events are only ever appended, so the batch is still at the front unless trimmed by _MAX_PENDING
            if _pending[: len(batch)] == batch:
                del _pending[: len(batch)]


# --- Reading (admin Activity area) ------------------------------------------

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
_EVENT_FIELDS = ("@timestamp, event, workspace, workspace_name, key_prefix, caller, source, "
                 "filename, chunks, doc_id, session_id, has_attachment, duration_ms, changed, bytes")


def _insights(query: str, start: int, end: int, limit: int) -> list[dict]:
    try:
        qid = _logs().start_query(logGroupName=config.ACTIVITY_LOG_GROUP, startTime=start, endTime=end,
                                  queryString=query, limit=limit)["queryId"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return []  # nothing has been logged yet
        raise
    deadline = time.time() + 30
    while time.time() < deadline:
        res = _logs().get_query_results(queryId=qid)
        if res["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            if res["status"] != "Complete":
                raise RuntimeError(f"Logs Insights query {res['status'].lower()}")
            return [{f["field"]: f["value"] for f in row if f["field"] != "@ptr"} for row in res["results"]]
        time.sleep(0.4)
    raise RuntimeError("Logs Insights query timed out")


def search(hours: int, limit: int, key_prefix: str = "", workspace: str = "", event: str = "") -> dict:
    """Recent events and per (key_prefix, workspace, event) counts for the filters given.

    Filter values must match _SAFE_VALUE (they are placed in the query text).
    """
    filters = []
    for field, value in (("key_prefix", key_prefix), ("workspace", workspace), ("event", event)):
        if value:
            if not _SAFE_VALUE.match(value):
                raise ValueError(f"invalid {field}")
            filters.append(f'{field} = "{value}"')
    where = f"| filter {' and '.join(filters)} " if filters else ""
    end = int(time.time())
    start = end - hours * 3600
    queries = (
        (f"fields {_EVENT_FIELDS} {where}| sort @timestamp desc | limit {limit}", limit),
        (f"fields event {where}| stats count(*) as count by key_prefix, workspace, workspace_name, event "
         f"| sort count desc | limit 1000", 1000),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        events, counts = pool.map(lambda q: _insights(q[0], start, end, q[1]), queries)
    return {"events": events, "counts": counts}
