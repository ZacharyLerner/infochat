"""
main.py
=======
FastAPI application: workspace CRUD, file embedding, querying, and global settings.
"""

# ─── Patch: silence noisy LiteLLM startup logs ───────────────────────────────
import logging as _logging

_logging.getLogger("LiteLLM").setLevel(_logging.WARNING)
_logging.getLogger("litellm").setLevel(_logging.WARNING)
_logging.getLogger("LiteLLM Router").setLevel(_logging.WARNING)
_logging.getLogger("LiteLLM Proxy").setLevel(_logging.WARNING)

import httpx as _httpx

_httpx_logger = _logging.getLogger("httpx")
_httpx_logger.setLevel(_logging.WARNING)
# ─────────────────────────────────────────────────────────────────────────────

import datetime
import json
import os
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, APIRouter, UploadFile, File, HTTPException, Security, Depends
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import db
import embedding
import manager
import query

# --- Doc tracking (JSON file) ------------------------------------------------
DOCS_FILE = Path("docs.json")
_docs_lock = threading.Lock()

# --- Query log (per-workspace JSON files in logs/) ---------------------------
LOGS_DIR = Path("logs")
_logs_lock = threading.Lock()


def _log_path(slug: str) -> Path:
    return LOGS_DIR / f"{slug}.json"


def _read_log(slug: str) -> list:
    """Read log entries for a workspace. Caller must hold _logs_lock."""
    path = _log_path(slug)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []


def _append_log(slug: str, entry: dict) -> None:
    """Append a single log entry atomically. Safe to call from a thread."""
    with _logs_lock:
        LOGS_DIR.mkdir(exist_ok=True)
        data = _read_log(slug)
        data.append(entry)
        text = json.dumps(data, indent=2)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=LOGS_DIR, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(text)
            try:
                os.replace(tmp_path, _log_path(slug))
            except OSError:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                _log_path(slug).write_text(text)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _read_docs() -> Dict[str, Any]:
    if DOCS_FILE.exists():
        return json.loads(DOCS_FILE.read_text())
    return {}


def _write_docs(data: Dict[str, Any]):
    """Write docs.json under the _docs_lock (caller must hold the lock).

    Uses a write-to-temp-then-rename strategy for atomicity on native
    filesystems. On Docker bind-mounts (macOS virtiofs/gRPC-FUSE), os.replace()
    across the overlay boundary raises EBUSY, so we fall back to writing
    directly to the target path — safe because the caller already holds
    _docs_lock, which serialises all reads and writes.
    """
    text = json.dumps(data, indent=2)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=DOCS_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(text)
        try:
            os.replace(tmp_path, DOCS_FILE)
        except OSError:
            # Bind-mount atomic rename not supported (Docker on macOS).
            # Fall back to direct overwrite — safe under _docs_lock.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            DOCS_FILE.write_text(text)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- Lifespan ----------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    await asyncio.to_thread(db.init_db)
    if not await asyncio.to_thread(DOCS_FILE.exists):
        await asyncio.to_thread(_write_docs, {})
    LOGS_DIR.mkdir(exist_ok=True)
    yield


# --- Auth (admin key required on all API routes) -----------------------------
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_admin_key(key: Optional[str] = Security(api_key_header)):
    """Require APP_API_KEY on all API endpoints when it is configured."""
    if config.APP_API_KEY and key != config.APP_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


app = FastAPI(
    title="LLM RAG Backend",
    version="1.0.0",
    lifespan=lifespan,
    # /docs, /redoc, and /openapi.json are registered internally by FastAPI
    # in a way that bypasses this app's dependency-injected auth entirely,
    # so they are unauthenticated whenever enabled. Disabled unless
    # config.ENABLE_API_DOCS is explicitly set — see config.py.
    docs_url="/docs" if config.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if config.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if config.ENABLE_API_DOCS else None,
)



