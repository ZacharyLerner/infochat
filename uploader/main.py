import asyncio
import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from anythingllm import (
    LLM_workspace_exists,
    LLM_upload_document,
    LLM_remove_document,
    LLM_json_workspace_settings,
    LLM_update_workspace_settings,
    LLM_delete_workspace,
)
from config import APP_API_KEY, TEXT_EXTENSIONS, MAX_UPLOAD_BYTES
from decling_conversion import docling_filename, fetch_page, DOCLING_SUFFIX
from schemas import (
    FileResponse,
    FileCreate,
    WorkspaceCreate,
    WorkspaceResponse,
    WorkspaceUpdate,
    ScrapeJobCreate,
    ScrapeJobUpdate,
    ScrapeJobResponse,
)
from scraper import get_links_by_depth, get_links_by_prefix
import conversion
import store
from config import DOCLING_RESULTS_QUEUE_URL

NY = ZoneInfo("America/New_York")

# App Setup
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Semaphores
SEM = asyncio.Semaphore(10)
DELETE_SEM = asyncio.Semaphore(5)

# Scheduled runs started by /api/v1/scrape/run-due (and the results consumer); held so they
# aren't garbage-collected mid-run.
_background_runs: set[asyncio.Task] = set()


@app.on_event("startup")
async def start_results_consumer():
    """Apply docling-pipeline results (see conversion.py) on this server."""
    if DOCLING_RESULTS_QUEUE_URL:
        task = asyncio.create_task(conversion.consume_results())
        _background_runs.add(task)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
# Due jobs are started by scheduler.py (run on one host) calling
# POST /api/v1/scrape/run-due, so uploader servers don't each run a scheduler.

def _compute_next_run(interval: str | None, from_time=None):
    """Return a datetime for the next scheduled run, or None if manual."""
    if not interval:
        return None
    if from_time is None:
        from_time = datetime.now(NY)
    deltas = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(weeks=1),
    }
    delta = deltas.get(interval)
    return (from_time + delta) if delta else None


def _next_run_iso(job: dict, from_iso: str) -> str | None:
    next_run = _compute_next_run(job.get("schedule_interval"), datetime.fromisoformat(from_iso))
    return next_run.isoformat() if next_run else None


async def _discover_job_urls(job):
    """Return the list of URLs a job should scrape, according to its mode."""
    if job["mode"] == "single":
        return [job["base_url"]]
    if job["mode"] == "list":
        return list(job.get("urls") or [])
    if job["mode"] == "prefix":
        parsed_path = urlparse(job["base_url"]).path or "/"
        if not parsed_path.endswith("/"):
            parsed_path = parsed_path.rsplit("/", 1)[0] + "/"
        return await get_links_by_prefix(
            job["base_url"],
            prefixes=[parsed_path],
            allow_offsite=job["allow_offsite"],
            max_pages=job["max_pages"],
        )
    return await get_links_by_depth(
        job["base_url"],
        max_depth=job["max_depth"],
        allow_offsite=job["allow_offsite"],
        max_pages=job["max_pages"],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_url_to_filename(url: str) -> str:
    """Turn a URL into a safe, readable filename slug."""
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "_") or "index"
    slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", path)
    slug = re.sub(r"_+", "_", slug).strip("_")
    domain = parsed.netloc.replace(".", "_")
    return f"{domain}_{slug}" if slug else domain


MAX_LIST_URLS = 1000

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_LIST_MARKER_RE = re.compile(r"^(?:\d+[.)]|[-*•])$")
_WRAPPING_CHARS = "<>\"'`[]()"
_TRAILING_PUNCT = ".,;:!?"


def _normalize_list_url(token: str) -> str | None:
    """Clean one pasted token into an http(s) URL, or return None if it isn't one."""
    token = token.strip().lstrip(_WRAPPING_CHARS)
    token = token.rstrip(_TRAILING_PUNCT).rstrip(_WRAPPING_CHARS.replace(")", "")).rstrip(_TRAILING_PUNCT)
    # A trailing ")" with no matching "(" is sentence punctuation, not part of the URL
    while token.endswith(")") and token.count("(") < token.count(")"):
        token = token[:-1].rstrip(_TRAILING_PUNCT)
    if not token:
        return None
    if not _SCHEME_RE.match(token):
        token = "https://" + token

    parsed = urlparse(token)
    host = parsed.hostname or ""
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if not host or ("." not in host and host != "localhost"):
        return None
    try:
        parsed.port  # raises ValueError on a malformed port
    except ValueError:
        return None

    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path or "/",
        fragment="",
    ).geturl()


