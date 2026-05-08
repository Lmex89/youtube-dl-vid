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
- Multi-stage Docker build: `python:3.12-alpine` builder (wheel-based install) → minimal runtime with ffmpeg + postgresql-client
- `psycopg[binary]` replaces `psycopg2` — uses musllinux wheels on Alpine
- Non-root `appuser` created (runs as root for bind mount compatibility)
- Wheel-based pip install pattern (CFE style) — builds wheels in builder stage, installs from local wheels in runtime
- Timezone set to `America/Mexico_City` via ENV + tzdata

## Mandatory Logging Standards

**ALL new code MUST implement structured JSON logging.** Use Context7 to look up best practices for your library/framework.

### Requirements

1. **Use Django standard logging** with `python-json-logger` for JSON output
   ```python
   import logging
   logger = logging.getLogger('videos')
   ```

2. **Log levels by severity:**
   - `DEBUG` — Detailed technical info (queries, internal state)
   - `INFO` — Normal operations (create, complete, list)
   - `WARNING` — Recoverable issues (slow requests >2s, missing files)
   - `ERROR` — Failures (exceptions, validation errors)

3. **JSON format for all logs:**
   ```python
   logger.info(json.dumps({
       "event": "operation_name",
       "key_detail": "value",
       "user": username,
   }))
   ```

4. **Sensitive data filtering** — NEVER log:
   - Passwords, secrets, tokens, API keys
   - Full authorization headers
   - Credentials or auth tokens

5. **Request ID tracking** — Use `APILoggingMiddleware` request_id for correlation

6. **Performance thresholds:**
   - Requests >2s: WARNING level
   - Database queries >500ms: WARNING level (auto-logged by `SlowQueryLoggingMiddleware`)
   - Downloads >500ms: WARNING level

7. **Truncation** — Truncate long values (URLs, payloads) to 50-100 chars with `...`

8. **Exception handling** — Use `custom_exception_handler` in REST_FRAMEWORK settings

9. **Loguru** — Available for non-Django code; configure with `serialize=True` for JSON

### Context7 Usage

For any library integration, use Context7 to find logging best practices:
```
use context7 to show how to implement structured logging in [library]
```
