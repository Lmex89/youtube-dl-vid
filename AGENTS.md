# youtube-dl-vid

Django 4.2 REST API that downloads YouTube videos via yt-dlp.

## Quick start

```bash
docker compose up --build
```

- Web at `http://localhost:8011`, PostgreSQL at `localhost:51432`
- DB host inside container is `db` (not `localhost`); credentials are hardcoded
  in `Youtube_dl_vid/settings.py` — the `POSTGRES_*` env vars in
  `docker-compose.yaml` are **not read**
- `DEBUG=False` always; only gunicorn via `docker-entrypoint.sh` (no `runserver`)
- Repo is bind-mounted (`.:/code`), so code edits need no rebuild — but gunicorn
  runs without `--reload`

## Commands

```bash
docker compose exec web python manage.py <command>
docker compose exec web python manage.py test          # Django TestCase suite in videos/tests.py (no pytest)
docker compose exec web tail -f /var/log/gunicorn/<file>  # logs
docker compose restart web                             # REQUIRED after editing Python code
```

## API (`/api/v1/yt/`)

| Endpoint | Method | Rate Limit |
|---|---|---|
| `videos/` | POST (submit URL) | 5/m |
| `videos/` | GET (list) | 30/m |
| `videos/<uuid:pk>` | GET (status + progress) | 30/m |
| `categorias/` | POST (create) | 10/m |
| `categorias/` | GET (list) | 30/m |
| `videos-uploaded/` | POST (async download → 202) | 5/m |
| `videos-uploaded/` | GET (list) | 30/m |
| `videos-uploaded/<uuid:pk>` | GET (download file) | 10/m |

**Download flow (async):** `POST videos-uploaded/` starts a background thread and
returns `202 {"id": <codecurl_uuid>, "status": "pending", "url": ..., "output_profile": ...}`.
`output_profile` is optional and defaults to `standard_video` (legacy MP4
behavior); `whatsapp_video` → MP4 H.264 Main/AAC re-encoded to ≤16 MiB and
`audio_m4a` → M4A/AAC are opt-in. Invalid values return `400`. Poll `GET videos/<uuid:pk>` for status
(`2`=downloading, `1`=success, `3`=error) and `progress` (real download % 0-100).
A `VideosUploaded` record is created only after the download succeeds.

## Architecture

- **`videos/`** — models, views, serializers, yt-dlp integration
- **`videos/profiles.py`** — output profiles (`standard_video`, `whatsapp_video`, `audio_m4a`): yt-dlp args, ffmpeg command, extension, MIME type; add profiles here via `BaseDownloadProfile` subclasses + `PROFILES` registry
- **`videos/process.py`** — generic subprocess streaming runner + progress parsers (yt-dlp `[download] N%`); **`videos/media.py`** — ffprobe duration + ffmpeg runner (parses `out_time_us`)
- **`Youtube_dl_vid/`** — project settings, logging config, middleware, URL routing
- **`videos/download.py`** — async worker orchestration: temp download dir → profile post-process → atomic move → DB record + cleanup
- **`docker-entrypoint.sh`** — runs `makemigrations` → `migrate` → `collectstatic` → `gunicorn`
- **`downloads/<codecurl-uuid>.<ext>`** — final files (`mp4`/`m4a`); re-encoded profiles stage under `downloads/.tmp/<codecurl-uuid>/` and are cleaned on success/failure. Old downloads for same URL **and profile** auto-cleaned. Per-download logs: `logs/<codecurl-uuid>.txt` (yt-dlp) and `logs/<codecurl-uuid>.ffmpeg.txt`
- Downloads run in a daemon thread (`start_download_async`) that updates `CodecUrls.progress` in DB in real time; the thread is tracked in a module-level registry keyed by codecurl id. Restarting the container kills in-flight downloads and leaves `status=2` forever (no retry/resume)
- `Youtube_dl_vid/settings_bkp.py` is a dead Django 3.2 backup — edit `settings.py` only
- yt-dlp format: `bestvideo[height<=720]+bestaudio/best[height<=720]/best` merged to MP4 (requires ffmpeg); `--newline` enables per-line progress output parsed by `PROGRESS_RE`
- `whatsapp_video` re-encodes with libx264 (`-profile:v main -bf 0 -pix_fmt yuv420p +faststart`) + AAC, stepping 720p→480p→360p to fit `WHATSAPP_MEDIA_MAX_BYTES` (16 MiB); `audio_m4a` uses `yt-dlp -x --audio-format m4a --audio-quality 128K`