def _parse_url_list(raw) -> tuple[list[str], list[str], int]:
    """
    Parse user-supplied URLs (a pasted block of text or a list of strings).

    URLs may be separated by newlines, spaces, tabs, or commas. List markers
    ("1.", "-", "*") and wrapping quotes/brackets are ignored, a missing scheme
    defaults to https://, and fragments are dropped. Plain words next to a URL
    are ignored; a line with no URL, or a URL-like token that can't be parsed,
    is reported as invalid.

    Returns (valid_urls, invalid_entries, duplicate_count); valid_urls keeps
    first-seen order with duplicates removed.
    """
    if raw is None:
        return [], [], 0
    text_block = "\n".join(str(x) for x in raw) if isinstance(raw, list) else str(raw)

    valid, invalid, seen = [], [], set()
    duplicates = 0
    for line in text_block.splitlines():
        line_urls, line_bad, line_has_content = [], [], False
        for tok in re.split(r"\s+", line):
            # "a.com,b.com" or "https://a.com,https://b.com" pasted without spaces
            for part in re.split(r",(?=\S)(?=[^,]*\.)", tok) if "," in tok else [tok]:
                part = part.strip().strip(",;")
                if not part or _LIST_MARKER_RE.match(part):
                    continue
                line_has_content = True
                url = _normalize_list_url(part)
                if url is not None:
                    line_urls.append(url)
                elif "." in part or "/" in part:
                    line_bad.append(part)  # looks like an attempted URL — report it

        if line_has_content and not line_urls and not line_bad:
            invalid.append(line.strip())  # a line of plain text with no URL in it
        invalid.extend(line_bad)
        for url in line_urls:
            if url in seen:
                duplicates += 1
            else:
                seen.add(url)
                valid.append(url)
    return valid, invalid, duplicates


def _job_to_dict(job: dict, page_count: int = 0) -> dict:
    return {**job, "is_running": store.is_running(job), "page_count": page_count}


def _require_url_list(raw) -> list[str]:
    """Parse a URL list for saving on a job; 400 unless every entry is a usable URL."""
    urls, invalid, _ = _parse_url_list(raw)
    if invalid:
        shown = ", ".join(invalid[:5]) + (" …" if len(invalid) > 5 else "")
        raise HTTPException(status_code=400, detail=f"Not valid URLs: {shown}")
    if not urls:
        raise HTTPException(status_code=400, detail="At least one URL is required")
    if len(urls) > MAX_LIST_URLS:
        raise HTTPException(
            status_code=400, detail=f"Too many URLs ({len(urls)}); the limit is {MAX_LIST_URLS}"
        )
    return urls


def _require_workspace(workspace_id: str) -> dict:
    workspace = store.get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


