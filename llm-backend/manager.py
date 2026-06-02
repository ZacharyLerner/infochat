"""
manager.py
==========
Propagates workspace lifecycle events (create, rename, delete) from this RAG
backend to the two downstream services:

  - File server   (FILE_SERVER_URL, default http://10.140.2.31:3001)
      POST   /api/v1/workspaces/new          create
      PATCH  /api/v1/workspaces/{id}         rename
      DELETE /api/v1/workspaces/{id}         delete

  - Chat frontend (CHAT_SERVER_URL, default http://10.140.2.30:3000)
      POST   /api/workspaces                 create
      PUT    /api/workspaces/{slug}          rename
      DELETE /api/workspaces/{slug}          delete

Both downstream servers may optionally require an API key header.
Set FILE_SERVER_API_KEY / CHAT_SERVER_API_KEY in .env if needed.

All calls are fire-and-forget with a short timeout; failures are logged but
never raise — they must not block or roll back the primary operation.
"""

import logging
import threading
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Downstream targets (resolved once at import time from config)
# ---------------------------------------------------------------------------

_FILE_BASE = config.FILE_SERVER_URL.rstrip("/")
_CHAT_BASE = config.CHAT_SERVER_URL.rstrip("/")

_FILE_HEADERS: dict[str, str] = {"Content-Type": "application/json"}
_CHAT_HEADERS: dict[str, str] = {"Content-Type": "application/json"}

if config.FILE_SERVER_API_KEY:
    _FILE_HEADERS["X-API-Key"] = config.FILE_SERVER_API_KEY
if config.CHAT_SERVER_API_KEY:
    _CHAT_HEADERS["X-API-Key"] = config.CHAT_SERVER_API_KEY

_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _post(base: str, path: str, headers: dict, payload: dict) -> Optional[dict]:
    url = f"{base}{path}"
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as exc:
        logger.warning("manager POST %s failed: %s", url, exc)
        return None


