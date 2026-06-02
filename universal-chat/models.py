from typing import Optional
from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    session_id: str
    reset: bool = False
    followup_suffix: str = ""


class ChatHistoryMessage(BaseModel):
    role: str       # "user" or "assistant"
    content: str


class FileContext(BaseModel):
    """Metadata and content for a file attached to a chat message."""
    filename: str
    markdown: str   # Full document text (for LLM context)
    summary: str    # One-sentence summary (prepended to RAG query)


class ChatSessionRequest(BaseModel):
    """Request body for a persistent chat session stream."""
    message: str
    session_id: str
    history: list[ChatHistoryMessage] = []
    followup_suffix: str = ""
    file_context: Optional[FileContext] = None
