"""
config.py
=========
Central configuration loaded from environment variables (via .env file).
"""

import os

from dotenv import load_dotenv

load_dotenv()

# LLM gateway base URL (OpenAI-compatible)
API_BASE = os.getenv("OPENAI_API_BASE", "https://llmgw.its.uri.edu/v1")

# AWS S3 Vectors: pre-created vector bucket holding one index per workspace.
# Credentials come from the standard boto3 chain (AWS_PROFILE, AWS_ACCESS_KEY_ID/
# AWS_SECRET_ACCESS_KEY, instance role, ...).
S3_VECTOR_BUCKET = os.getenv("S3_VECTOR_BUCKET", "infochat-vectors")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Hugging Face tokenizer used to size DoclingDocument chunks. It should match
# the embedding model so chunk limits are counted in the tokens it sees.
DOCLING_TOKENIZER = os.getenv("DOCLING_TOKENIZER", "Qwen/Qwen3-Embedding-8B")

# S3 bucket (regular, not vector) holding global settings, workspace settings
# and document records as JSON — see db.py.
S3_STATE_BUCKET = os.getenv("S3_STATE_BUCKET", "zl-workspace-storage-727646498592-us-east-1-an")

# CloudWatch Logs group for the activity log (see activity.py); blank turns it off.
ACTIVITY_LOG_GROUP = os.getenv("ACTIVITY_LOG_GROUP", "infochat-activity")

# Admin API key (read from ADMIN_API_KEY in .env)
APP_API_KEY = os.getenv("ADMIN_API_KEY", "")

# Addresses (IPs or CIDRs, comma-separated; "*" = anywhere) allowed to send the admin key in
# X-API-Key: chat and the uploader. Everyone else logs in to the admin UI (session cookie) or
# uses a workspace key. Set for production in docker-compose.yml.
TRUSTED_SERVICE_IPS = os.getenv("TRUSTED_SERVICE_IPS", "127.0.0.1,::1")

# Interactive API docs (Swagger UI / ReDoc / raw OpenAPI schema) are served
# by FastAPI on internal routes that bypass this app's own auth dependency
# system, so they are unauthenticated by design. Disabled by default —
# enable only for local development by setting ENABLE_API_DOCS=1 in .env.
# Path prefix the app is served under (e.g. /admin behind the campus load balancer); "" = root.
ROOT_PATH = os.getenv("ROOT_PATH", "").rstrip("/")

ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").strip().lower() in ("1", "true", "yes")

# Server host/port
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3001"))

# SearXNG instance URL (used for web search augmentation)
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")

# --- Downstream service propagation ------------------------------------------
# File-server (document upload/management)
FILE_SERVER_URL = os.getenv("FILE_SERVER_URL", "http://10.140.2.31:3001")
FILE_SERVER_API_KEY = os.getenv("FILE_SERVER_API_KEY", "")

# Chat-frontend server (workspace UI / chat)
CHAT_SERVER_URL = os.getenv("CHAT_SERVER_URL", "http://10.140.2.30:3000")
CHAT_SERVER_API_KEY = os.getenv("CHAT_SERVER_API_KEY", "")

# --- Chatbot intake (unauthenticated, campus-network only; see intake.py) -----
# Models and gateway key given to bots created through /api/intake. Blank falls
# back to the global settings, so these only need setting when the global
# settings are empty or should differ for requested bots.
INTAKE_LLM_MODEL = os.getenv("INTAKE_LLM_MODEL", "")
INTAKE_EMBED_MODEL = os.getenv("INTAKE_EMBED_MODEL", "")
INTAKE_API_KEY = os.getenv("INTAKE_API_KEY", "")

# Public base URLs people reach chat and the uploader at (through the load balancer).
# Used only to build the links intake returns; FILE_SERVER_URL / CHAT_SERVER_URL above
# are the addresses this server itself calls.
PUBLIC_CHAT_URL = os.getenv("PUBLIC_CHAT_URL", "https://infochat.its.uri.edu/chat").rstrip("/")
PUBLIC_UPLOAD_URL = os.getenv("PUBLIC_UPLOAD_URL", "https://infochat.its.uri.edu/upload").rstrip("/")
