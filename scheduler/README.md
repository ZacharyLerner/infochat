# Infochat scheduler

Starts the uploader's scheduled scrape jobs. Every 60 s it calls
`POST /api/v1/scrape/run-due` on an uploader server, which starts every scrape job that is due.
Uploader servers never schedule anything themselves. `scheduler.py` is a stdlib-only script.

Run **exactly one** scheduler for the whole deployment. A second one by mistake is harmless:
the uploader claims each due job with an S3 conditional write, so a job never starts twice. It
only doubles the requests.

## Configure `.env`

```
UPLOADER_URL=http://<uploader-host>:3000
APP_API_KEY=<the uploader's APP_API_KEY>
```

Docker Compose passes these to the container. `APP_API_KEY` can be left empty only if the
uploader has none set.

## Run

```bash
git clone --filter=blob:none --sparse https://github.com/ZacharyLerner/infochat.git
cd infochat && git sparse-checkout set scheduler && cd scheduler
# create .env as above
docker compose up -d --build
docker compose logs -f scheduler      # prints only started jobs and failures
```

The container restarts on failure and after a reboot (`restart: unless-stopped`). After
`git pull`, run `docker compose up -d --build` again.

One tick by hand, without Docker: `UPLOADER_URL=... APP_API_KEY=... python3 scheduler.py`.
With Docker: `docker compose run --rm scheduler python scheduler.py`.