def _require_job(workspace_id: str, job_id: str) -> dict:
    job = store.get_job(workspace_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scrape job not found")
    return job


def _require_api_key(request: Request) -> None:
    if APP_API_KEY and request.headers.get("X-API-Key") != APP_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _new_file_record(workspace_id: str, **fields) -> dict:
    return {
        "key": uuid.uuid4().hex,
        "id": None,
        "workspace_id": workspace_id,
        "category": "uploaded_file",
        "status": "ready",
        "error": None,
        "original_extension": None,
        "source_url": None,
        "scrape_job_id": None,
        "content_hash": None,
        "last_checked_at": None,
        "uploaded_at": store.now(),
        "original": None,
        "converted": None,
        **fields,
    }


def _upsert_by_key(files: list[dict], records: list[dict]) -> list[dict]:
    """Replace records with the same key, append the rest."""
    by_key = {r["key"]: r for r in records}
    present = {f["key"] for f in files}
    return [by_key.get(f["key"], f) for f in files] + [r for r in records if r["key"] not in present]


def _with_datetimes(record: dict, *fields) -> dict:
    """Copy of record with ISO strings parsed, for templates that call .strftime()."""
    return {**record, **{f: datetime.fromisoformat(record[f]) for f in fields if record.get(f)}}


def _is_text(name: str) -> bool:
    """Plain-text files (incl. ready-made .docling.json) are embedded as-is, not converted."""
    return Path(name).suffix.lower() in TEXT_EXTENSIONS


async def _add_document(workspace_id: str, filename: str, content: bytes) -> dict:
    """Store an uploaded file and get it embedded; returns its new record.

    Text files are already in their final form: they are stored as the record's
    converted document and embedded right away. Everything else is stored as an
    original for docling-pipeline to convert: the record stays "converting" (no
    doc id yet) until conversion.handle_result embeds the result and deletes the
    original. Only converted documents are kept.
    """
    record = _new_file_record(workspace_id, filename=filename, original_extension=Path(filename).suffix.lower())
    text = _is_text(filename)
    field, path = ("converted", store.converted_key) if text else ("original", store.original_key)
    record[field] = path(workspace_id, record["key"], filename)
    await asyncio.to_thread(store.put_object, record[field], content)
    try:
        if text:
            record["id"] = await asyncio.to_thread(
                LLM_upload_document, io.StringIO(content.decode("utf-8")), filename, workspace_id
            )
        else:
            record["status"] = "converting"
            await asyncio.to_thread(conversion.send_job, record, docling_filename(filename))
    except Exception:
        await asyncio.to_thread(store.delete_objects, [record[field]])
        raise
    return record


# ---------------------------------------------------------------------------
# Web UI Routes
# ---------------------------------------------------------------------------

@app.get("/{workspace_id}", include_in_schema=False, name="home")
async def home(request: Request, workspace_id: str):
    workspace = _require_workspace(workspace_id)
    files = store.list_files(workspace_id)

    # Newest first, matching the sort menu's default
    uploaded_files = [
        _with_datetimes(f, "uploaded_at")
        for f in sorted(files, key=lambda f: f["uploaded_at"], reverse=True)
        if not f["category"].startswith("scrape_")
    ]

    # Collect distinct file extensions present in this workspace (uploaded only)
    extensions = sorted(
        {
            (f["original_extension"] or Path(f["filename"]).suffix).lower()
            for f in uploaded_files
            if (f["original_extension"] or Path(f["filename"]).suffix)
        }
    )

    # Scrape jobs for this workspace, newest first, with page counts attached
    page_counts = {}
    for f in files:
        if f.get("scrape_job_id"):
            page_counts[f["scrape_job_id"]] = page_counts.get(f["scrape_job_id"], 0) + 1
    scrape_jobs = [
        _with_datetimes(_job_to_dict(job, page_counts.get(job["id"], 0)), "last_scraped_at", "next_scrape_at")
        for job in sorted(store.list_jobs(workspace_id), key=lambda j: j["created_at"], reverse=True)
    ]

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "files": uploaded_files,
            "workspace": workspace,
            "extensions": extensions,
            "scrape_jobs": scrape_jobs,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
        },
    )


# ---------------------------------------------------------------------------
# File upload (web UI SSE)
# ---------------------------------------------------------------------------

async def processes_file(content, fname, workspace_id, queue):
    async with SEM:
        try:
            await queue.put({"file": fname, "status": "uploaded"})
            if not _is_text(fname):
                await queue.put({"file": fname, "status": "queued"})
            record = await _add_document(workspace_id, fname, content)
            await queue.put(
                {
                    "file": fname,
                    "status": "done",
                    # Documents still converting have no doc id yet; the UI addresses them by key.
                    "location": record["id"] or record["key"],
                    "converting": record["status"] == "converting",
                    "key": record["key"],
                    "name": fname,
                    "original_extension": record["original_extension"],
                    "_file_record": record,
                }
            )
        except Exception as e:
            print(f"Error processing file {fname}: {e}")
            await queue.put(
                {
                    "file": fname,
                    "status": "error",
                    "message": f"Processing failed: {str(e)}",
                }
            )


async def _stream_upload_progress(file_data, workspace_id):
    queue = asyncio.Queue()
    completed_records = []
    valid_files = []
    rejected_events = []

    for content, filename in file_data:
        if len(content) > MAX_UPLOAD_BYTES:
            size_mb = len(content) / (1024 * 1024)
            limit_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
            rejected_events.append(
                {
                    "file": filename,
                    "status": "error",
                    "message": f"File is {size_mb:.1f} MB — exceeds {limit_mb:.0f} MB limit",
                }
            )
        else:
            valid_files.append((content, filename))

    for event in rejected_events:
        yield f"data: {json.dumps(event)}\n\n"

    async def run_all():
        coroutines = []
        for content, filename in valid_files:
            coroutines.append(processes_file(content, filename, workspace_id, queue))
        await asyncio.gather(*coroutines, return_exceptions=True)
        await queue.put(None)

    task = asyncio.create_task(run_all())

    while True:
        event = await queue.get()
        if event is None:
            break
        record = event.pop("_file_record", None)
        if record is not None:
            completed_records.append(record)
        yield f"data: {json.dumps(event)}\n\n"

    await asyncio.to_thread(store.add_files, workspace_id, completed_records)

    yield "data: [DONE]\n\n"
    await task


