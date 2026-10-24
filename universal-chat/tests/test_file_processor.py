"""Tests for file_processor.py — image detection, fence stripping, upload routing."""
import io
import json
from unittest.mock import patch, MagicMock

import pytest

import file_processor
from file_processor import is_image, _gateway_post, process_upload


# ---------------------------------------------------------------------------
# is_image
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/tmp/photo.png", True),
    ("/tmp/scan.JPG", True),   # case-insensitive
    ("/tmp/page.tif", True),
    ("/tmp/report.pdf", False),
    ("/tmp/noext", False),
])
def test_is_image(path, expected):
    assert is_image(path) is expected


# ---------------------------------------------------------------------------
# _gateway_post — fenced code block stripping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content,expected", [
    ("plain text response", "plain text response"),
    ("```markdown\n# Title\nContent here\n```", "# Title\nContent here"),
    ("```\nsome content\n```", "some content"),
])
def test_gateway_post_strips_fences(content, expected):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    with patch("requests.post", return_value=mock_resp):
        assert _gateway_post({"model": "m", "messages": []}, "http://gw.test", "key") == expected


# ---------------------------------------------------------------------------
# process_upload — orchestration
# ---------------------------------------------------------------------------

def test_process_upload_requires_api_key():
    with pytest.raises(ValueError, match="LLM_GW_KEY"):
        process_upload(tmp_path="/tmp/fake.pdf", original_filename="fake.pdf",
                       gw_url="http://gw.test", api_key="", workspace_id="ws")


@pytest.mark.parametrize("filename,used,unused", [
    ("photo.png", "image_to_markdown", "document_to_markdown"),
    ("report.pdf", "document_to_markdown", "image_to_markdown"),
])
def test_process_upload_routes_by_type(tmp_path, filename, used, unused):
    f = tmp_path / filename
    f.write_bytes(b"data")
    with patch.object(file_processor, used, return_value="# MD") as m_used, \
         patch.object(file_processor, unused) as m_unused, \
         patch.object(file_processor, "markdown_to_summary", return_value="Sum."):
        result = process_upload(tmp_path=str(f), original_filename=filename,
                                gw_url="http://gw.test", api_key="key", workspace_id="ws")
    m_used.assert_called_once()
    m_unused.assert_not_called()
    assert result == {"filename": filename, "markdown": "# MD", "summary": "Sum."}


# ---------------------------------------------------------------------------
# document_to_markdown — docling Lambda, file passed through S3
# ---------------------------------------------------------------------------

class _FakeLambda:
    def __init__(self, reply, function_error=None):
        self.reply, self.function_error, self.calls = reply, function_error, []

    def invoke(self, FunctionName, Payload):
        self.calls.append((FunctionName, json.loads(Payload)))
        resp = {"Payload": io.BytesIO(json.dumps(self.reply).encode())}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


@pytest.mark.parametrize("reply, function_error, ok", [
    ({"status": "success", "markdown": "# Report", "ocr": False, "truncated": False}, None, True),
    ({"status": "error", "error": "Document converted to empty content"}, None, False),
    ({"errorMessage": "Task timed out"}, "Unhandled", False),
])
def test_document_goes_through_s3_and_lambda(tmp_path, fake_s3, monkeypatch, reply, function_error, ok):
    f = tmp_path / "upload.pdf"
    f.write_bytes(b"%PDF-data")
    fake = _FakeLambda(reply, function_error)
    monkeypatch.setattr(file_processor, "_lambda", lambda: fake)
    stored, real_put = {}, fake_s3.put_object

    def put(**kw):  # record what was uploaded, then store it for real
        stored[kw["Key"]] = kw["Body"]
        return real_put(**kw)

    monkeypatch.setattr(fake_s3, "put_object", put)

    if ok:
        assert file_processor.document_to_markdown(str(f), "Q3 report.pdf", "ws") == "# Report"
    else:
        with pytest.raises(RuntimeError, match="Could not convert Q3 report.pdf"):
            file_processor.document_to_markdown(str(f), "Q3 report.pdf", "ws")

    [(function, payload)] = fake.calls
    [key] = stored
    assert function == file_processor.DOCLING_FUNCTION_NAME
    assert payload["key"] == key and key.startswith("workspaces/ws/chat/uploads/") and key.endswith("/Q3 report.pdf")
    assert stored[key] == b"%PDF-data"
    assert key not in fake_s3.objects  # temporary copy deleted, whatever the outcome
