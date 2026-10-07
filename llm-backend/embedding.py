"""
embedding.py
============
Embedding calls (LiteLLM) and vector storage in AWS S3 Vectors.

Each workspace owns one S3 Vectors index inside the bucket named by
config.S3_VECTOR_BUCKET. The index is created when the workspace is created
(its dimension is probed from the workspace's embedding model) and deleted
with the workspace. No vectors are stored locally.

Vector keys are "<doc_id>#<chunk_number>". S3 Vectors cannot delete by
metadata filter, so deleting a document rebuilds its keys from the chunk
count recorded in docs.json.
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid

import boto3
import litellm
from llama_index.core import SimpleDirectoryReader
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import NodeWithScore, TextNode

import config
import docling_chunks

logger = logging.getLogger(__name__)

# S3 Vectors API limits
_MAX_BATCH = 500       # vectors/keys per put_vectors / delete_vectors call
_MAX_TOP_K = 100       # results per query_vectors call

# Texts per embedding request to the gateway
_EMBED_BATCH = 16

# Chunk text is stored as metadata for retrieval. Filterable metadata is
# capped at 2 KB per vector, so large fields must be declared non-filterable
# when the index is created (this cannot be changed afterwards).
# source_type stays filterable (e.g. to search only DOCX files).
_NON_FILTERABLE_KEYS = ["text", "filename", "title", "uri", "source", "headings"]

# Indexes created before DoclingDocument support only declare text and
# filename non-filterable, so citation fields count toward the 2 KB filterable
# limit there. Cap them so a long title or link can't make put_vectors fail.
_MAX_TITLE_CHARS = 200
_MAX_URI_CHARS = 800
_MAX_HEADINGS = 4
_MAX_HEADING_CHARS = 80

_QWEN3_QUERY_TASK = "Given a question, retrieve passages that answer the question"

_client = None


def _s3v():
    """Return the shared (thread-safe) boto3 S3 Vectors client."""
    global _client
    if _client is None:
        _client = boto3.client("s3vectors", region_name=config.AWS_REGION or None)
    return _client


# --- Embedding ---------------------------------------------------------------

def _resolve_embed_model(workspace: dict) -> str:
    import db as _db
    embed_model = workspace["embed_model"] or _db.get_settings()["embed_model"]
    if not embed_model:
        raise ValueError("No embedding model configured (check workspace or global settings)")
    return embed_model


def _embed_kwargs(workspace: dict) -> dict:
    """LiteLLM embedding kwargs for a workspace's embedding model.

    Dispatch on model prefix:
      - 'direct-openai/<model>' — calls api.openai.com directly using embed_api_key,
        bypassing the university gateway.
      - anything else — routes through the gateway (config.API_BASE) using
        embed_api_key if set, otherwise falling back to api_key. This lets
        a workspace's embedding model live under a different LiteLLM key/project
        than its chat model. Gateway model ids (e.g.
        'its_rhodyrag_prod/qwen3-embed-8b-selfhosted') get an 'openai/' prefix
        since the gateway is OpenAI-compatible.
    """
    embed_model = _resolve_embed_model(workspace)
    if embed_model.startswith("direct-openai/"):
        return {
            "model": embed_model.replace("direct-openai/", "openai/", 1),
            "api_base": "https://api.openai.com/v1",
            "api_key": workspace["embed_api_key"],
        }
    if not embed_model.startswith("openai/"):
        embed_model = f"openai/{embed_model}"
    return {
        "model": embed_model,
        "api_base": config.API_BASE,
        "api_key": workspace["embed_api_key"] or workspace["api_key"],
    }


def _format_query(workspace: dict, text: str) -> str:
    """Qwen3 embedding models expect an instruction prefix on queries (not documents)."""
    if "qwen3" in _resolve_embed_model(workspace).lower():
        return f"Instruct: {_QWEN3_QUERY_TASK}\nQuery: {text}"
    return text


def embed_texts(workspace: dict, texts: list[str]) -> list[list[float]]:
    """Embed document texts (blocking), batching requests to the gateway."""
    kwargs = _embed_kwargs(workspace)
    vectors = []
    for i in range(0, len(texts), _EMBED_BATCH):
        response = litellm.embedding(input=texts[i:i + _EMBED_BATCH], **kwargs)
        vectors.extend(d["embedding"] for d in response.data)
    return vectors


# --- Index lifecycle ---------------------------------------------------------

def index_name(slug: str) -> str:
    """S3 Vectors index name for a workspace slug.

    Index names allow only lowercase letters, digits, hyphens and dots
    (3–63 chars, alphanumeric at both ends). Slugs may contain underscores,
    non-ASCII letters or a leading hyphen, so sanitise and prefix.
    """
    safe = re.sub(r"[^a-z0-9-]+", "-", slug.lower())
    safe = re.sub(r"-{2,}", "-", safe).strip("-")
    return f"ws-{safe}"[:63].rstrip("-")


def create_workspace_index(workspace: dict) -> None:
    """Create the workspace's S3 Vectors index.

    The dimension is probed by embedding a short string with the workspace's
    model, which also validates the model and API key up front. An index that
    already exists is left as-is.
    """
    dimension = len(embed_texts(workspace, ["dimension probe"])[0])
    try:
        _s3v().create_index(
            vectorBucketName=config.S3_VECTOR_BUCKET,
            indexName=index_name(workspace["slug"]),
            dataType="float32",
            dimension=dimension,
            distanceMetric="cosine",
            metadataConfiguration={"nonFilterableMetadataKeys": _NON_FILTERABLE_KEYS},
        )
    except _s3v().exceptions.ConflictException:
        pass


def ensure_workspace_index(workspace: dict) -> None:
    """Create the workspace's index if it doesn't exist yet."""
    try:
        _s3v().get_index(
            vectorBucketName=config.S3_VECTOR_BUCKET,
            indexName=index_name(workspace["slug"]),
        )
    except _s3v().exceptions.NotFoundException:
        create_workspace_index(workspace)


def delete_workspace_index(slug: str) -> None:
    """Delete the workspace's index and all its vectors. Missing index is fine."""
    try:
        _s3v().delete_index(
            vectorBucketName=config.S3_VECTOR_BUCKET,
            indexName=index_name(slug),
        )
    except _s3v().exceptions.NotFoundException:
        pass