def _put(base: str, path: str, headers: dict, payload: dict) -> Optional[dict]:
    url = f"{base}{path}"
    try:
        r = httpx.put(url, json=payload, headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as exc:
        logger.warning("manager PUT %s failed: %s", url, exc)
        return None


def _patch(base: str, path: str, headers: dict, payload: dict) -> Optional[dict]:
    url = f"{base}{path}"
    try:
        r = httpx.patch(url, json=payload, headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as exc:
        logger.warning("manager PATCH %s failed: %s", url, exc)
        return None


def _delete(base: str, path: str, headers: dict) -> bool:
    url = f"{base}{path}"
    try:
        r = httpx.delete(url, headers=headers, timeout=_TIMEOUT)
        # 404 is acceptable — the workspace may never have been created there
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("manager DELETE %s failed: %s", url, exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _run_in_background(fn, *args, **kwargs) -> None:
    """Fire-and-forget: run fn in a daemon thread so callers return immediately."""
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()


def _do_create(slug: str, name: str) -> None:
    # --- File server ---------------------------------------------------------
    result = _post(
        _FILE_BASE,
        "/api/v1/workspaces/db",
        _FILE_HEADERS,
        {"id": slug, "name": name},
    )
    if result is None:
        logger.error("manager: failed to create workspace '%s' on file server (%s)", slug, _FILE_BASE)
    else:
        logger.info("manager: workspace '%s' created on file server", slug)

    # --- Chat server ---------------------------------------------------------
    result = _post(
        _CHAT_BASE,
        "/api/workspaces",
        _CHAT_HEADERS,
        {"slug": slug, "name": name, "followup_enabled": True, "followup_count": 3},
    )
    if result is None:
        logger.error("manager: failed to create workspace '%s' on chat server (%s)", slug, _CHAT_BASE)
    else:
        logger.info("manager: workspace '%s' created on chat server", slug)


def _do_rename(slug: str, new_name: str) -> None:
    # --- File server ---------------------------------------------------------
    file_result = _patch(
        _FILE_BASE,
        f"/api/v1/workspaces/{slug}",
        _FILE_HEADERS,
        {"name": new_name},
    )
    if file_result is None:
        logger.error("manager: failed to rename workspace '%s' on file server (%s)", slug, _FILE_BASE)
    else:
        logger.info("manager: workspace '%s' renamed to '%s' on file server", slug, new_name)

    # --- Chat server ---------------------------------------------------------
    result = _put(
        _CHAT_BASE,
        f"/api/workspaces/{slug}",
        _CHAT_HEADERS,
        {"name": new_name},
    )
    if result is None:
        logger.error("manager: failed to rename workspace '%s' on chat server (%s)", slug, _CHAT_BASE)
    else:
        logger.info("manager: workspace '%s' renamed to '%s' on chat server", slug, new_name)


def _do_delete(slug: str) -> None:
    # --- File server ---------------------------------------------------------
    ok = _delete(_FILE_BASE, f"/api/v1/workspaces/{slug}", _FILE_HEADERS)
    if not ok:
        logger.error("manager: failed to delete workspace '%s' on file server (%s)", slug, _FILE_BASE)
    else:
        logger.info("manager: workspace '%s' deleted on file server", slug)

    # --- Chat server ---------------------------------------------------------
    ok = _delete(_CHAT_BASE, f"/api/workspaces/{slug}", _CHAT_HEADERS)
    if not ok:
        logger.error("manager: failed to delete workspace '%s' on chat server (%s)", slug, _CHAT_BASE)
    else:
        logger.info("manager: workspace '%s' deleted on chat server", slug)


def on_workspace_created(slug: str, name: str) -> None:
    """
    Called after a workspace is successfully created on this server.
    Propagates to file server and chat server in a background thread.
    """
    _run_in_background(_do_create, slug, name)


def on_workspace_renamed(slug: str, new_name: str) -> None:
    """
    Called after a workspace name is successfully updated on this server.
    Propagates to file server and chat server in a background thread.
    """
    _run_in_background(_do_rename, slug, new_name)


def on_workspace_deleted(slug: str) -> None:
    """
    Called after a workspace is successfully deleted on this server.
    Propagates to file server and chat server in a background thread.
    """
    _run_in_background(_do_delete, slug)
    if result is None:
        logger.error(
            "manager: failed to create workspace '%s' on file server (%s)",
            slug, _FILE_BASE,
        )
    else:
        logger.info(
            "manager: workspace '%s' created on file server", slug
        )

    # --- Chat server ---------------------------------------------------------
    result = _post(
        _CHAT_BASE,
        "/api/workspaces",
        _CHAT_HEADERS,
        {"slug": slug, "name": name, "followup_enabled": True, "followup_count": 3},
    )
    if result is None:
        logger.error(
            "manager: failed to create workspace '%s' on chat server (%s)",
            slug, _CHAT_BASE,
        )
    else:
        logger.info(
            "manager: workspace '%s' created on chat server", slug
        )


def on_workspace_renamed(slug: str, new_name: str) -> None:
    """
    Called after a workspace name is successfully updated on this server.

    Propagates to:
      - File server:  PATCH /api/v1/workspaces/{slug}  { name }
      - Chat server:  PUT   /api/workspaces/{slug}      { name }
    """
    # --- File server ---------------------------------------------------------
    file_result = _patch(
        _FILE_BASE,
        f"/api/v1/workspaces/{slug}",
        _FILE_HEADERS,
        {"name": new_name},
    )
    if file_result is None:
        logger.error(
            "manager: failed to rename workspace '%s' on file server (%s)",
            slug, _FILE_BASE,
        )
    else:
        logger.info(
            "manager: workspace '%s' renamed to '%s' on file server",
            slug, new_name,
        )

    # --- Chat server ---------------------------------------------------------
    result = _put(
        _CHAT_BASE,
        f"/api/workspaces/{slug}",
        _CHAT_HEADERS,
        {"name": new_name},
    )
    if result is None:
        logger.error(
            "manager: failed to rename workspace '%s' on chat server (%s)",
            slug, _CHAT_BASE,
        )
    else:
        logger.info(
            "manager: workspace '%s' renamed to '%s' on chat server",
            slug, new_name,
        )


def on_workspace_deleted(slug: str) -> None:
    """
    Called after a workspace is successfully deleted on this server.

    Propagates to:
      - File server:  DELETE /api/v1/workspaces/{id}
      - Chat server:  DELETE /api/workspaces/{slug}
    """
    # --- File server ---------------------------------------------------------
    ok = _delete(_FILE_BASE, f"/api/v1/workspaces/{slug}", _FILE_HEADERS)
    if not ok:
        logger.error(
            "manager: failed to delete workspace '%s' on file server (%s)",
            slug, _FILE_BASE,
        )
    else:
        logger.info(
            "manager: workspace '%s' deleted on file server", slug
        )

    # --- Chat server ---------------------------------------------------------
    ok = _delete(_CHAT_BASE, f"/api/workspaces/{slug}", _CHAT_HEADERS)
    if not ok:
        logger.error(
            "manager: failed to delete workspace '%s' on chat server (%s)",
            slug, _CHAT_BASE,
        )
    else:
        logger.info(
            "manager: workspace '%s' deleted on chat server", slug
        )
