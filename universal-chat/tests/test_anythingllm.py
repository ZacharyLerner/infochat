"""Tests for anythingllm.py — translating RhodyRAG SSE into the chat UI's format."""
import json
import pytest

from anythingllm import _translate_sse_lines


def _data(line):
    """Parse a translated `data: {...}\\n\\n` SSE line."""
    assert line.startswith("data: ") and line.endswith("\n\n")
    return json.loads(line.removeprefix("data: "))


# ---------------------------------------------------------------------------
# _translate_sse_lines
# ---------------------------------------------------------------------------

class TestTranslateSseLines:
    def test_token_unescapes_newlines(self):
        # RhodyRAG sends \n as the literal two-char sequence \\n
        assert _data(_translate_sse_lines("token", "line1\\nline2"))["textResponse"] == "line1\nline2"

    def test_sources_keep_citation_fields(self):
        """Citation numbers, cited flags, titles, sections and links reach the frontend."""
        payload = json.dumps({
            "documents": [
                {"n": 1, "cited": True, "filename": "wifi.docling.json", "title": "URI ITSD Internal - Wi-Fi",
                 "uri": "https://sites.google.com/uri.edu/wiki/wi-fi",
                 "headings": ["URI ITSD Internal - Wi-Fi", "Wi-Fi", "Gaming Devices"], "score": 0.8, "text": "t"},
                {"n": 2, "cited": False, "filename": "quiz.docling.json", "title": "Quiz", "uri": "", "headings": []},
            ],
            "web": [{"n": 3, "cited": True, "title": "URI WiFi", "url": "https://rhodywifi.uri.edu/", "snippet": "s"}],
        })
        sources = _data(_translate_sse_lines("sources", payload))["sources"]
        assert [(s["n"], s["cited"], s["type"]) for s in sources] == [
            (1, True, "document"), (2, False, "document"), (3, True, "web"),
        ]
        assert sources[0]["title"] == "URI ITSD Internal - Wi-Fi"
        assert sources[0]["section"] == "Wi-Fi › Gaming Devices"
        assert sources[0]["url"] == "https://sites.google.com/uri.edu/wiki/wi-fi"
        assert sources[1]["url"] == "" and sources[1]["section"] == ""
        assert sources[2]["url"] == "https://rhodywifi.uri.edu/"

    def test_truncated_title_heading_not_repeated_as_section(self):
        title = "KB0010034 - Students - Self-Enroll in the Students Getting Started with Brightspace Course"
        payload = json.dumps({"documents": [{"n": 1, "cited": True, "title": title, "headings": [title[:80]]}]})
        assert _data(_translate_sse_lines("sources", payload))["sources"][0]["section"] == ""

    def test_sources_legacy_flat_array(self):
        """Backwards-compat: RhodyRAG used to send a flat list of doc sources."""
        sources = _data(_translate_sse_lines("sources", json.dumps([{"filename": "legacy.pdf"}])))["sources"]
        assert [(s["type"], s["title"]) for s in sources] == [("document", "legacy.pdf")]

    def test_sources_invalid_json_does_not_break_stream(self):
        assert _data(_translate_sse_lines("sources", "NOT_JSON{{{"))["sources"] == []

    def test_error_event_forwarded(self):
        assert _data(_translate_sse_lines("error", "LLM context limit exceeded"))["error"] == "LLM context limit exceeded"

    @pytest.mark.parametrize("event", [None, "ping", "done"])
    def test_other_events_dropped(self, event):
        assert _translate_sse_lines(event, "x") is None