# All real API routes are registered on this router, which enforces the
# admin API key. The SPA static-file catch-all route (registered directly on
# `app` further below) is intentionally NOT behind this dependency: it only
# ever serves the frontend's static HTML/JS/CSS shell (no data), and the
# browser cannot attach a custom X-API-Key header on a normal top-level
# navigation — if the catch-all required the key, the login page itself
# would be unreachable for anyone without an out-of-band way to set the
# header. All actual data continues to require the key via apiFetch() in
# app.js, which does attach the header on every XHR/fetch call.
api_router = APIRouter(dependencies=[Depends(verify_admin_key)])


# --- Auth verification endpoint ----------------------------------------------
@api_router.get("/auth/verify", summary="Verify API key")
def verify_auth():
    """Returns 200 if the API key is valid (enforced by global dependency)."""
    return {"status": "ok"}



# --- Pydantic models ---------------------------------------------------------

class CreateWorkspace(BaseModel):
    name: str = Field(..., description="Display name for the workspace.")
    llm_model: Optional[str] = Field(None, description="LiteLLM model string for the LLM, e.g. 'openai/gpt-4o'.")
    api_key: Optional[str] = Field(None, description="API key forwarded to the LLM gateway.")
    temperature: Optional[float] = Field(None, description="Sampling temperature for the LLM (0.0 = deterministic, 1.0 = creative).")
    system_prompt: Optional[str] = Field(None, description="System prompt prepended to every query.")
    top_n: Optional[int] = Field(None, description="Number of most-similar chunks to retrieve and pass to the LLM.")
    similarity_threshold: Optional[float] = Field(None, description="Minimum cosine similarity score (0–1) a chunk must meet to be included.")
    chunk_size: Optional[int] = Field(None, description="Token size of each chunk. Locked after creation — changing this after files are embedded would cause inconsistent retrieval.")
    chunk_overlap: Optional[int] = Field(None, description="Token overlap between consecutive chunks. Locked after creation for the same reason as chunk_size.")
    embed_model: Optional[str] = Field(None, description="Embedding model for this workspace. Locked after creation — changing it would cause vector dimension mismatches. Falls back to the global default if blank. Use 'direct-openai/<model>' to bypass the gateway.")
    embed_api_key: Optional[str] = Field(None, description="API key for the embedding model. Only needed when using a direct-openai/ embedding model that requires its own key separate from the LLM gateway key.")
    max_tokens: Optional[int] = Field(None, description="Maximum number of tokens the LLM may generate in a single response.")
    searxng_enabled: Optional[bool] = Field(None, description="Enable SearXNG web search augmentation for every query in this workspace.")
    searxng_num_results: Optional[int] = Field(None, description="Number of web search results to fetch per query (1–10).")
    searxng_query_suffix: Optional[str] = Field(None, description="Text appended to every web search query (e.g. 'site:uri.edu'). Does not affect vector retrieval.")
    rewrite_model: Optional[str] = Field(None, description="LiteLLM model string for query rewriting (e.g. 'openai/gpt-4o-mini'). Leave blank to disable rewriting.")
    rewrite_prompt: Optional[str] = Field(None, description="System prompt for the query rewriter. Leave blank to use the built-in default.")


class UpdateWorkspace(BaseModel):
    """Mutable workspace settings."""
    name: Optional[str] = Field(None, description="New display name for the workspace.")
    llm_model: Optional[str] = Field(None)
    api_key: Optional[str] = Field(None)
    temperature: Optional[float] = Field(None)
    system_prompt: Optional[str] = Field(None)
    top_n: Optional[int] = Field(None)
    similarity_threshold: Optional[float] = Field(None)
    embed_api_key: Optional[str] = Field(None)
    max_tokens: Optional[int] = Field(None)
    searxng_enabled: Optional[bool] = Field(None, description="Enable SearXNG web search augmentation.")
    searxng_num_results: Optional[int] = Field(None, description="Number of web search results to fetch per query (1–10).")
    searxng_query_suffix: Optional[str] = Field(None, description="Text appended to every web search query (e.g. 'site:uri.edu'). Does not affect vector retrieval.")
    rewrite_model: Optional[str] = Field(None, description="Model for query rewriting. Empty string disables rewriting.")
    rewrite_prompt: Optional[str] = Field(None, description="Custom rewrite prompt. Empty string uses built-in default.")


