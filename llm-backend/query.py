"""
query.py
========
Workspace-scoped querying: reconnect to the workspace's table, build the LLM
from that workspace's settings, retrieve top_n chunks above the similarity
threshold, and answer.

The embed model is read from the workspace row, falling back to the global
settings row if the workspace has no embed_model set.

Chat sessions maintain a ChatMemoryBuffer per session_id (UUID) for rolling
conversation context. Each message retrieves fresh context nodes then streams
tokens directly via llm.astream_chat() — bypassing LlamaIndex's ChatEngine
astream_chat() which buffers the full response before yielding. History is
re-seeded from the browser's localStorage payload on the first message after
a server restart (last 6 turns).

Web search (SearXNG) and query rewriting run concurrently with vector
retrieval when enabled. Results are merged into a single labeled context block.
"""

import asyncio
import datetime as _dt
import json
import time as _time
import uuid as _uuid
from typing import AsyncGenerator

# Embedding models like qwen3-embed-8b have a 2048-token context window.
# Truncate retrieval queries to this many characters as a safe guard — well
# under 2048 tokens for any realistic text (avg ~4 chars/token → ~7000 chars
# for 1750 tokens, leaving headroom).
_MAX_EMBED_CHARS = 6000

from prompts import DEFAULT_SYSTEM_PROMPT_RAG, DEFAULT_SYSTEM_PROMPT_WEB  # noqa: E402


def _safe_embed_query(text: str) -> str:
    """Truncate text to _MAX_EMBED_CHARS to avoid embedding model context overflow."""
    return text[:_MAX_EMBED_CHARS] if len(text) > _MAX_EMBED_CHARS else text

from llama_index.core import VectorStoreIndex
from llama_index.core.memory import ChatMemoryBuffer
from llama_index.core.postprocessor import SimilarityPostprocessor
from llama_index.llms.litellm import LiteLLM
from llama_index.vector_stores.lancedb.base import TableNotFoundError

import config
import db
from embedding import build_embed_model, get_vector_store
import searxng as _searxng
import rewriter as _rewriter

# ---------------------------------------------------------------------------
# In-process chat session registry
# Maps session_id (str UUID) → {"index": VectorStoreIndex, "memory": ChatMemoryBuffer, "workspace": dict}
# Lost on server restart; re-seeded from browser history on first message.
#
# _chat_sessions_lock serializes session creation so that two concurrent
# first-messages to the same session don't each build (and then overwrite)
# their own index state. Individual message turns within an established session
# are serialized by the same lock — per-session locks would be cleaner but the
# session count is small and index builds are the bottleneck, not the dict ops.
# ---------------------------------------------------------------------------
import threading as _threading
_chat_sessions: dict = {}
_chat_sessions_lock = _threading.Lock()


# Gateway model strings are not in LiteLLM's registry, so it falls back to a
# 2048-token context window. With max_tokens near that ceiling, LlamaIndex
# calculates negative available context and raises ValueError before the query
# reaches the LLM. We subclass to override the metadata property with a fixed
# large window; LlamaIndex only uses this for prompt budgeting — the gateway
# enforces the real model limit.
_CONTEXT_WINDOW = 128_000


class _GatewayLiteLLM(LiteLLM):
    """LiteLLM with a fixed context_window for unrecognised gateway model strings."""

    @property
    def metadata(self):
        from llama_index.core.llms import LLMMetadata
        base = super().metadata
        return LLMMetadata(
            context_window=_CONTEXT_WINDOW,
            num_output=base.num_output,
            is_chat_model=base.is_chat_model,
            is_function_calling_model=base.is_function_calling_model,
            model_name=base.model_name,
        )


def build_llm(llm_model: str, api_key: str, temperature: float, system_prompt: str = "", max_tokens: int = 1024) -> LiteLLM:
    return _GatewayLiteLLM(
        model=llm_model,
        api_base=config.API_BASE,
        api_key=api_key,
        temperature=temperature,
        system_prompt=system_prompt or None,
        max_tokens=max_tokens,
    )


