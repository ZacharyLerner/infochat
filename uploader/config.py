"""
Centralised configuration -- loads environment variables once and exposes
them as module-level constants used by both main.py and scraper.py.

Required environment variables (set in .env):
    RAG_API_URL  -- Base URL of the RAG backend (e.g. http://host:8000)
    RAG_API_KEY  -- API key sent as X-API-Key header
"""

import os
from dotenv import load_dotenv

# Load .env from the project root (idempotent if already loaded).
load_dotenv()

# ---------------------------------------------------------------------------
# RAG backend connection
# ---------------------------------------------------------------------------
API_URL: str = os.getenv("RAG_API_URL", "").rstrip("/")
API_KEY: str = os.getenv("RAG_API_KEY", "")
HEADERS: dict = {"X-API-Key": API_KEY} if API_KEY else {}

APP_API_KEY: str = os.getenv("APP_API_KEY", "")

# ---------------------------------------------------------------------------
# S3 state bucket shared with the RAG backend (see store.py). Credentials come
# from the standard boto3 chain (AWS_PROFILE, AWS_ACCESS_KEY_ID/..., instance role).
# ---------------------------------------------------------------------------
S3_STATE_BUCKET: str = os.getenv("S3_STATE_BUCKET", "zl-workspace-storage-727646498592-us-east-1-an")
AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# docling-pipeline (see conversion.py): non-text documents are converted by a
# Lambda. Jobs go to DOCLING_QUEUE_URL; its results come back on
# DOCLING_RESULTS_QUEUE_URL, which every uploader server polls.
# ---------------------------------------------------------------------------
DOCLING_QUEUE_URL: str = os.getenv("DOCLING_QUEUE_URL", "")
DOCLING_RESULTS_QUEUE_URL: str = os.getenv("DOCLING_RESULTS_QUEUE_URL", "")

# ---------------------------------------------------------------------------
# Upload constraints
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024  # 100 MB

# ---------------------------------------------------------------------------
# File extensions that are already plain-text and never need Docling
# conversion (they can be uploaded directly to the RAG backend).
# ---------------------------------------------------------------------------
TEXT_EXTENSIONS: set[str] = {".txt", ".json", ".xml", ".md", ".csv"}
