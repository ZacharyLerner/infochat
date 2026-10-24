"""
test_embedding.py
=================
Unit tests for embedding.py.

External dependencies (S3 Vectors, LiteLLM, LlamaIndex) are mocked so tests
run fully offline.
"""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import embedding


class _NotFound(Exception):
    pass


class _Conflict(Exception):
    pass


@pytest.fixture()
def s3v():
    """A mock S3 Vectors client with real exception classes."""
    client = MagicMock()
    client.exceptions.NotFoundException = _NotFound
    client.exceptions.ConflictException = _Conflict
    with patch("embedding._s3v", return_value=client):
        yield client


def _ws(**overrides):
    ws = {
        "slug": "test-ws",
        "embed_model": "openai/text-embedding-3-small",
        "api_key": "key",
        "embed_api_key": "",
    }
    ws.update(overrides)
    return ws


def _embedding_response(vectors):
    resp = MagicMock()
    resp.data = [{"embedding": v} for v in vectors]
    return resp


# ---------------------------------------------------------------------------
# delete_workspace_file
# ---------------------------------------------------------------------------

class TestDeleteWorkspaceFile:
    def test_deletes_keys_built_from_chunk_count(self, s3v):
        assert embedding.delete_workspace_file("test-ws", "doc1", 3) == 3
        keys = s3v.delete_vectors.call_args.kwargs["keys"]
        assert keys == ["doc1#0", "doc1#1", "doc1#2"]


# ---------------------------------------------------------------------------
# embed_workspace_file
# ---------------------------------------------------------------------------

class TestEmbedWorkspaceFile:
    def _embed(self, ws, texts, filename="report.pdf"):
        """Run embed_workspace_file with parsing/splitting mocked to yield `texts`."""
        nodes = []
        for t in texts:
            node = MagicMock()
            node.get_content.return_value = t
            nodes.append(node)

        import db
        with (
            patch.object(db, "get_workspace", return_value=ws),
            patch("embedding.SimpleDirectoryReader") as mock_reader,
            patch("embedding.SentenceSplitter") as mock_splitter,
            patch("embedding.litellm.embedding",
                  side_effect=lambda input, **kw: _embedding_response([[0.1, 0.2]] * len(input))),
        ):
            mock_reader.return_value.load_data.return_value = [MagicMock()]
            mock_splitter.return_value.get_nodes_from_documents.return_value = nodes
            result = embedding.embed_workspace_file(ws["slug"], filename, io.BytesIO(b"content"))
        return result, mock_splitter

    def test_plain_file_split_by_sentence_splitter(self, s3v):
        (chunks, doc_id), mock_splitter = self._embed(_ws(), ["a", "b", "c"])
        assert mock_splitter.call_args.kwargs == {"chunk_size": 1024, "chunk_overlap": 104}
        assert chunks == 3
        assert len(doc_id) > 0

        vectors = s3v.put_vectors.call_args.kwargs["vectors"]
        assert [v["key"] for v in vectors] == [f"{doc_id}#0", f"{doc_id}#1", f"{doc_id}#2"]
        assert vectors[0]["data"] == {"float32": [0.1, 0.2]}
        assert vectors[0]["metadata"] == {"text": "a", "filename": "report.pdf", "doc_id": doc_id, "chunk_index": 0}

    def test_failed_upload_cleans_up_written_keys(self, s3v):
        s3v.put_vectors.side_effect = [None, RuntimeError("throttled")]
        with pytest.raises(RuntimeError):
            self._embed(_ws(), ["t"] * 600)
        deleted = sum(len(c.kwargs["keys"]) for c in s3v.delete_vectors.call_args_list)
        assert deleted == 600


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------

class TestRetrieve:
    def _retrieve(self, ws, top_k=5):
        with patch("embedding.litellm.aembedding",
                   new=AsyncMock(return_value=_embedding_response([[0.1, 0.2]]))):
            return asyncio.run(embedding.retrieve(ws, "question", top_k))

    def test_converts_distance_to_similarity(self, s3v):
        s3v.query_vectors.return_value = {"vectors": [
            {"key": "d#0", "distance": 0.25, "metadata": {"text": "chunk", "filename": "f.pdf", "doc_id": "d"}},
        ]}
        nodes = self._retrieve(_ws())
        assert nodes[0].score == pytest.approx(0.75)
        assert nodes[0].node.get_content() == "chunk"
        assert nodes[0].node.metadata["filename"] == "f.pdf"


