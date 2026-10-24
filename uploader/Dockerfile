# Infochat uploader (FastAPI)
# Compose: docker compose up -d  (see docker-compose.yml; docker-compose.override.yml adds dev reload)
FROM python:3.12-slim

WORKDIR /app

# Dependencies first, so the layer is cached unless requirements change.
# curl_cffi ships manylinux wheels for amd64 and arm64, so no compiler is needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 3000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]
