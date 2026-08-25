"""
db.py
=====
SQLite-backed storage for workspaces and global settings.
"""

import random
import sqlite3
import string
from typing import Optional

import config


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist and run migrations."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                llm_model    TEXT NOT NULL DEFAULT '',
                api_key      TEXT NOT NULL DEFAULT '',
                temperature  REAL NOT NULL DEFAULT 0.7,
                system_prompt TEXT NOT NULL DEFAULT '',
                top_n        INTEGER NOT NULL DEFAULT 5,
                similarity_threshold REAL NOT NULL DEFAULT 0.5,
                chunk_size   INTEGER NOT NULL DEFAULT 1024,
                chunk_overlap INTEGER NOT NULL DEFAULT 104,
                embed_model  TEXT NOT NULL DEFAULT '',
                embed_api_key        TEXT NOT NULL DEFAULT '',
                max_tokens   INTEGER NOT NULL DEFAULT 1024,
                searxng_enabled INTEGER NOT NULL DEFAULT 0,
                searxng_num_results INTEGER NOT NULL DEFAULT 3,
                searxng_query_suffix TEXT NOT NULL DEFAULT 'site:uri.edu',
                rewrite_model TEXT NOT NULL DEFAULT '',
                rewrite_prompt TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                slug         TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                llm_model    TEXT NOT NULL DEFAULT '',
                api_key      TEXT NOT NULL DEFAULT '',
                temperature  REAL NOT NULL DEFAULT 0.7,
                system_prompt TEXT NOT NULL DEFAULT '',
                top_n        INTEGER NOT NULL DEFAULT 5,
                similarity_threshold REAL NOT NULL DEFAULT 0.5,
                chunk_size   INTEGER NOT NULL DEFAULT 1024,
                chunk_overlap INTEGER NOT NULL DEFAULT 104,
                embed_model  TEXT NOT NULL DEFAULT '',
                embed_api_key        TEXT NOT NULL DEFAULT '',
                max_tokens   INTEGER NOT NULL DEFAULT 1024,
                searxng_enabled INTEGER NOT NULL DEFAULT 0,
                searxng_num_results INTEGER NOT NULL DEFAULT 3,
                searxng_query_suffix TEXT NOT NULL DEFAULT '',
                rewrite_model TEXT NOT NULL DEFAULT '',
                rewrite_prompt TEXT NOT NULL DEFAULT ''
            )
        """)
        # Ensure the singleton settings row exists
        conn.execute(
            "INSERT OR IGNORE INTO settings (id) VALUES (1)"
        )

        # --- Migrations: add columns that may not exist yet ---
        _migrate_columns(conn, "workspaces", [
            ("embed_api_key", "TEXT NOT NULL DEFAULT ''"),
            ("max_tokens", "INTEGER NOT NULL DEFAULT 1024"),
            ("searxng_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("searxng_num_results", "INTEGER NOT NULL DEFAULT 3"),
            ("searxng_query_suffix", "TEXT NOT NULL DEFAULT ''"),
            ("rewrite_model", "TEXT NOT NULL DEFAULT ''"),
            ("rewrite_prompt", "TEXT NOT NULL DEFAULT ''"),
        ])
        _migrate_columns(conn, "settings", [
            ("embed_api_key", "TEXT NOT NULL DEFAULT ''"),
            ("max_tokens", "INTEGER NOT NULL DEFAULT 1024"),
            ("searxng_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("searxng_num_results", "INTEGER NOT NULL DEFAULT 3"),
            ("searxng_query_suffix", "TEXT NOT NULL DEFAULT ''"),
            ("rewrite_model", "TEXT NOT NULL DEFAULT ''"),
            ("rewrite_prompt", "TEXT NOT NULL DEFAULT ''"),
        ])


def _migrate_columns(conn, table: str, columns: list):
    """Add columns to a table if they don't already exist."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col_name, col_def in columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")


# --- Settings ----------------------------------------------------------------

def get_settings() -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return dict(row)


