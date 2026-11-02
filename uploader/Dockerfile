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

# Self-signed cert: the campus load balancer needs HTTPS to this server but doesn't validate it.
# The scheduler trusts this cert and reaches the uploader as https://uploader:3000 (docker-compose.yml).
RUN mkdir /certs && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -subj "/CN=infochat.its.uri.edu" \
    -addext "subjectAltName=DNS:infochat.its.uri.edu,DNS:uploader,DNS:localhost" \
    -keyout /certs/key.pem -out /certs/cert.pem

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000", \
     "--ssl-keyfile", "/certs/key.pem", "--ssl-certfile", "/certs/cert.pem"]
