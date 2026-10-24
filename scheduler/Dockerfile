# Infochat scrape scheduler: POSTs /api/v1/scrape/run-due on the uploader every minute.
# Compose: docker compose up -d  (see docker-compose.yml)
FROM python:3.12-slim

WORKDIR /app
COPY scheduler.py .

# -u: unbuffered, so `docker compose logs` shows ticks as they happen
CMD ["python", "-u", "scheduler.py", "--loop"]
