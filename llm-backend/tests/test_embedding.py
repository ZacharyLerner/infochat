"""
test_embedding.py
=================
Unit tests for embedding.py.

External dependencies (LanceDB, LiteLLM, LlamaIndex) are mocked so tests
run fully offline.
"""

import io
import threading
from unittest.mock import MagicMock, patch, call

import pytest

import embedding


# ---------------------------------------------------------------------------
# table_name
# ---------------------------------------------------------------------------

class TestTableName:
    def test_basic_slug(self):
        assert embedding.table_name("my-workspace") == "ws_my-workspace"

    def test_slug_with_special_chars_sanitized(self):
        result = embedding.table_name("hello world!")
        # Spaces and ! become underscores
        assert result.startswith("ws_")
        assert " " not in result
        assert "!" not in result

    def test_alphanumeric_hyphens_underscores_preserved(self):
        slug = "abc_123-XYZ"
        assert embedding.table_name(slug) == f"ws_{slug}"

    def test_empty_slug(self):
        result = embedding.table_name("")
        assert result == "ws_"


# ---------------------------------------------------------------------------
# build_embed_model
# ---------------------------------------------------------------------------

class TestBuildEmbedModel:
    def test_gateway_path(self):
        """Non-direct-openai models should use the configured API_BASE."""
        with patch("embedding.LiteLLMEmbedding") as mock_cls:
            mock_cls.return_value = MagicMock()
            import config
            result = embedding.build_embed_model(
                "openai/text-embedding-3-small",
                api_key="gateway-key",
            )
            mock_cls.assert_called_once()
            kwargs = mock_cls.call_args.kwargs
            assert kwargs["model_name"] == "openai/text-embedding-3-small"
            assert kwargs["api_base"] == config.API_BASE
            assert kwargs["api_key"] == "gateway-key"

    def test_direct_openai_path(self):
        """direct-openai/ prefix must bypass the gateway and call OpenAI directly."""
        with patch("embedding.LiteLLMEmbedding") as mock_cls:
            mock_cls.return_value = MagicMock()
            result = embedding.build_embed_model(
                "direct-openai/text-embedding-3-large",
                embed_api_key="openai-key",
            )
            kwargs = mock_cls.call_args.kwargs
            assert kwargs["api_base"] == "https://api.openai.com/v1"
            assert kwargs["api_key"] == "openai-key"
            # Model name prefix must be translated: direct-openai/ → openai/
            assert kwargs["model_name"] == "openai/text-embedding-3-large"

    def test_direct_openai_uses_embed_api_key_not_api_key(self):
        """embed_api_key should be used for direct-openai path, not api_key."""
        with patch("embedding.LiteLLMEmbedding") as mock_cls:
            mock_cls.return_value = MagicMock()
            embedding.build_embed_model(
                "direct-openai/text-embedding-ada-002",
                api_key="should-be-ignored",
                embed_api_key="correct-key",
            )
            kwargs = mock_cls.call_args.kwargs
            assert kwargs["api_key"] == "correct-key"


# ---------------------------------------------------------------------------
# get_vector_store
# ---------------------------------------------------------------------------

class TestGetVectorStore:
    def test_uses_append_mode_when_table_exists(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "LANCEDB_DIR", str(tmp_path))

        mock_ldb = MagicMock()
        mock_ldb.table_names.return_value = ["ws_existing-ws"]

        with (
            patch("lancedb.connect", return_value=mock_ldb),
            patch("embedding.LanceDBVectorStore") as mock_vs,
        ):
            mock_vs.return_value = MagicMock()
            embedding.get_vector_store("existing-ws")
            mock_vs.assert_called_once()
            assert mock_vs.call_args.kwargs["mode"] == "append"

    def test_uses_overwrite_mode_when_table_absent(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "LANCEDB_DIR", str(tmp_path))

        mock_ldb = MagicMock()
        mock_ldb.table_names.return_value = []   # no tables

        with (
            patch("lancedb.connect", return_value=mock_ldb),
            patch("embedding.LanceDBVectorStore") as mock_vs,
        ):
            mock_vs.return_value = MagicMock()
            embedding.get_vector_store("brand-new-ws")
            assert mock_vs.call_args.kwargs["mode"] == "overwrite"


