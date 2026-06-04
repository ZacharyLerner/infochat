"""
searxng.py
==========
Thin async wrapper around the SearXNG JSON search API.

All errors are caught and logged — a search failure never propagates up to
the query pipeline. The caller always receives a list (possibly empty).
"""

import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

# Maximum time to wait for SearXNG to respond. Kept short so a slow or
# unavailable instance does not stall the entire query.
_TIMEOUT_SECONDS = 5.0

# Number of web results to fetch per query (hard ceiling applied server-side).
_DEFAULT_NUM_RESULTS = 3


async def web_search(query: str, num_results: int = _DEFAULT_NUM_RESULTS) -> list[dict]:
    """Query the SearXNG JSON API and return a list of result dicts.

    Each result dict has keys:
        title   (str) — page title
        url     (str) — canonical URL
        snippet (str) — short excerpt / description

    Returns an empty list on any error (network timeout, parse failure,
    SearXNG unavailable, JSON format not enabled, etc.).

    Args:
        query:       The search query string.
        num_results: Maximum number of results to return (default 3).
    """
    if not query or not query.strip():
        return []

    url = f"{config.SEARXNG_URL.rstrip('/')}/search"
    params: dict[str, Any] = {
        "q": query.strip(),
        "format": "json",
        "categories": "general",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        logger.warning("SearXNG request timed out after %.1fs (query: %r)", _TIMEOUT_SECONDS, query[:80])
        return []
    except httpx.HTTPStatusError as exc:
        logger.warning("SearXNG returned HTTP %s for query %r", exc.response.status_code, query[:80])
        return []
    except Exception as exc:
        logger.warning("SearXNG request failed: %s", exc)
        return []

    raw_results: list[dict] = data.get("results", [])
    if not isinstance(raw_results, list):
        logger.warning("SearXNG response missing 'results' list (query: %r)", query[:80])
        return []

    results = []
    for item in raw_results[:num_results]:
        if not isinstance(item, dict):
            continue
        title   = item.get("title") or ""
        url_val = item.get("url") or ""
        snippet = item.get("content") or item.get("snippet") or ""
        if url_val:
            results.append({"title": str(title), "url": str(url_val), "snippet": str(snippet)})

    return results
