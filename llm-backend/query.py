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
"""

import asyncio
import json
from typing import AsyncGenerator

from llama_index.core import VectorStoreIndex
from llama_index.core.memory import ChatMemoryBuffer
from llama_index.core.postprocessor import SimilarityPostprocessor
from llama_index.llms.litellm import LiteLLM
from llama_index.vector_stores.lancedb.base import TableNotFoundError

import config
import db
from embedding import build_embed_model, get_vector_store

# ---------------------------------------------------------------------------
# In-process chat session registry
# Maps session_id (str UUID) → {"index": VectorStoreIndex, "memory": ChatMemoryBuffer, "workspace": dict}
# Lost on server restart; re-seeded from browser history on first message.
# ---------------------------------------------------------------------------
_chat_sessions: dict = {}


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
    Returns None if no documents have been embedded yet.

    `chat_history` is a list of {"role": "user"|"assistant", "content": str}
    dicts from the browser's localStorage. The last 6 entries are pre-loaded
    into ChatMemoryBuffer so the LLM has context after a server restart.
    """
    from llama_index.core.base.llms.types import ChatMessage, MessageRole as MR

    try:
        index = _build_index(workspace)
    except TableNotFoundError:
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


async def stream_chat_session(
    session_id: str,
    workspace: dict,
    message: str,
    history: list[dict] | None = None,
) -> AsyncGenerator[str, None]:
    """Stream a persistent chat session response with true token-by-token streaming.

    Avoids LlamaIndex's ChatEngine.astream_chat() which buffers the full
    response before yielding (due to its internal queue/task architecture).

    Instead, replicates what stream_query_workspace does — retrieves context
    nodes, builds the prompt manually, and calls llm.astream_chat() directly —
    while maintaining a ChatMemoryBuffer per session so follow-up questions
    have full conversation context.

    Flow per message:
      1. Get or build the session state (index + memory).
      2. Retrieve relevant nodes using the raw message as the retrieval query.
         (For a richer experience, the system prompt already captures context.)
      3. Build a messages list: prior history from memory + system prompt +
         context-augmented user message.
      4. Stream tokens directly from llm.astream_chat() — no buffering.
      5. After streaming, append user+assistant messages to memory so the
         next turn has full context.
    """
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    try:
        # ── 1. Get or build session state ────────────────────────────────────
        if session_id not in _chat_sessions:
            state = await asyncio.to_thread(_build_session_state, workspace, history or [])
            if state is None:
                yield "event: token\ndata: No documents have been embedded in this workspace yet.\n\n"
                yield "event: sources\ndata: []\n\n"
                yield "event: done\ndata: [DONE]\n\n"
                return
            _chat_sessions[session_id] = state

        state = _chat_sessions[session_id]
        index = state["index"]
        memory = state["memory"]

        # ── 2. Retrieve relevant context nodes ───────────────────────────────
        threshold = workspace["similarity_threshold"]
        retriever = index.as_retriever(similarity_top_k=workspace["top_n"])
        nodes = await retriever.aretrieve(message)
        nodes = [n for n in nodes if n.score is None or n.score >= threshold]

        sources = [
            {
                "score": node.score,
                "filename": node.node.metadata.get("filename"),
                "text": node.node.get_content()[:200],
            }
            for node in nodes
        ]

        # ── 3. Build the messages list ───────────────────────────────────────
        system_prompt = workspace.get("system_prompt") or ""

        # Prior turns from memory (excludes the current message)
        prior_messages = memory.get()

        # Context-augmented user prompt — same format as stream_query_workspace
        if nodes:
            context_str = "\n\n".join(n.node.get_content() for n in nodes)
            user_content = (
                f"Context information is below.\n"
                f"---------------------\n"
                f"{context_str}\n"
                f"---------------------\n"
                f"Given the context information and the conversation history, answer the query.\n"
                f"Query: {message}\n"
                f"Answer: "
            )
        else:
            # No matching context — answer from conversation history alone
            user_content = message

        messages: list[ChatMessage] = []
        if system_prompt:
            messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
        messages.extend(prior_messages)
        messages.append(ChatMessage(role=MessageRole.USER, content=user_content))

        # ── 4. Stream directly from the LLM ─────────────────────────────────
        llm = build_llm(
            workspace["llm_model"],
            workspace["api_key"],
            workspace["temperature"],
            workspace["system_prompt"],
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

        # ── 5. Update memory with this turn ─────────────────────────────────
        # Store the raw user message (not the context-augmented one) so the
        # conversation history reads naturally in subsequent turns.
        memory.put(ChatMessage(role=MessageRole.USER, content=message))
        memory.put(ChatMessage(role=MessageRole.ASSISTANT, content=full_response.strip()))

        yield f"event: sources\ndata: {json.dumps(sources)}\n\n"
        yield "event: done\ndata: [DONE]\n\n"

    except Exception as exc:
        error_msg = str(exc).replace('\n', ' ')
        yield f"event: error\ndata: {error_msg}\n\n"
        yield "event: done\ndata: [DONE]\n\n"


def query_workspace(workspace: dict, question: str) -> dict:
    """Run a blocking query against a workspace using its stored settings.

    `workspace` is the dict returned from db.get_workspace().
    """
    try:
        index = _build_index(workspace)
    except TableNotFoundError:
        # No documents have been embedded yet — valid state, not an error.
        return {"answer": "No documents have been embedded in this workspace yet.", "sources": []}

    query_engine = index.as_query_engine(
        llm=build_llm(workspace["llm_model"], workspace["api_key"], workspace["temperature"], workspace["system_prompt"], workspace.get("max_tokens", 1024)),
        similarity_top_k=workspace["top_n"],
        node_postprocessors=[
            SimilarityPostprocessor(
                similarity_cutoff=workspace["similarity_threshold"]
            )
        ],
    )
    response = query_engine.query(question)

    return {
        "answer": str(response),
        "sources": [
            {
                "score": node.score,
                "filename": node.node.metadata.get("filename"),
                "text": node.node.get_content()[:200],
            }
            for node in response.source_nodes
        ],
    }


async def stream_query_workspace(workspace: dict, question: str, prompt_suffix: str = "") -> AsyncGenerator[str, None]:
    """Stream a query response as Server-Sent Events.

    Bypasses the query engine's response synthesizer (which buffers the full
    response internally before yielding) by:
      1. Retrieving relevant nodes via the index retriever.
      2. Building the prompt manually with context.
      3. Streaming tokens directly from the LLM via astream_chat.
    """
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    try:
        try:
            index = await asyncio.to_thread(_build_index, workspace)
        except TableNotFoundError:
            yield "event: token\ndata: No documents have been embedded in this workspace yet.\n\n"
            yield "event: sources\ndata: []\n\n"
            yield "event: done\ndata: [DONE]\n\n"
            return

        # --- Step 1: Retrieve relevant nodes ---
        retriever = index.as_retriever(similarity_top_k=workspace["top_n"])
        nodes = await retriever.aretrieve(question)

        # Apply similarity threshold post-processing
        threshold = workspace["similarity_threshold"]
        nodes = [n for n in nodes if n.score is None or n.score >= threshold]

        if not nodes:
            yield "event: token\ndata: No relevant documents found for your question.\n\n"
            yield "event: sources\ndata: []\n\n"
            yield "event: done\ndata: [DONE]\n\n"
            return

        # --- Step 2: Build the prompt with retrieved context ---
        context_str = "\n\n".join(
            n.node.get_content() for n in nodes
        )

        system_prompt = workspace["system_prompt"] or ""
        user_prompt = (
            f"Context information is below.\n"
            f"---------------------\n"
            f"{context_str}\n"
            f"---------------------\n"
            f"Given the context information and not prior knowledge, answer the query.\n"
            f"Query: {question}\n"
            f"Answer: "
        )
        if prompt_suffix:
            user_prompt += prompt_suffix

        messages = []
        if system_prompt:
            messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
        messages.append(ChatMessage(role=MessageRole.USER, content=user_prompt))

        # --- Step 3: Stream directly from the LLM ---
        llm = build_llm(workspace["llm_model"], workspace["api_key"], workspace["temperature"], workspace["system_prompt"], workspace.get("max_tokens", 1024))
        try:
            response_gen = await llm.astream_chat(messages)
            async for chat_response in response_gen:
                token = chat_response.delta
                if token:
                    safe = token.replace('\n', '\\n')
                    yield f"event: token\ndata: {safe}\n\n"
        except Exception as exc:
            error_msg = str(exc).replace('\n', ' ')
            yield f"event: error\ndata: {error_msg}\n\n"
            yield "event: done\ndata: [DONE]\n\n"
            return

        # --- Step 4: Emit sources ---
        sources = [
            {
                "score": node.score,
                "filename": node.node.metadata.get("filename"),
                "text": node.node.get_content()[:200],
            }
            for node in nodes
        ]
        yield f"event: sources\ndata: {json.dumps(sources)}\n\n"
        yield "event: done\ndata: [DONE]\n\n"

    except Exception as exc:
        # Catch-all: ensure the stream always terminates cleanly even for
        # unexpected errors (retrieval failures, encoding issues, etc.)
        error_msg = str(exc).replace('\n', ' ')
        yield f"event: error\ndata: {error_msg}\n\n"
        yield "event: done\ndata: [DONE]\n\n"