# ---------------------------------------------------------------------------
# _get_workspace_lock
# ---------------------------------------------------------------------------

class TestWorkspaceLock:
    def test_same_slug_returns_same_lock(self):
        lock1 = embedding._get_workspace_lock("ws-alpha")
        lock2 = embedding._get_workspace_lock("ws-alpha")
        assert lock1 is lock2

    def test_different_slugs_return_different_locks(self):
        lock_a = embedding._get_workspace_lock("lock-test-aaa")
        lock_b = embedding._get_workspace_lock("lock-test-bbb")
        assert lock_a is not lock_b

    def test_lock_is_threading_lock(self):
        lock = embedding._get_workspace_lock("type-check")
        assert isinstance(lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# delete_workspace_file
# ---------------------------------------------------------------------------

class TestDeleteWorkspaceFile:
    def _make_mock_lancedb(self, table_names, row_count):
        mock_ldb = MagicMock()
        mock_ldb.table_names.return_value = table_names
        mock_tbl = MagicMock()
        mock_tbl.search.return_value.where.return_value.to_list.return_value = [
            {} for _ in range(row_count)
        ]
        mock_ldb.open_table.return_value = mock_tbl
        return mock_ldb, mock_tbl

    def test_returns_zero_when_table_does_not_exist(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "LANCEDB_DIR", str(tmp_path))
        mock_ldb = MagicMock()
        mock_ldb.table_names.return_value = []
        with patch("lancedb.connect", return_value=mock_ldb):
            result = embedding.delete_workspace_file("no-table-slug", "some-doc-id")
        assert result == 0

    def test_returns_count_and_calls_delete(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "LANCEDB_DIR", str(tmp_path))
        mock_ldb, mock_tbl = self._make_mock_lancedb(["ws_del-ws"], 4)
        with patch("lancedb.connect", return_value=mock_ldb):
            count = embedding.delete_workspace_file("del-ws", "doc-abc")
        assert count == 4
        mock_tbl.delete.assert_called_once_with("doc_id = 'doc-abc'")

    def test_returns_zero_when_no_matching_rows(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "LANCEDB_DIR", str(tmp_path))
        mock_ldb, mock_tbl = self._make_mock_lancedb(["ws_empty-ws"], 0)
        with patch("lancedb.connect", return_value=mock_ldb):
            count = embedding.delete_workspace_file("empty-ws", "no-match-doc")
        assert count == 0
        mock_tbl.delete.assert_not_called()


# ---------------------------------------------------------------------------
# embed_workspace_file
# ---------------------------------------------------------------------------

class TestEmbedWorkspaceFile:
    def _workspace_dict(self):
        return {
            "slug": "test-ws",
            "embed_model": "openai/text-embedding-3-small",
            "api_key": "key",
            "embed_api_key": "",
            "chunk_size": 1024,
            "chunk_overlap": 104,
        }

    def test_raises_when_workspace_not_found(self):
        import db
        with patch.object(db, "get_workspace", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                embedding.embed_workspace_file("bad-slug", "f.txt", io.BytesIO(b"x"))

    def test_raises_when_no_embed_model_configured(self):
        ws = self._workspace_dict()
        ws["embed_model"] = ""
        import db
        with (
            patch.object(db, "get_workspace", return_value=ws),
            patch.object(db, "get_settings", return_value={"embed_model": ""}),
        ):
            with pytest.raises(ValueError, match="No embedding model"):
                embedding.embed_workspace_file("test-ws", "f.txt", io.BytesIO(b"x"))

    def test_returns_zero_chunks_when_no_documents_parsed(self):
        ws = self._workspace_dict()
        import db
        with (
            patch.object(db, "get_workspace", return_value=ws),
            patch.object(db, "get_settings", return_value={"embed_model": "openai/text-embedding-3-small"}),
            patch("embedding.SimpleDirectoryReader") as mock_reader,
        ):
            mock_reader.return_value.load_data.return_value = []
            result = embedding.embed_workspace_file("test-ws", "f.txt", io.BytesIO(b""))
        assert result == (0, "")

    def test_returns_chunk_count_and_doc_id(self):
        ws = self._workspace_dict()

        mock_doc = MagicMock()
        mock_doc.doc_id = ""
        mock_doc.metadata = {}
        mock_doc.excluded_embed_metadata_keys = []

        mock_node = MagicMock()
        mock_node.get_content.return_value = "some chunk text"
        mock_node.text = "some chunk text"

        import db
        with (
            patch.object(db, "get_workspace", return_value=ws),
            patch.object(db, "get_settings", return_value={"embed_model": "openai/text-embedding-3-small"}),
            patch("embedding.SimpleDirectoryReader") as mock_reader,
            patch("embedding.SentenceSplitter") as mock_splitter,
            patch("embedding.build_embed_model") as mock_embed,
            patch("embedding.get_vector_store") as mock_vs,
            patch("embedding.StorageContext") as mock_sc,
            patch("embedding.VectorStoreIndex") as mock_idx,
        ):
            mock_reader.return_value.load_data.return_value = [mock_doc]
            mock_splitter.return_value.get_nodes_from_documents.return_value = [
                mock_node, mock_node, mock_node
            ]
            mock_embed.return_value = MagicMock()
            mock_vs.return_value = MagicMock()
            mock_sc.from_defaults.return_value = MagicMock()

            chunks, doc_id = embedding.embed_workspace_file(
                "test-ws", "report.pdf", io.BytesIO(b"content")
            )

        assert chunks == 3
        assert len(doc_id) > 0   # UUID-like string

    def test_chunks_truncated_at_6000_chars(self):
        """Nodes with text > 6000 chars should be hard-truncated."""
        ws = self._workspace_dict()

        mock_doc = MagicMock()
        mock_doc.doc_id = ""
        mock_doc.metadata = {}
        mock_doc.excluded_embed_metadata_keys = []

        long_text = "x" * 8000
        mock_node = MagicMock()
        mock_node.get_content.return_value = long_text
        mock_node.text = long_text

        import db
        with (
            patch.object(db, "get_workspace", return_value=ws),
            patch.object(db, "get_settings", return_value={"embed_model": "openai/text-embedding-3-small"}),
            patch("embedding.SimpleDirectoryReader") as mock_reader,
            patch("embedding.SentenceSplitter") as mock_splitter,
            patch("embedding.build_embed_model"),
            patch("embedding.get_vector_store"),
            patch("embedding.StorageContext"),
            patch("embedding.VectorStoreIndex"),
        ):
            mock_reader.return_value.load_data.return_value = [mock_doc]
            mock_splitter.return_value.get_nodes_from_documents.return_value = [mock_node]

            embedding.embed_workspace_file("test-ws", "big.txt", io.BytesIO(b"x" * 8000))

        # The node's text attribute must have been truncated to 6000 chars
        assert mock_node.text == long_text[:6000]

    def test_chunk_size_capped_at_1700(self):
        """chunk_size > 1700 must be silently capped to 1700 for the splitter."""
        ws = {**self._workspace_dict(), "chunk_size": 2048, "chunk_overlap": 104}

        mock_doc = MagicMock()
        mock_doc.doc_id = ""
        mock_doc.metadata = {}
        mock_doc.excluded_embed_metadata_keys = []

        import db
        with (
            patch.object(db, "get_workspace", return_value=ws),
            patch.object(db, "get_settings", return_value={"embed_model": "openai/text-embedding-3-small"}),
            patch("embedding.SimpleDirectoryReader") as mock_reader,
            patch("embedding.SentenceSplitter") as mock_splitter,
            patch("embedding.build_embed_model"),
            patch("embedding.get_vector_store"),
            patch("embedding.StorageContext"),
            patch("embedding.VectorStoreIndex"),
        ):
            mock_reader.return_value.load_data.return_value = [mock_doc]
            mock_splitter.return_value.get_nodes_from_documents.return_value = []

            embedding.embed_workspace_file("test-ws", "doc.txt", io.BytesIO(b"text"))

        # SentenceSplitter must have been called with chunk_size <= 1700
        call_kwargs = mock_splitter.call_args.kwargs
        assert call_kwargs["chunk_size"] <= 1700