# --- Vectors -----------------------------------------------------------------

def _chunk_keys(doc_id: str, chunks: int) -> list[str]:
    return [f"{doc_id}#{i}" for i in range(chunks)]


def _delete_keys(slug: str, keys: list[str]) -> None:
    for i in range(0, len(keys), _MAX_BATCH):
        _s3v().delete_vectors(
            vectorBucketName=config.S3_VECTOR_BUCKET,
            indexName=index_name(slug),
            keys=keys[i:i + _MAX_BATCH],
        )


def delete_workspace_file(slug: str, doc_id: str, chunks: int) -> int:
    """Delete all embedded chunks for a doc_id. Returns the number of keys deleted."""
    if chunks <= 0:
        return 0
    try:
        _delete_keys(slug, _chunk_keys(doc_id, chunks))
    except _s3v().exceptions.NotFoundException:
        return 0
    return chunks


def _split_text_file(ws: dict, filename: str, file_obj) -> list[dict]:
    """Parse a plain file with SimpleDirectoryReader and split it into sentence chunks."""
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
        return []

    # Cap chunk_size to 1700 tokens. SentenceSplitter counts with tiktoken
    # (cl100k), but qwen3-embed-8b uses its own tokenizer which produces
    # higher counts for the same text — empirically up to ~10% more tokens.
    # 1700 tiktoken tokens ≈ ≤1870 qwen3 tokens, safely under the 2048 limit.
    chunk_size = min(ws["chunk_size"], 1700)

    splitter = SentenceSplitter(
        chunk_size=chunk_size,
        chunk_overlap=ws["chunk_overlap"],
    )
    nodes = splitter.get_nodes_from_documents(documents)

    # Hard-truncate each chunk to 6000 characters as a final safety net
    # against tokenizer variance (tiktoken vs qwen3 tokenizer disagreements).
    # 6000 chars ≈ 1500 tokens on average — well under the 2048-token limit.
    _MAX_CHUNK_CHARS = 6000
    texts = [n.get_content()[:_MAX_CHUNK_CHARS] for n in nodes]
    return [{"text": t} for t in texts if t.strip()]


