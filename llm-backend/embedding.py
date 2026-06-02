"""
embedding.py
============
Embedding model construction, vector store access, and file embedding logic.
"""

import os
import re
import shutil
import tempfile
import threading
import uuid
from collections import defaultdict

from llama_index.core import SimpleDirectoryReader, StorageContext, VectorStoreIndex
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.litellm import LiteLLMEmbedding
from llama_index.vector_stores.lancedb import LanceDBVectorStore

import config

# Per-workspace locks to serialize concurrent LanceDB write operations.
# Multiple asyncio.to_thread() calls for the same workspace slug can run on
# different OS threads simultaneously; without serialization, concurrent
# Append and Overwrite transactions conflict inside LanceDB.
_lancedb_lock_map: dict[str, threading.Lock] = defaultdict(threading.Lock)
_lancedb_lock_map_lock = threading.Lock()


def _get_workspace_lock(slug: str) -> threading.Lock:
    """Return (and create if needed) the per-workspace threading.Lock."""
    with _lancedb_lock_map_lock:
        return _lancedb_lock_map[slug]


def build_embed_model(embed_model: str, api_key: str = "", embed_api_key: str = "") -> BaseEmbedding:
    """Construct an embedding model from a model-name string.

    Dispatch on model prefix:
      - 'direct-openai/<model>' — calls api.openai.com directly using embed_api_key,
        explicitly overriding OPENAI_API_BASE so the university gateway is bypassed.
        Example: 'direct-openai/text-embedding-3-large'
      - anything else — routes through the configured gateway (config.API_BASE)
        using api_key.
        Example: 'openai/its_rhodyrag_prod/titan-embed-text-v2-us'
    """
    if embed_model.startswith("direct-openai/"):
        openai_model = embed_model.replace("direct-openai/", "openai/", 1)
        return LiteLLMEmbedding(
            model_name=openai_model,
            api_base="https://api.openai.com/v1",
            api_key=embed_api_key,
        )

    return LiteLLMEmbedding(
        model_name=embed_model,
        api_base=config.API_BASE,
        api_key=api_key,
    )


def table_name(slug: str) -> str:
    """LanceDB table name for a workspace slug (already safe, but be defensive)."""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slug)
    return f"ws_{safe}"


def get_vector_store(slug: str) -> LanceDBVectorStore:
    import lancedb
    ldb = lancedb.connect(config.LANCEDB_DIR)
    tname = table_name(slug)
    mode = "append" if tname in ldb.table_names() else "overwrite"
    return LanceDBVectorStore(uri=config.LANCEDB_DIR, table_name=tname, mode=mode)


def init_workspace_table(slug: str) -> None:
    """No-op if the workspace's LanceDB table already exists.

    Called at startup and workspace-creation time purely to validate the table
    is reachable. New workspaces have no table yet — that is fine; LlamaIndex
    creates it automatically on the first embed. We never pre-create with a
    synthetic schema because the vector dimension is embed-model-dependent and
    only known at embed time.
    """
    import lancedb

    ldb = lancedb.connect(config.LANCEDB_DIR)
    tname = table_name(slug)
    # Only open (validate) if it already exists — don't create anything.
    if tname in ldb.list_tables().tables:
        ldb.open_table(tname)


def delete_workspace_file(slug: str, doc_id: str) -> int:
    """Delete all embedded chunks for a given doc_id from the workspace's table.

    Returns the number of chunks deleted. Returns 0 if the table doesn't exist
    or the doc had no embeddings.
    """
    import lancedb

    ws_lock = _get_workspace_lock(slug)
    with ws_lock:
        db = lancedb.connect(config.LANCEDB_DIR)
        tname = table_name(slug)
        if tname not in db.table_names():
            return 0

        tbl = db.open_table(tname)
        # Count matching rows before deletion
        try:
            count = len(tbl.search().where(f"doc_id = '{doc_id}'").to_list())
        except Exception:
            count = 0

        if count > 0:
            tbl.delete(f"doc_id = '{doc_id}'")
        return count


def embed_workspace_file(slug: str, filename: str, file_obj) -> tuple[int, str]:
    """Parse a file and embed its chunks into the workspace's LanceDB table.

    Returns (num_chunks, doc_id).
    """
    import db as _db

    ws = _db.get_workspace(slug)
    if ws is None:
        raise ValueError(f"Workspace '{slug}' not found")

    # Resolve embed model — workspace-level falls back to global
    embed_model = ws["embed_model"] or _db.get_settings()["embed_model"]
    api_key = ws["api_key"]
    embed_api_key = ws["embed_api_key"]

    if not embed_model:
        raise ValueError("No embedding model configured (check workspace or global settings)")

    # Write uploaded file to a temp directory for SimpleDirectoryReader
    tmp_dir = tempfile.mkdtemp()
    try:
        safe_name = re.sub(r'[^\w.\-]', '_', filename)
        tmp_path = os.path.join(tmp_dir, safe_name)
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file_obj, f)

        # Parse document
        documents = SimpleDirectoryReader(input_dir=tmp_dir).load_data()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not documents:
        return 0, ""

    # Assign a unique doc_id for tracking/deletion
    doc_id = str(uuid.uuid4())
    for doc in documents:
        doc.doc_id = doc_id          # sets ref_doc_id on all child nodes → top-level doc_id col in LanceDB
        doc.metadata["filename"] = filename
        doc.metadata["doc_id"] = doc_id
        # Exclude metadata keys from the text sent to the embedding model.
        # doc_id is only for deletion filtering; filename metadata can push
        # chunks over the 2048-token context window of models like qwen3-embed-8b.
        doc.excluded_embed_metadata_keys = ["doc_id", "filename"]

    # Cap chunk_size to 1900 tokens so metadata overhead never pushes a chunk
    # over a 2048-token embedding model context window (e.g. qwen3-embed-8b).
    chunk_size = min(ws["chunk_size"], 1900)

    # Chunk documents
    splitter = SentenceSplitter(
        chunk_size=chunk_size,
        chunk_overlap=ws["chunk_overlap"],
    )
    nodes = splitter.get_nodes_from_documents(documents)

    # Embed and store.
    # Acquire the per-workspace lock before touching LanceDB to prevent concurrent
    # Append/Overwrite transaction conflicts when multiple files are uploaded at the
    # same time for the same workspace (each runs in its own asyncio.to_thread worker).
    embed = build_embed_model(embed_model, api_key=api_key, embed_api_key=embed_api_key)
    ws_lock = _get_workspace_lock(slug)
    with ws_lock:
        vector_store = get_vector_store(slug)
        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        VectorStoreIndex(
            nodes,
            embed_model=embed,
            storage_context=storage_context,
        )

    return len(nodes), doc_id
