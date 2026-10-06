# VelaFlow Enterprise — API Container
# Multi-stage build for minimal image size
# Target: LXC container / Docker / Kubernetes

FROM python:3.11-slim AS base

LABEL maintainer="VelaFlow Contributors"
LABEL description="VelaFlow Enterprise API — Multi-tenant AI productivity platform"

# Security: run as non-root user
RUN groupadd -r velaflow && useradd -r -g velaflow -d /app -s /sbin/nologin velaflow

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# ── Dependencies stage ────────────────────────────────────────────────
FROM base AS deps

COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0" "pydantic>=2.10.0"

# ── Application stage ─────────────────────────────────────────────────
FROM deps AS app

COPY src/ ./src/
COPY config/.env.enterprise.example ./config/.env.example
COPY prompts/ ./prompts/

# Create data directories
RUN mkdir -p /app/data/medallion && chown -R velaflow:velaflow /app

USER velaflow

# Health check for Kubernetes liveness probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# Uvicorn with production settings
CMD ["python", "-m", "uvicorn", "brain.api.app:create_app", \
    "--factory", "--host", "0.0.0.0", "--port", "8000", \
    "--workers", "2", "--loop", "uvloop", "--http", "httptools"]
