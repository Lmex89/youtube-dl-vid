# youtube-dl-vid

A Django 4.2 REST API that downloads videos via yt-dlp (YouTube, Facebook, and
other supported sites) as MP4, tracks real download progress, and serves the
resulting files. Containerized with Docker Compose (Django + PostgreSQL).

## Features

- **Async downloads** — `POST videos-uploaded/` returns `202` immediately and
  downloads in a background thread
- **Real progress** — live yt-dlp download percentage (`0-100`) persisted to DB
  and exposed via a polling endpoint
- **Any yt-dlp source** — YouTube, Facebook, etc. (requires ffmpeg to merge)
- **MP4 output** — best quality video+audio ≤720p merged to MP4
- **Categorization** — organize downloaded videos into categories
- **Rate limiting** — per-endpoint limits with JSON `429` responses
- **Auto-cleanup** — re-downloading a URL removes previous copies
- **Structured JSON logging** — Loguru with request-id correlation
- **CORS enabled** — ready for frontend integration

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, Django ~4.2, DRF ~3.15 |
| Database | PostgreSQL (via `psycopg[binary]`) |
| Download | yt-dlp + ffmpeg |
| Server | Gunicorn ~22 (3 workers) |
| Deploy | Docker Compose, Alpine image, WhiteNoise |
| Rate limit | django-ratelimit ~4.1 |
| Logging | Loguru (JSON), `python-json-logger` |

## Quick start

Prerequisites: Docker Engine 20.10+ and Docker Compose 2.0+.

```bash
git clone <repository-url>
cd youtube-dl-vid
docker compose up --build
```

- Web API: `http://localhost:8011`
- PostgreSQL: `localhost:51432` (internal hostname: `db`)
- `DEBUG=False` always; served only by gunicorn via `docker-entrypoint.sh`

Verify it's running:

```bash
curl http://localhost:8011/api/v1/yt/videos/
# -> []
```

## API reference

All endpoints are under `/api/v1/yt/`. There is no authentication; every
endpoint is rate limited by client IP.

### Endpoint summary

| Endpoint | Method | Purpose | Rate limit |
|---|---|---|---|
| `videos/` | POST | Create a URL record | 5/m |
| `videos/` | GET | List URL records | 30/m |
| `videos/<uuid:pk>/` | GET | Status + progress of a URL | 30/m |
| `categorias/` | POST | Create a category | 10/m |
| `categorias/` | GET | List categories | 30/m |
| `videos-uploaded/` | POST | **Download a video (async)** | 5/m |
| `videos-uploaded/` | GET | List completed downloads | 30/m |
| `videos-uploaded/<uuid:pk>/` | GET | Download the MP4 file | 10/m |

Exceeding a rate limit returns `429` with:
`{"error": "rate limit exceeded", "detail": ...}`.

### Status codes

Used by the `status` field on `CodecUrls` records:

| Value | Meaning |
|---|---|
| `1` | Success — download completed |
| `2` | Pending — download in progress / queued |
| `3` | Error — download failed |

### Download a video (async)

