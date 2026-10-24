# Universal Chat

The chat frontend for Infochat workspaces (FastAPI + Jinja templates):

- `/<slug>`: a workspace's chat page. Answers stream from the RAG backend (llm-backend).
- `/embed.js`: the embeddable chat widget for other sites.
- `/settings`: per-workspace chat settings (welcome text, follow-up questions, default questions).
- `/api/workspaces`: workspace CRUD, called by llm-backend when a workspace is created, renamed or deleted.
- Chat attachments: images and documents are summarised for the conversation. Documents (PDF,
  DOCX, ...) are converted by the docling Lambda (see [`docling-pipeline`](../docling-pipeline));
  the summaries use the LLM gateway.

It keeps no local state. Chat settings live in the backend's S3 state bucket at
`workspaces/<slug>/chat.json` (see `store.py`), so several chat servers can run side by side.

## Configure `.env`

```
RAG_API_URL=http://<llm-backend-host>:3001
RAG_API_KEY=<llm-backend ADMIN_API_KEY>
LLM_GW_URL=https://llmgw.its.uri.edu/v1/chat/completions
LLM_GW_KEY=<LLM gateway key>

# S3 state, shared with llm-backend (defaults shown)
S3_STATE_BUCKET=zl-workspace-storage-727646498592-us-east-1-an
AWS_REGION=us-east-1
# Credentials: an instance role, or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY

# Name of the docling Lambda used for chat attachments (default shown)
DOCLING_FUNCTION_NAME=docling-md-converter
```

The AWS identity needs `s3:GetObject`, `PutObject` and `DeleteObject` on
`arn:aws:s3:::<state-bucket>/workspaces/*` and `lambda:InvokeFunction` on the docling Lambda,
and the bucket policy must allow it.

## Run with Docker

```bash
git clone --filter=blob:none --sparse https://github.com/ZacharyLerner/infochat.git
cd infochat && git sparse-checkout set universal-chat && cd universal-chat
# create .env as above

docker compose -f docker-compose.yml up -d --build   # production
docker compose logs -f chat
```

Plain `docker compose up -d --build` also merges `docker-compose.override.yml`, the development
setup: the source folder is mounted into the container and uvicorn reloads on every change.
The container restarts on failure and after a reboot. After `git pull`, run the same
`up -d --build` again. Server at `http://<host>:3000`.

Credentials from an EC2 instance role only reach a container if the instance's metadata
hop limit is at least 2
(`aws ec2 modify-instance-metadata-options --instance-id <id> --http-put-response-hop-limit 2`).
Otherwise put `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `.env`.

Without Docker (Python 3.11+): `python3 -m venv venv && venv/bin/pip install -r requirements.txt`,
then `venv/bin/uvicorn main:app --host 0.0.0.0 --port 3000` (add `--reload` for development).

## Tests

```bash
venv/bin/pip install pytest
venv/bin/python -m pytest
```

## Migrating from SQLite (one time)

Servers that ran the pre-S3 version have `universal_chat.db`. From this folder, with the S3
settings above:

```bash
docker compose -f docker-compose.yml run --rm -v "$PWD:/app" chat python migrate_to_s3.py           # dry run
docker compose -f docker-compose.yml run --rm -v "$PWD:/app" chat python migrate_to_s3.py --apply   # copy
```

(`-v` makes the `.db` visible in the container, since the image leaves it out. Without Docker,
use `venv/bin/python migrate_to_s3.py`.) Only workspaces that exist in the backend are copied, and
workspaces already in S3 are left alone. After checking the result, the `.db` file can be deleted.
