import os
from dotenv import load_dotenv

# Load .env from the project root (idempotent if already loaded).
load_dotenv()

# ---------------------------------------------------------------------------
# RhodyRAG (LLM_Backend_Dev) connection
# ---------------------------------------------------------------------------
API_URL: str = os.getenv("RAG_API_URL")
API_KEY: str = os.getenv("RAG_API_KEY", "")
HEADERS: dict = {"X-API-Key": API_KEY} if API_KEY else {}

# ---------------------------------------------------------------------------
# S3 state bucket shared with the RAG backend (see store.py). Credentials come
# from the standard boto3 chain (AWS_PROFILE, AWS_ACCESS_KEY_ID/..., instance role).
# ---------------------------------------------------------------------------
S3_STATE_BUCKET: str = os.getenv("S3_STATE_BUCKET", "zl-workspace-storage-727646498592-us-east-1-an")
AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")

# docling-pipeline Lambda that converts chat attachments (PDF, DOCX, ...) to Markdown.
# Invoked directly; the file is passed through the state bucket (see file_processor.py).
DOCLING_FUNCTION_NAME: str = os.getenv("DOCLING_FUNCTION_NAME", "docling-md-converter")

# ---------------------------------------------------------------------------
# LLM Gateway (file processing — vision + summarisation)
# ---------------------------------------------------------------------------
LLM_GW_URL: str = os.getenv("LLM_GW_URL")
LLM_GW_KEY: str = os.getenv("LLM_GW_KEY")
