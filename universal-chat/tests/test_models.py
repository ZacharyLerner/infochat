"""Tests for models.py — request Pydantic model validation."""
import pytest
from pydantic import ValidationError

from models import (
    SendEmailRequest,
    ChatRequest,
    ChatHistoryMessage,
    FileContext,
    ChatSessionRequest,
)


# ---------------------------------------------------------------------------
# SendEmailRequest
# ---------------------------------------------------------------------------

class TestSendEmailRequest:
    def test_defaults(self):
        req = SendEmailRequest(to="user@example.com", subject="Hi", body="Hello")
        assert req.from_addr == "noreply@uri.edu"
        assert req.html is False

    def test_custom_fields(self):
        req = SendEmailRequest(
            to="a@b.com, c@d.com",
            subject="Test",
            body="<b>Body</b>",
            from_addr="sender@uri.edu",
            html=True,
        )
        assert req.html is True
        assert req.from_addr == "sender@uri.edu"
        assert "c@d.com" in req.to

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            SendEmailRequest(subject="Hi", body="Hello")  # missing 'to'


# ---------------------------------------------------------------------------
# ChatRequest
# ---------------------------------------------------------------------------

class TestChatRequest:
    def test_defaults(self):
        req = ChatRequest(message="Hello", session_id="abc123")
        assert req.reset is False
        assert req.followup_suffix == ""

    def test_with_all_fields(self):
        req = ChatRequest(
            message="What is AI?",
            session_id="sess-1",
            reset=True,
            followup_suffix="\n\nFollow up with 3 questions.",
        )
        assert req.reset is True
        assert req.followup_suffix != ""

    def test_missing_session_id(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="Hello")

    def test_missing_message(self):
        with pytest.raises(ValidationError):
            ChatRequest(session_id="sess-1")


# ---------------------------------------------------------------------------
# ChatHistoryMessage
# ---------------------------------------------------------------------------

class TestChatHistoryMessage:
    def test_user_message(self):
        msg = ChatHistoryMessage(role="user", content="Hello")
        assert msg.role == "user"

    def test_assistant_message(self):
        msg = ChatHistoryMessage(role="assistant", content="Hi there!")
        assert msg.role == "assistant"

    def test_missing_role(self):
        with pytest.raises(ValidationError):
            ChatHistoryMessage(content="No role")


# ---------------------------------------------------------------------------
# FileContext
# ---------------------------------------------------------------------------

class TestFileContext:
    def test_valid(self):
        fc = FileContext(filename="doc.pdf", markdown="# Title\nContent", summary="A doc about X.")
        assert fc.filename == "doc.pdf"
        assert fc.markdown.startswith("# Title")
        assert fc.summary == "A doc about X."

    def test_empty_strings_allowed(self):
        fc = FileContext(filename="f.txt", markdown="", summary="")
        assert fc.markdown == ""

    def test_missing_filename(self):
        with pytest.raises(ValidationError):
            FileContext(markdown="text", summary="sum")


# ---------------------------------------------------------------------------
# ChatSessionRequest
# ---------------------------------------------------------------------------

class TestChatSessionRequest:
    def test_minimal(self):
        req = ChatSessionRequest(message="Hi", session_id="s1")
        assert req.history == []
        assert req.followup_suffix == ""
        assert req.file_context is None

    def test_with_history(self):
        req = ChatSessionRequest(
            message="Follow up",
            session_id="s1",
            history=[
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
            ],
        )
        assert len(req.history) == 2
        assert req.history[0].role == "user"

    def test_with_file_context(self):
        req = ChatSessionRequest(
            message="Summarize this",
            session_id="s1",
            file_context={
                "filename": "report.pdf",
                "markdown": "# Report\nDetails here.",
                "summary": "A quarterly report.",
            },
        )
        assert req.file_context is not None
        assert req.file_context.filename == "report.pdf"

    def test_missing_message(self):
        with pytest.raises(ValidationError):
            ChatSessionRequest(session_id="s1")
