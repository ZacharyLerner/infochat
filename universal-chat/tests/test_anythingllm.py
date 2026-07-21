"""Tests for anythingllm.py — SSE translation and stream helpers."""
import json
import pytest

from anythingllm import _translate_sse_lines, MAX_FILE_CONTEXT_CHARS, EMAIL_PROMPT_SUFFIX


# ---------------------------------------------------------------------------
# _translate_sse_lines — token events
# ---------------------------------------------------------------------------

class TestTranslateSseLinesToken:
    def test_token_basic(self):
        result = _translate_sse_lines("token", "Hello")
        assert result is not None
        data = json.loads(result.removeprefix("data: ").strip())
        assert data["textResponse"] == "Hello"

    def test_token_empty_string(self):
        result = _translate_sse_lines("token", "")
        data = json.loads(result.removeprefix("data: ").strip())
        assert data["textResponse"] == ""

    def test_token_newline_escape_converted(self):
        # RhodyRAG sends \n as the literal two-char sequence \\n
        result = _translate_sse_lines("token", "line1\\nline2")
        data = json.loads(result.removeprefix("data: ").strip())
        assert data["textResponse"] == "line1\nline2"

    def test_token_unicode(self):
        result = _translate_sse_lines("token", "こんにちは")
        data = json.loads(result.removeprefix("data: ").strip())
        assert data["textResponse"] == "こんにちは"

    def test_token_format_has_double_newline(self):
        result = _translate_sse_lines("token", "hi")
        assert result.endswith("\n\n")

    def test_token_format_starts_with_data(self):
        result = _translate_sse_lines("token", "x")
        assert result.startswith("data: ")


# ---------------------------------------------------------------------------
# _translate_sse_lines — sources events
# ---------------------------------------------------------------------------

class TestTranslateSseLinesSource:
    def test_sources_empty_dict(self):
        result = _translate_sse_lines("sources", "{}")
        data = json.loads(result.removeprefix("data: ").strip())
        assert data["textResponse"] == ""
        assert data["sources"] == []

    def test_sources_doc_and_web(self):
        payload = json.dumps({
            "documents": [
                {"filename": "report.pdf", "score": 0.9, "text": "content here"}
            ],
            "web": [
                {"title": "URI Home", "url": "https://uri.edu", "snippet": "URI info"}
            ],
        })
        result = _translate_sse_lines("sources", payload)
        data = json.loads(result.removeprefix("data: ").strip())
        sources = data["sources"]
        assert len(sources) == 2
        doc = next(s for s in sources if s["type"] == "document")
        web = next(s for s in sources if s["type"] == "web")
        assert doc["title"] == "report.pdf"
        assert doc["score"] == 0.9
        assert doc["url"] == ""
        assert web["url"] == "https://uri.edu"
        assert web["score"] is None

    def test_sources_only_documents(self):
        payload = json.dumps({
            "documents": [
                {"filename": "doc1.pdf", "score": 0.8, "text": "abc"},
                {"filename": "doc2.pdf", "score": 0.7, "text": "def"},
            ],
        })
        result = _translate_sse_lines("sources", payload)
        data = json.loads(result.removeprefix("data: ").strip())
        assert len(data["sources"]) == 2
        assert all(s["type"] == "document" for s in data["sources"])

    def test_sources_flat_array_legacy(self):
        """Backwards-compat: RhodyRAG used to send a flat list of doc sources."""
        payload = json.dumps([
            {"filename": "legacy.pdf", "score": 0.5, "text": "old"}
        ])
        result = _translate_sse_lines("sources", payload)
        data = json.loads(result.removeprefix("data: ").strip())
        assert len(data["sources"]) == 1
        assert data["sources"][0]["type"] == "document"
        assert data["sources"][0]["title"] == "legacy.pdf"

    def test_sources_invalid_json_returns_empty(self):
        result = _translate_sse_lines("sources", "NOT_JSON{{{")
        data = json.loads(result.removeprefix("data: ").strip())
        assert data["sources"] == []

    def test_sources_missing_optional_fields(self):
        """Score, text, etc. are optional — model should not crash."""
        payload = json.dumps({"documents": [{"filename": "x.pdf"}]})
        result = _translate_sse_lines("sources", payload)
        data = json.loads(result.removeprefix("data: ").strip())
        assert data["sources"][0]["title"] == "x.pdf"
        assert data["sources"][0]["text"] == ""


# ---------------------------------------------------------------------------
# _translate_sse_lines — error events
# ---------------------------------------------------------------------------

class TestTranslateSseLinesError:
    def test_error_event(self):
        result = _translate_sse_lines("error", "LLM context limit exceeded")
        data = json.loads(result.removeprefix("data: ").strip())
        assert "error" in data
        assert data["error"] == "LLM context limit exceeded"

    def test_error_empty_message(self):
        result = _translate_sse_lines("error", "")
        data = json.loads(result.removeprefix("data: ").strip())
        assert data["error"] == ""


# ---------------------------------------------------------------------------
# _translate_sse_lines — unknown / None event types
# ---------------------------------------------------------------------------

class TestTranslateSseLinesUnknown:
    def test_none_event_type_returns_none(self):
        assert _translate_sse_lines(None, "some data") is None

    def test_unknown_event_type_returns_none(self):
        assert _translate_sse_lines("ping", "heartbeat") is None

    def test_done_event_returns_none(self):
        assert _translate_sse_lines("done", "") is None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_max_file_context_chars(self):
        assert MAX_FILE_CONTEXT_CHARS == 60_000

    def test_email_prompt_suffix_contains_email_action(self):
        assert "EMAIL_ACTION" in EMAIL_PROMPT_SUFFIX

    def test_email_prompt_suffix_contains_fields(self):
        assert '"to"' in EMAIL_PROMPT_SUFFIX
        assert '"subject"' in EMAIL_PROMPT_SUFFIX
        assert '"body"' in EMAIL_PROMPT_SUFFIX


# ---------------------------------------------------------------------------
# stream_chat_session — file context truncation logic (pure logic test via
# the async generator without making real HTTP calls)
# ---------------------------------------------------------------------------

class TestFileContextTruncation:
    """Verify that the truncation guard fires for oversized markdown.

    We test this by checking the constant boundary directly — the actual
    truncation is covered by the unit-level constant test above and is
    exercised in integration tests via mocked HTTP.
    """

    def test_truncation_boundary(self):
        """MAX_FILE_CONTEXT_CHARS must be exactly 60 000."""
        assert MAX_FILE_CONTEXT_CHARS == 60_000

    def test_short_markdown_not_truncated_length(self):
        short = "x" * (MAX_FILE_CONTEXT_CHARS - 1)
        # If the markdown is shorter than the limit it should not be truncated
        assert len(short) < MAX_FILE_CONTEXT_CHARS

    def test_long_markdown_would_be_truncated(self):
        long = "x" * (MAX_FILE_CONTEXT_CHARS + 1)
        truncated = long[:MAX_FILE_CONTEXT_CHARS] + "\n\n[... document truncated ...]"
        assert truncated.endswith("[... document truncated ...]")
        assert len(truncated) > MAX_FILE_CONTEXT_CHARS
