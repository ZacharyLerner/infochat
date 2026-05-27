import json
import httpx
from typing import AsyncGenerator, Optional
from config import API_URL, HEADERS


def _translate_sse_lines(event_type: Optional[str], raw: str) -> Optional[str]:
    """Translate a single RhodyRAG SSE data line into the frontend's JSON format.

    RhodyRAG emits:  event: token|sources|done  /  data: <payload>
    Frontend expects: data: {"textResponse": "..."} or data: {"textResponse": "", "sources": [...]}

    Returns the translated SSE line string, or None if nothing should be emitted.
    """
    if event_type == "token":
        text = raw.replace("\\n", "\n")
        return f"data: {json.dumps({'textResponse': text})}\n\n"

    elif event_type == "sources":
        try:
            sources = json.loads(raw)
        except json.JSONDecodeError:
            sources = []
        mapped = [
            {
                "title": s.get("filename", "Source"),
                "url": "",
                "score": s.get("score"),
                "text": s.get("text", ""),
            }
            for s in sources
        ]
        return f"data: {json.dumps({'textResponse': '', 'sources': mapped})}\n\n"

    return None


async def _iter_rhodyrag_sse(response: httpx.Response) -> AsyncGenerator[str, None]:
    """Shared SSE-translation iterator for RhodyRAG streaming responses."""
    event_type = None
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            raw = line[len("data:"):]
            if raw.startswith(" "):
                raw = raw[1:]
            translated = _translate_sse_lines(event_type, raw)
            if translated:
                yield translated
        elif line == "":
            event_type = None


async def stream_chat(slug: str, message: str, session_id: str, reset: bool = False, followup_suffix: str = "") -> AsyncGenerator[str, None]:
    """Stream a one-off query response from RhodyRAG (stateless, no session memory).

    Translates RhodyRAG's named SSE event format into the data:{textResponse, sources}
    format the frontend expects.

    followup_suffix is passed as prompt_suffix to the RAG backend so it is
    appended to the LLM prompt only — never used for vector retrieval.
    """
    url = f"{API_URL}/workspace/{slug}/query/stream"
    payload: dict = {"question": message}
    if followup_suffix:
        payload["prompt_suffix"] = followup_suffix

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload, headers=HEADERS) as response:
            response.raise_for_status()
            async for line in _iter_rhodyrag_sse(response):
                yield line


async def stream_chat_session(
    slug: str,
    session_id: str,
    message: str,
    history: Optional[list] = None,
    followup_suffix: str = "",
) -> AsyncGenerator[str, None]:
    """Stream a chat response using a persistent ChatEngine session on RhodyRAG.

    Sends the last 6 turns of conversation history alongside the message so the
    backend can re-seed the LlamaIndex ChatEngine after a server restart.

    followup_suffix is appended to the LLM prompt only (not used for retrieval).
    """
    url = f"{API_URL}/workspace/{slug}/chat/{session_id}/stream"
    payload: dict = {
        "message": message,
        "history": (history or [])[-6:],
    }
    if followup_suffix:
        # Append suffix to message — the chat engine passes the full message to
        # the LLM, so this surfaces in the prompt without affecting retrieval.
        payload["message"] = message + followup_suffix

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload, headers=HEADERS) as response:
            response.raise_for_status()
            async for line in _iter_rhodyrag_sse(response):
                yield line
