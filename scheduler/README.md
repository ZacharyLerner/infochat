# Infochat scheduler

Starts the uploader's scheduled scrape jobs. Every 60 s it calls
`POST /api/v1/scrape/run-due` on an uploader server, which starts every scrape job that is due.
Uploader servers never schedule anything themselves. `scheduler.py` is a stdlib-only script.

Run **exactly one** scheduler for the whole deployment. A second one by mistake is harmless:
the uploader claims each due job with an S3 conditional write, so a job never starts twice. It
only doubles the requests.

## Run

The uploader's [`docker-compose.yml`](../uploader/docker-compose.yml) builds and starts it next to
the uploader: it reuses the uploader's `.env` for `APP_API_KEY`, calls `https://uploader:3000` and
trusts the uploader's self-signed cert through `SSL_CERT_FILE` (shared `certs` volume). Logs:
`docker compose logs -f scheduler` from `uploader/`, which print only started jobs and failures.
The development override doesn't start it, so local stacks never run the shared scrape jobs.

One tick by hand, without Docker: `UPLOADER_URL=... APP_API_KEY=... python3 scheduler.py`.
With Docker, from `uploader/`: `docker compose -f docker-compose.yml run --rm scheduler python scheduler.py`.
