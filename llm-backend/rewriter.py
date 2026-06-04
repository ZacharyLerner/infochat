"""
rewriter.py
===========
Query rewriting using a fast, cheap LLM.

The rewriter turns vague or conversational user queries into specific,
self-contained search queries optimised for both vector retrieval and web
search. It is entirely optional — if no rewrite_model is configured, or if
the LLM call fails for any reason, the original query is returned unchanged.

The rewrite prompt is configurable per-workspace (stored in the DB as
`rewrite_prompt`). When blank the built-in DEFAULT_REWRITE_PROMPT is used.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in default rewrite prompt — used when workspace.rewrite_prompt is blank.
# Stored here (not in the DB) so it is always available as a fallback and can
# be viewed / overridden in the admin panel.
# ---------------------------------------------------------------------------
DEFAULT_REWRITE_PROMPT = (
    "You are a search query optimizer. "
    "Given a user's question — and optionally recent conversation context — "
    "rewrite it into a single, specific, self-contained search query that "
    "performs well for both semantic document retrieval and web search.\n\n"
    "Rules:\n"
    "- Output ONLY the rewritten query. No explanation, no quotes, no extra punctuation.\n"
    "- Resolve pronouns and vague references using the conversation context if provided.\n"
    "- Expand abbreviations. Be specific and concrete but concise.\n"
    "- If the query is already clear and specific, return it unchanged."
)


def _sync_rewrite(
    original: str,
    rewrite_model: str,
    api_key: str,
    system_prompt: str,
    history: list[dict] | None,
) -> str:
    """Synchronous inner rewrite — called via asyncio.to_thread.

    Returns the rewritten query string, or `original` on any failure.
    """
    # Import here to avoid circular imports and keep startup fast
    from llama_index.core.base.llms.types import ChatMessage, MessageRole
    from query import build_llm  # reuse the existing LLM builder

    try:
        # temperature=0.0 for deterministic, reproducible rewrites
        # max_tokens=128 — rewrites are short; cap cost and latency
        llm = build_llm(
            llm_model=rewrite_model,
            api_key=api_key,
            temperature=0.0,
            system_prompt="",   # system prompt injected manually below
            max_tokens=128,
        )

        messages: list[ChatMessage] = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
        ]

        # Inject last 2 history turns for pronoun/reference resolution
        if history:
            for turn in history[-4:]:   # -4 because each "turn" = 1 message; we want 2 user+2 assistant
                role_str = turn.get("role", "")
                content  = turn.get("content", "")
                if not content:
                    continue
                if role_str == "user":
                    messages.append(ChatMessage(role=MessageRole.USER, content=content))
                elif role_str == "assistant":
                    messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=content))

        messages.append(ChatMessage(role=MessageRole.USER, content=original))

        response = llm.chat(messages)
        rewritten = (response.message.content or "").strip()

        if not rewritten:
            logger.warning("Rewrite model returned empty response for query %r", original[:80])
            return original

        return rewritten

    except Exception as exc:
        logger.warning("Query rewrite failed (%s): %s", type(exc).__name__, exc)
        return original


async def rewrite_query(
    original: str,
    rewrite_model: str,
    api_key: str,
    rewrite_prompt: str = "",
    history: list[dict] | None = None,
) -> str:
    """Rewrite `original` using a fast LLM for better retrieval performance.

    Args:
        original:      The raw user query string.
        rewrite_model: LiteLLM model string (e.g. 'openai/gpt-4o-mini').
        api_key:       API key for the LLM gateway (reuses the workspace key).
        rewrite_prompt: Custom system prompt. Falls back to DEFAULT_REWRITE_PROMPT
                        when blank or None.
        history:       Recent conversation turns as list of
                       {"role": "user"|"assistant", "content": str} dicts.
                       Last 2 turns (user + assistant) are passed to the
                       rewriter for pronoun/reference resolution.

    Returns:
        The rewritten query string, or `original` unchanged on any failure.
        Never raises.
    """
    if not rewrite_model or not rewrite_model.strip():
        return original

    system_prompt = (rewrite_prompt or "").strip() or DEFAULT_REWRITE_PROMPT

    return await asyncio.to_thread(
        _sync_rewrite,
        original,
        rewrite_model.strip(),
        api_key,
        system_prompt,
        history,
    )
