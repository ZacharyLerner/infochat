"""
intake.py
=========
Self-service chatbot requests, for the opencode "request a chatbot" skill.

    POST /api/intake/chatbot               create a bot, return its links
    GET  /api/intake/chatbot/{slug}        the links again
    POST /api/intake/chatbot/{slug}/test   ask the bot a question (not logged)

These routes need NO admin key: the service is for campus-network use only, and
the requester's opencode has no way to hold a secret. They are deliberately
narrow so that is safe enough:

  - the caller picks only the name, prompt, welcome text, starter questions,
    follow-ups and web search; models and the gateway key come from this
    server's config (INTAKE_* in config.py, or the global settings);
  - /test and the GET only work for bots created here (they have an owner record),
    never for other workspaces;
  - the owner's username and email are stored in workspaces/<slug>/owner.json and
    are never returned by any public route (see GET /api/owners, admin key).

Files and websites are added afterwards through the uploader page, which is also
how the owner edits the bot later.
"""

import asyncio
import datetime
import logging
import re
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

import config
import db
import embedding
import manager
import query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/intake", tags=["Intake"])

# Low temperature keeps answers close to the loaded documents.
INTAKE_TEMPERATURE = 0.3
WEB_SEARCH_SUFFIX = "site:uri.edu"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Limits mirror universal-chat's schema (welcome 300, category 40, question 120)
# and the uploader's (name 100), so a bot is never accepted here and refused there.
Question = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class StarterGroup(BaseModel):
    category: str = Field(min_length=1, max_length=40, description="Label shown above the question chips.")
    questions: list[Question] = Field(default_factory=list, max_length=10)


class ChatbotRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=64, description="Requester's URI username.")
    email: str = Field(max_length=254, description="Requester's email, for contact about the bot.")
    name: str = Field(min_length=1, max_length=100, description="Display name of the bot.")
    purpose: str = Field(default="", max_length=1000, description="Who it is for and what it is for. Recorded with the owner.")
    system_prompt: str = Field(default="", max_length=4000, description="Blank uses the built-in document-grounded prompt.")
    welcome_text: str = Field(default="Send a message to get started.", max_length=300)
    starter_questions: list[StarterGroup] = Field(default_factory=list, max_length=6)
    followups: bool = Field(default=False, description="Suggest follow-up questions after each answer.")
    web_search: bool = Field(default=False, description=f"Also search the web ({WEB_SEARCH_SUFFIX}) when the documents don't answer.")

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError("not a valid email address")
        return v


class TestRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


def links(slug: str) -> dict:
    """Everything a requester needs: the chat page, the default embed snippet, and the upload page."""
    chat, upload = config.PUBLIC_CHAT_URL, config.PUBLIC_UPLOAD_URL
    return {
        "slug": slug,
        "chat_page": f"{chat}/{slug}",
        "embed_script_url": f"{chat}/embed.js",
        "embed_snippet": f'<script\n  src="{chat}/embed.js"\n  data-workspace="{slug}"\n></script>',
        "uploader_page": f"{upload}/{slug}",
        "uploader_settings_page": f"{upload}/{slug}/settings",
        "chat_settings_page": f"{chat}/settings?slug={slug}",
    }


def _rollback(slug: str) -> None:
    """Undo a half-made bot, ignoring each step's failure so the others still run."""
    for step, args in ((manager.unregister_downstream, (slug,)), (embedding.delete_workspace_index, (slug,)),
                       (db.delete_workspace, (slug,))):
        try:
            step(*args)
        except Exception as exc:
            logger.warning("intake rollback: %s(%s) failed: %s", step.__name__, slug, exc)


def _create(body: ChatbotRequest) -> dict:
    defaults = db.get_settings()
    llm_model = config.INTAKE_LLM_MODEL or defaults["llm_model"]
    embed_model = config.INTAKE_EMBED_MODEL or defaults["embed_model"]
    api_key = config.INTAKE_API_KEY or defaults["api_key"]
    if not (llm_model and embed_model and api_key):
        raise HTTPException(status_code=503, detail="Chatbot intake is not configured yet: an administrator "
                            "must set INTAKE_LLM_MODEL, INTAKE_EMBED_MODEL and INTAKE_API_KEY (or the global settings).")

    ws = db.create_workspace(
        name=body.name, llm_model=llm_model, api_key=api_key, temperature=INTAKE_TEMPERATURE,
        system_prompt=body.system_prompt, embed_model=embed_model, embed_api_key=defaults["embed_api_key"],
        searxng_enabled=int(body.web_search), searxng_query_suffix=WEB_SEARCH_SUFFIX if body.web_search else "",
    )
    slug = ws["slug"]

    try:
        embedding.create_workspace_index(ws)
    except Exception as exc:
        db.delete_workspace(slug)
        raise HTTPException(status_code=502, detail=f"Could not create the vector index: {exc}")

    try:
        db.set_owner(slug, {
            "username": body.username, "email": body.email, "name": body.name, "purpose": body.purpose,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": "intake",
        })
        # Chat and the uploader must know the slug, or both pages 404. Wait for them and undo on failure.
        registered = manager.register_downstream(slug, body.name, {
            "welcome_text": body.welcome_text,
            "followup_enabled": body.followups, "followup_count": 3,
            "default_questions": [g.model_dump() for g in body.starter_questions],
        })
    except Exception as exc:
        _rollback(slug)
        raise HTTPException(status_code=502, detail=f"Could not set up the chatbot: {exc}")
    if not all(registered.values()):
        _rollback(slug)
        down = ", ".join(k for k, ok in registered.items() if not ok)
        raise HTTPException(status_code=502, detail=f"The chatbot was not created: could not reach {down}. Try again shortly.")

    logger.info("intake: created '%s' (%s) for %s", body.name, slug, body.username)
    return {"name": body.name, **links(slug)}


def _intake_workspace(slug: str) -> dict:
    """The workspace, but only if it was created through intake."""
    ws = db.get_workspace(slug)
    if ws is None or db.get_owner(slug) is None:
        raise HTTPException(status_code=404, detail="Chatbot not found")
    return ws


@router.post("/chatbot", summary="Request a new chatbot")
async def request_chatbot(body: ChatbotRequest):
    return await asyncio.to_thread(_create, body)


@router.get("/chatbot/{slug}", summary="Get a requested chatbot's links")
async def get_chatbot(slug: str):
    ws = await asyncio.to_thread(_intake_workspace, slug)
    return {"name": ws["name"], **links(slug)}


@router.post("/chatbot/{slug}/test", summary="Ask a requested chatbot a question")
async def test_chatbot(slug: str, body: TestRequest):
    ws = await asyncio.to_thread(_intake_workspace, slug)
    result = await asyncio.to_thread(query.query_workspace, ws, body.question)
    docs = (result.get("sources") or {}).get("documents", [])
    return {
        "answer": result.get("answer", ""),
        "cited_sources": [d.get("title") or d.get("uri") for d in docs if d.get("cited")],
    }


def list_owners() -> list[dict]:
    """Owner records of every intake-created bot, newest first. For the admin-keyed route in main.py."""
    def one(slug: str) -> Optional[dict]:
        owner = db.get_owner(slug)
        return {"slug": slug, **owner} if owner else None

    from concurrent.futures import ThreadPoolExecutor
    slugs = db._list_slugs()
    if not slugs:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(slugs))) as pool:
        owners = [o for o in pool.map(one, slugs) if o]
    return sorted(owners, key=lambda o: o.get("created_at", ""), reverse=True)