def _split_docling_file(file_obj) -> list[dict]:
    """Chunk a DoclingDocument upload; each chunk carries citation metadata."""
    doc = docling_chunks.load(file_obj.read())
    meta = docling_chunks.doc_metadata(doc)
    meta["title"] = meta["title"][:_MAX_TITLE_CHARS]
    meta["source"] = meta["source"][:_MAX_TITLE_CHARS]
    if len(meta["uri"]) > _MAX_URI_CHARS:
        logger.warning("dropping %d-char uri for '%s'", len(meta["uri"]), meta["title"])
        meta["uri"] = ""
    chunks = []
    for c in docling_chunks.chunk(doc):
        chunk = {**meta, "text": c["text"]}
        if c["headings"]:
            chunk["headings"] = [h[:_MAX_HEADING_CHARS] for h in c["headings"][:_MAX_HEADINGS]]
        chunks.append(chunk)
    return chunks


def embed_workspace_file(slug: str, filename: str, file_obj) -> tuple[int, str]:
    """Parse a file, embed its chunks and store them in the workspace's index.

    DoclingDocument JSON (".docling.json", sent by the upload service) is split
    along its sections and stored with title/uri/headings for citations; any
    other file is parsed with SimpleDirectoryReader and split into sentences.

    Returns (num_chunks, doc_id).
    """
    import db as _db

    ws = _db.get_workspace(slug)
    if ws is None:
        raise ValueError(f"Workspace '{slug}' not found")

    if docling_chunks.is_docling_file(filename):
        chunks = _split_docling_file(file_obj)
    else:
        chunks = _split_text_file(ws, filename, file_obj)
    if not chunks:
        return 0, ""

    vectors = embed_texts(ws, [c["text"] for c in chunks])

    doc_id = str(uuid.uuid4())
    keys = _chunk_keys(doc_id, len(chunks))
    records = [
        {
            "key": key,
            "data": {"float32": vec},
            "metadata": {**c, "filename": filename, "doc_id": doc_id, "chunk_index": i},
        }
        for i, (key, c, vec) in enumerate(zip(keys, chunks, vectors))
    ]

    try:
        for i in range(0, len(records), _MAX_BATCH):
            _s3v().put_vectors(
                vectorBucketName=config.S3_VECTOR_BUCKET,
                indexName=index_name(slug),
                vectors=records[i:i + _MAX_BATCH],
            )
    except Exception:
        # Don't leave a partially-uploaded document behind
        try:
            _delete_keys(slug, keys)
        except Exception as exc:
            logger.warning("cleanup of partial upload %s in '%s' failed: %s", doc_id, slug, exc)
        raise

    return len(records), doc_id


async def retrieve(workspace: dict, text: str, top_k: int) -> list[NodeWithScore]:
    """Return the top_k nearest chunks to `text` as scored nodes.

    Scores are cosine similarity (1 - cosine distance). Returns an empty list
    if the workspace has no index.
    """
    response = await litellm.aembedding(input=[_format_query(workspace, text)], **_embed_kwargs(workspace))
    vector = response.data[0]["embedding"]

    try:
        result = await asyncio.to_thread(
            _s3v().query_vectors,
            vectorBucketName=config.S3_VECTOR_BUCKET,
            indexName=index_name(workspace["slug"]),
            queryVector={"float32": vector},
            topK=max(1, min(top_k, _MAX_TOP_K)),
            returnDistance=True,
            returnMetadata=True,
        )
    except _s3v().exceptions.NotFoundException:
        return []

    nodes = []
    for v in result["vectors"]:
        metadata = dict(v.get("metadata") or {})
        text = metadata.pop("text", "")
        nodes.append(NodeWithScore(
            node=TextNode(id_=v["key"], text=text, metadata=metadata),
            score=1.0 - v["distance"],
        ))
    return nodes
