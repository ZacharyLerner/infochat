from typing import Optional
from pydantic import BaseModel


class SendEmailRequest(BaseModel):
    """Request body for the /api/send-email endpoint."""
    to: str                     # Recipient address (or comma-separated list)
    subject: str
    body: str                   # Plain-text body
    from_addr: str = "noreply@uri.edu"
    html: bool = False


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