class QueryRequest(BaseModel):
    question: str = Field(..., description="The question to ask against the workspace's embedded documents.")
    prompt_suffix: Optional[str] = Field(None, description="Optional text appended to the LLM prompt only (not used for retrieval).")


# --- Global settings ---------------------------------------------------------
class UpdateSettings(BaseModel):
    llm_model: Optional[str] = None
    api_key: Optional[str] = None
    temperature: Optional[float] = None
    system_prompt: Optional[str] = None
    top_n: Optional[int] = None
    similarity_threshold: Optional[float] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    embed_model: Optional[str] = None
    embed_api_key: Optional[str] = None
    max_tokens: Optional[int] = None
    searxng_enabled: Optional[bool] = None
    searxng_num_results: Optional[int] = None
    searxng_query_suffix: Optional[str] = None
    rewrite_model: Optional[str] = None
    rewrite_prompt: Optional[str] = None


@api_router.get("/settings", summary="Get global settings")
def get_settings():
    return db.get_settings()


@api_router.put("/settings", summary="Update global settings")
def update_settings(body: UpdateSettings):
    fields = body.model_dump()
    # Cast bool → int for SQLite INTEGER column
    if fields.get("searxng_enabled") is not None:
        fields["searxng_enabled"] = int(fields["searxng_enabled"])
    # Clamp num_results to 1–10
    if fields.get("searxng_num_results") is not None:
        fields["searxng_num_results"] = max(1, min(int(fields["searxng_num_results"]), 10))
    return db.update_settings(**fields)


@api_router.get("/defaults", summary="Get built-in default prompt values")
def get_defaults():
    """Return the hardcoded default prompts so the frontend can pre-fill forms."""
    from prompts import DEFAULT_SYSTEM_PROMPT_RAG, DEFAULT_SYSTEM_PROMPT_WEB, DEFAULT_REWRITE_PROMPT
    return {
        "default_system_prompt_rag": DEFAULT_SYSTEM_PROMPT_RAG,
        "default_system_prompt_web": DEFAULT_SYSTEM_PROMPT_WEB,
        "default_rewrite_prompt": DEFAULT_REWRITE_PROMPT,
    }


# --- Workspace CRUD ----------------------------------------------------------

@api_router.get("/workspaces", summary="List all workspaces")
def list_workspaces():
    return db.list_workspaces()


@api_router.post("/workspace", summary="Create a new workspace")
def create_workspace(body: CreateWorkspace):
    ws = db.create_workspace(
        name=body.name,
        llm_model=body.llm_model or "",
        api_key=body.api_key or "",
        temperature=body.temperature if body.temperature is not None else 0.7,
        system_prompt=body.system_prompt or "",
        top_n=body.top_n if body.top_n is not None else 5,
        similarity_threshold=body.similarity_threshold if body.similarity_threshold is not None else 0.5,
        chunk_size=body.chunk_size if body.chunk_size is not None else 1024,
        chunk_overlap=body.chunk_overlap if body.chunk_overlap is not None else 104,
        embed_model=body.embed_model or "",
        embed_api_key=body.embed_api_key or "",
        max_tokens=body.max_tokens if body.max_tokens is not None else 1024,
        searxng_enabled=int(body.searxng_enabled) if body.searxng_enabled is not None else 0,
        searxng_num_results=min(int(body.searxng_num_results), 10) if body.searxng_num_results is not None else 3,
        searxng_query_suffix=body.searxng_query_suffix or "",
        rewrite_model=body.rewrite_model or "",
        rewrite_prompt=body.rewrite_prompt or "",
    )
    manager.on_workspace_created(slug=ws["slug"], name=ws["name"])
    return ws


