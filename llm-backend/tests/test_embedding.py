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

import config
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
        "chunk_size": 1024,
        "chunk_overlap": 104,
    }
    ws.update(overrides)
    return ws


def _embedding_response(vectors):
    resp = MagicMock()
    resp.data = [{"embedding": v} for v in vectors]
    return resp


# ---------------------------------------------------------------------------
# index_name
# ---------------------------------------------------------------------------

class TestIndexName:
    def test_basic_slug(self):
        assert embedding.index_name("my-workspace-abc123") == "ws-my-workspace-abc123"

    def test_invalid_chars_replaced(self):
        assert embedding.index_name("My_Space ÄÖ-x1") == "ws-my-space-x1"

    def test_leading_hyphen_slug_is_valid(self):
        name = embedding.index_name("-abc123")
        assert name == "ws-abc123"

    def test_max_length_63(self):
        name = embedding.index_name("a" * 40 + "-" + "b" * 16 + "-" + "c" * 20)
        assert len(name) <= 63
        assert name[-1].isalnum()


# ---------------------------------------------------------------------------
# _embed_kwargs / _format_query
# ---------------------------------------------------------------------------

class TestEmbedKwargs:
    def test_gateway_model_gets_openai_prefix(self):
        kwargs = embedding._embed_kwargs(_ws(embed_model="its_rhodyrag_prod/qwen3-embed-8b-selfhosted"))
        assert kwargs["model"] == "openai/its_rhodyrag_prod/qwen3-embed-8b-selfhosted"
        assert kwargs["api_base"] == config.API_BASE

    def test_gateway_prefers_embed_api_key(self):
        kwargs = embedding._embed_kwargs(_ws(api_key="chat-key", embed_api_key="embed-key"))
        assert kwargs["api_key"] == "embed-key"

    def test_gateway_falls_back_to_api_key(self):
        kwargs = embedding._embed_kwargs(_ws(api_key="chat-key", embed_api_key=""))
        assert kwargs["api_key"] == "chat-key"

    def test_direct_openai_path(self):
        kwargs = embedding._embed_kwargs(_ws(
            embed_model="direct-openai/text-embedding-3-large", embed_api_key="sk-direct",
        ))
        assert kwargs == {
            "model": "openai/text-embedding-3-large",
            "api_base": "https://api.openai.com/v1",
            "api_key": "sk-direct",
        }

    def test_falls_back_to_global_embed_model(self):
        import db
        with patch.object(db, "get_settings", return_value={"embed_model": "openai/global-embed"}):
            kwargs = embedding._embed_kwargs(_ws(embed_model=""))
        assert kwargs["model"] == "openai/global-embed"

    def test_no_embed_model_raises(self):
        import db
        with patch.object(db, "get_settings", return_value={"embed_model": ""}):
            with pytest.raises(ValueError, match="No embedding model"):
                embedding._embed_kwargs(_ws(embed_model=""))

    def test_qwen3_queries_get_instruction_prefix(self):
        text = embedding._format_query(_ws(embed_model="its_rhodyrag_prod/qwen3-embed-8b"), "hours?")
        assert text.startswith("Instruct: ")
        assert text.endswith("\nQuery: hours?")

    def test_other_models_queries_unchanged(self):
        assert embedding._format_query(_ws(), "hours?") == "hours?"


# ---------------------------------------------------------------------------
# Index lifecycle
# ---------------------------------------------------------------------------

class TestIndexLifecycle:
    def test_create_probes_dimension_and_marks_text_non_filterable(self, s3v):
        with patch("embedding.litellm.embedding", return_value=_embedding_response([[0.1] * 7])):
            embedding.create_workspace_index(_ws())
        kwargs = s3v.create_index.call_args.kwargs
        assert kwargs["vectorBucketName"] == config.S3_VECTOR_BUCKET
        assert kwargs["indexName"] == "ws-test-ws"
        assert kwargs["dimension"] == 7
        assert kwargs["distanceMetric"] == "cosine"
        assert "text" in kwargs["metadataConfiguration"]["nonFilterableMetadataKeys"]

    def test_create_ignores_existing_index(self, s3v):
        s3v.create_index.side_effect = _Conflict()
        with patch("embedding.litellm.embedding", return_value=_embedding_response([[0.1]])):
            embedding.create_workspace_index(_ws())  # no exception

    def test_ensure_creates_missing_index(self, s3v):
        s3v.get_index.side_effect = _NotFound()
        with patch("embedding.create_workspace_index") as mock_create:
            embedding.ensure_workspace_index(_ws())
        mock_create.assert_called_once()

    def test_ensure_skips_existing_index(self, s3v):
        with patch("embedding.create_workspace_index") as mock_create:
            embedding.ensure_workspace_index(_ws())
        mock_create.assert_not_called()

    def test_delete_ignores_missing_index(self, s3v):
        s3v.delete_index.side_effect = _NotFound()
        embedding.delete_workspace_index("test-ws")  # no exception


