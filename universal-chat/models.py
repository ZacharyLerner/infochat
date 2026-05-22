from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    session_id: str
    reset: bool = False
    followup_suffix: str = ""
