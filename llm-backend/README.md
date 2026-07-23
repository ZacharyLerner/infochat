# RhodyRAG — LLM RAG Backend

A multi-workspace Retrieval-Augmented Generation (RAG) API built for the University of Rhode Island. Each workspace has its own vector store, LLM settings, document collection, and query log. An optional SearXNG integration adds live web-search augmentation to any workspace.

---

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
- [Running the System](#running-the-system)
- [Managing the System](#managing-the-system)
- [API Reference](#api-reference)
- [Admin UI](#admin-ui)
- [Workspace Settings Reference](#workspace-settings-reference)
- [Data & Persistence](#data--persistence)

---

## Architecture

```
┌─────────────────────────────────────┐
│  FastAPI backend  (port 3001)       │
│  ├── LlamaIndex  (chunking/RAG)     │
│  ├── LiteLLM     (LLM proxy)        │
│  ├── LanceDB     (vector store)     │
│  └── SQLite      (workspace DB)     │
└──────────────┬──────────────────────┘
               │ internal network
┌──────────────▼──────────────────────┐
│  SearXNG  (port 8888 / 8080)        │
│  Google · Bing · DuckDuckGo · Wiki  │
└─────────────────────────────────────┘
```

**Key files:**

| File | Purpose |
|---|---|
| `main.py` | FastAPI app — all HTTP routes |
| `config.py` | Central config, reads from `.env` |
| `db.py` | SQLite ORM (workspaces + global settings) |
| `embedding.py` | File parsing, chunking, LanceDB embedding |
| `query.py` | RAG pipeline: retrieval, LLM calls, streaming, chat |
| `rewriter.py` | Optional query rewriting via secondary LLM |
| `searxng.py` | Async wrapper around SearXNG JSON API |
| `manager.py` | Propagates workspace events to downstream services |
| `prompts.py` | Default system prompt constants |

---

## Prerequisites

- **Docker + Docker Compose** (recommended for all environments)
- **Python 3.11+** (for local development without Docker)
- Access to the URI LLM gateway at `https://llmgw.its.uri.edu/v1` (or another OpenAI-compatible endpoint)

---

## Configuration

Copy `.env` and fill in values before starting:

```bash
cp .env .env.local   # optional: work from a copy
```

**`.env` variables:**

| Variable | Required | Default | Description |
|---|---|---|---|
| `ADMIN_API_KEY` | **Yes** | _(none)_ | API key required on all requests (`X-API-Key` header) |
| `OPENAI_API_BASE` | No | `https://llmgw.its.uri.edu/v1` | OpenAI-compatible LLM gateway URL |
| `LANCEDB_DIR` | No | `./lancedb` | Directory for the on-disk vector store |
| `DB_PATH` | No | `./settings.db` | Path to the SQLite database |
| `HOST` | No | `0.0.0.0` | Bind address for uvicorn |
| `PORT` | No | `3001` | Bind port |
| `SEARXNG_URL` | No | `http://localhost:8888` | SearXNG instance URL (auto-overridden inside Docker Compose to use the internal network) |
| `SEARXNG_SECRET_KEY` | No | `change-me-in-production` | SearXNG container secret — change in production |
| `FILE_SERVER_URL` | No | `http://10.140.2.31:3001` | Downstream file-server to notify on workspace events |
| `FILE_SERVER_API_KEY` | No | _(none)_ | Auth key for the file server |
| `CHAT_SERVER_URL` | No | `http://10.140.2.30:3000` | Downstream chat frontend to notify on workspace events |
| `CHAT_SERVER_API_KEY` | No | _(none)_ | Auth key for the chat server |

---

## Running the System

### Option A — Docker Compose (recommended)

**Production (detached, no hot-reload):**
```bash
docker compose up -d
```

**Development (live source mount + hot-reload):**
```bash
docker compose up
# docker-compose.override.yml is merged automatically
```

The backend is available at `http://localhost:3001`.  
SearXNG is available at `http://localhost:8888`.

### Option B — Local Python

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

Or equivalently:
```bash
python -m uvicorn main:app --host 0.0.0.0 --port 3001 --reload
```

### Option C — Docker (standalone, no SearXNG)

```bash
docker build -t rhodyrag .
docker run -p 3001:3001 --env-file .env rhodyrag
```

---

## Managing the System

### Start / Stop / Restart

```bash
# Start all services in background
docker compose up -d

# Stop without removing containers
docker compose stop

# Stop and remove containers (data volumes are preserved)
docker compose down

# Restart a single service
docker compose restart backend
docker compose restart searxng

# Full restart
docker compose down && docker compose up -d
```

### View Logs

```bash
# Follow all service logs
docker compose logs -f

# Follow backend only
docker compose logs -f backend

# Follow SearXNG only
docker compose logs -f searxng

# Last 100 lines
docker compose logs --tail=100
```

### Rebuild After Code Changes

```bash
docker compose build backend
docker compose up -d
```

### Upgrade SearXNG Image

```bash
docker compose pull searxng
docker compose up -d searxng
```

---

## API Reference

All endpoints require the header:
```
X-API-Key: <ADMIN_API_KEY>
```

Interactive Swagger docs are available at:
```
http://localhost:3001/docs
```
Click **Authorize** in the Swagger UI and enter your API key to test endpoints interactively.

### Authentication

| Method | Path | Description |
|---|---|---|
| `GET` | `/auth/verify` | Returns `200` if the API key is valid |

### Global Settings

| Method | Path | Description |
|---|---|---|
| `GET` | `/settings` | Get global default settings |
| `PUT` | `/settings` | Update global defaults |
| `GET` | `/defaults` | Get built-in default prompt text |

**Example — update global LLM model:**
```bash
curl -X PUT http://localhost:3001/settings \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"llm_model": "openai/gpt-4o"}'
```

### Workspaces

| Method | Path | Description |
|---|---|---|
| `GET` | `/workspaces` | List all workspaces |
| `POST` | `/workspace` | Create a workspace |
| `GET` | `/workspace/{slug}` | Get workspace details |
| `PUT` | `/workspace/{slug}` | Update workspace settings |
| `DELETE` | `/workspace/{slug}` | Delete a workspace (drops vector table, logs, and doc records) |

**Example — create a workspace:**
```bash
curl -X POST http://localhost:3001/workspace \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Workspace",
    "llm_model": "openai/its_rhodyrag_prod/gpt-4o",
    "temperature": 0.5,
    "top_n": 5,
    "searxng_enabled": false
  }'
```

### Documents

| Method | Path | Description |
|---|---|---|
| `POST` | `/workspace/{slug}/embed` | Upload and embed a file (PDF, DOCX, TXT) |
| `DELETE` | `/workspace/{slug}/embed/{doc_id}` | Delete an embedded file and its vectors |
| `GET` | `/docs/{slug}` | List tracked documents for a workspace |
| `DELETE` | `/docs/{slug}/{doc_id}` | Remove a document record |
| `DELETE` | `/docs/{slug}` | Remove all document records for a workspace |

**Example — embed a PDF:**
```bash
curl -X POST http://localhost:3001/workspace/my-workspace/embed \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -F "file=@/path/to/document.pdf"
```

### Querying

| Method | Path | Description |
|---|---|---|
| `POST` | `/workspace/{slug}/query` | Single-shot RAG query (returns full JSON response) |
| `POST` | `/workspace/{slug}/query/stream` | Streaming RAG query (SSE) |

**Example — single-shot query:**
```bash
curl -X POST http://localhost:3001/workspace/my-workspace/query \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the refund policy?"}'
```

**Example — streaming query:**
```bash
curl -X POST http://localhost:3001/workspace/my-workspace/query/stream \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question": "Summarize the key points."}'
```

### Chat Sessions (context-aware, multi-turn)

| Method | Path | Description |
|---|---|---|
| `POST` | `/workspace/{slug}/chat/session` | Create a new chat session; returns `session_id` |
| `POST` | `/workspace/{slug}/chat/{session_id}/stream` | Stream a chat message within a session (SSE) |
| `DELETE` | `/workspace/{slug}/chat/{session_id}` | Destroy a chat session |

**Example — multi-turn chat:**
```bash
# 1. Create session
SESSION=$(curl -s -X POST http://localhost:3001/workspace/my-workspace/chat/session \
  -H "X-API-Key: $ADMIN_API_KEY" | jq -r .session_id)

# 2. Send a message
curl -X POST http://localhost:3001/workspace/my-workspace/chat/$SESSION/stream \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "What documents are available?"}'
```

### Query Logs

| Method | Path | Description |
|---|---|---|
| `GET` | `/workspace/{slug}/logs` | List all query log entries (newest first) |
| `DELETE` | `/workspace/{slug}/logs` | Clear the log for a workspace |

---

## Admin UI

A built-in single-page admin interface is served at the root URL:
```
http://localhost:3001
```

Features:
- Login with `ADMIN_API_KEY`
- Create, configure, and delete workspaces
- Upload and manage embedded documents
- Run test queries and view streamed responses
- Inspect per-workspace query/chat logs

---

## Workspace Settings Reference

Settings marked **locked** cannot be changed after workspace creation (changing them would cause vector dimension mismatches or inconsistent retrieval).

| Setting | Type | Default | Locked | Description |
|---|---|---|---|---|
| `name` | string | — | No | Display name |
| `llm_model` | string | global default | No | LiteLLM model string (e.g. `openai/gpt-4o`) |
| `api_key` | string | — | No | API key forwarded to the LLM gateway |
| `temperature` | float | `0.7` | No | Sampling temperature (0 = deterministic, 1 = creative) |
| `system_prompt` | string | built-in | No | System prompt prepended to every query |
| `top_n` | int | `5` | No | Number of vector chunks to retrieve |
| `similarity_threshold` | float | `0.5` | No | Minimum cosine similarity for a chunk to be included |
| `max_tokens` | int | `1024` | No | Max tokens in a single LLM response |
| `searxng_enabled` | bool | `false` | No | Enable web-search augmentation |
| `searxng_num_results` | int | `3` | No | Number of web results per query (1–10) |
| `searxng_query_suffix` | string | — | No | Text appended to web search queries (e.g. `site:uri.edu`) |
| `rewrite_model` | string | — | No | LiteLLM model for query rewriting; blank = disabled |
| `rewrite_prompt` | string | built-in | No | System prompt for the query rewriter |
| `embed_model` | string | global default | **Yes** | Embedding model (e.g. `openai/its_rhodyrag_prod/titan-embed-text-v2-us`; use `direct-openai/<model>` to bypass the gateway) |
| `embed_api_key` | string | — | No | API key for the embedding model (only needed for `direct-openai/` models) |
| `chunk_size` | int | `1024` | **Yes** | Token size per chunk |
| `chunk_overlap` | int | `104` | **Yes** | Token overlap between consecutive chunks |

---

## Data & Persistence

The following files/directories are bind-mounted in Docker Compose and should be backed up:

| Path | Contents |
|---|---|
| `./lancedb/` | LanceDB vector store (one Lance table per workspace, named `ws_<slug>`) |
| `./settings.db` | SQLite database — workspace metadata and global settings |
| `./docs.json` | Flat-file document tracking (workspace slug → list of embedded documents) |
| `./logs/` | Per-workspace query/chat logs (one JSON file per workspace slug) |
| `./searxng/` | SearXNG configuration (`settings.yml`) |

**Backup example:**
```bash
tar czf rhodyrag-backup-$(date +%F).tar.gz lancedb/ settings.db docs.json logs/
```

**Reset a single workspace's vector data** (requires workspace recreation):
```bash
# Delete via API — drops LanceDB table, docs.json entries, and log file
curl -X DELETE http://localhost:3001/workspace/<slug> \
  -H "X-API-Key: $ADMIN_API_KEY"
```
