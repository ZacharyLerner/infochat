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
# LLM Gateway (file processing — vision + summarisation)
# ---------------------------------------------------------------------------
LLM_GW_URL: str = os.getenv("LLM_GW_URL")
LLM_GW_KEY: str = os.getenv("LLM_GW_KEY")
