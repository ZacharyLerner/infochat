# Infochat

Monorepo for the three Infochat services. Each folder deploys to its own server and has its own README, `requirements.txt`, and `.env`.

| Folder | Service |
|---|---|
| [`llm-backend/`](llm-backend/) | RAG backend (FastAPI + SearXNG, Docker Compose) |
| [`universal-chat/`](universal-chat/) | Embeddable chat frontend/server |
| [`uploader/`](uploader/) | Document uploader and website scraper |

## Deploying one service

Each server only needs its own folder:

```bash
git clone --filter=blob:none --sparse https://github.com/ZacharyLerner/infochat.git
cd infochat
git sparse-checkout set llm-backend   # or universal-chat / uploader
cd llm-backend
```

Updates: `git pull` from the repo root.
