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

import json
import os
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Security, Depends
from fastapi.responses import StreamingResponse
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


def _read_docs() -> Dict[str, Any]:
    if DOCS_FILE.exists():
        return json.loads(DOCS_FILE.read_text())
    return {}


def _write_docs(data: Dict[str, Any]):
    """Atomically write docs.json using a temp file + rename to avoid corruption."""
    text = json.dumps(data, indent=2)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=DOCS_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, DOCS_FILE)
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
    dependencies=[Depends(verify_admin_key)],
)


# --- Auth verification endpoint ----------------------------------------------
@app.get("/auth/verify", summary="Verify API key")
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


@app.get("/settings", summary="Get global settings")
def get_settings():
    return db.get_settings()


@app.put("/settings", summary="Update global settings")
def update_settings(body: UpdateSettings):
    return db.update_settings(**body.model_dump())


# --- Workspace CRUD ----------------------------------------------------------

@app.get("/workspaces", summary="List all workspaces")
def list_workspaces():
    return db.list_workspaces()


@app.post("/workspace", summary="Create a new workspace")
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
    )
    manager.on_workspace_created(slug=ws["slug"], name=ws["name"])
    return ws


@app.get("/workspace/{slug}", summary="Get workspace details")
def get_workspace(slug: str):
    ws = db.get_workspace(slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


@app.put("/workspace/{slug}", summary="Update a workspace")
def update_workspace(slug: str, body: UpdateWorkspace):
    if db.get_workspace(slug) is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    ws = db.update_workspace(slug, **body.model_dump())
    if body.name is not None:
        manager.on_workspace_renamed(slug=slug, new_name=body.name)
    return ws


def _drop_lancedb_table(slug: str) -> None:
    """Sync helper: drop the LanceDB table for a workspace if it exists."""
    import lancedb as _lancedb
    ldb = _lancedb.connect(config.LANCEDB_DIR)
    tname = embedding.table_name(slug)
    if tname in ldb.table_names():
        ldb.drop_table(tname)


@app.delete("/workspace/{slug}", summary="Delete a workspace")
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


@app.post("/workspace/{slug}/embed", summary="Upload and embed a file")
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


@app.delete("/workspace/{slug}/embed/{doc_id:path}", summary="Delete an embedded file")
async def delete_embed(slug: str, doc_id: str):
    import asyncio
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

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
@app.post("/workspace/{slug}/query", summary="Query a workspace")
async def query_workspace(slug: str, body: QueryRequest):
    import asyncio
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return await asyncio.to_thread(query.query_workspace, ws, body.question)


@app.post("/workspace/{slug}/query/stream", summary="Stream a query response")
async def stream_query_workspace(slug: str, body: QueryRequest):
    import asyncio
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return StreamingResponse(
        query.stream_query_workspace(ws, body.question, prompt_suffix=body.prompt_suffix),
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


@app.post("/workspace/{slug}/chat/session", summary="Create a new chat session")
async def create_chat_session(slug: str):
    """Returns a fresh session_id UUID. The client stores this and sends it
    back on subsequent /chat/{session_id}/stream requests."""
    import asyncio
    ws = await asyncio.to_thread(db.get_workspace, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"session_id": str(_uuid.uuid4())}


@app.post("/workspace/{slug}/chat/{session_id}/stream", summary="Stream a chat response within a session")
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
    return StreamingResponse(
        query.stream_chat_session(
            session_id, ws, body.message,
            history=history,
            retrieval_query=body.retrieval_query,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/workspace/{slug}/chat/{session_id}", status_code=204, summary="Delete a chat session")
async def delete_chat_session(slug: str, session_id: str):
    """Remove the chat session from the in-process registry. The browser
    should also remove it from localStorage."""
    query._chat_sessions.pop(session_id, None)
    return None


# --- Doc tracking (flat-file store, auth-protected) --------------------------

class DocRecord(BaseModel):
    doc_id: str
    filename: str
    chunks_embedded: Optional[int] = None
    uploaded_at: Optional[str] = None


@app.get("/docs/{slug}", summary="List tracked documents for a workspace")
def list_docs(slug: str):
    data = _read_docs()
    return data.get(slug, [])


@app.post("/docs/{slug}", status_code=201, summary="Track a document record")
def add_doc(slug: str, body: DocRecord):
    with _docs_lock:
        data = _read_docs()
        if slug not in data:
            data[slug] = []
        data[slug].append(body.model_dump())
        _write_docs(data)
    return {"ok": True}


@app.delete("/docs/{slug}/{doc_id}", summary="Remove a document record")
def remove_doc(slug: str, doc_id: str):
    with _docs_lock:
        data = _read_docs()
        if slug in data:
            data[slug] = [d for d in data[slug] if d.get("doc_id") != doc_id]
        _write_docs(data)
    return {"ok": True}


@app.delete("/docs/{slug}", summary="Remove all doc records for a workspace")
def remove_workspace_docs(slug: str):
    with _docs_lock:
        data = _read_docs()
        data.pop(slug, None)
        _write_docs(data)
    return {"ok": True}


# --- Static files (frontend) -------------------------------------------------
app.mount("/", StaticFiles(directory="public", html=True), name="static")


# --- Run ---------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
