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

# Server host/port
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3001"))