def _build_index(workspace: dict) -> VectorStoreIndex:
    """Shared index construction for both query modes."""
    embed_model = workspace["embed_model"] or db.get_settings()["embed_model"]
    return VectorStoreIndex.from_vector_store(
        get_vector_store(workspace["slug"]),
        embed_model=build_embed_model(
            embed_model,
            api_key=workspace["api_key"],
            embed_api_key=workspace["embed_api_key"],
        ),
    )


def _build_session_state(workspace: dict, chat_history: list[dict] | None = None) -> dict | None:
    """Build and return the session state dict for a chat session.

    Returns a dict with keys: index, memory, workspace
    Returns None if no documents have been embedded yet AND web search is disabled.

    `chat_history` is a list of {"role": "user"|"assistant", "content": str}
    dicts from the browser's localStorage. The last 6 entries are pre-loaded
    into ChatMemoryBuffer so the LLM has context after a server restart.
    """
    from llama_index.core.base.llms.types import ChatMessage, MessageRole as MR

    index = None
    try:
        index = _build_index(workspace)
    except TableNotFoundError:
        # No documents embedded — allowed when web search is enabled
        if not workspace.get("searxng_enabled"):
            return None

    memory = ChatMemoryBuffer.from_defaults(token_limit=4096)

    # Re-seed from browser history (last 6 turns max)
    if chat_history:
        for msg in chat_history[-6:]:
            role = MR.USER if msg.get("role") == "user" else MR.ASSISTANT
            content = msg.get("content", "")
            if content:
                memory.put(ChatMessage(role=role, content=content))

    return {"index": index, "memory": memory, "workspace": workspace}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_merged_context(nodes: list, web_results: list[dict]) -> str:
    """Build a merged context block from vector nodes and/or web results.

    Sections present only when non-empty:
      --- Document Context ---      (vector chunks)
      --- Web Search Results ---    (SearXNG results)

    Returns an empty string when both are empty.
    """
    parts = []

    if nodes:
        doc_text = "\n\n".join(n.node.get_content() for n in nodes)
        parts.append(f"--- Document Context ---\n{doc_text}")

    if web_results:
        lines = [
            "--- Web Search Results ---",
            "The following live web results were retrieved for this query.",
            "When they are relevant, you MUST cite the source URL in your answer.",
        ]
        for i, r in enumerate(web_results, 1):
            title   = r.get("title", "")
            url     = r.get("url", "")
            snippet = r.get("snippet", "")
            lines.append(
                f"[Web Result {i}]\n"
                f"  Title:   {title}\n"
                f"  Source:  {url}\n"
                f"  Excerpt: {snippet}"
            )
        parts.append("\n\n".join(lines))

    return "\n\n".join(parts)


async def _rewrite_if_enabled(
    query: str,
    workspace: dict,
    history: list[dict] | None = None,
) -> tuple[str, str | None]:
    """Rewrite `query` if the workspace has a rewrite_model configured.

    Returns:
        (effective_query, rewritten_or_None)
        rewritten_or_None is None when rewriting is disabled or the rewritten
        query is identical to the original.
    """
    rewrite_model = workspace.get("rewrite_model", "")
    if not rewrite_model:
        return query, None

    rewritten = await _rewriter.rewrite_query(
        original=query,
        rewrite_model=rewrite_model,
        api_key=workspace.get("api_key", ""),
        rewrite_prompt=workspace.get("rewrite_prompt", ""),
        history=history,
    )

    # Only treat as rewritten if the query actually changed
    changed = rewritten != query
    return rewritten, rewritten if changed else None


async def _retrieve_nodes(index: VectorStoreIndex | None, query: str, workspace: dict) -> list:
    """Retrieve relevant nodes from the vector index.

    Returns an empty list when index is None (no documents embedded).
    """
    if index is None:
        return []

    threshold = workspace["similarity_threshold"]
    retriever = index.as_retriever(similarity_top_k=workspace["top_n"])
    nodes = await asyncio.to_thread(retriever.retrieve, _safe_embed_query(query))
    return [n for n in nodes if n.score is None or n.score >= threshold]


