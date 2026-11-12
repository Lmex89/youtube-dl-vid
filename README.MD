# youtube-dl-vid

A Django 5.2 REST API that downloads videos via yt-dlp (YouTube, Facebook, and
other supported sites) as MP4, tracks real download progress, and serves the
resulting files. Containerized with Docker Compose (Django + PostgreSQL).

## Features

- **Async downloads** — `POST videos-uploaded/` returns `202` immediately and
  downloads in a background thread
- **Real progress** — live yt-dlp download percentage (`0-100`) persisted to DB
  and exposed via a polling endpoint
- **Any yt-dlp source** — YouTube, Facebook, etc. (requires ffmpeg to merge)
- **Output profiles** — standard MP4, WhatsApp-ready MP4 (H.264/AAC, ≤16 MiB),
  and M4A/AAC audio extraction
- **Categorization** — organize downloaded videos into categories
- **Rate limiting** — per-endpoint limits with JSON `429` responses
- **Auto-cleanup** — re-downloading a URL removes previous copies
- **Structured JSON logging** — Loguru with request-id correlation
- **CORS enabled** — ready for frontend integration

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.14, Django ~5.2, DRF ~3.16 |
| Database | PostgreSQL (via `psycopg[binary]`) |
| Download | yt-dlp + ffmpeg |
| Server | Gunicorn ~22 (2 workers × 2 threads) |
| Deploy | Docker Compose, Alpine image, WhiteNoise |
| Rate limit | django-ratelimit ~4.1 |
| Logging | Loguru (JSON), `python-json-logger` |

## Quick start

Prerequisites: Docker Engine 20.10+ and Docker Compose 2.0+.
If `.env` already exists, keep it and skip the copy step.

```bash
git clone <repository-url>
cd youtube-dl-vid
cp .env.example .env
# Edit .env: set SECRET_KEY and the database values before starting.
docker compose up --build
```

- For an existing PostgreSQL volume, use its current database name, user and
  password in `.env`; changing the file does not change the stored credentials.
- Generate a new `SECRET_KEY` with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`
  and put it in `.env`. Keep it private.
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
| `videos/<uuid:pk>` | GET | Status + progress of a URL | 30/m |
| `categorias/` | POST | Create a category | 10/m |
| `categorias/` | GET | List categories | 30/m |
| `videos-uploaded/` | POST | **Download a video (async)** | 5/m |
| `videos-uploaded/` | GET | List completed downloads | 30/m |
| `videos-uploaded/<uuid:pk>` | GET | Download the MP4 file | 10/m |

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
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "output_profile": "standard_video"
}
```

### Output profiles

`output_profile` is optional — omitting it (or sending `null`/`""`) preserves
the original MP4 behavior. Pass it in the POST body to opt into:

| Profile | Output | Details |
|---|---|---|
| `standard_video` (default) | `downloads/<id>.mp4` | Best video+audio ≤720p merged to MP4 — unchanged legacy behavior |
| `whatsapp_video` | `downloads/<id>.mp4` | H.264 Main (no B-frames) + AAC, `yuv420p`, `+faststart`, re-encoded by ffmpeg to fit **16 MiB** (reduces resolution/bitrate if needed) |
| `audio_m4a` | `downloads/<id>.m4a` | Audio-only AAC/M4A at 128 kbps |

Unknown profiles return `400` with the list of allowed values.

```bash
# WhatsApp-ready video
curl -X POST http://localhost:8011/api/v1/yt/videos-uploaded/ \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "output_profile": "whatsapp_video"}'

# Audio (M4A/AAC)
curl -X POST http://localhost:8011/api/v1/yt/videos-uploaded/ \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "output_profile": "audio_m4a"}'
```

### Poll download status and progress

```bash
curl http://localhost:8011/api/v1/yt/videos/52fd264c-eb2a-4f85-a9d9-d3f912aa8988
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
curl -O -J http://localhost:8011/api/v1/yt/videos-uploaded/<video-uuid>
```

List response shape:

