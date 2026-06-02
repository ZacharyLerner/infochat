import json
import logging
import time
import httpx
from typing import AsyncGenerator, Optional
from config import API_URL, HEADERS

logger = logging.getLogger(__name__)

# Maximum number of characters of document markdown injected into the LLM
# message. This goes to the LLM only — the embedding model never sees it
# (we send retrieval_query separately for vector search). The LLM context
# window is 128k tokens, so 60 000 chars ≈ 15 000 tokens is safe while
# leaving room for system prompt, RAG chunks, history, and the output budget.
MAX_FILE_CONTEXT_CHARS = 60_000


def _translate_sse_lines(event_type: Optional[str], raw: str) -> Optional[str]:
    """Translate a single RhodyRAG SSE data line into the frontend's JSON format.

    RhodyRAG emits:  event: token|sources|error|done  /  data: <payload>
    Frontend expects: data: {"textResponse": "..."} or data: {"textResponse": "", "sources": [...]}
                      data: {"error": "..."}

    Returns the translated SSE line string, or None if nothing should be emitted.
    """
    if event_type == "token":
        text = raw.replace("\\n", "\n")
        return f"data: {json.dumps({'textResponse': text})}\n\n"

    elif event_type == "sources":
        try:
            sources = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("_translate_sse_lines: failed to parse sources JSON: %s — raw='%s'",
                           exc, raw[:200])
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
        logger.debug("_translate_sse_lines: sources event — %d source(s) mapped", len(mapped))
        return f"data: {json.dumps({'textResponse': '', 'sources': mapped})}\n\n"

    elif event_type == "error":
        # RhodyRAG emits event: error when the LLM call fails (e.g. token
        # limit exceeded, gateway error, no documents embedded, etc.)
        logger.error("_translate_sse_lines: RhodyRAG error event — raw='%s'", raw[:500])
        return f"data: {json.dumps({'error': raw})}\n\n"

    return None


async def _iter_rhodyrag_sse(response: httpx.Response) -> AsyncGenerator[str, None]:
    """Shared SSE-translation iterator for RhodyRAG streaming responses."""
    event_type = None
    token_count = 0
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
            logger.debug("SSE event type: '%s'", event_type)
        elif line.startswith("data:"):
            raw = line[len("data:"):]
            if raw.startswith(" "):
                raw = raw[1:]
            # Log the full data payload for non-token events so errors are
            # always visible regardless of log level filtering downstream.
            if event_type and event_type != "token":
                logger.debug("SSE data [event=%s]: %s", event_type, raw[:1000])
            translated = _translate_sse_lines(event_type, raw)
            if translated:
                if event_type == "token":
                    token_count += 1
                yield translated
        elif line == "":
            event_type = None
    logger.debug("_iter_rhodyrag_sse: stream finished — %d token events yielded", token_count)


async def stream_chat(
    slug: str,
    message: str,
    session_id: str,
    reset: bool = False,
    followup_suffix: str = "",
) -> AsyncGenerator[str, None]:
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

    logger.debug(
        "stream_chat: slug=%s session=%s url=%s message_len=%d followup=%s",
        slug, session_id, url, len(message), bool(followup_suffix),
    )

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload, headers=HEADERS) as response:
                logger.debug("stream_chat: RhodyRAG responded HTTP %d", response.status_code)
                response.raise_for_status()
                async for line in _iter_rhodyrag_sse(response):
                    yield line
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.debug("stream_chat: stream complete in %.0fms", elapsed_ms)
    except httpx.HTTPStatusError as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.error(
            "stream_chat: HTTP %d from RhodyRAG after %.0fms — url=%s body=%s",
            exc.response.status_code, elapsed_ms, url,
            exc.response.text[:500],
        )
        raise
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.error("stream_chat: error after %.0fms: %s", elapsed_ms, exc, exc_info=True)
        raise