# ---------------------------------------------------------------------------
# Chat session management
# ---------------------------------------------------------------------------

async def stream_chat_session(
    session_id: str,
    workspace: dict,
    message: str,
    history: list[dict] | None = None,
    retrieval_query: str | None = None,
) -> AsyncGenerator[str, None]:
    """Stream a persistent chat session response with true token-by-token streaming.

    Avoids LlamaIndex's ChatEngine.astream_chat() which buffers the full
    response before yielding (due to its internal queue/task architecture).

    Instead, replicates what stream_query_workspace does — retrieves context
    nodes, builds the prompt manually, and calls llm.astream_chat() directly —
    while maintaining a ChatMemoryBuffer per session so follow-up questions
    have full conversation context.

    If workspace.searxng_enabled is True, a SearXNG web search runs concurrently
    with vector retrieval and the results are merged into a single context block.

    If workspace.rewrite_model is set, the query is rewritten before retrieval
    and web search. A rewritten_query SSE event is emitted when the query changes.

    Args:
        retrieval_query: Optional short text used *only* for vector similarity
            retrieval (pre-rewrite). When provided the caller is already passing
            a focused retrieval string; rewriting still applies on top of it.

    Flow per message:
      1. Get or build the session state (index + memory).
      2. Optionally rewrite the retrieval query.
      3. Concurrently: retrieve relevant nodes + run web search (if enabled).
      4. Build a messages list: prior history from memory + system prompt +
         context-augmented user message.
      5. Stream tokens directly from llm.astream_chat() — no buffering.
      6. After streaming, append user+assistant messages to memory so the
         next turn has full context.
    """
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    try:
        # ── 1. Get or build session state ────────────────────────────────────
        if session_id not in _chat_sessions:
            state = await asyncio.to_thread(_build_session_state, workspace, history or [])
            if state is None:
                yield "event: token\ndata: No documents have been embedded in this workspace yet.\n\n"
                yield "event: sources\ndata: {\"documents\": [], \"web\": []}\n\n"
                yield "event: done\ndata: [DONE]\n\n"
                return
            with _chat_sessions_lock:
                if session_id not in _chat_sessions:
                    _chat_sessions[session_id] = state

        with _chat_sessions_lock:
            state = _chat_sessions[session_id]
        index  = state["index"]
        memory = state["memory"]

        # ── 2. Determine base retrieval query (caller override or message) ───
        # Strip any frontend-injected system instructions appended after \n\n[
        clean_message = message.split("\n\n[")[0].strip()
        base_query = retrieval_query if retrieval_query else clean_message

        # For rewrite context: extract the last 2 prior turns from memory
        prior_turns = [
            {"role": m.role.value, "content": m.content}
            for m in memory.get()
        ][-4:]   # last 4 messages = last 2 user+assistant turns

        # ── 3. Rewrite if enabled ─────────────────────────────────────────────
        effective_query, rewritten = await _rewrite_if_enabled(
            base_query, workspace, prior_turns
        )

        if rewritten:
            safe_rw = rewritten.replace('\n', '\\n')
            yield f"event: rewritten_query\ndata: {safe_rw}\n\n"

        # ── 4. Concurrent: vector retrieval + web search ──────────────────────
        web_enabled = bool(workspace.get("searxng_enabled"))

        async def _web_task():
            if not web_enabled:
                return []
            num = max(1, min(int(workspace.get("searxng_num_results") or 3), 10))
            suffix = (workspace.get("searxng_query_suffix") or "").strip()
            web_query = f"{effective_query} {suffix}".strip() if suffix else effective_query
            return await _searxng.web_search(web_query, num_results=num)

        nodes, web_results = await asyncio.gather(
            _retrieve_nodes(index, effective_query, workspace),
            _web_task(),
        )

        # ── 5. Build sources payload ──────────────────────────────────────────
        doc_sources = [
            {
                "score": node.score,
                "filename": node.node.metadata.get("filename"),
                "text": node.node.get_content()[:200],
            }
            for node in nodes
        ]

        # ── 6. Build the context block and prompt ─────────────────────────────
        system_prompt = (
            workspace.get("system_prompt")
            or (DEFAULT_SYSTEM_PROMPT_WEB if web_enabled else DEFAULT_SYSTEM_PROMPT_RAG)
        )
        prior_messages = memory.get()

        context_str = _build_merged_context(nodes, web_results)

        if context_str:
            web_note = (
                " When citing web results, include the source URL."
                if web_enabled and web_results else ""
            )
            user_content = (
                f"Context information is below.\n"
                f"---------------------\n"
                f"{context_str}\n"
                f"---------------------\n"
                f"Given the context information above and the conversation history, answer the query.{web_note}\n"
                f"Query: {message}\n"
                f"Answer: "
            )
        else:
            if not nodes and not web_results:
                # Nothing from either source
                if web_enabled:
                    yield "event: token\ndata: No relevant information found in documents or web search.\n\n"
                else:
                    yield "event: token\ndata: No documents have been embedded in this workspace yet.\n\n"
                yield "event: sources\ndata: {\"documents\": [], \"web\": []}\n\n"
                yield "event: done\ndata: [DONE]\n\n"
                return
            user_content = message

        messages: list[ChatMessage] = []
        if system_prompt:
            messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
        messages.extend(prior_messages)
        messages.append(ChatMessage(role=MessageRole.USER, content=user_content))

        # ── 7. Stream directly from the LLM ──────────────────────────────────
        llm = build_llm(
            workspace["llm_model"],
            workspace["api_key"],
            workspace["temperature"],
            system_prompt,
            workspace.get("max_tokens", 1024),
        )

        full_response = ""
        try:
            response_gen = await llm.astream_chat(messages)
            async for chat_response in response_gen:
                token = chat_response.delta
                if token:
                    full_response += token
                    safe = token.replace('\n', '\\n')
                    yield f"event: token\ndata: {safe}\n\n"
        except Exception as exc:
            error_msg = str(exc).replace('\n', ' ')
            yield f"event: error\ndata: {error_msg}\n\n"
            yield "event: done\ndata: [DONE]\n\n"
            return

        # ── 8. Update memory with this turn ──────────────────────────────────
        # Store the raw user message (not the context-augmented one) so the
        # conversation history reads naturally in subsequent turns.
        memory.put(ChatMessage(role=MessageRole.USER, content=message))
        memory.put(ChatMessage(role=MessageRole.ASSISTANT, content=full_response.strip()))

        sources_payload = {"documents": doc_sources, "web": web_results}
        yield f"event: sources\ndata: {json.dumps(sources_payload)}\n\n"
        yield "event: done\ndata: [DONE]\n\n"

        # ── 9. Emit log event (intercepted by main.py, never reaches browser) ─
        log_entry = {
            "id": str(_uuid.uuid4()),
            "timestamp": _dt.datetime.utcnow().isoformat() + "Z",
            "streamed": True,
            "chat_session": True,
            "session_id": session_id,
            "question": clean_message,
            "rewritten_query": rewritten,
            "answer": full_response.strip(),
            "sources": sources_payload,
        }
        yield f"event: log\ndata: {json.dumps(log_entry)}\n\n"

    except Exception as exc:
        error_msg = str(exc).replace('\n', ' ')
        yield f"event: error\ndata: {error_msg}\n\n"
        yield "event: done\ndata: [DONE]\n\n"