@api_router.get("/workspace/{slug}", summary="Get workspace details")
def get_workspace(slug: str):
    ws = db.get_workspace(slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


@api_router.put("/workspace/{slug}", summary="Update a workspace")
def update_workspace(slug: str, body: UpdateWorkspace):
    if db.get_workspace(slug) is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    fields = body.model_dump()
    # Cast bool → int for SQLite INTEGER column
    if fields.get("searxng_enabled") is not None:
        fields["searxng_enabled"] = int(fields["searxng_enabled"])
    ws = db.update_workspace(slug, **fields)
    if body.name is not None:
        manager.on_workspace_renamed(slug=slug, new_name=body.name)
    return ws


def _drop_lancedb_table(slug: str) -> None:
    """Sync helper: drop the LanceDB table for a workspace if it exists.

    Acquires the per-workspace lock so a concurrent in-flight embed cannot
    write to the table while it is being dropped.
    """
    import lancedb as _lancedb
    ws_lock = embedding._get_workspace_lock(slug)
    with ws_lock:
        ldb = _lancedb.connect(config.LANCEDB_DIR)
        tname = embedding.table_name(slug)
        if tname in ldb.table_names():
            ldb.drop_table(tname)


@api_router.delete("/workspace/{slug}", summary="Delete a workspace")
async def delete_workspace(slug: str):
    import asyncio
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Drop the LanceDB table (blocking disk I/O — offload to thread)
    await asyncio.to_thread(_drop_lancedb_table, slug)

    # Remove doc tracking records (blocking file I/O — offload to thread)
    def _remove_docs():
        with _docs_lock:
            data = _read_docs()
            data.pop(slug, None)
            _write_docs(data)
    await asyncio.to_thread(_remove_docs)

    # Remove query log file for this workspace
    def _remove_log():
        with _logs_lock:
            p = _log_path(slug)
            if p.exists():
                p.unlink()
    await asyncio.to_thread(_remove_log)

    await asyncio.to_thread(db.delete_workspace, slug)
    manager.on_workspace_deleted(slug=slug)  # fire-and-forget background thread
    return {"status": "ok", "slug": slug}


# --- Embed -------------------------------------------------------------------
def _record_doc(slug: str, doc_id: str, filename: str, chunks: int) -> None:
    """Sync helper: append a doc record to docs.json under the lock."""
    from datetime import datetime, timezone
    with _docs_lock:
        data = _read_docs()
        if slug not in data:
            data[slug] = []
        data[slug].append({
            "doc_id": doc_id,
            "filename": filename,
            "chunks_embedded": chunks,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_docs(data)


@api_router.post("/workspace/{slug}/embed", summary="Upload and embed a file")
async def embed_file(slug: str, file: UploadFile = File(..., description="File to parse and embed. Supported types include PDF, DOCX, and plain text.")):
    import asyncio, io
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Read the upload bytes on the async side before handing off to a thread,
    # so the thread receives a plain BytesIO and never touches the async file object.
    file_bytes = await file.read()
    await file.close()

    filename = file.filename
    chunks, doc_id = await asyncio.to_thread(
        embedding.embed_workspace_file, slug, filename, io.BytesIO(file_bytes)
    )

    if chunks == 0:
        raise HTTPException(status_code=422, detail="No text could be extracted")

    # Record in docs.json (blocking file I/O — offload to thread)
    await asyncio.to_thread(_record_doc, slug, doc_id, filename, chunks)

    return {"status": "ok", "slug": slug, "filename": filename,
            "doc_id": doc_id, "chunks_embedded": chunks}


def _remove_doc_from_json(slug: str, doc_id: str) -> None:
    """Sync helper: remove a doc record from docs.json under the lock."""
    with _docs_lock:
        data = _read_docs()
        if slug in data:
            data[slug] = [d for d in data[slug] if d.get("doc_id") != doc_id]
        _write_docs(data)


@api_router.delete("/workspace/{slug}/embed/{doc_id:path}", summary="Delete an embedded file")
async def delete_embed(slug: str, doc_id: str):
    import asyncio
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # doc_id is always a server-generated UUID (see embedding.embed_workspace_file).
    # Reject anything else outright rather than letting it reach the LanceDB
    # filter expression — defense in depth against filter/query injection.
    try:
        uuid.UUID(doc_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Document not found")

    deleted = await asyncio.to_thread(embedding.delete_workspace_file, slug, doc_id)

    if deleted == 0:
        # Check if the doc was tracked in docs.json even if no vectors were found
        def _check_tracked():
            with _docs_lock:
                return _read_docs()
        data = await asyncio.to_thread(_check_tracked)
        tracked = any(d.get("doc_id") == doc_id for d in data.get(slug, []))
        if not tracked:
            raise HTTPException(status_code=404, detail="Document not found")

    # Keep docs.json in sync (blocking file I/O — offload to thread)
    await asyncio.to_thread(_remove_doc_from_json, slug, doc_id)

    return {"status": "ok", "slug": slug, "doc_id": doc_id, "chunks_deleted": deleted}


# --- Query -------------------------------------------------------------------
@api_router.post("/workspace/{slug}/query", summary="Query a workspace")
async def query_workspace(slug: str, body: QueryRequest):
    import asyncio, time as _time
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    start = _time.time()
    result = await asyncio.to_thread(query.query_workspace, ws, body.question)
    duration_ms = int((_time.time() - start) * 1000)
    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "question": body.question,
        "rewritten_query": result.get("rewritten_query"),
        "answer": result.get("answer", ""),
        "sources": result.get("sources", {"documents": [], "web": []}),
        "duration_ms": duration_ms,
    }
    await asyncio.to_thread(_append_log, slug, entry)
    return result


@api_router.post("/workspace/{slug}/query/stream", summary="Stream a query response")
async def stream_query_workspace(slug: str, body: QueryRequest):
    import asyncio
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    async def _logging_stream():
        async for chunk in query.stream_query_workspace(
            ws, body.question, prompt_suffix=body.prompt_suffix
        ):
            if chunk.startswith("event: log\n"):
                # Intercept the log event — write to disk, don't forward to browser
                try:
                    data_line = chunk.split("data: ", 1)[1].strip()
                    entry = json.loads(data_line)
                    await asyncio.to_thread(_append_log, slug, entry)
                except Exception:
                    pass
            else:
                yield chunk

    return StreamingResponse(
        _logging_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --- Chat sessions (persistent, context-aware) --------------------------------

import uuid as _uuid


class ChatMessageRecord(BaseModel):
    role: str
    content: str


class ChatSessionStreamRequest(BaseModel):
    message: str
    history: Optional[List[ChatMessageRecord]] = Field(
        default_factory=list,
        description="Recent conversation turns from the browser (last 6 used to re-seed context after server restart).",
    )
    retrieval_query: Optional[str] = Field(
        default=None,
        description=(
            "Optional short query used *only* for vector similarity retrieval. "
            "When omitted, `message` is used for retrieval. "
            "Use this to pass a concise retrieval query (e.g. document summary + question) "
            "while keeping large document context in `message` for the LLM only — "
            "preventing oversized embeddings from exceeding the embedding model's context window."
        ),
    )


@api_router.post("/workspace/{slug}/chat/session", summary="Create a new chat session")
async def create_chat_session(slug: str):
    """Returns a fresh session_id UUID. The client stores this and sends it
    back on subsequent /chat/{session_id}/stream requests."""
    import asyncio
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"session_id": str(_uuid.uuid4())}


@api_router.post("/workspace/{slug}/chat/{session_id}/stream", summary="Stream a chat response within a session")
async def stream_chat_session(slug: str, session_id: str, body: ChatSessionStreamRequest):
    """Send a message in an existing chat session and stream the response.

    The `history` field contains the last N conversation turns from the
    browser's localStorage. This is used to re-seed the LlamaIndex ChatEngine
    if the session is not in the in-process registry (e.g. after a server
    restart), ensuring follow-up questions always have context.
    """
    import asyncio
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    history = [m.model_dump() for m in (body.history or [])]

    async def _logging_stream():
        async for chunk in query.stream_chat_session(
            session_id, ws, body.message,
            history=history,
            retrieval_query=body.retrieval_query,
        ):
            if chunk.startswith("event: log\n"):
                # Intercept the log event — write to disk, don't forward to browser
                try:
                    data_line = chunk.split("data: ", 1)[1].strip()
                    entry = json.loads(data_line)
                    await asyncio.to_thread(_append_log, slug, entry)
                except Exception:
                    pass
            else:
                yield chunk

    return StreamingResponse(
        _logging_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@api_router.delete("/workspace/{slug}/chat/{session_id}", status_code=204, summary="Delete a chat session")
async def delete_chat_session(slug: str, session_id: str):
    """Remove the chat session from the in-process registry. The browser
    should also remove it from localStorage."""
    with query._chat_sessions_lock:
        query._chat_sessions.pop(session_id, None)
    return None


# --- Query log endpoints -----------------------------------------------------

@api_router.get("/workspace/{slug}/logs", summary="Get query log for a workspace")
def get_logs(slug: str):
    """Return all log entries for a workspace, newest first."""
    with _logs_lock:
        data = _read_log(slug)
    return list(reversed(data))


@api_router.delete("/workspace/{slug}/logs", status_code=204, summary="Clear query log for a workspace")
def clear_logs(slug: str):
    """Delete all log entries for a workspace."""
    with _logs_lock:
        p = _log_path(slug)
        if p.exists():
            p.unlink()
    return None


# --- Doc tracking (flat-file store, auth-protected) --------------------------

class DocRecord(BaseModel):
    doc_id: str
    filename: str
    chunks_embedded: Optional[int] = None
    uploaded_at: Optional[str] = None


@api_router.get("/docs/{slug}", summary="List tracked documents for a workspace")
def list_docs(slug: str):
    with _docs_lock:
        data = _read_docs()
    return data.get(slug, [])


@api_router.post("/docs/{slug}", status_code=201, summary="Track a document record")
def add_doc(slug: str, body: DocRecord):
    with _docs_lock:
        data = _read_docs()
        if slug not in data:
            data[slug] = []
        data[slug].append(body.model_dump())
        _write_docs(data)
    return {"ok": True}


@api_router.delete("/docs/{slug}/{doc_id}", summary="Remove a document record")
def remove_doc(slug: str, doc_id: str):
    with _docs_lock:
        data = _read_docs()
        if slug in data:
            data[slug] = [d for d in data[slug] if d.get("doc_id") != doc_id]
        _write_docs(data)
    return {"ok": True}


@api_router.delete("/docs/{slug}", summary="Remove all doc records for a workspace")
def remove_workspace_docs(slug: str):
    with _docs_lock:
        data = _read_docs()
        data.pop(slug, None)
        _write_docs(data)
    return {"ok": True}


app.include_router(api_router, prefix="/api")


# --- Static files (frontend) -------------------------------------------------
# A catch-all route serves index.html for all History API URLs so that
# direct loads and page refreshes work at any depth (/workspace/{slug}/query,
# /settings, etc.).  Real static files (app.js, styles.css …) are detected
# by checking whether the path resolves to an actual file in public/ first.
_PUBLIC_DIR = Path("public")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_catchall(full_path: str):
    """Serve static files from public/ or fall back to index.html (SPA).

    - /app.js, /styles.css, etc. → served as files
    - /workspaces, /workspace/{slug}/query, /settings, … → index.html
    """
    candidate = _PUBLIC_DIR / full_path
    # Resolve to prevent path traversal outside public/
    try:
        resolved = candidate.resolve()
        resolved.relative_to(_PUBLIC_DIR.resolve())
    except ValueError:
        return FileResponse(str(_PUBLIC_DIR / "index.html"))

    if resolved.is_file():
        # Prevent browsers from caching JS/CSS so deploys take effect immediately.
        no_cache_headers = {"Cache-Control": "no-store"} \
            if full_path.endswith((".js", ".css")) else {}
        return FileResponse(str(resolved), headers=no_cache_headers)
    # SPA fallback
    return FileResponse(str(_PUBLIC_DIR / "index.html"),
                        headers={"Cache-Control": "no-store"})


# --- Run ---------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
