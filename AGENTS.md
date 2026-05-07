# youtube-dl-vid

Django 4.2 REST API that downloads YouTube videos via yt-dlp.

## Quick start

```bash
docker compose up --build
```

- Web server at `http://localhost:8011`
- PostgreSQL at `localhost:51432`

## Architecture

- **Django app** `videos/` — contains models, views, serializers, and yt-dlp integration
- **`Youtube_dl_vid/`** — project settings, WSGI/ASGI entrypoints, URL routing
- **`videos/download.py`** — yt-dlp command builder + subprocess runner + download cleanup
- **`docker-entrypoint.sh`** — runs `makemigrations`, `migrate`, `collectstatic`, then `gunicorn`
- **`downloads/`** — downloaded video files stored here by yt-dlp

## API (prefix: `/api/v1/yt/`)

| Endpoint | Method | Purpose |
|---|---|---|
| `videos/` | POST | Submit URL → triggers yt-dlp download |
| `videos/` | GET | List submitted URLs |
| `videos/<uuid:pk>` | GET | Get URL download status |
| `categorias/` | GET/POST | List/create categories |
| `videos-uploaded/` | GET/POST | List/create uploaded video records |
| `videos-uploaded/<uuid:pk>` | GET | Download video file |

## Key facts

- **DEBUG=False** always; no `runserver` — only gunicorn via Docker entrypoint
- DB hostname inside Docker is `db`, not `localhost`
- **No lint / format / typecheck config** exists in the repo
- Tests: only vanilla `django.test.TestCase` (no pytest), no test runner command defined
- **Migrations are auto-applied** on container start (`makemigrations` + `migrate`)
- `collectstatic` runs on every container start (output to `staticfiles/`)
- yt-dlp merges best video+audio ≤720p into MP4; requires ffmpeg (installed in Dockerfile)
- Videos are downloaded to `downloads/` and streamed via `FileResponse`; old downloads for the same URL are cleaned up automatically
- Logging: Loguru to stdout + `/var/log/gunicorn/app.log`; Django logging to `/var/log/gunicorn/django.log`