@app.post("/{workspace_id}/uploadfiles/", include_in_schema=False)
async def create_upload_files(
    workspace_id: str,
    uploaded_files: list[UploadFile] = File(...),
):
    _require_workspace(workspace_id)

    file_data = [(await f.read(), f.filename) for f in uploaded_files]

    return StreamingResponse(
        _stream_upload_progress(file_data, workspace_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# Delete endpoints
# ---------------------------------------------------------------------------

async def _delete_file(file_id, workspace_id):
    async with DELETE_SEM:
        success = await asyncio.to_thread(LLM_remove_document, workspace_id, file_id)
        return file_id, success


def _find_file(workspace_id: str, file_id: str) -> dict | None:
    """A record by doc id, or by stable key for a document that is still converting."""
    return next((f for f in store.list_files(workspace_id) if file_id in (f["id"], f["key"])), None)


@app.delete("/{workspace_id}/delete/{file_id:path}", include_in_schema=False)
async def delete_uploaded_file(workspace_id: str, file_id: str):
    record = _find_file(workspace_id, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")

    if record["id"]:
        success = await asyncio.to_thread(LLM_remove_document, workspace_id, record["id"])
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete from LLM")

    await asyncio.to_thread(store.remove_files, workspace_id, [record["key"]])
    return {"deleted": file_id}


@app.post("/{workspace_id}/delete-bulk", include_in_schema=False)
async def delete_bulk_files(workspace_id: str, request: Request):
    body = await request.json()
    file_ids = body.get("file_ids", [])

    records = {}
    for f in store.list_files(workspace_id):
        for fid in (f["id"], f["key"]):
            if fid in file_ids:
                records[fid] = f
    found = [fid for fid in file_ids if fid in records]

    print(f"[bulk-delete] received {len(file_ids)} id(s), {len(found)} found in workspace")
    if len(file_ids) != len(found):
        missing = set(file_ids) - set(records)
        print(f"[bulk-delete] {len(missing)} id(s) not in workspace (will be skipped): {list(missing)[:5]}")

    embedded = [fid for fid in found if records[fid]["id"] == fid]
    results = await asyncio.gather(
        *[_delete_file(fid, workspace_id) for fid in embedded],
        return_exceptions=True,
    )
    deleted = [fid for fid in found if records[fid]["id"] != fid]  # still converting: nothing to remove upstream
    for r in results:
        if isinstance(r, Exception):
            print(f"[bulk-delete] exception during delete: {r}")
            continue
        file_id, success = r
        if success:
            deleted.append(file_id)
        else:
            print(f"[bulk-delete] LLM_remove_document returned False for {file_id!r}")

    await asyncio.to_thread(store.remove_files, workspace_id, [records[fid]["key"] for fid in deleted])
    print(f"[bulk-delete] done: {len(deleted)}/{len(found)} deleted successfully")
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

@app.get("/api/v1/workspaces/{workspace_id}/settings", include_in_schema=False)
async def fetch_workspace_settings(workspace_id: str):
    settings = LLM_json_workspace_settings(workspace_id)
    if settings is None:
        raise HTTPException(status_code=404, detail="Workspace settings not found")
    return settings


@app.get("/{workspace_id}/settings", include_in_schema=False, name="workspace_settings")
async def workspace_settings_page(request: Request, workspace_id: str):
    workspace = _require_workspace(workspace_id)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"workspace": workspace},
    )


@app.post("/api/v1/workspaces/{workspace_id}/settings", include_in_schema=False)
async def save_workspace_settings(workspace_id: str, request: Request):
    body = await request.json()
    success = LLM_update_workspace_settings(workspace_id, body)
    if not success:
        raise HTTPException(
            status_code=500, detail="Failed to update workspace settings"
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Scrape: discover (unchanged)
# ---------------------------------------------------------------------------

@app.post("/{workspace_id}/scrape/discover", include_in_schema=False)
async def scrape_discover(workspace_id: str, request: Request):
    _require_workspace(workspace_id)

    body = await request.json()
    mode = body.get("mode", "depth")

    if mode == "list":
        urls, invalid, duplicates = _parse_url_list(body.get("urls"))
        if len(urls) > MAX_LIST_URLS:
            raise HTTPException(
                status_code=400,
                detail=f"Too many URLs ({len(urls)}); the limit is {MAX_LIST_URLS}",
            )
        return {
            "urls": urls,
            "count": len(urls),
            "invalid": invalid,
            "duplicates": duplicates,
        }

    base_url = body.get("base_url", "").strip()
    max_depth = int(body.get("max_depth", 2))
    max_pages = int(body.get("max_pages", 100))
    allow_offsite = bool(body.get("allow_offsite", False))

    if not base_url:
        raise HTTPException(status_code=400, detail="base_url is required")

    try:
        if mode == "prefix":
            parsed_path = urlparse(base_url).path or "/"
            if not parsed_path.endswith("/"):
                parsed_path = parsed_path.rsplit("/", 1)[0] + "/"
            urls = await get_links_by_prefix(
                base_url,
                prefixes=[parsed_path],
                allow_offsite=allow_offsite,
                max_pages=max_pages,
            )
        else:
            urls = await get_links_by_depth(
                base_url,
                max_depth=max_depth,
                allow_offsite=allow_offsite,
                max_pages=max_pages,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Crawl failed: {str(e)}")

    return {"urls": urls, "count": len(urls)}


# ---------------------------------------------------------------------------
# Scrape Jobs CRUD
# ---------------------------------------------------------------------------

def _job_page_count(workspace_id: str, job_id: str) -> int:
    return sum(1 for f in store.list_files(workspace_id) if f.get("scrape_job_id") == job_id)


@app.get("/{workspace_id}/scrape/jobs", include_in_schema=False)
async def list_scrape_jobs(workspace_id: str):
    _require_workspace(workspace_id)

    files = store.list_files(workspace_id)
    jobs = sorted(store.list_jobs(workspace_id), key=lambda j: j["created_at"], reverse=True)
    return [
        _job_to_dict(job, sum(1 for f in files if f.get("scrape_job_id") == job["id"]))
        for job in jobs
    ]


@app.get("/{workspace_id}/scrape/jobs/{job_id}/pages", include_in_schema=False)
async def list_job_pages(workspace_id: str, job_id: str):
    _require_job(workspace_id, job_id)

    files = sorted(
        (f for f in store.list_files(workspace_id) if f.get("scrape_job_id") == job_id),
        key=lambda f: f["uploaded_at"],
        reverse=True,
    )

    return [
        {
            "id": f["id"],
            "filename": f["filename"],
            "source_url": f["source_url"],
            "last_checked_at": f["last_checked_at"],
            "uploaded_at": f["uploaded_at"],
        }
        for f in files
    ]


@app.post("/{workspace_id}/scrape/jobs", include_in_schema=False)
async def create_scrape_job(workspace_id: str, request: Request):
    _require_workspace(workspace_id)

    body = await request.json()
    mode = body.get("mode", "depth")
    job = {
        "id": str(uuid.uuid4()),
        "workspace_id": workspace_id,
        "name": body.get("name", "Untitled Job"),
        "base_url": body.get("base_url", ""),
        "mode": mode,
        "urls": None,
        "max_depth": int(body.get("max_depth", 2)),
        "max_pages": int(body.get("max_pages", 100)),
        "allow_offsite": bool(body.get("allow_offsite", False)),
        "schedule_interval": body.get("schedule_interval") or None,
        "last_scraped_at": None,
        "next_scrape_at": None,
        "is_running": False,
        "run_started_at": None,
        "created_at": store.now(),
    }

    if mode == "list":
        job["urls"] = _require_url_list(body.get("urls"))
        job["base_url"] = job["urls"][0]
        job["max_depth"] = 0
        job["max_pages"] = len(job["urls"])
        job["allow_offsite"] = True

    if job["schedule_interval"]:
        job["next_scrape_at"] = _next_run_iso(job, store.now())

    store.add_job(workspace_id, job)
    return _job_to_dict(job)


@app.patch("/{workspace_id}/scrape/jobs/{job_id}", include_in_schema=False)
async def update_scrape_job(workspace_id: str, job_id: str, request: Request):
    job = _require_job(workspace_id, job_id)
    body = await request.json()

    changes = {}
    if "name" in body and body["name"]:
        changes["name"] = body["name"]
    if "base_url" in body and body["base_url"]:
        changes["base_url"] = body["base_url"]
    if "mode" in body:
        changes["mode"] = body["mode"]
    if "max_depth" in body:
        changes["max_depth"] = int(body["max_depth"])
    if "max_pages" in body:
        changes["max_pages"] = int(body["max_pages"])
    if "allow_offsite" in body:
        changes["allow_offsite"] = bool(body["allow_offsite"])
    if "urls" in body or (changes.get("mode", job["mode"]) == "list" and not job.get("urls")):
        changes["urls"] = _require_url_list(body.get("urls"))
        changes["base_url"] = changes["urls"][0]
        changes["max_pages"] = len(changes["urls"])

    # Schedule change: recompute next_scrape_at
    if "schedule_interval" in body:
        changes["schedule_interval"] = body["schedule_interval"] or None
        changes["next_scrape_at"] = _next_run_iso(changes, store.now())

    job = store.update_job(workspace_id, job_id, lambda cur: {**cur, **changes})
    if not job:
        raise HTTPException(status_code=404, detail="Scrape job not found")
    return _job_to_dict(job, _job_page_count(workspace_id, job_id))


@app.delete("/{workspace_id}/scrape/jobs/{job_id}", include_in_schema=False)
async def delete_scrape_job(workspace_id: str, job_id: str):
    _require_job(workspace_id, job_id)

    # Delete all files associated with this job from the RAG backend
    files = [f for f in store.list_files(workspace_id) if f.get("scrape_job_id") == job_id]
    results = await asyncio.gather(
        *[_delete_file(f["id"], workspace_id) for f in files if f["id"]],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            print(f"[delete-job] exception during file delete: {r}")

    await asyncio.to_thread(store.remove_files, workspace_id, [f["key"] for f in files])
    store.delete_job(workspace_id, job_id)
    return {"deleted": job_id}


# ---------------------------------------------------------------------------
# Scrape Job: Run (smart change detection)
# ---------------------------------------------------------------------------

async def _process_job_url(url, job, existing_files, queue):
    """
    Fetch one URL for a job run, store the page as fetched as a temporary
    original, and queue it for conversion. Whether the page changed is decided
    when the result comes back (conversion.handle_result compares content hashes).
    """
    async with SEM:
        workspace_id = job["workspace_id"]
        try:
            await queue.put({"url": url, "status": "fetching"})
            content, content_type = await asyncio.to_thread(fetch_page, url)

            filename = _sanitize_url_to_filename(url) + DOCLING_SUFFIX
            record = existing_files.get(url) or _new_file_record(
                workspace_id,
                filename=filename,
                original_extension=".html",
                category=f"scrape_{job['name']}",
                source_url=url,
                scrape_job_id=job["id"],
                status="converting",
            )
            # Each run gets its own original (deleted once converted), so an earlier run's
            # result can't remove the page this run is about to convert.
            original = store.original_key(
                workspace_id, record["key"], f"{uuid.uuid4().hex[:8]}/{_sanitize_url_to_filename(url)}"
            )
            await asyncio.to_thread(store.put_object, original, content)
            record = {**record, "original": original, "filename": filename}
            await asyncio.to_thread(conversion.send_job, record, filename, scraped=True, content_type=content_type)

            await queue.put({
                "url": url,
                "status": "queued",
                "name": filename,
                "category": job["name"],
                "_file_record": record,
            })

        except Exception as e:
            print(f"[job-run] error processing {url}: {e}")
            await queue.put({"url": url, "status": "error", "message": str(e)})


async def _job_run_events(job: dict):
    """Run a job that was claimed with store.claim_job, yielding progress events.

    Shared by the streamed UI run and scheduled background runs; always
    releases the job when it finishes.
    """
    workspace_id, job_id = job["workspace_id"], job["id"]
    try:
        # Re-discover URLs
        yield {"status": "discovering"}
        try:
            urls = await _discover_job_urls(job)
        except Exception as e:
            yield {"status": "error", "message": f"Discovery failed: {str(e)}"}
            return

        url_set = set(urls)
        yield {"status": "discovered", "count": len(urls)}

        # Existing files for this job
        existing_files = {
            f["source_url"]: f
            for f in store.list_files(workspace_id)
            if f.get("scrape_job_id") == job_id and f.get("source_url")
        }

        # Remove pages no longer in the crawl
        stale = [f for source_url, f in existing_files.items() if source_url not in url_set]
        for f in stale:
            if f["id"]:
                await asyncio.to_thread(LLM_remove_document, workspace_id, f["id"])
            yield {"url": f["source_url"], "status": "removed"}
        if stale:
            await asyncio.to_thread(store.remove_files, workspace_id, [f["key"] for f in stale])

        # Process each URL
        queue = asyncio.Queue()
        file_records = []

        async def run_all():
            coros = [
                _process_job_url(url, job, existing_files, queue)
                for url in urls
            ]
            await asyncio.gather(*coros, return_exceptions=True)
            await queue.put(None)

        task = asyncio.create_task(run_all())

        while True:
            event = await queue.get()
            if event is None:
                break

            rec = event.pop("_file_record", None)
            if rec is not None:
                file_records.append(rec)
            yield event

        # Persist all file records
        await asyncio.to_thread(store.update_files, workspace_id, lambda files: _upsert_by_key(files, file_records))
        await task

    finally:
        await asyncio.to_thread(store.release_job, workspace_id, job_id, _next_run_iso)

    yield {"status": "done", "job_id": job_id, "page_count": _job_page_count(workspace_id, job_id)}


async def _stream_job_run(job: dict):
    async for event in _job_run_events(job):
        # Don't stream internal unchanged events for now
        if event.get("status") != "unchanged":
            yield f"data: {json.dumps(event)}\n\n"
    yield "data: [DONE]\n\n"


async def _run_job_in_background(job: dict):
    try:
        async for event in _job_run_events(job):
            if event.get("status") == "error" and "url" not in event:
                print(f"[scheduler] job {job['id']} ({job['name']}): {event['message']}")
    except Exception as e:
        print(f"[scheduler] error running job {job['id']}: {e}")


def _claim_or_raise(workspace_id: str, job_id: str) -> dict:
    try:
        job = store.claim_job(workspace_id, job_id)
    except store.JobBusy:
        raise HTTPException(status_code=409, detail="Job is already running")
    if not job:
        raise HTTPException(status_code=404, detail="Scrape job not found")
    return job


@app.post("/{workspace_id}/scrape/jobs/{job_id}/run", include_in_schema=False)
async def run_scrape_job(workspace_id: str, job_id: str):
    _require_workspace(workspace_id)
    job = _claim_or_raise(workspace_id, job_id)

    return StreamingResponse(
        _stream_job_run(job),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/api/v1/scrape/run-due")
async def run_due_scrape_jobs(request: Request):
    """
    Start every scheduled scrape job that is due, in the background on this server.

    Called about once a minute by scheduler.py. Each job is claimed with a
    conditional write first, so overlapping calls (or a user pressing Run)
    can't start the same job twice. Requires X-API-Key when APP_API_KEY is configured.
    """
    _require_api_key(request)

    now = datetime.fromisoformat(store.now())
    started = []
    for workspace_id in await asyncio.to_thread(store.list_workspace_slugs):
        for job in await asyncio.to_thread(store.list_jobs, workspace_id):
            due = job.get("next_scrape_at") and datetime.fromisoformat(job["next_scrape_at"]) <= now
            if not due or store.is_running(job):
                continue
            try:
                claimed = await asyncio.to_thread(store.claim_job, workspace_id, job["id"])
            except store.JobBusy:
                continue
            if claimed:
                print(f"[scheduler] triggering job {job['id']} ({job['name']})")
                task = asyncio.create_task(_run_job_in_background(claimed))
                _background_runs.add(task)
                task.add_done_callback(_background_runs.discard)
                started.append(job["id"])
    return {"started": started}


# ---------------------------------------------------------------------------
# REST API endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/workspaces/{workspace_id}/upload", response_model=list[FileResponse])
async def upload_to_workspace(
    workspace_id: str,
    uploaded_files: list[UploadFile] = File(...),
):
    """
    Upload one or more files to a workspace.

    Text files are embedded right away. Other files are converted to DoclingDocument JSON
    by docling-pipeline first: their records come back with status "converting" and no id,
    and get one once conversion finishes. Only the converted documents are kept in S3.

    Raises **404** if the workspace does not exist, or **413** if any file exceeds
    the maximum upload size.
    """
    _require_workspace(workspace_id)

    # Check every size before embedding anything, so a 413 leaves nothing half-uploaded.
    contents = [await f.read() for f in uploaded_files]
    for f, content in zip(uploaded_files, contents):
        if len(content) > MAX_UPLOAD_BYTES:
            size_mb = len(content) / (1024 * 1024)
            limit_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=f"File '{f.filename}' is {size_mb:.1f} MB — exceeds {limit_mb:.0f} MB limit",
            )

    saved_files = []
    for f, content in zip(uploaded_files, contents):
        saved_files.append(await _add_document(workspace_id, f.filename, content))

    await asyncio.to_thread(store.add_files, workspace_id, saved_files)
    return saved_files


@app.post("/api/v1/workspaces/new")
async def create_new_workspace(workspace: WorkspaceCreate, request: Request):
    """
    Register a workspace with the uploader.

    Workspaces are created and managed in the RAG backend. This endpoint
    only records the workspace for the uploader.

    - **id**: the workspace slug as it exists in the RAG backend
    - **name**: display name for the workspace
    - **owners**: list of owner user IDs

    Raises **409** if a workspace with the given ID already exists.
    """
    created = store.create_workspace(workspace.id, workspace.name, workspace.owners)
    if not created:
        raise HTTPException(status_code=409, detail="Workspace already exists")
    return created


@app.post("/api/v1/workspaces/db")
async def create_new_workspace_DB_only(workspace: WorkspaceCreate, request: Request):
    """
    Register a workspace with the uploader only (does not create it in the RAG backend).

    Use this endpoint when the workspace already exists in the RAG backend and you only need
    to register it with the uploader.

    - **id**: unique workspace identifier
    - **name**: display name for the workspace
    - **owners**: list of owner user IDs

    Raises **409** if a workspace with the given ID already exists.
    """
    created = store.create_workspace(workspace.id, workspace.name, workspace.owners)
    if not created:
        raise HTTPException(status_code=409, detail="Workspace already exists")
    return created


@app.get("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace_info(workspace_id: str, request: Request):
    """
    Retrieve a workspace by its ID, including its associated files.

    Raises **404** if no workspace with the given ID exists.
    """
    workspace = _require_workspace(workspace_id)
    return {**workspace, "files": store.list_files(workspace_id)}


@app.patch("/api/v1/workspaces/{workspace_id}")
async def rename_workspace(workspace_id: str, body: WorkspaceUpdate):
    """
    Rename a workspace by its ID.

    Raises **404** if no workspace with the given ID exists.
    """
    workspace = store.update_workspace(workspace_id, name=body.name)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


@app.delete("/api/v1/workspaces/{workspace_id}")
async def delete_workspace_by_id(workspace_id: str):
    """
    Delete a workspace by its ID from both the RAG backend and the uploader,
    including every stored document.

    Raises **404** if the workspace does not exist, or **500** if deletion in the RAG backend fails.
    """
    _require_workspace(workspace_id)
    response = LLM_delete_workspace(workspace_id)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to delete workspace")
    await asyncio.to_thread(store.delete_workspace, workspace_id)
    return {"deleted": workspace_id}


# ---------------------------------------------------------------------------
# Reembed / copy: rebuild embeddings from the converted documents kept in S3
# ---------------------------------------------------------------------------

async def _embed_converted(workspace_id: str, record: dict) -> str:
    """Embed a record's stored converted document into workspace_id; returns the doc id."""
    if not record.get("converted"):
        raise ValueError("still converting" if record.get("status") == "converting"
                         else "no converted document stored for this file")
    body = await asyncio.to_thread(store.get_object, record["converted"])
    name = record["converted"].rsplit("/", 1)[-1]
    return await asyncio.to_thread(LLM_upload_document, io.StringIO(body.decode("utf-8")), name, workspace_id)


@app.post("/api/v1/workspaces/{workspace_id}/reembed")
async def reembed_workspace(workspace_id: str, request: Request):
    """
    Embed every document of a workspace in the RAG backend again from its converted
    documents in S3, e.g. after a backend wipe or to rebuild a deleted workspace.

    Updates each file record with its new doc_id. Old embeddings are not removed.
    Requires X-API-Key header when APP_API_KEY is configured.
    """
    _require_api_key(request)
    _require_workspace(workspace_id)

    new_ids = {}
    failed = []
    for record in store.list_files(workspace_id):
        try:
            new_ids[record["key"]] = await _embed_converted(workspace_id, record)
        except Exception as e:
            print(f"[reembed] failed for {record['filename']}: {e}")
            failed.append({"file": record["filename"], "error": str(e)})

    await asyncio.to_thread(
        store.update_files, workspace_id,
        lambda files: [{**f, "id": new_ids.get(f["key"], f["id"])} for f in files],
    )
    return {"reembedded": len(new_ids), "failed": failed}


@app.post("/api/v1/workspaces/{workspace_id}/copy")
async def copy_workspace_documents(workspace_id: str, request: Request):
    """
    Copy every document of another workspace into this one: each converted
    document is copied and embedded here, and gets its own record.

    Body: { "source_workspace": "<slug>" }
    Copied scrape pages become plain documents (the scrape jobs are not copied).
    Requires X-API-Key header when APP_API_KEY is configured.
    """
    _require_api_key(request)
    _require_workspace(workspace_id)
    body = await request.json()
    source_id = body.get("source_workspace", "")
    _require_workspace(source_id)

    copied = []
    failed = []
    for record in store.list_files(source_id):
        try:
            new = _new_file_record(
                workspace_id,
                filename=record["filename"],
                original_extension=record["original_extension"],
                category="copied" if record["category"].startswith("scrape_") else record["category"],
                source_url=record.get("source_url"),
                content_hash=record.get("content_hash"),
            )
            new["id"] = await _embed_converted(workspace_id, record)
            new["converted"] = store.converted_key(workspace_id, new["key"], record["converted"].rsplit("/", 1)[-1])
            content = await asyncio.to_thread(store.get_object, record["converted"])
            await asyncio.to_thread(store.put_object, new["converted"], content)
            copied.append(new)
        except Exception as e:
            print(f"[copy] failed for {record['filename']}: {e}")
            failed.append({"file": record["filename"], "error": str(e)})

    await asyncio.to_thread(store.add_files, workspace_id, copied)
    return {"copied": len(copied), "failed": failed}
