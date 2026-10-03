# Infochat

A multi-workspace RAG (retrieval-augmented generation) backend for the University of Rhode Island. Each workspace has its own documents, vector index, and LLM settings, and can optionally add live web results from SearXNG.

## How it works

- **FastAPI backend** (`main.py`) serves the API under `/api` and an admin UI at `/`.
- **Embeddings and LLM calls** go through LiteLLM to the URI gateway (`https://llmgw.its.uri.edu/v1`).
- **Vectors** live in **AWS S3 Vectors**: one index per workspace (`ws-<slug>`) in the bucket set by `S3_VECTOR_BUCKET`. The index is created with the workspace and deleted with it.
- **Settings and document records** live as JSON in a regular S3 bucket (`S3_STATE_BUCKET`): `settings.json` for global settings, and `workspaces/<slug>/settings.json` and `docs.json` for each workspace. Updates use S3 conditional writes, so concurrent requests can't overwrite each other.
- **Query logs** are stored in the same bucket under `workspaces/<slug>/logs/<YYYY-MM-DD>/`: one file per single query, and one file per chat conversation holding all of its turns. The admin UI loads them 20 at a time, newest first, and can filter by date.
- The container keeps no state of its own (chat memory is rebuilt from the browser's history after a restart).
- **SearXNG** (optional) adds web search results to answers.

| File | Purpose |
|---|---|
| `main.py` | HTTP routes |
| `embedding.py` | File parsing, chunking, S3 Vectors storage and retrieval |
| `query.py` | RAG pipeline, streaming, chat sessions |
| `db.py` | Settings and document records in S3 |
| `rewriter.py`, `searxng.py`, `prompts.py` | Query rewriting, web search, default prompts |
| `manager.py` | Notifies the file and chat servers of workspace changes |

## Setup

Create a `.env` file:

```bash
ADMIN_API_KEY=<key required in the X-API-Key header>

# AWS (S3 Vectors)
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
S3_VECTOR_BUCKET=infochat-vectors   # default
S3_STATE_BUCKET=zl-workspace-storage-727646498592-us-east-1-an   # default
AWS_REGION=us-east-1                # default
```

The AWS user needs:
- on `arn:aws:s3vectors:<region>:<account>:bucket/<vector-bucket>/index/ws-*`: `s3vectors:CreateIndex`, `GetIndex`, `DeleteIndex`, `PutVectors`, `QueryVectors`, `GetVectors` and `DeleteVectors`
- on `arn:aws:s3:::<state-bucket>/settings.json` and `.../workspaces/*`: `s3:GetObject`, `PutObject` and `DeleteObject`, plus `s3:ListBucket` on the bucket

API keys stored in settings are never returned by the API; responses carry only a hint (`…a1b2`).

Other optional variables (`OPENAI_API_BASE`, `SEARXNG_URL`, `FILE_SERVER_URL`, `CHAT_SERVER_URL`, `ENABLE_API_DOCS`, ...) are listed with their defaults in `config.py`.

## Running

**Docker Compose (development):** mounts the source and reloads on code changes.

```bash
docker compose up -d
docker compose logs -f backend
```

**Docker Compose (production):** runs the built image without the dev override.

```bash
docker compose -f docker-compose.yml up -d --build
```

**Local Python:**

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 3001 --reload
```

Then open:

- Admin UI: http://localhost:3001 (log in with `ADMIN_API_KEY`)
- SearXNG: http://localhost:8888
- API docs: http://localhost:3001/docs (only when `ENABLE_API_DOCS=true`)

**Tests:**

```bash
python -m pytest
```

## Quick API example

```bash
H="X-API-Key: $ADMIN_API_KEY"

# Create a workspace (this also creates its S3 Vectors index)
curl -X POST localhost:3001/api/workspace -H "$H" -H "Content-Type: application/json" \
  -d '{"name": "Demo", "api_key": "<gateway key>", "embed_model": "its_rhodyrag_prod/qwen3-embed-8b-selfhosted"}'

# Upload a document
curl -X POST localhost:3001/api/workspace/<slug>/embed -H "$H" -F "file=@doc.pdf"

# Ask a question
curl -X POST localhost:3001/api/workspace/<slug>/query -H "$H" -H "Content-Type: application/json" \
  -d '{"question": "What are the library hours?"}'
```

The embedding model, chunk size and chunk overlap are fixed once a workspace is created.
