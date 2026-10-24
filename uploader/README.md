# AnythingLLM Uploader — Server Setup

Runs in Docker Compose on any Linux server with Docker (amd64 or arm64). No OCR or ML
libraries are needed: document conversion runs in [`docling-pipeline`](../docling-pipeline).

## 1. Install Docker and clone

```bash
# Amazon Linux 2023
sudo dnf install -y docker git && sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # log out and back in
# Compose plugin: https://docs.docker.com/compose/install/linux/

git clone --filter=blob:none --sparse https://github.com/ZacharyLerner/infochat.git
cd infochat && git sparse-checkout set uploader && cd uploader
```

## 2. Configure `.env`

Create a `.env` file in the project root:

```
RAG_API_URL=http://<llm-backend-host>:3001
RAG_API_KEY=<llm-backend ADMIN_API_KEY>
APP_API_KEY=<key required on /reembed, /copy and /scrape/run-due>

# S3 state, shared with llm-backend (defaults shown)
S3_STATE_BUCKET=zl-workspace-storage-727646498592-us-east-1-an
AWS_REGION=us-east-1
# Credentials: an instance role, or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
# (an instance role from inside Docker needs the EC2 metadata hop limit >= 2, see below)

# docling-pipeline queues (see ../docling-pipeline)
DOCLING_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/727646498592/docling-md-queue
DOCLING_RESULTS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/727646498592/<results queue>
```

The uploader keeps no local state. Workspace records, file records, scrape jobs and
each document's converted version live in the backend's state bucket under
`workspaces/<slug>/uploader/` (see `store.py`), so any number of uploader servers can
run side by side. The AWS identity needs `s3:GetObject`, `PutObject` and `DeleteObject`
on `arn:aws:s3:::<state-bucket>/workspaces/*` and `s3:ListBucket` on the bucket, and the
bucket policy must allow it. It also needs `sqs:SendMessage` on the jobs queue and
`sqs:ReceiveMessage` / `DeleteMessage` on the results queue.

### How documents are converted

The uploader doesn't run docling. Text files (`.txt .md .csv .json .xml`) are stored as
they are and embedded straight away; everything else, and every scraped page, goes through
[`docling-pipeline`](../docling-pipeline) (an SQS-triggered Lambda):

```
workspaces/<slug>/uploader/originals/<key>/<file>                temporary: uploader saves it, sends a job
workspaces/<slug>/uploader/converted/<key>/<name>.docling.json   kept: the Lambda writes it, posts the result
```

Originals are deleted as soon as their result is applied (converted, or failed for
good). The converted documents plus the records in `files.json` are everything needed
to rebuild a workspace's embeddings (`/reembed`).

Each uploader server polls `DOCLING_RESULTS_QUEUE_URL` (SQS gives each result to one of
them), embeds the converted document in the RAG backend and marks the file record
`ready`, or `failed` with the error. Until then the record is `converting` and has no
doc id; the upload page polls and updates the row. Scrape runs queue every page; a
page whose converted output hasn't changed (same hash) isn't embedded again.

## 3. Run

Production:

```bash
docker compose -f docker-compose.yml up -d --build
docker compose logs -f uploader
```

Plain `docker compose up -d --build` also merges `docker-compose.override.yml`, the development
setup: the source folder is mounted into the container and uvicorn reloads on every change.
The container restarts on failure and after a reboot. After `git pull`, run the same
`up -d --build` again. Server at `http://<host>:3000`, docs at `/docs`.

Credentials from an EC2 instance role only reach a container if the instance's metadata
hop limit is at least 2
(`aws ec2 modify-instance-metadata-options --instance-id <id> --http-put-response-hop-limit 2`).
Otherwise put `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `.env`.

Without Docker (Python 3.11+): `python3 -m venv venv && venv/bin/pip install -r requirements.txt`,
then `venv/bin/fastapi run main.py --host 0.0.0.0 --port 3000` (`fastapi dev` for reload).

## 4. Scheduled scrapes

Uploader servers don't schedule anything themselves. The [`scheduler`](../scheduler) service,
run on **one** host, calls `POST /api/v1/scrape/run-due` once a minute; the uploader claims each
due job with an S3 conditional write, so a job never runs twice at once. Without a scheduler,
schedules never fire (manual runs still work).

## 5. Rebuilding embeddings

- `POST /api/v1/workspaces/<slug>/reembed` embeds every stored converted document again, e.g. after a backend wipe or to rebuild a deleted workspace. Originals aren't kept, so documents can't be re-converted (e.g. after a docling upgrade) without uploading them again.
- `POST /api/v1/workspaces/<slug>/copy` with `{"source_workspace": "<other-slug>"}` copies another workspace's converted documents into this one and embeds them.

Both need the `X-API-Key: $APP_API_KEY` header.

## Migrating from SQLite (one time)

Servers that ran the pre-S3 uploader have `uploader_files.db` and `debug_uploads/`. From the
uploader folder, with the S3 settings above (`-v` makes them visible in the container, since the
image leaves them out; without Docker, use `venv/bin/python migrate_to_s3.py`):

```bash
docker compose run --rm -v "$PWD:/app" uploader python migrate_to_s3.py           # dry run
docker compose run --rm -v "$PWD:/app" uploader python migrate_to_s3.py --apply   # copy
```

Only workspaces that exist in the backend are copied. What `debug_uploads/` kept for each file
becomes its converted document; files without one are reported by `/reembed` until re-uploaded.
After checking the result, the `.db` file and `debug_uploads/` can be deleted.