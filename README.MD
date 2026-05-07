# youtube-dl-vid

Django 3.2 REST API that downloads YouTube videos via yt-dlp.

## Quick start

```bash
docker compose up --build
```

- Web server at `http://localhost:8011`
- PostgreSQL at `localhost:51432`

## API

All endpoints are under `/api/v1/yt/`.

### Videos

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/yt/videos/` | Submit a YouTube URL — triggers download |
| `GET` | `/api/v1/yt/videos/` | List all submitted URLs and their status |
| `GET` | `/api/v1/yt/videos/<uuid:pk>` | Check status of a specific URL |

**POST /api/v1/yt/videos/**

```json
{ "url": "https://www.youtube.com/watch?v=..." }
```

### Categories

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/yt/categorias/` | List categories |
| `POST` | `/api/v1/yt/categorias/` | Create a category |

### Uploaded videos

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/yt/videos-uploaded/` | List downloaded videos |
| `POST` | `/api/v1/yt/videos-uploaded/` | Manually create a video record |
| `GET` | `/api/v1/yt/videos-uploaded/<uuid:pk>` | Download the video file |

## Architecture

```
docker-compose.yaml
├── db        — PostgreSQL
└── web       — Django + gunicorn (port 8000 → host 8011)
```

- **`videos/`** — single Django app with models, views, serializers, and yt-dlp integration
- **`Youtube_dl_vid/`** — project settings, URL routing, wsgi/asgi entrypoints
- **`videos/functions_utils/services.py`** — yt-dlp subprocess wrapper
- **`media/videos/`** — downloaded video storage
- **`logs/`** — per-download log files
- **`docker-entrypoint.sh`** — runs `collectstatic` then starts gunicorn

## Tech stack

- Python 3.11, Django 3.2, Django REST Framework
- PostgreSQL, psycopg2
- yt-dlp + ffmpeg (installed in Docker image)
- Gunicorn, whitenoise, django-cors-headers
- Loguru for application logging
