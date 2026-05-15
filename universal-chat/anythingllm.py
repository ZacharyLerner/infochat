import httpx
from typing import AsyncGenerator
from config import API_URL, HEADERS


async def stream_chat(slug: str, message: str, session_id: str, reset: bool = False) -> AsyncGenerator[str, None]:
    """Stream a chat response from AnythingLLM, yielding SSE lines as they arrive."""
    url = f"{API_URL}/workspace/{slug}/stream-chat"
    payload = {
        "message": message,
        "mode": "chat",
        "sessionId": session_id,
        "reset": reset,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", url, json=payload, headers=HEADERS) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    yield f"{line}\n\n"