```json
[
  {
    "id": "b87df865-3360-4c36-ac81-c6860bf547d6",
    "video": "/code/downloads/4be03709-cf61-4a57-ba63-b98dbc93f3bc.mp4",
    "mime_type": "video/mp4",
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
  curl -s http://localhost:8011/api/v1/yt/videos/<codecurl-uuid>
  echo
  sleep 2
done

# 3. Once status=1, list uploads to find the video
curl http://localhost:8011/api/v1/yt/videos-uploaded/

# 4. Download the file
curl -O -J http://localhost:8011/api/v1/yt/videos-uploaded/<video-uuid>
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
├── downloads/                   # Downloaded MP4/M4A files (bind mount)
│   └── .tmp/                    # Per-download working dirs (ffmpeg re-encode)
├── logs/                        # yt-dlp and ffmpeg per-download logs
├── Youtube_dl_vid/              # Project settings, logging, middleware, routing
│   ├── settings.py              # Django configuration
│   ├── logging_config.py        # Loguru JSON config
│   ├── middleware.py            # APILoggingMiddleware (request-id)
│   └── urls.py
└── videos/                      # Main application
    ├── models.py                # CodecUrls, VideosUploaded, Categorias
    ├── views.py                 # DRF views + rate limiting
    ├── serializers.py
    ├── profiles.py              # Output profiles (formats, ffmpeg commands)
    ├── process.py               # Subprocess runner + progress parsing
    ├── media.py                 # ffprobe duration + ffmpeg runner
    ├── download.py              # Async worker orchestration + cleanup
    └── urls.py
```

### Download process

1. `POST videos-uploaded/` validates `output_profile`, creates a `CodecUrls`
   record, and starts a daemon thread (`start_download_async` in
   `videos/download.py`).
2. yt-dlp runs with `--newline`, streaming progress lines that are parsed and
   written to `CodecUrls.progress` in the DB in real time.
3. The subprocess is capped at `DOWNLOAD_TIMEOUT=300s`; `--socket-timeout 30`
   guards against network stalls. A hung process is killed and the URL is
   marked `status=3`.
4. `whatsapp_video` and `audio_m4a` download into `downloads/.tmp/<id>/` and
   are re-encoded/moved to the final `downloads/<id>.<ext>` only after success;
   temp dirs are removed on success and failure.
5. On success a `VideosUploaded` record is created with `file_path` and
   `mime_type`, and status becomes `1` with `progress=100`.
6. Old downloads for the same URL **and profile** are auto-cleaned
   (`cleanup_old_downloads`), so a WhatsApp copy does not delete the standard
   or audio copy.

yt-dlp format spec:
`bestvideo[height<=720]+bestaudio/best[height<=720]/best`, merged to MP4 via
ffmpeg. The `whatsapp_video` profile then re-encodes with libx264 Main
(`-bf 0`, `yuv420p`, `+faststart`) and AAC, targeting ≤16 MiB by lowering
resolution/bitrate when needed.

## Data models

**CodecUrls** — URL + download lifecycle
- `id` (UUID), `url`, `output_profile`
  (`standard_video`/`whatsapp_video`/`audio_m4a`), `status` (1/2/3),
  `progress` (0-100 float)
- `created_at`, `updated_at`, `visible`

**VideosUploaded** — completed downloads
- `id` (UUID), `file_path`, `mime_type`, `title`, `category` (FK),
  `codecurl` (FK)
- `created_at`, `updated_at`, `visible`

**Categorias** — video categories
- `id` (UUID), `title`, timestamps, `visible`

## Configuration

Copy `.env.example` to the git-ignored `.env` and fill in the values. Compose
loads this file into both services and maps the `DB_*` values to the PostgreSQL
image's `POSTGRES_*` variables. Django reads the same database values.

| Variable | Default in Django | Purpose |
|---|---|---|
| `SECRET_KEY` | None | Required, non-empty Django signing key |
| `DB_NAME` | None | Required, non-empty database name; set it to the existing or new database name |
| `DB_USER` | None | Required, non-empty database user; set it to the existing or new role |
| `DB_PASSWORD` | None | Required, non-empty database password |
| `DB_HOST` | `db` | Database host from the web container |
| `DB_PORT` | `5432` | Database port from the web container |