## Key facts

- **Rate limiting**: `django-ratelimit~=4.1` with `block=True`, key=`user_or_ip`, custom 429 view at `videos.views.ratelimited_error`. LocMemCache is per-process, so limits are enforced **per gunicorn worker**, not globally
- **Migrations**: entrypoint runs `makemigrations --no-input` then `migrate` on every start, so missing migrations are auto-generated — still create and commit them with `docker compose exec web python manage.py makemigrations` after model changes
- **Download timeouts**: subprocess capped at `DOWNLOAD_TIMEOUT=300s` (kills hung yt-dlp); `--socket-timeout 30` guards against network stalls
- **No lint / format / typecheck config** exists
- **Logging**: structured JSON via Loguru (`serialize=True`) at `/var/log/gunicorn/`, with stdlib logs intercepted by `InterceptHandler`:
  - `app.log` (INFO+, 30-day retention)
  - `error.log` (ERROR+, 90-day retention)
  - `debug.log` (DEBUG+, 7-day retention)
- `psycopg[binary]` (not `psycopg2`) — musllinux wheels for Alpine
- Non-root `appuser` created but runs as root for bind mount compatibility
- `collectstatic` runs every container start (output to `staticfiles/`)
- `ALLOWED_HOSTS = ["*"]`, CORS open, no auth on endpoints
- Timezone: `America/Mexico_City`

## Logging standards for new code

- Use Django stdlib logging (`logging.getLogger('videos')`)
- **ALL logs must be JSON** — `logger.info(json.dumps({"event": "name", "key": "val", ...}))`
- Log levels: DEBUG (details), INFO (operations), WARNING (recoverable), ERROR (failures), EXCEPTION (ERROR + traceback inside `except` blocks)
- Sensitive data NEVER logged (passwords, tokens, secrets, auth headers)
- Include `request_id` from `APILoggingMiddleware` for correlation
- Truncate long values (URLs, payloads) to 50-100 chars
- Downloads >500ms: WARNING; requests >2s: WARNING (auto-logged by middleware)
- Exception handling uses `custom_exception_handler` (in `REST_FRAMEWORK` settings)
- Loguru available for non-Django code with `serialize=True`

## Commit practices (mandatory)

Follow Conventional Commits format:

```
<type>(<scope>): <subject>

<body>
```

- **Subject**: capitalized, imperative mood, ≤50 chars, no period
- **Body**: wrap at 72 chars, explain **why** not what (the diff shows what)
- **Types**: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `style`, `perf`
- Use `git commit -s` (Signed-off-by) for attribution
- One logical change per commit — no mixed concerns

## SOLID principles (mandatory)

All backend code must follow SOLID:

1. **SRP** — One class/view/serializer = one responsibility. Split `videos/views.py` logic where appropriate.
2. **OCP** — Extend via subclassing or composition, not by modifying existing classes. DRF generic views and model serializers already favor this.
3. **LSP** — Subtypes must be substitutable for their base types. Don't override base behavior in ways that break callers.
4. **ISP** — Keep interfaces narrow. Views should not depend on methods they don't use. Split large serializers.
5. **DIP** — Depend on abstractions, not concretions. Use DRF's abstract views, pass dependencies via constructor/settings (e.g., `settings.DOWNLOADS_DIR`).

## Codegraph (mandatory)

Always use Codegraph tools for codebase exploration, symbol navigation, and impact analysis. Prefer Codegraph over built-in tools (Grep, Glob, Read, explore agent) for any codebase query; fall back to built-in tools only when the Codegraph query does not satisfy the request.

1. **Context & symbol discovery** — Use `codegraph_context_for_task` and `codegraph_find_symbol` / `codegraph_search_symbols` to locate relevant code and symbols before making modifications.
2. **Impact radius & callers** — Check `codegraph_get_impact_radius`, `codegraph_find_callers`, and `codegraph_find_callees` prior to refactoring, renaming, or deleting functions, classes, or models.
3. **Dependency tracing** — Use `codegraph_trace_dependencies` and `codegraph_architecture_overview` to understand call chains and project structure.
4. **Graph synchronization** — Run `codegraph_index_repo` or `codegraph_update_graph` when adding or restructuring files to keep the graph index up to date.