# ---------------------------------------------------------------------------
# DoclingDocument uploads (.docling.json)
# ---------------------------------------------------------------------------

def _whitespace_tokenizer(max_tokens):
    """Offline stand-in for the Qwen tokenizer: one token per word."""
    from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer

    class _Words(BaseTokenizer):
        max_tokens: int

        def count_tokens(self, text: str) -> int:
            return len(text.split())

        def get_max_tokens(self) -> int:
            return self.max_tokens

        def get_tokenizer(self):
            return lambda text: len(text.split())

    return _Words(max_tokens=max_tokens)


@pytest.fixture()
def docling_chunker():
    """Use a HybridChunker with a word-count tokenizer instead of downloading one."""
    import docling_chunks
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    chunker = HybridChunker(tokenizer=_whitespace_tokenizer(docling_chunks.MAX_TOKENS))
    with patch("docling_chunks._chunker", chunker):
        yield chunker


def _wifi_doc(uri="https://sites.google.com/uri.edu/wiki/wi-fi", filename="URI ITSD Internal - Wi-Fi.html"):
    from docling_core.types.doc import DocItemLabel, DoclingDocument
    from docling_core.types.doc.document import DocumentOrigin
    doc = DoclingDocument(name="URI ITSD Internal - Wi-Fi")
    doc.origin = DocumentOrigin(filename=filename, mimetype="text/html", binary_hash=1, uri=uri)
    doc.add_text(label=DocItemLabel.TITLE, text="URI ITSD Internal - Wi-Fi")
    doc.add_heading(text="Wi-Fi", level=1)
    doc.add_text(label=DocItemLabel.TEXT, text="Short intro.")  # tiny section: merged into a neighbour
    doc.add_heading(text="Gaming Devices", level=2)
    doc.add_text(label=DocItemLabel.TEXT, text=" ".join(["Register the Xbox MAC address at rhodywifi."] * 40))
    doc.add_heading(text="Eduroam", level=2)
    doc.add_text(label=DocItemLabel.TEXT, text=" ".join(["Students connect to eduroam with SSO credentials."] * 40))
    return doc


class TestEmbedDoclingFile:
    def _embed(self, doc_or_bytes, filename="wifi.docling.json"):
        import db
        raw = doc_or_bytes if isinstance(doc_or_bytes, bytes) else doc_or_bytes.model_dump_json().encode()
        with (
            patch.object(db, "get_workspace", return_value=_ws()),
            patch("embedding.SimpleDirectoryReader") as mock_reader,
            patch("embedding.litellm.embedding",
                  side_effect=lambda input, **kw: _embedding_response([[0.1]] * len(input))),
        ):
            result = embedding.embed_workspace_file("test-ws", filename, io.BytesIO(raw))
        mock_reader.assert_not_called()
        return result

    def test_chunks_carry_citation_metadata(self, s3v, docling_chunker):
        (count, doc_id) = self._embed(_wifi_doc())
        metas = [v["metadata"] for c in s3v.put_vectors.call_args_list for v in c.kwargs["vectors"]]
        assert count == len(metas) == 2
        for i, m in enumerate(metas):
            assert m["title"] == "URI ITSD Internal - Wi-Fi"
            assert m["uri"] == "https://sites.google.com/uri.edu/wiki/wi-fi"
            assert m["source"] == "URI ITSD Internal - Wi-Fi.html"
            assert m["source_type"] == "html"
            assert m["filename"] == "wifi.docling.json"
            assert (m["doc_id"], m["chunk_index"]) == (doc_id, i)

        gaming, eduroam = metas
        # Embedded text is prefixed with the heading path (Docling puts the title first)
        assert "URI ITSD Internal - Wi-Fi\nWi-Fi\nGaming Devices\nRegister the Xbox" in gaming["text"]
        # The tiny intro section was merged; the merged chunk keeps its larger part's headings
        assert "Short intro." in gaming["text"]
        assert gaming["headings"] == ["URI ITSD Internal - Wi-Fi", "Wi-Fi", "Gaming Devices"]
        assert eduroam["headings"] == ["URI ITSD Internal - Wi-Fi", "Wi-Fi", "Eduroam"]
        assert "Xbox" not in eduroam["text"]

