#syntax=docker/dockerfile:1

# ===== Builder stage: create virtual environment and install packages =====
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements.txt yt-dlp

# ===== Final runtime stage: minimal image with only runtime deps =====
FROM python:3.12-slim

LABEL Author="Luis Mex"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/log/gunicorn /code/downloads

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /code
COPY . /code

RUN chmod +x docker-entrypoint.sh && \
    chown www-data:www-data /var/log/gunicorn

# Switch to non-root user for production deployments (remove bind mount first).
# USER www-data

ENTRYPOINT ["bash", "/code/docker-entrypoint.sh"]
