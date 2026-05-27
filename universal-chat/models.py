from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    session_id: str
    reset: bool = False
    followup_suffix: str = ""


class ChatHistoryMessage(BaseModel):
    role: str       # "user" or "assistant"
    content: str


class ChatSessionRequest(BaseModel):
    """Request body for a persistent chat session stream."""
    message: str
    session_id: str
    history: list[ChatHistoryMessage] = []
    followup_suffix: str = ""
