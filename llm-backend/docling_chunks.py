"""
docling_chunks.py
=================
Chunking for DoclingDocument uploads.

The upload service converts files and web pages with Docling and sends the
result as DoclingDocument JSON under a ".docling.json" filename. Those
documents are split with Docling's HybridChunker, which cuts at section
boundaries and prefixes each chunk with its heading path, and then chunks
under MIN_TOKENS are merged into a neighbour (HybridChunker never merges
across headings, so short sections would otherwise become tiny chunks that
crowd real answers out of the top results).

Each chunk carries the document's title, link (origin.uri) and the chunk's
section headings so answers can cite where they came from.

Docling and the tokenizer are imported lazily: they are only needed when a
DoclingDocument is embedded.
"""

import json
import threading
from pathlib import Path

import config

DOCLING_SUFFIX = ".docling.json"

# Token limits, counted with the embedding model's own tokenizer
MAX_TOKENS = 512
MIN_TOKENS = 128


class InvalidDocument(ValueError):
    """The upload claims to be a DoclingDocument but isn't one."""


_chunker = None
_chunker_lock = threading.Lock()


def is_docling_file(filename: str) -> bool:
    return filename.lower().endswith(DOCLING_SUFFIX)


def _get_chunker():
    """Return the shared HybridChunker (loads the tokenizer on first use)."""
    global _chunker
    with _chunker_lock:
        if _chunker is None:
            from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
            from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

            _chunker = HybridChunker(
                tokenizer=HuggingFaceTokenizer.from_pretrained(
                    model_name=config.DOCLING_TOKENIZER, max_tokens=MAX_TOKENS,
                )
            )
        return _chunker


def load(raw: bytes):
    """Parse DoclingDocument JSON. Raises InvalidDocument if it isn't one."""
    from docling_core.types.doc import DoclingDocument
    from pydantic import ValidationError

    try:
        return DoclingDocument.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise InvalidDocument(f"Not a valid DoclingDocument: {exc}") from exc


def chunk(doc) -> list[dict]:
    """Split a DoclingDocument into {"text", "headings"} chunks.

    "text" is the heading path plus the chunk's text, which is what gets
    embedded. A merged chunk keeps the headings of its larger part, so its
    citation names the section most of its text came from.
    """
    chunker = _get_chunker()
    count = chunker.tokenizer.count_tokens
    merged: list[dict] = []
    for c in chunker.chunk(dl_doc=doc):
        text = chunker.contextualize(chunk=c)
        if not text.strip():
            continue
        tokens = count(text)
        headings = list(c.meta.headings or [])
        if merged:
            prev = merged[-1]
            combined = prev["text"] + "\n\n" + text
            if min(prev["tokens"], tokens) < MIN_TOKENS and count(combined) <= MAX_TOKENS:
                if tokens > prev["tokens"]:
                    prev["headings"] = headings
                prev["text"], prev["tokens"] = combined, count(combined)
                continue
        merged.append({"text": text, "tokens": tokens, "headings": headings})
    return [{"text": c["text"], "headings": c["headings"]} for c in merged]


def doc_metadata(doc) -> dict:
    """Per-document citation metadata: title, link, original file name and type."""
    from docling_core.types.doc import DocItemLabel

    title = next(
        (t.text.strip() for t in doc.texts if t.label == DocItemLabel.TITLE and t.text.strip()),
        doc.name,
    )
    origin = doc.origin
    source = origin.filename if origin else doc.name
    return {
        "title": title,  # HTML <title>; falls back to the file name (e.g. DOCX)
        "uri": str(origin.uri) if origin and origin.uri else "",
        "source": source,
        "source_type": Path(source).suffix.lstrip(".").lower(),
    }