An absent or empty `SECRET_KEY`, `DB_NAME`, `DB_USER` or `DB_PASSWORD` stops Django at startup with a
configuration error naming the missing variable; Compose also rejects a missing
or empty `DB_PASSWORD`. `DJANGO_SETTINGS_MODULE` and `TZ` are set in Compose.

Compose interpolates unquoted and double-quoted `.env` values, including `$`.
Use single quotes for literal values (escape an embedded single quote as `\'`),
or use generated URL-safe secrets to avoid quoting surprises. Exported `DB_*`
shell variables override `.env` for Compose interpolation; unset unintended
overrides. Avoid sharing `docker compose config` output: it can expose secrets.

### Rotating secrets

Treat the old keys in Git history as compromised. Replace `SECRET_KEY` with a
unique random key and recreate the web service to apply it; this invalidates
existing sessions and other signed data. Keep `.env` private and never commit it.

For an existing PostgreSQL volume, changing `DB_PASSWORD` in `.env` alone does
not update the database role. Plan a maintenance window, back up the database,
change the role password in PostgreSQL (for example with psql's interactive
`\password` command), then update `.env` and recreate the services so both use
the new password (`docker compose up -d`). Do not delete the volume to rotate
credentials. Changing `DB_NAME` or `DB_USER` in `.env` does not create a database
or role in an initialized volume.

Key settings in `Youtube_dl_vid/settings.py`:

- `DOWNLOADS_DIR` — where MP4/M4A files are written
- `DOWNLOAD_LOGS_DIR` — per-download yt-dlp/ffmpeg logs
- `FFMPEG_BINARY` / `FFPROBE_BINARY` — media tool paths
- `MEDIA_PROCESS_TIMEOUT` — per-process timeout for ffmpeg (300s)
- `WHATSAPP_MEDIA_MAX_BYTES` — WhatsApp size cap (16 MiB)
- `DOWNLOAD_TIMEOUT` / `--socket-timeout` — subprocess and socket timeouts
  (see `videos/download.py`)
- Gunicorn timeout is `300s` (matches `DOWNLOAD_TIMEOUT`)

Adjust gunicorn workers in `docker-entrypoint.sh`
(`--workers=2 --threads=2 --worker-connections=200`).

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

Per-download output is saved to `logs/<codecurl-uuid>.txt` (yt-dlp) and, for
profiles that re-encode, `logs/<codecurl-uuid>.ffmpeg.txt`.

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

Tests live in `videos/tests.py` (plain Django `TestCase`, no pytest). Run with:

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

**WhatsApp re-encode fails or exceeds the limit**
- Check the ffmpeg log: `docker compose exec web cat /code/logs/<codecurl-uuid>.ffmpeg.txt`
- `whatsapp_video` re-encodes with libx264 Main + AAC and steps resolution down
  (720p → 480p → 360p) to fit `WHATSAPP_MEDIA_MAX_BYTES`; if 360p still does not
  fit, the URL is marked `status=3`.

**Database connection errors**
```bash
docker compose ps
docker compose logs db
```
Check that `.env` uses the database name, role and password already stored in
the PostgreSQL volume, and that exported `DB_*` variables are not overriding it.
For missing-secret errors, fill in the named variable in `.env`.

**Reset everything (WARNING: deletes data)**
```bash
docker compose down -v
docker compose up --build
```

## Security notes

Current defaults are development-friendly: `ALLOWED_HOSTS=["*"]`, open CORS,
and no auth. Use a unique `SECRET_KEY` and database password for each deployment
and protect the environment file. For production, further changes are needed:

- Restrict `ALLOWED_HOSTS` in settings (it is not environment-configurable)
- Restrict `CORS_ALLOWED_ORIGINS`
- Add authentication to the endpoints
- Terminate TLS with a reverse proxy
- Add URL allow-listing / SSRF guards before accepting arbitrary URLs

## License

[Add your license here]