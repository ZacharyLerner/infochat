from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import os

import anythingllm
from models import ChatRequest
from db import engine, get_db
import orm_models
from schemas import WorkspaceCreate, WorkspaceUpdate, WorkspaceResponse

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
    return templates.TemplateResponse("settings.html", {"request": request, "slug": slug})


@app.get("/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def chat_page(request: Request, slug: str, db: Session = Depends(get_db)):
    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        return templates.TemplateResponse(
            "workspace_not_found.html",
            {"request": request, "slug": slug},
            status_code=404,
        )
    return templates.TemplateResponse("home.html", {"request": request, "slug": slug, "name": ws.name or slug})


# ---------------------------------------------------------------------------
# Chat proxy (hidden from docs)
# ---------------------------------------------------------------------------

@app.post("/api/chat/{slug}", include_in_schema=False)
async def chat_endpoint(slug: str, body: ChatRequest, db: Session = Depends(get_db)):
    ws = db.get(orm_models.Workspace, slug)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{slug}' not found.")
    return StreamingResponse(
        anythingllm.stream_chat(
            slug=slug,
            message=body.message,
            session_id=body.session_id,
            reset=body.reset,
            followup_suffix=body.followup_suffix,
        ),
        media_type="text/event-stream",
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
