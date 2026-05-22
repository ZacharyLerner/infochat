import json
import httpx
from typing import AsyncGenerator
from config import API_URL, HEADERS


async def stream_chat(slug: str, message: str, session_id: str, reset: bool = False) -> AsyncGenerator[str, None]:
    """Stream a chat response from RhodyRAG, translating its SSE event format
    into the data:{textResponse, sources} format the frontend expects.

    RhodyRAG emits three named event types:
      event: token  / data: <raw text delta>   (newlines escaped as \\n)
      event: sources / data: <json array>
      event: done   / data: [DONE]

    The frontend expects bare data-only SSE lines whose payload is JSON:
      data: {"textResponse": "..."}
      data: {"textResponse": "", "sources": [...]}
    """
    url = f"{API_URL}/workspace/{slug}/query/stream"
    payload = {"question": message}

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload, headers=HEADERS) as response:
            response.raise_for_status()

            event_type = None
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    raw = line[len("data:"):].strip()

                    if event_type == "token":
                        # Unescape \\n back to real newlines for rendering
                        text = raw.replace("\\n", "\n")
                        yield f"data: {json.dumps({'textResponse': text})}\n\n"

                    elif event_type == "sources":
                        try:
                            sources = json.loads(raw)
                        except json.JSONDecodeError:
                            sources = []
                        # Map RhodyRAG source fields to what the frontend renders:
                        # frontend uses: source.title and source.url (citation links)
                        # RhodyRAG provides: filename, score, text
                        mapped = [
                            {
                                "title": s.get("filename", "Source"),
                                "url": "",
                                "score": s.get("score"),
                                "text": s.get("text", ""),
                            }
                            for s in sources
                        ]
                        yield f"data: {json.dumps({'textResponse': '', 'sources': mapped})}\n\n"

                    elif event_type == "done":
                        # Terminal sentinel — nothing to emit
                        pass

                elif line == "":
                    # Blank line resets the current event type (SSE spec)
                    event_type = None
