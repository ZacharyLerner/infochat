"""file_processor.py — Document / image → Markdown + summary pipeline.

Stage 1A (image):   base64-encode → vision LLM (Maverick) → Markdown
Stage 1B (document): docling (local, CPU) → Markdown
Stage 2:            Markdown → one-sentence summary via Scout LLM

The gateway URL and API key are read from config.py (LLM_GW_URL / LLM_GW_KEY).
"""

from __future__ import annotations

import base64
import mimetypes
import os
import time
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model identifiers
# ---------------------------------------------------------------------------
IMAGE_MODEL = "its_direct/pt1-llama-4-maverick-17b-us"   # vision → markdown
SUMMARY_MODEL = "its_direct/pt1-llama-4-scout-17b-us"    # markdown → sentence

# Extensions treated as images (vision model path)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_image(path: str) -> bool:
    """Return True if the file's extension indicates an image."""
    ext = os.path.splitext(path)[1].lower()
    result = ext in IMAGE_EXTENSIONS
    logger.debug("is_image('%s'): ext='%s' → %s", os.path.basename(path), ext, result)
    return result


def _gateway_post(payload: dict, gw_url: str, api_key: str, label: str = "gateway") -> str:
    """POST to the LiteLLM gateway and return the assistant message content."""
    model = payload.get("model", "unknown")
    # Log the outbound request (truncate large content fields for readability)
    msg_preview = ""
    if payload.get("messages"):
        first_msg = payload["messages"][0]
        content = first_msg.get("content", "")
        if isinstance(content, str):
            msg_preview = content[:120].replace("\n", "\\n")
        elif isinstance(content, list):
            msg_preview = f"[multipart, {len(content)} parts]"
    logger.debug(
        "[%s] POST %s — model=%s msg_preview='%s...'",
        label, gw_url, model, msg_preview,
    )

    t0 = time.perf_counter()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(gw_url, headers=headers, json=payload, timeout=120)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.debug("[%s] response HTTP %d in %.0fms", label, resp.status_code, elapsed_ms)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.error(
            "[%s] HTTP error after %.0fms: %s — response body: %s",
            label, elapsed_ms, exc, exc.response.text[:500] if exc.response else "N/A",
        )
        raise
    except requests.RequestException as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.error("[%s] request failed after %.0fms: %s", label, elapsed_ms, exc)
        raise

    content: str = resp.json()["choices"][0]["message"]["content"].strip()
    logger.debug("[%s] raw response length=%d chars", label, len(content))

    # Strip fenced code block wrapper if the model wrapped its output
    if content.startswith("```"):
        lines = content.splitlines()
        start = 1
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        content = "\n".join(lines[start:end]).strip()
        logger.debug("[%s] stripped fenced code block, content now %d chars", label, len(content))

    return content


# ---------------------------------------------------------------------------
# Stage 1A: Image → Markdown (via vision LLM)
# ---------------------------------------------------------------------------

def image_to_markdown(path: str, gw_url: str, api_key: str) -> str:
    """Convert an image file to Markdown using the vision model."""
    file_size = os.path.getsize(path)
    mime = mimetypes.guess_type(path)[0] or "image/png"
    logger.info("[Stage 1A] image→markdown: file=%s size=%.1fKB mime=%s model=%s",
                os.path.basename(path), file_size / 1024, mime, IMAGE_MODEL)

    t0 = time.perf_counter()
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    logger.debug("[Stage 1A] base64 encoded: %d chars", len(b64))

    payload = {
        "model": IMAGE_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
                {
                    "type": "text",
                    "text": (
                        "Convert this image to markdown. Preserve all text, "
                        "structure, tables, and formatting as faithfully as possible. "
                        "Return only the markdown content, nothing else."
                    ),
                },
            ],
        }],
        "max_tokens": 4096,
    }
    result = _gateway_post(payload, gw_url, api_key, label="Stage1A-vision")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info("[Stage 1A] done in %.0fms — markdown=%d chars", elapsed_ms, len(result))
    return result


# ---------------------------------------------------------------------------
# Stage 1B: Document → Markdown (local docling, CPU-only)
# ---------------------------------------------------------------------------

