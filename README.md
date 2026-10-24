# Infochat

Monorepo for the Infochat services. Each folder deploys to its own server and has its own README and `.env`. Every service except docling-pipeline (a Lambda) runs with Docker Compose.

| Folder | Service |
|---|---|
| [`llm-backend/`](llm-backend/) | RAG backend (FastAPI + SearXNG, Docker Compose) |
| [`universal-chat/`](universal-chat/) | Embeddable chat frontend/server (Docker Compose) |
| [`uploader/`](uploader/) | Document uploader and website scraper (Docker Compose) |
| [`scheduler/`](scheduler/) | Starts due scrape jobs on the uploader once a minute; run exactly one (Docker Compose) |
| [`docling-pipeline/`](docling-pipeline/) | docling converter (DoclingDocument JSON for llm-backend): S3 upload → SQS → Lambda (ECR image) |

## State

None of the services keep local state. Everything lives in the backend's S3 state bucket
(`S3_STATE_BUCKET`), under one prefix per workspace, so each service can run on several servers:

| Key | Owner |
|---|---|
| `workspaces/<slug>/settings.json`, `docs.json`, `logs/` | llm-backend |
| `workspaces/<slug>/uploader/` (records, scrape jobs, converted documents; originals only while converting) | uploader |
| `workspaces/<slug>/chat.json` (welcome text, follow-ups, default questions) | universal-chat |

Each service (except the scheduler) needs `S3_STATE_BUCKET`, `AWS_REGION` and AWS credentials (an instance role or
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) that the bucket policy allows. The services run in
containers, so an EC2 instance role only reaches them if the instance metadata hop limit is at least 2
(`aws ec2 modify-instance-metadata-options --instance-id <id> --http-put-response-hop-limit 2`);
otherwise put the keys in `.env`. Servers that ran the
older SQLite versions of universal-chat or the uploader migrate once with
`migrate_to_s3.py --apply` in that folder (dry run without `--apply` first; the READMEs show how in Docker).

## Deploying one service

Each server only needs its own folder:

```bash
git clone --filter=blob:none --sparse https://github.com/ZacharyLerner/infochat.git
cd infochat
git sparse-checkout set llm-backend   # or universal-chat / uploader / scheduler
cd llm-backend
# create .env (see the folder's README)
docker compose -f docker-compose.yml up -d --build
```

`-f docker-compose.yml` skips `docker-compose.override.yml`, the development setup (source mount and
auto-reload) that a plain `docker compose up` merges in. Updates: `git pull` from the repo root, then the
same `up -d --build`. The scheduler can share a host with the uploader.
