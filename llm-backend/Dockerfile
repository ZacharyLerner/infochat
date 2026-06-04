# RhodyRAG — FastAPI backend
# Build:   docker build -t rhodyrag .
# Run:     docker run -p 3001:3001 --env-file .env rhodyrag
# Compose: docker compose up -d  (see docker-compose.yml)

FROM python:3.12-slim

# Install system deps needed by some Python packages (e.g. lancedb, pypdf)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create persistent data directories so volume mounts initialise cleanly
RUN mkdir -p lancedb

EXPOSE 3001

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3001"]