def document_to_markdown(path: str) -> str:
    """Convert a document (PDF, DOCX, PPTX, etc.) to Markdown using docling."""
    file_size = os.path.getsize(path)
    logger.info("[Stage 1B] document→markdown: file=%s size=%.1fKB",
                os.path.basename(path), file_size / 1024)

    # Import here so docling is only loaded when actually needed
    logger.debug("[Stage 1B] importing docling…")
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        AcceleratorOptions,
        AcceleratorDevice,
    )

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False           # use embedded text; skip OCR
    pipeline_options.accelerator_options = AcceleratorOptions(
        num_threads=4,
        device=AcceleratorDevice.CPU,         # avoids MPS float64 crash on Apple Silicon
    )
    logger.debug("[Stage 1B] pipeline_options: do_ocr=False device=CPU num_threads=4")

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )

    t0 = time.perf_counter()
    logger.debug("[Stage 1B] starting docling conversion…")
    result = converter.convert(path)
    markdown = result.document.export_to_markdown()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info("[Stage 1B] docling done in %.0fms — markdown=%d chars", elapsed_ms, len(markdown))
    return markdown


# ---------------------------------------------------------------------------
# Stage 2: Markdown → one-sentence summary
# ---------------------------------------------------------------------------

def markdown_to_summary(markdown: str, gw_url: str, api_key: str) -> str:
    """Summarise a Markdown document in one concise sentence suitable for RAG retrieval."""
    logger.info("[Stage 2] markdown→summary: input=%d chars model=%s", len(markdown), SUMMARY_MODEL)

    # Warn if the document is very large (may hit token limits)
    if len(markdown) > 50_000:
        logger.warning(
            "[Stage 2] large document (%d chars) — consider chunking; may exceed model context",
            len(markdown),
        )

    payload = {
        "model": SUMMARY_MODEL,
        "messages": [{
            "role": "user",
            "content": (
                "Summarize the following document in one concise sentence "
                "suitable for semantic search retrieval. "
                "Return nothing other than the summary text.\n\n"
                + markdown
            ),
        }],
        "max_tokens": 150,
    }
    t0 = time.perf_counter()
    summary = _gateway_post(payload, gw_url, api_key, label="Stage2-summary")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info("[Stage 2] done in %.0fms — summary=%d chars", elapsed_ms, len(summary))
    logger.debug("[Stage 2] summary text: '%s'", summary)
    return summary


# ---------------------------------------------------------------------------
# Orchestration entry point
# ---------------------------------------------------------------------------

def process_upload(
    tmp_path: str,
    original_filename: str,
    gw_url: str,
    api_key: str,
) -> dict:
    """Run the full two-stage pipeline on an uploaded file.

    Args:
        tmp_path:          Absolute path to the saved temporary file.
        original_filename: The filename as supplied by the browser.
        gw_url:            LLM gateway URL (from config.LLM_GW_URL).
        api_key:           LLM gateway API key (from config.LLM_GW_KEY).

    Returns:
        {
            "filename": str,   # original filename
            "markdown": str,   # full document markdown (Stage 1)
            "summary":  str,   # one-sentence summary (Stage 2)
        }

    Raises:
        ValueError if LLM_GW_KEY is not configured.
        requests.HTTPError / Exception on processing failures.
    """
    if not api_key:
        raise ValueError(
            "LLM_GW_KEY is not set. Add it to your .env file: LLM_GW_KEY=sk-your-key-here"
        )

    t_total = time.perf_counter()
    logger.info("process_upload: START file='%s' tmp='%s' gw_url='%s'",
                original_filename, tmp_path, gw_url)

    # Stage 1 ─────────────────────────────────────────────────────────────────
    if is_image(tmp_path):
        logger.info("process_upload: routing to Stage 1A (vision model)")
        markdown = image_to_markdown(tmp_path, gw_url, api_key)
    else:
        logger.info("process_upload: routing to Stage 1B (docling)")
        markdown = document_to_markdown(tmp_path)

    # Stage 2 ─────────────────────────────────────────────────────────────────
    summary = markdown_to_summary(markdown, gw_url, api_key)

    total_ms = (time.perf_counter() - t_total) * 1000
    logger.info(
        "process_upload: DONE file='%s' total=%.0fms markdown=%d chars summary=%d chars",
        original_filename, total_ms, len(markdown), len(summary),
    )

    md_preview = markdown[:300] + (" [...]" if len(markdown) > 300 else "")
    logger.debug(
        "process_upload: summary (full):\n%s",
        summary,
    )
    logger.debug(
        "process_upload: markdown preview (first 300 chars):\n%s",
        md_preview,
    )

    return {
        "filename": original_filename,
        "markdown": markdown,
        "summary": summary,
    }
