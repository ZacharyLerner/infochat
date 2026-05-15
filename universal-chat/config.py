import os
from dotenv import load_dotenv

# Load .env from the project root (idempotent if already loaded).
load_dotenv()

# ---------------------------------------------------------------------------
# AnythingLLM connection
# ---------------------------------------------------------------------------
API_URL: str = os.getenv("AnythingLLM_API_URL", "")
API_KEY: str = os.getenv("AnythingLLM_API_Key", "")
HEADERS: dict = {"Authorization": f"Bearer {API_KEY}"}
