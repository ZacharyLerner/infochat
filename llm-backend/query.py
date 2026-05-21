"""
query.py
========
Workspace-scoped querying: reconnect to the workspace's table, build the LLM
from that workspace's settings, retrieve top_n chunks above the similarity
threshold, and answer.

The embed model is read from the workspace row, falling back to the global
settings row if the workspace has no embed_model set.
"""

import asyncio
import json
from typing import AsyncGenerator

from llama_index.core import VectorStoreIndex
from llama_index.core.postprocessor import SimilarityPostprocessor
from llama_index.llms.litellm import LiteLLM
from llama_index.vector_stores.lancedb.base import TableNotFoundError

import config
import db
from embedding import build_embed_model, get_vector_store


def build_llm(llm_model: str, api_key: str, temperature: float, system_prompt: str = "") -> LiteLLM:
    return LiteLLM(
        model=llm_model,
        api_base=config.API_BASE,
        api_key=api_key,
        temperature=temperature,
        system_prompt=system_prompt or None,
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
        llm=build_llm(workspace["llm_model"], workspace["api_key"], workspace["temperature"], workspace["system_prompt"]),
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


async def stream_query_workspace(workspace: dict, question: str) -> AsyncGenerator[str, None]:
    """Stream a query response as Server-Sent Events.

    Bypasses the query engine's response synthesizer (which buffers the full
    response internally before yielding) by:
      1. Retrieving relevant nodes via the index retriever.
      2. Building the prompt manually with context.
      3. Streaming tokens directly from the LLM via astream_chat.
    """
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

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

    messages = []
    if system_prompt:
        messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
    messages.append(ChatMessage(role=MessageRole.USER, content=user_prompt))

    # --- Step 3: Stream directly from the LLM ---
    llm = build_llm(workspace["llm_model"], workspace["api_key"], workspace["temperature"], workspace["system_prompt"])
    response_gen = await llm.astream_chat(messages)

    async for chat_response in response_gen:
        token = chat_response.delta
        if token:
            safe = token.replace('\n', '\\n')
            yield f"event: token\ndata: {safe}\n\n"

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
