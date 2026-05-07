#syntax=docker/dockerfile:1

# ===== Builder stage: create wheels and install packages =====
FROM python:3.12-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apk add --no-cache \
    build-base \
    postgresql-dev \
    pkgconfig

WORKDIR /build

COPY requirements.txt ./requirements.txt

RUN pip install --upgrade pip setuptools wheel && \
    pip wheel --disable-pip-version-check --no-cache-dir \
        --wheel-dir /wheels \
        -r requirements.txt yt-dlp

# ===== Final runtime stage: minimal image with only runtime deps =====
FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Mexico_City

RUN apk add --no-cache \
    ffmpeg \
    postgresql-client \
    tzdata && \
    cp /usr/share/zoneinfo/America/Mexico_City /etc/localtime && \
    echo "America/Mexico_City" > /etc/timezone

RUN adduser -D -s /sbin/nologin appuser

WORKDIR /code

COPY --from=builder /wheels /wheels
COPY requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir --no-compile --no-index \
    --find-links=/wheels -r /tmp/requirements.txt yt-dlp && \
    rm -rf /wheels /tmp/requirements.txt

COPY --chown=appuser:appuser . /code

RUN mkdir -p /var/log/gunicorn /code/downloads && \
    chown -R appuser:appuser /var/log/gunicorn /code/downloads && \
    chmod +x docker-entrypoint.sh

# Keep running as root for bind mount compatibility
# USER appuser

EXPOSE 8000

ENTRYPOINT ["sh", "/code/docker-entrypoint.sh"]
