from fastapi import FastAPI, Request, Depends, HTTPException, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import os
import smtplib
import tempfile
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anythingllm
from models import ChatRequest, ChatSessionRequest, SendEmailRequest
from db import engine, get_db
import orm_models
from schemas import WorkspaceCreate, WorkspaceUpdate, WorkspaceResponse
from config import LLM_GW_URL, LLM_GW_KEY
import file_processor

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Quieten noisy third-party loggers to WARNING so our DEBUG output is readable
for _noisy in ("httpx", "httpcore", "uvicorn.access", "sqlalchemy.engine"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Create tables on startup
orm_models.Base.metadata.create_all(bind=engine)

# Existing non-documented routes use include_in_schema=False
# New workspace management routes are visible in docs
app = FastAPI(title="Universal Chat API")

# Allow any origin to load the embed script and call the chat API.
# Restrict origins in production by replacing allow_origins=["*"].
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Request timing middleware — logs every API call and its duration
# ---------------------------------------------------------------------------

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    logger.debug("→ %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        logger.error("✗ %s %s — unhandled exception after %.1fms: %s",
                     request.method, request.url.path, elapsed, exc, exc_info=True)
        raise
    elapsed = (time.perf_counter() - start) * 1000
    level = logging.WARNING if response.status_code >= 400 else logging.DEBUG
    logger.log(level, "← %s %s %d (%.1fms)",
               request.method, request.url.path, response.status_code, elapsed)
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error("422 Validation error on %s %s: %s", request.method, request.url.path, exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.on_event("startup")
async def _startup():
    logger.info("=" * 60)
    logger.info("Universal Chat API starting up")
    logger.info("  LLM_GW_URL : %s", LLM_GW_URL)
    logger.info("  LLM_GW_KEY : %s", "SET" if LLM_GW_KEY else "NOT SET (file upload disabled)")
    from config import API_URL
    logger.info("  RAG_API_URL: %s", API_URL)
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Embeddable widget script (served with permissive CORS so external sites
# can load it via a plain <script src="..."> tag)
# ---------------------------------------------------------------------------

@app.get("/embed.js", include_in_schema=False)
async def embed_script():
    path = os.path.join("static", "js", "embed.js")
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=300",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------------------------------------------------------------------------
# Page routes (hidden from docs)
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request, slug: str = ""):
    return templates.TemplateResponse(request, "settings.html", {"slug": slug})


@app.get("/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def chat_page(request: Request, slug: str, db: Session = Depends(get_db)):
    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        return templates.TemplateResponse(
            request,
            "workspace_not_found.html",
            {"slug": slug},
            status_code=404,
        )
    return templates.TemplateResponse(request, "home.html", {"slug": slug, "name": ws.name or slug})


# ---------------------------------------------------------------------------
# Chat proxy (hidden from docs)
# ---------------------------------------------------------------------------

@app.post("/api/chat/{slug}", include_in_schema=False)
async def chat_endpoint(slug: str, body: ChatRequest, db: Session = Depends(get_db)):
    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{slug}' not found.")
    email_suffix = anythingllm.EMAIL_PROMPT_SUFFIX if ws.email_enabled else ""
    return StreamingResponse(
        anythingllm.stream_chat(
            slug=slug,
            message=body.message,
            session_id=body.session_id,
            reset=body.reset,
            followup_suffix=body.followup_suffix,
            email_suffix=email_suffix,
        ),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# Persistent chat session endpoints
# ---------------------------------------------------------------------------

@app.post("/api/chat/{slug}/session", include_in_schema=False)
async def create_chat_session(slug: str, db: Session = Depends(get_db)):
    """Create a new persistent chat session, proxying to RhodyRAG.

    Returns {"session_id": "<uuid>"} which the browser stores in localStorage
    and sends back on every subsequent stream request.
    """
    import httpx as _httpx
    from config import API_URL, HEADERS

    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        logger.warning("create_chat_session: workspace '%s' not found", slug)
        raise HTTPException(status_code=404, detail=f"Workspace '{slug}' not found.")

    logger.debug("create_chat_session: slug=%s → POST %s/workspace/%s/chat/session", slug, API_URL, slug)
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{API_URL}/api/workspace/{slug}/chat/session",
                headers=HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.debug("create_chat_session: got session_id=%s", data.get("session_id"))
            return data
    except Exception as exc:
        logger.error("create_chat_session: failed for slug=%s: %s", slug, exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to create session: {exc}")


@app.post("/api/chat/{slug}/{session_id}/stream", include_in_schema=False)
async def stream_chat_session(slug: str, session_id: str, body: ChatSessionRequest, db: Session = Depends(get_db)):
    """Stream a response in a persistent chat session.

    Sends the conversation history to RhodyRAG so the ChatEngine can be
    re-seeded after a server restart (last 6 turns are used).
    """
    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        logger.warning("stream_chat_session: workspace '%s' not found", slug)
        raise HTTPException(status_code=404, detail=f"Workspace '{slug}' not found.")

    history = [m.model_dump() for m in (body.history or [])]
    has_file = body.file_context is not None
    logger.debug(
        "stream_chat_session: slug=%s session=%s message_len=%d history_turns=%d "
        "followup=%s file_attached=%s%s",
        slug, session_id, len(body.message), len(history),
        bool(body.followup_suffix), has_file,
        f" file='{body.file_context.filename}'" if has_file else "",
    )

    email_suffix = anythingllm.EMAIL_PROMPT_SUFFIX if ws.email_enabled else ""
    return StreamingResponse(
        anythingllm.stream_chat_session(
            slug=slug,
            session_id=session_id,
            message=body.message,
            history=history,
            followup_suffix=body.followup_suffix,
            email_suffix=email_suffix,
            file_context=body.file_context.model_dump() if body.file_context else None,
        ),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# File upload — process a document or image into Markdown + summary
# ---------------------------------------------------------------------------

@app.post("/api/upload/{slug}", include_in_schema=False)
async def upload_file(slug: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Accept a file upload, run the two-stage processing pipeline, and return
    the resulting Markdown and one-sentence summary.

    The processed content is returned to the browser and stored client-side;
    nothing is persisted on the server after the request completes.
    """
    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        logger.warning("upload_file: workspace '%s' not found", slug)
        raise HTTPException(status_code=404, detail=f"Workspace '{slug}' not found.")

    if not LLM_GW_KEY:
        logger.error("upload_file: LLM_GW_KEY not set — file upload disabled")
        raise HTTPException(
            status_code=503,
            detail="File upload is not configured. Set LLM_GW_KEY in your .env file.",
        )

    original_name = file.filename or "upload"
    _, ext = os.path.splitext(original_name)
    logger.info("upload_file: received '%s' (content_type=%s) for workspace '%s'",
                original_name, file.content_type, slug)

    tmp_path: str | None = None
    t_start = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp_path = tmp.name
            content = await file.read()
            tmp.write(content)
        file_size_kb = len(content) / 1024
        logger.debug("upload_file: saved to tmp=%s (%.1f KB)", tmp_path, file_size_kb)

        result = file_processor.process_upload(
            tmp_path=tmp_path,
            original_filename=original_name,
            gw_url=LLM_GW_URL,
            api_key=LLM_GW_KEY,
        )

        elapsed = (time.perf_counter() - t_start) * 1000
        md_chars = len(result.get("markdown", ""))
        summary_chars = len(result.get("summary", ""))
        logger.info(
            "upload_file: completed '%s' in %.0fms — markdown=%d chars, summary=%d chars",
            original_name, elapsed, md_chars, summary_chars,
        )
        logger.debug("upload_file: summary text: %s", result.get("summary", ""))
        return JSONResponse(result)

    except ValueError as exc:
        logger.error("upload_file: configuration error for '%s': %s", original_name, exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        elapsed = (time.perf_counter() - t_start) * 1000
        logger.exception(
            "upload_file: processing failed for '%s' after %.0fms: %s",
            original_name, elapsed, exc,
        )
        raise HTTPException(status_code=500, detail=f"File processing failed: {exc}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
            logger.debug("upload_file: deleted tmp file %s", tmp_path)


# ---------------------------------------------------------------------------
# Send email — called by the frontend after the user confirms an email action
# ---------------------------------------------------------------------------

SMTP_HOST = "smtpserv.uri.edu"
SMTP_PORT = 587

@app.post("/api/send-email", include_in_schema=False)
async def send_email_endpoint(body: SendEmailRequest):
    """Send an email via the URI SMTP relay.

    Called by the chat frontend after the user reviews and confirms an email
    that was drafted by the LLM.  No authentication is required; the server
    must be reachable on the URI network / VPN.
    """
    recipients = [r.strip() for r in body.to.split(",") if r.strip()]
    if not recipients:
        raise HTTPException(status_code=400, detail="No valid recipient address provided.")

    logger.info(
        "send_email_endpoint: to=%s subject='%s' from=%s html=%s",
        recipients, body.subject, body.from_addr, body.html,
    )

    msg = MIMEMultipart()
    msg["From"] = body.from_addr
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = body.subject
    content_type = "html" if body.html else "plain"
    msg.attach(MIMEText(body.body, content_type))

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.sendmail(body.from_addr, recipients, msg.as_string())
        server.quit()
        logger.info("send_email_endpoint: sent successfully to %s", recipients)
        return JSONResponse({"ok": True, "message": f"Email sent to {', '.join(recipients)}"})
    except smtplib.SMTPException as exc:
        logger.error("send_email_endpoint: SMTP error: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"SMTP error: {exc}")
    except OSError as exc:
        logger.error("send_email_endpoint: connection error (VPN?): %s", exc, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Could not connect to the mail server. Make sure you are on the URI network or VPN.",
        )


# ---------------------------------------------------------------------------
# Workspace settings API (visible in docs)
# ---------------------------------------------------------------------------

@app.post(
    "/api/workspaces",
    response_model=WorkspaceResponse,
    status_code=201,
    summary="Create a workspace",
    tags=["Workspaces"],
)
def create_workspace(body: WorkspaceCreate, db: Session = Depends(get_db)):
    existing = db.get(orm_models.Workspace, body.slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Workspace '{body.slug}' already exists.")
    ws = orm_models.Workspace(
        slug=body.slug,
        name=body.name,
        welcome_text=body.welcome_text,
        followup_enabled=body.followup_enabled,
        followup_count=body.followup_count,
        email_enabled=body.email_enabled,
        default_questions=[c.model_dump() for c in body.default_questions],
    )
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


@app.get(
    "/api/workspaces",
    response_model=list[WorkspaceResponse],
    summary="List all workspaces",
    tags=["Workspaces"],
)
def list_workspaces(db: Session = Depends(get_db)):
    return db.query(orm_models.Workspace).all()


@app.get(
    "/api/workspaces/{slug}",
    response_model=WorkspaceResponse,
    summary="Get workspace settings",
    tags=["Workspaces"],
)
def get_workspace(slug: str, db: Session = Depends(get_db)):
    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{slug}' not found.")
    return ws


@app.put(
    "/api/workspaces/{slug}",
    response_model=WorkspaceResponse,
    summary="Update workspace settings",
    tags=["Workspaces"],
)
def update_workspace(slug: str, body: WorkspaceUpdate, db: Session = Depends(get_db)):
    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{slug}' not found.")
    ws.name = body.name
    ws.welcome_text = body.welcome_text
    ws.followup_enabled = body.followup_enabled
    ws.followup_count = body.followup_count
    ws.email_enabled = body.email_enabled
    ws.default_questions = [c.model_dump() for c in body.default_questions]
    db.commit()
    db.refresh(ws)
    return ws


@app.delete(
    "/api/workspaces/{slug}",
    status_code=204,
    summary="Delete a workspace",
    tags=["Workspaces"],
)
def delete_workspace(slug: str, db: Session = Depends(get_db)):
    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{slug}' not found.")
    db.delete(ws)
    db.commit()
