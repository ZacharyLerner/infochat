"""Tests for file_processor.py — is_image, fenced-code stripping, routing."""
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

import file_processor
from file_processor import (
    is_image,
    IMAGE_EXTENSIONS,
    IMAGE_MODEL,
    SUMMARY_MODEL,
    _gateway_post,
    process_upload,
)


# ---------------------------------------------------------------------------
# is_image
# ---------------------------------------------------------------------------

class TestIsImage:
    @pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"])
    def test_image_extensions_return_true(self, ext):
        assert is_image(f"/tmp/file{ext}") is True

    @pytest.mark.parametrize("ext", [".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".csv", ".mp4"])
    def test_non_image_extensions_return_false(self, ext):
        assert is_image(f"/tmp/file{ext}") is False

    def test_uppercase_extension(self):
        # Extensions should be compared case-insensitively
        assert is_image("/tmp/photo.PNG") is True
        assert is_image("/tmp/scan.JPG") is True

    def test_no_extension(self):
        assert is_image("/tmp/noext") is False

    def test_path_with_directories(self):
        assert is_image("/very/deep/path/image.jpeg") is True
        assert is_image("/very/deep/path/doc.pdf") is False

    def test_image_extensions_set_completeness(self):
        """Ensure the set contains all documented extensions."""
        expected = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
        assert expected == IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# _gateway_post — fenced code block stripping
# ---------------------------------------------------------------------------

class TestGatewayPostFencedCodeStripping:
    """Test the fenced-code-block stripping logic inside _gateway_post."""

    def _make_mock_response(self, content: str):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": content}}]
        }
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_plain_content_returned_as_is(self):
        payload = {"model": "m", "messages": [{"role": "user", "content": "test"}]}
        mock_resp = self._make_mock_response("plain text response")
        with patch("requests.post", return_value=mock_resp):
            result = _gateway_post(payload, "http://gw.test", "key")
        assert result == "plain text response"

    def test_fenced_markdown_block_stripped(self):
        wrapped = "```markdown\n# Title\nContent here\n```"
        payload = {"model": "m", "messages": [{"role": "user", "content": "test"}]}
        mock_resp = self._make_mock_response(wrapped)
        with patch("requests.post", return_value=mock_resp):
            result = _gateway_post(payload, "http://gw.test", "key")
        assert result == "# Title\nContent here"
        assert "```" not in result

    def test_fenced_bare_backtick_block_stripped(self):
        wrapped = "```\nsome content\n```"
        payload = {"model": "m", "messages": [{"role": "user", "content": "test"}]}
        mock_resp = self._make_mock_response(wrapped)
        with patch("requests.post", return_value=mock_resp):
            result = _gateway_post(payload, "http://gw.test", "key")
        assert result == "some content"

    def test_no_closing_fence_not_stripped(self):
        """If there's no closing ``` the stripping logic leaves the tail intact."""
        content = "```\npartial block without closing"
        payload = {"model": "m", "messages": [{"role": "user", "content": "test"}]}
        mock_resp = self._make_mock_response(content)
        with patch("requests.post", return_value=mock_resp):
            result = _gateway_post(payload, "http://gw.test", "key")
        # First line (```) is stripped, rest remains
        assert "partial block without closing" in result
        assert not result.startswith("```")


# ---------------------------------------------------------------------------
# process_upload — orchestration
# ---------------------------------------------------------------------------

class TestProcessUpload:
    def test_raises_value_error_when_no_api_key(self):
        with pytest.raises(ValueError, match="LLM_GW_KEY"):
            process_upload(
                tmp_path="/tmp/fake.pdf",
                original_filename="fake.pdf",
                gw_url="http://gw.test",
                api_key="",
            )

    def test_routes_image_to_stage_1a(self, tmp_path):
        img_file = tmp_path / "photo.png"
        img_file.write_bytes(b"\x89PNG\r\n")

        with patch.object(file_processor, "image_to_markdown", return_value="# Image MD") as mock_img, \
             patch.object(file_processor, "markdown_to_summary", return_value="An image.") as mock_sum:
            result = process_upload(
                tmp_path=str(img_file),
                original_filename="photo.png",
                gw_url="http://gw.test",
                api_key="key",
            )
            mock_img.assert_called_once()
            mock_sum.assert_called_once()

        assert result["filename"] == "photo.png"
        assert result["markdown"] == "# Image MD"
        assert result["summary"] == "An image."

    def test_routes_document_to_stage_1b(self, tmp_path):
        doc_file = tmp_path / "report.pdf"
        doc_file.write_bytes(b"%PDF-1.4")

        with patch.object(file_processor, "document_to_markdown", return_value="# Doc MD") as mock_doc, \
             patch.object(file_processor, "markdown_to_summary", return_value="A document.") as mock_sum:
            result = process_upload(
                tmp_path=str(doc_file),
                original_filename="report.pdf",
                gw_url="http://gw.test",
                api_key="key",
            )
            mock_doc.assert_called_once()
            mock_sum.assert_called_once()

        assert result["filename"] == "report.pdf"
        assert result["markdown"] == "# Doc MD"
        assert result["summary"] == "A document."

    def test_result_keys(self, tmp_path):
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Hello world")

        with patch.object(file_processor, "document_to_markdown", return_value="Notes"), \
             patch.object(file_processor, "markdown_to_summary", return_value="Notes doc."):
            result = process_upload(
                tmp_path=str(txt_file),
                original_filename="notes.txt",
                gw_url="http://gw.test",
                api_key="key",
            )
        assert set(result.keys()) == {"filename", "markdown", "summary"}

    def test_image_to_markdown_not_called_for_doc(self, tmp_path):
        doc_file = tmp_path / "doc.docx"
        doc_file.write_bytes(b"PK")

        with patch.object(file_processor, "document_to_markdown", return_value="MD"), \
             patch.object(file_processor, "markdown_to_summary", return_value="Sum"), \
             patch.object(file_processor, "image_to_markdown") as mock_img:
            process_upload(
                tmp_path=str(doc_file),
                original_filename="doc.docx",
                gw_url="http://gw.test",
                api_key="key",
            )
            mock_img.assert_not_called()


# ---------------------------------------------------------------------------
# Model identifier constants
# ---------------------------------------------------------------------------

class TestModelConstants:
    def test_image_model_identifier(self):
        assert "maverick" in IMAGE_MODEL.lower()

    def test_summary_model_identifier(self):
        assert "scout" in SUMMARY_MODEL.lower()
