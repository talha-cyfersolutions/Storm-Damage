# ==============================================================================
# Production Dockerfile for Storm Damage Imagery API
# Multi-stage build with isolated virtualenv and non-root execution
# ==============================================================================

# ------------------------------------------------------------------------------
# 1. Builder Stage
# ------------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools and geospatial system libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgdal-dev \
    gdal-bin \
    libgeos-dev \
    libproj-dev \
    pkg-config \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies into virtual environment
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------------------------
# 2. Final Runtime Stage
# ------------------------------------------------------------------------------
FROM python:3.12-slim AS runner

WORKDIR /app

# Install runtime libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    gdal-bin \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-built virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create unprivileged application user
RUN groupadd -g 1001 appgroup && \
    useradd -u 1001 -g appgroup -s /bin/bash -m appuser

# Copy application source code
COPY . /app

# Ensure directories exist and have proper permissions
RUN mkdir -p /app/cog-storage && \
    chown -R appuser:appgroup /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    PORT=8000 \
    HOST=0.0.0.0

EXPOSE 8000

# Docker healthcheck querying the liveness probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health/live || exit 1

# Production WSGI/ASGI entrypoint
CMD ["gunicorn", "-c", "gunicorn_conf.py", "main:app"]