async def stream_chat_session(
    slug: str,
    session_id: str,
    message: str,
    history: Optional[list] = None,
    followup_suffix: str = "",
    file_context: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """Stream a chat response using a persistent ChatEngine session on RhodyRAG.

    Sends the last 6 turns of conversation history alongside the message so the
    backend can re-seed the LlamaIndex ChatEngine after a server restart.

    followup_suffix is appended to the LLM prompt only (not used for retrieval).

    file_context, when provided, has the shape:
        { "filename": str, "markdown": str, "summary": str }

    The summary is prepended to the RAG query so that the vector similarity
    search is guided by the document's topic.  The full markdown is injected
    into the message as additional context so the LLM can ground its answer
    in the uploaded document.
    """
    url = f"{API_URL}/workspace/{slug}/chat/{session_id}/stream"
    history_count = len((history or [])[-6:])

    # Build the effective query message ──────────────────────────────────────
    # If a file is attached we:
    #   1. Prepend the summary to the user message for better vector retrieval.
    #   2. Inject the full markdown as a context block so the LLM can read it.
    if file_context:
        summary = file_context.get("summary", "").strip()
        markdown = file_context.get("markdown", "").strip()
        filename = file_context.get("filename", "attached document")

        logger.debug(
            "stream_chat_session: file_context present — filename='%s' "
            "markdown=%d chars summary=%d chars",
            filename, len(markdown), len(summary),
        )

        # Truncate markdown if needed to protect the LLM context budget.
        # The embedding model never sees this — retrieval_query is sent separately.
        if len(markdown) > MAX_FILE_CONTEXT_CHARS:
            logger.warning(
                "stream_chat_session: markdown truncated from %d → %d chars "
                "(MAX_FILE_CONTEXT_CHARS=%d) for file '%s'",
                len(markdown), MAX_FILE_CONTEXT_CHARS, MAX_FILE_CONTEXT_CHARS, filename,
            )
            markdown = markdown[:MAX_FILE_CONTEXT_CHARS] + "\n\n[... document truncated ...]"

        # retrieval_query — sent to the embedding model ONLY for vector search.
        # Contains: summary + user question, kept short to stay within the
        # embedding model's 2048-token context window.
        # The summary grounds the semantic search in the document's topic;
        # the user question targets the specific information they need.
        retrieval_query = f"{summary}\n\n{message}"

        # effective_message — sent to the LLM ONLY, never embedded.
        # Contains: full document markdown as context, then the user question.
        doc_block = (
            f"[The user has attached a document named '{filename}'. "
            f"Use the following content to help answer their question.]\n\n"
            f"{markdown}\n\n---\n\n"
        )
        effective_message = doc_block + message
        logger.debug(
            "stream_chat_session: effective_message built — total=%d chars "
            "(doc_block=%d + message=%d) | retrieval_query=%d chars (summary=%d + message=%d)",
            len(effective_message), len(doc_block), len(message),
            len(retrieval_query), len(summary), len(message),
        )
    else:
        effective_message = message
        retrieval_query = None
        logger.debug("stream_chat_session: no file context — message_len=%d", len(message))

    # Append follow-up suffix if enabled (only affects LLM, not retrieval)
    if followup_suffix:
        effective_message = effective_message + followup_suffix
        logger.debug("stream_chat_session: followup_suffix appended (%d chars)", len(followup_suffix))

    payload: dict = {
        "message": effective_message,
        "history": (history or [])[-6:],
    }
    if retrieval_query is not None:
        payload["retrieval_query"] = retrieval_query
        logger.debug(
            "stream_chat_session: retrieval_query set (%d chars) — embedding model "
            "will see only this, not the full %d-char message",
            len(retrieval_query), len(effective_message),
        )

    logger.debug(
        "stream_chat_session: → POST %s  payload_message_len=%d history_turns=%d",
        url, len(effective_message), history_count,
    )

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload, headers=HEADERS) as response:
                logger.debug(
                    "stream_chat_session: RhodyRAG responded HTTP %d", response.status_code
                )
                response.raise_for_status()
                async for line in _iter_rhodyrag_sse(response):
                    yield line
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.debug("stream_chat_session: stream complete in %.0fms", elapsed_ms)
    except httpx.HTTPStatusError as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.error(
            "stream_chat_session: HTTP %d from RhodyRAG after %.0fms — url=%s body=%s",
            exc.response.status_code, elapsed_ms, url,
            exc.response.text[:500],
        )
        raise
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.error(
            "stream_chat_session: error after %.0fms: %s", elapsed_ms, exc, exc_info=True
        )
        raise