def update_settings(**fields) -> dict:
    """Update any subset of global settings fields."""
    allowed = {
        "llm_model", "api_key", "temperature", "system_prompt",
        "top_n", "similarity_threshold", "chunk_size", "chunk_overlap",
        "embed_model", "embed_api_key", "max_tokens",
        "searxng_enabled", "searxng_num_results", "searxng_query_suffix",
        "rewrite_model", "rewrite_prompt",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_settings()

    cols = ", ".join(f"{k} = ?" for k in updates)
    with _connect() as conn:
        conn.execute(
            f"UPDATE settings SET {cols} WHERE id = 1",
            tuple(updates.values()),
        )
    return get_settings()


# --- Workspaces --------------------------------------------------------------

def _row_to_dict(row) -> Optional[dict]:
    return dict(row) if row else None


def _generate_slug(name: str) -> str:
    """Generate a URL-safe slug from a workspace name."""
    base = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in name.lower())
    base = base.strip("-")[:40]
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
    return f"{base}-{suffix}"


def list_workspaces() -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM workspaces ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def create_workspace(
    name: str,
    llm_model: str = "",
    api_key: str = "",
    temperature: float = 0.7,
    system_prompt: str = "",
    top_n: int = 5,
    similarity_threshold: float = 0.5,
    chunk_size: int = 1024,
    chunk_overlap: int = 104,
    embed_model: str = "",
    embed_api_key: str = "",
    max_tokens: int = 1024,
    searxng_enabled: int = 0,
    searxng_num_results: int = 3,
    searxng_query_suffix: str = "",
    rewrite_model: str = "",
    rewrite_prompt: str = "",
) -> dict:
    """Create a new workspace. Falls back to global defaults for blank fields."""
    defaults = get_settings()
    slug = _generate_slug(name)

    with _connect() as conn:
        conn.execute(
            """INSERT INTO workspaces
               (slug, name, llm_model, api_key, temperature, system_prompt,
                top_n, similarity_threshold, chunk_size, chunk_overlap,
                embed_model, embed_api_key, max_tokens,
                searxng_enabled, searxng_num_results, searxng_query_suffix,
                rewrite_model, rewrite_prompt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                slug,
                name,
                llm_model or defaults["llm_model"],
                api_key or defaults["api_key"],
                temperature if temperature is not None else defaults["temperature"],
                system_prompt or defaults["system_prompt"],
                top_n if top_n is not None else defaults["top_n"],
                similarity_threshold if similarity_threshold is not None else defaults["similarity_threshold"],
                chunk_size if chunk_size is not None else defaults["chunk_size"],
                chunk_overlap if chunk_overlap is not None else defaults["chunk_overlap"],
                embed_model or defaults["embed_model"],
                embed_api_key if embed_api_key is not None else defaults["embed_api_key"],
                max_tokens if max_tokens is not None else defaults.get("max_tokens", 1024),
                searxng_enabled if searxng_enabled is not None else defaults.get("searxng_enabled", 0),
                searxng_num_results if searxng_num_results is not None else defaults.get("searxng_num_results", 3),
                searxng_query_suffix if searxng_query_suffix is not None else defaults.get("searxng_query_suffix", ""),
                rewrite_model or defaults.get("rewrite_model", ""),
                rewrite_prompt or defaults.get("rewrite_prompt", ""),
            ),
        )
    return get_workspace(slug)


def get_workspace(slug: str) -> Optional[dict]:
    """Return a workspace by slug, or None if it doesn't exist."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM workspaces WHERE slug = ?", (slug,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def update_workspace(slug: str, **fields) -> Optional[dict]:
    """Update any subset of settings fields. Returns the updated workspace."""
    allowed = {
        "name", "llm_model", "api_key", "temperature", "system_prompt",
        "top_n", "similarity_threshold", "embed_api_key", "max_tokens",
        "searxng_enabled", "searxng_num_results", "searxng_query_suffix",
        "rewrite_model", "rewrite_prompt",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_workspace(slug)

    cols = ", ".join(f"{k} = ?" for k in updates)
    with _connect() as conn:
        conn.execute(
            f"UPDATE workspaces SET {cols} WHERE slug = ?",
            (*updates.values(), slug),
        )
    return get_workspace(slug)


def delete_workspace(slug: str) -> bool:
    """Delete a workspace row by slug. Returns True if a row was deleted."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM workspaces WHERE slug = ?", (slug,))
    return cur.rowcount > 0
