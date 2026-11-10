# Infochat — FastAPI backend
# Build:   docker build -t infochat .
# Run:     docker run -p 3001:3001 --env-file .env infochat
# Compose: docker compose up -d  (see docker-compose.yml)

FROM python:3.12-slim

# Install system deps needed by some Python packages (e.g. pypdf)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the chunking tokenizer into the image so the first DoclingDocument
# upload doesn't download it (must match DOCLING_TOKENIZER in config.py / .env)
ARG DOCLING_TOKENIZER=Qwen/Qwen3-Embedding-8B
RUN python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('${DOCLING_TOKENIZER}')"

# Copy application source
COPY . .

EXPOSE 3001

RUN mkdir /certs

# HTTPS with a self-signed cert made on first start and kept in the certs volume, so it survives
# rebuilds and callers can trust a copy of it (RAG_CA_FILE in universal-chat and the uploader).
CMD ["sh", "-c", "[ -f /certs/cert.pem ] || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
       -subj /CN=llm-backend -addext subjectAltName=${CERT_SAN} -keyout /certs/key.pem -out /certs/cert.pem \
     && exec python -m uvicorn main:app --host 0.0.0.0 --port 3001 \
       --ssl-keyfile /certs/key.pem --ssl-certfile /certs/cert.pem"]