def query_workspace(workspace: dict, question: str) -> dict:
    """Run a blocking query against a workspace using its stored settings.

    Supports query rewriting and SearXNG web search when enabled. Runs the
    async gather inside a fresh event loop (this function is called from a
    thread pool via asyncio.to_thread in main.py).

    `workspace` is the dict returned from db.get_workspace().
    """
    return asyncio.run(_async_query_workspace(workspace, question))


async def _async_query_workspace(workspace: dict, question: str) -> dict:
    """Async implementation of the blocking query — called via asyncio.run()."""
    # Step 1: Rewrite if enabled
    effective_query, rewritten = await _rewrite_if_enabled(question, workspace)

    # Step 2: Build index (may raise TableNotFoundError)
    web_enabled = bool(workspace.get("searxng_enabled"))

    index = None
    try:
        index = await asyncio.to_thread(_build_index, workspace)
    except TableNotFoundError:
        if not web_enabled:
            return {
                "answer": "No documents have been embedded in this workspace yet.",
                "sources": {"documents": [], "web": []},
                "rewritten_query": rewritten,
            }
        # Web search is enabled — proceed without vector results

    # Step 3: Concurrent retrieval + web search
    async def _web_task():
        if not web_enabled:
            return []
        num = max(1, min(int(workspace.get("searxng_num_results") or 3), 10))
        suffix = (workspace.get("searxng_query_suffix") or "").strip()
        web_query = f"{effective_query} {suffix}".strip() if suffix else effective_query
        return await _searxng.web_search(web_query, num_results=num)

    nodes, web_results = await asyncio.gather(
        _retrieve_nodes(index, effective_query, workspace),
        _web_task(),
    )

    # Step 4: Build context
    context_str = _build_merged_context(nodes, web_results)

    if not context_str:
        return {
            "answer": "No relevant information found in documents or web search." if web_enabled
                      else "No relevant documents found for your question.",
            "sources": {"documents": [], "web": []},
            "rewritten_query": rewritten,
        }

    # Step 5: Build prompt and call LLM (blocking — fine inside asyncio.run)
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    system_prompt = (
        workspace.get("system_prompt")
        or (DEFAULT_SYSTEM_PROMPT_WEB if web_enabled else DEFAULT_SYSTEM_PROMPT_RAG)
    )
    web_note = (
        " When citing web results, include the source URL."
        if web_enabled and web_results else ""
    )
    user_prompt = (
        f"Context information is below.\n"
        f"---------------------\n"
        f"{context_str}\n"
        f"---------------------\n"
        f"Given the context information above and not prior knowledge, answer the query.{web_note}\n"
        f"Query: {question}\n"
        f"Answer: "
    )

    messages = []
    if system_prompt:
        messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
    messages.append(ChatMessage(role=MessageRole.USER, content=user_prompt))

    llm = build_llm(
        workspace["llm_model"],
        workspace["api_key"],
        workspace["temperature"],
        system_prompt,
        workspace.get("max_tokens", 1024),
    )

    response = await asyncio.to_thread(llm.chat, messages)
    answer = (response.message.content or "").strip()

    doc_sources = [
        {
            "score": node.score,
            "filename": node.node.metadata.get("filename"),
            "text": node.node.get_content()[:200],
        }
        for node in nodes
    ]

    return {
        "answer": answer,
        "sources": {"documents": doc_sources, "web": web_results},
        "rewritten_query": rewritten,
    }