# ---------------------------------------------------------------------------
# delete_workspace_file
# ---------------------------------------------------------------------------

class TestDeleteWorkspaceFile:
    def test_deletes_keys_built_from_chunk_count(self, s3v):
        assert embedding.delete_workspace_file("test-ws", "doc1", 3) == 3
        keys = s3v.delete_vectors.call_args.kwargs["keys"]
        assert keys == ["doc1#0", "doc1#1", "doc1#2"]

    def test_batches_of_500(self, s3v):
        embedding.delete_workspace_file("test-ws", "doc1", 1200)
        sizes = [len(c.kwargs["keys"]) for c in s3v.delete_vectors.call_args_list]
        assert sizes == [500, 500, 200]

    def test_zero_chunks_is_noop(self, s3v):
        assert embedding.delete_workspace_file("test-ws", "doc1", 0) == 0
        s3v.delete_vectors.assert_not_called()

    def test_missing_index_returns_zero(self, s3v):
        s3v.delete_vectors.side_effect = _NotFound()
        assert embedding.delete_workspace_file("test-ws", "doc1", 3) == 0


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

    def test_raises_when_workspace_not_found(self):
        import db
        with patch.object(db, "get_workspace", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                embedding.embed_workspace_file("bad-slug", "f.txt", io.BytesIO(b"x"))

    def test_returns_zero_chunks_when_no_documents_parsed(self, s3v):
        import db
        with (
            patch.object(db, "get_workspace", return_value=_ws()),
            patch("embedding.SimpleDirectoryReader") as mock_reader,
        ):
            mock_reader.return_value.load_data.return_value = []
            result = embedding.embed_workspace_file("test-ws", "f.txt", io.BytesIO(b""))
        assert result == (0, "")
        s3v.put_vectors.assert_not_called()

    def test_returns_chunk_count_and_doc_id(self, s3v):
        (chunks, doc_id), _ = self._embed(_ws(), ["a", "b", "c"])
        assert chunks == 3
        assert len(doc_id) > 0

        vectors = s3v.put_vectors.call_args.kwargs["vectors"]
        assert [v["key"] for v in vectors] == [f"{doc_id}#0", f"{doc_id}#1", f"{doc_id}#2"]
        assert vectors[0]["data"] == {"float32": [0.1, 0.2]}
        assert vectors[0]["metadata"] == {"text": "a", "filename": "report.pdf", "doc_id": doc_id}

    def test_blank_chunks_skipped(self, s3v):
        (chunks, _), _ = self._embed(_ws(), ["a", "   ", "b"])
        assert chunks == 2

    def test_chunks_truncated_at_6000_chars(self, s3v):
        self._embed(_ws(), ["x" * 8000])
        text = s3v.put_vectors.call_args.kwargs["vectors"][0]["metadata"]["text"]
        assert len(text) == 6000

    def test_chunk_size_capped_at_1700(self, s3v):
        _, mock_splitter = self._embed(_ws(chunk_size=2048), ["a"])
        assert mock_splitter.call_args.kwargs["chunk_size"] <= 1700

    def test_put_batched_at_500(self, s3v):
        self._embed(_ws(), ["t"] * 1100)
        sizes = [len(c.kwargs["vectors"]) for c in s3v.put_vectors.call_args_list]
        assert sizes == [500, 500, 100]

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

    def test_top_k_capped_at_100(self, s3v):
        s3v.query_vectors.return_value = {"vectors": []}
        self._retrieve(_ws(), top_k=500)
        assert s3v.query_vectors.call_args.kwargs["topK"] == 100

    def test_missing_index_returns_empty(self, s3v):
        s3v.query_vectors.side_effect = _NotFound()
        assert self._retrieve(_ws()) == []