```bash
curl -X POST http://localhost:8011/api/v1/yt/videos-uploaded/ \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

The download starts immediately in a background thread and the request returns
`202 Accepted` with the codecurl id to poll:

```json
{
  "id": "52fd264c-eb2a-4f85-a9d9-d3f912aa8988",
  "status": "pending",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

### Poll download status and progress

```bash
curl http://localhost:8011/api/v1/yt/videos/52fd264c-eb2a-4f85-a9d9-d3f912aa8988/
```

```json
{
  "id": "52fd264c-eb2a-4f85-a9d9-d3f912aa8988",
  "created_at": "2026-08-29T10:02:43.154422-06:00",
  "updated_at": "2026-08-29T10:02:53.604863-06:00",
  "visible": true,
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "status": 1,
  "progress": 100.0
}
```

Poll this endpoint until:
- `status == 1` → download succeeded; the file is ready
- `status == 3` → download failed
- otherwise → keep polling; `progress` holds the real download % (`0-100`)

### Create a URL record (no download)

`POST videos/` only stores a URL and its status; it does **not** start a
download. Use it to track URLs or store ones you will download later.

```bash
curl -X POST http://localhost:8011/api/v1/yt/videos/ \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

### Categories

```bash
# Create
curl -X POST http://localhost:8011/api/v1/yt/categorias/ \
  -H "Content-Type: application/json" \
  -d '{"title": "Tech Talks"}'

# List
curl http://localhost:8011/api/v1/yt/categorias/
```

### Completed downloads

```bash
# List downloaded videos
curl http://localhost:8011/api/v1/yt/videos-uploaded/

# Download a video file
curl -O -J http://localhost:8011/api/v1/yt/videos-uploaded/<video-uuid>/
```

List response shape:

```json
[
  {
    "id": "b87df865-3360-4c36-ac81-c6860bf547d6",
    "video": "/code/downloads/4be03709-cf61-4a57-ba63-b98dbc93f3bc.mp4",
    "title": "Test",
    "category": null,
    "codecurl": "4be03709-cf61-4a57-ba63-b98dbc93f3bc",
    "created_at": "2026-08-29T10:04:05.735856-06:00",
    "updated_at": "2026-08-29T10:04:05.735882-06:00",
    "visible": true
  }
]
```

A `VideosUploaded` record is created only after the download succeeds.

## Example workflow

```bash
# 1. Start an async download
curl -X POST http://localhost:8011/api/v1/yt/videos-uploaded/ \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
# -> {"id": "<codecurl-uuid>", "status": "pending", ...}

# 2. Poll progress until status=1
while true; do
  curl -s http://localhost:8011/api/v1/yt/videos/<codecurl-uuid>/
  echo
  sleep 2
done

# 3. Once status=1, list uploads to find the video
curl http://localhost:8011/api/v1/yt/videos-uploaded/

# 4. Download the file
curl -O -J http://localhost:8011/api/v1/yt/videos-uploaded/<video-uuid>/
```

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      Docker Compose                          │
│  ┌──────────────┐          ┌──────────────────────────────┐  │
│  │  PostgreSQL  │◄─────────│  Django web (gunicorn:8000)  │  │
│  │   db:5432    │          │  videos/  Youtube_dl_vid/    │  │
│  └──────────────┘          │  downloads/  logs/           │  │
│                            └──────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
       │  host:8011                     │  host:51432
```

### Project structure

```
youtube-dl-vid/
├── docker-compose.yaml          # web + db services
├── Dockerfile                   # Multi-stage Alpine build
├── docker-entrypoint.sh         # makemigrations → migrate → collectstatic → gunicorn
├── requirements.txt             # Python dependencies
├── downloads/                   # Downloaded MP4 files (bind mount)
├── logs/                        # yt-dlp per-download log files
├── Youtube_dl_vid/              # Project settings, logging, middleware, routing
│   ├── settings.py              # Django configuration
│   ├── logging_config.py        # Loguru JSON config
│   ├── middleware.py            # APILoggingMiddleware (request-id)
│   └── urls.py
└── videos/                      # Main application
    ├── models.py                # CodecUrls, VideosUploaded, Categorias
    ├── views.py                 # DRF views + rate limiting
    ├── serializers.py
    ├── download.py              # yt-dlp builder, async runner, progress tracking
    └── urls.py
```

### Download process

1. `POST videos-uploaded/` creates a `CodecUrls` record and starts a daemon
   thread (`start_download_async` in `videos/download.py`).
2. yt-dlp runs with `--newline`, streaming progress lines that are parsed
   (`PROGRESS_RE`) and written to `CodecUrls.progress` in the DB in real time.
3. The subprocess is capped at `DOWNLOAD_TIMEOUT=300s`; `--socket-timeout 30`
   guards against network stalls. A hung yt-dlp is killed and the URL is
   marked `status=3`.
4. On success the MP4 is stored in `downloads/<codecurl-uuid>.mp4`, a
   `VideosUploaded` record is created, and status becomes `1` with
   `progress=100`.
5. Old downloads for the same URL are auto-cleaned (`cleanup_old_downloads`).

yt-dlp format spec:
`bestvideo[height<=720]+bestaudio/best[height<=720]/best`, merged to MP4 via
ffmpeg.

## Data models

**CodecUrls** — URL + download lifecycle
- `id` (UUID), `url`, `status` (1/2/3), `progress` (0-100 float)
- `created_at`, `updated_at`, `visible`

**VideosUploaded** — completed downloads
- `id` (UUID), `video_path`, `title`, `category` (FK), `codecurl` (FK)
- `created_at`, `updated_at`, `visible`

**Categorias** — video categories
- `id` (UUID), `title`, timestamps, `visible`

## Configuration

Environment variables (in `docker-compose.yaml`):

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | `Youtube_dl_vid.settings` | Django settings path |
| `TZ` | `America/Mexico_City` | Container timezone |
| `POSTGRES_NAME` | `youtube_project` | Database name |
| `POSTGRES_USER` | `lmex` | Database user |
| `POSTGRES_PASSWORD` | `postgres` | Database password |

Key settings in `Youtube_dl_vid/settings.py`:

- `DOWNLOADS_DIR` — where MP4 files are written
- `DOWNLOAD_TIMEOUT` / `--socket-timeout` — subprocess and socket timeouts
  (see `videos/download.py`)
- Gunicorn timeout is `300s` (matches `DOWNLOAD_TIMEOUT`)

Adjust gunicorn workers in `docker-entrypoint.sh`
(`--workers=3 --threads=1 --worker-connections=500`).

## Logging

All logs are structured JSON via Loguru. Standard-library logs are intercepted
(`InterceptHandler`) and routed to the same sinks. Include `request_id` in log
messages for correlation.

Files inside the container at `/var/log/gunicorn/`:

| File | Level | Retention |
|---|---|---|
| `app.log` | INFO+ | 30 days |
| `error.log` | ERROR+ | 90 days |
| `debug.log` | DEBUG+ | 7 days |

```bash
docker compose exec web tail -f /var/log/gunicorn/app.log
docker compose exec web tail -f /var/log/gunicorn/debug.log
```

Per-download yt-dlp output is saved to `logs/<codecurl-uuid>.txt`.

## Development

### Common commands

```bash
docker compose up --build          # build + start
docker compose exec web python manage.py makemigrations   # create a migration
docker compose exec web python manage.py migrate          # apply migrations
docker compose exec web python manage.py test             # run tests
docker compose exec web python manage.py shell            # Django shell
docker compose logs -f web                                 # service logs
```

Migrations are applied automatically on container start. Since the repo is
bind-mounted (`.:/code`), code edits do not require a rebuild — but gunicorn
runs without `--reload`, so restart the container after changing Python code:

```bash
docker compose restart web
```

### Adding a migration

```bash
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate
```

### Tests

The test suite is minimal (`videos/tests.py`). Run with:

```bash
docker compose exec web python manage.py test
```

## Troubleshooting

**Container won't start**
```bash
docker compose logs web
```
Port `8011` or `51432` already in use → change the mapping in
`docker-compose.yaml`.

**Download fails or hangs**
- Check the per-download log: `docker compose exec web cat /code/logs/<codecurl-uuid>.txt`
- A hung yt-dlp is killed after `DOWNLOAD_TIMEOUT=300s` and the URL is marked
  `status=3`; retry the download.
- Large videos may exceed 300s — raise `DOWNLOAD_TIMEOUT` in
  `videos/download.py` and the gunicorn `--timeout` in `docker-entrypoint.sh`.

**Database connection errors**
```bash
docker compose ps
docker compose logs db
```
Credentials in `docker-compose.yaml` must match `settings.py` `DATABASES`.

**Reset everything (WARNING: deletes data)**
```bash
docker compose down -v
docker compose up --build
```

## Security notes

Current defaults are development-friendly: `ALLOWED_HOSTS=["*"]`, open CORS,
no auth, hardcoded `SECRET_KEY`. For production:

- Set `SECRET_KEY` and `ALLOWED_HOSTS` via environment variables
- Restrict `CORS_ALLOWED_ORIGINS`
- Add authentication to the endpoints
- Terminate TLS with a reverse proxy
- Add URL allow-listing / SSRF guards before accepting arbitrary URLs

## License

[Add your license here]