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

# LanceDB storage directory
LANCEDB_DIR = os.getenv("LANCEDB_DIR", "./lancedb")

# SQLite database path
DB_PATH = os.getenv("DB_PATH", "./settings.db")

# Admin API key (read from ADMIN_API_KEY in .env)
APP_API_KEY = os.getenv("ADMIN_API_KEY", "")

# Interactive API docs (Swagger UI / ReDoc / raw OpenAPI schema) are served
# by FastAPI on internal routes that bypass this app's own auth dependency
# system, so they are unauthenticated by design. Disabled by default —
# enable only for local development by setting ENABLE_API_DOCS=1 in .env.
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