async def stream_query_workspace(workspace: dict, question: str, prompt_suffix: str = "") -> AsyncGenerator[str, None]:
    """Stream a query response as Server-Sent Events.

    Supports query rewriting and SearXNG web search when enabled on the workspace.
    Vector retrieval and web search run concurrently via asyncio.gather.

    SSE events emitted (in order):
      event: rewritten_query   (only when rewriting changes the query)
      event: token             (one or more — streamed answer tokens)
      event: sources           (JSON: {"documents": [...], "web": [...]})
      event: done              (always last)
      event: log               (intercepted by main.py — never forwarded to browser)
      event: error             (replaces token/sources on failure)
    """
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    _start_time = _time.time()

    try:
        # ── 1. Rewrite if enabled ─────────────────────────────────────────────
        effective_query, rewritten = await _rewrite_if_enabled(question, workspace)

        if rewritten:
            safe_rw = rewritten.replace('\n', '\\n')
            yield f"event: rewritten_query\ndata: {safe_rw}\n\n"

        # ── 2. Build index + concurrent web search ────────────────────────────
        web_enabled = bool(workspace.get("searxng_enabled"))

        index = None
        try:
            index = await asyncio.to_thread(_build_index, workspace)
        except TableNotFoundError:
            if not web_enabled:
                yield "event: token\ndata: No documents have been embedded in this workspace yet.\n\n"
                yield "event: sources\ndata: {\"documents\": [], \"web\": []}\n\n"
                yield "event: done\ndata: [DONE]\n\n"
                return
            # Web enabled — continue without vector results

        # ── 3. Concurrent: vector retrieval + web search ──────────────────────
        async def _web_task():
            if not web_enabled:
                return []
            num = max(1, min(int(workspace.get("searxng_num_results") or 3), 10))
            suffix = (workspace.get("searxng_query_suffix") or "").strip()
            web_query = f"{effective_query} {suffix}".strip() if suffix else effective_query
            return await _searxng.web_search(web_query, num_results=num)

        nodes, web_results = await asyncio.gather(
            _retrieve_nodes(index, effective_query, workspace),
            _web_task(),
        )

        # ── 4. Build merged context ───────────────────────────────────────────
        context_str = _build_merged_context(nodes, web_results)

        if not context_str:
            msg = (
                "No relevant information found in documents or web search."
                if web_enabled else
                "No relevant documents found for your question."
            )
            yield f"event: token\ndata: {msg}\n\n"
            yield "event: sources\ndata: {\"documents\": [], \"web\": []}\n\n"
            yield "event: done\ndata: [DONE]\n\n"
            return

        # ── 5. Build the prompt ───────────────────────────────────────────────
        system_prompt = (
            workspace["system_prompt"]
            or (DEFAULT_SYSTEM_PROMPT_WEB if web_enabled else DEFAULT_SYSTEM_PROMPT_RAG)
        )
        web_note = (
            " When citing web results, include the source URL."
            if web_enabled and web_results else ""
        )
        user_prompt = (
            f"Context information is below.\n"
            f"---------------------\n"
            f"{context_str}\n"
            f"---------------------\n"
            f"Given the context information above and not prior knowledge, answer the query.{web_note}\n"
            f"Query: {question}\n"
            f"Answer: "
        )
        if prompt_suffix:
            user_prompt += prompt_suffix

        messages = []
        if system_prompt:
            messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
        messages.append(ChatMessage(role=MessageRole.USER, content=user_prompt))

        # ── 6. Stream directly from the LLM ──────────────────────────────────
        llm = build_llm(
            workspace["llm_model"],
            workspace["api_key"],
            workspace["temperature"],
            system_prompt,
            workspace.get("max_tokens", 1024),
        )
        full_answer = ""
        try:
            response_gen = await llm.astream_chat(messages)
            async for chat_response in response_gen:
                token = chat_response.delta
                if token:
                    full_answer += token
                    safe = token.replace('\n', '\\n')
                    yield f"event: token\ndata: {safe}\n\n"
        except Exception as exc:
            error_msg = str(exc).replace('\n', ' ')
            yield f"event: error\ndata: {error_msg}\n\n"
            yield "event: done\ndata: [DONE]\n\n"
            return

        # ── 7. Emit sources ───────────────────────────────────────────────────
        doc_sources = [
            {
                "score": node.score,
                "filename": node.node.metadata.get("filename"),
                "text": node.node.get_content()[:200],
            }
            for node in nodes
        ]
        sources_payload = {"documents": doc_sources, "web": web_results}
        yield f"event: sources\ndata: {json.dumps(sources_payload)}\n\n"
        yield "event: done\ndata: [DONE]\n\n"

        # ── 8. Emit log event (intercepted by main.py, never reaches browser) ─
        log_entry = {
            "id": str(_uuid.uuid4()),
            "timestamp": _dt.datetime.utcnow().isoformat() + "Z",
            "streamed": True,
            "question": question,
            "rewritten_query": rewritten,
            "answer": full_answer.strip(),
            "sources": sources_payload,
            "duration_ms": int((_time.time() - _start_time) * 1000),
        }
        yield f"event: log\ndata: {json.dumps(log_entry)}\n\n"

    except Exception as exc:
        # Catch-all: ensure the stream always terminates cleanly even for
        # unexpected errors (retrieval failures, encoding issues, etc.)
        error_msg = str(exc).replace('\n', ' ')
        yield f"event: error\ndata: {error_msg}\n\n"
        yield "event: done\ndata: [DONE]\n\n"
