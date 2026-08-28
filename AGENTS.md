# youtube-dl-vid

Django 4.2 REST API that downloads YouTube videos via yt-dlp.

## Quick start

```bash
docker compose up --build
```

- Web at `http://localhost:8011`, PostgreSQL at `localhost:51432`
- DB host inside container is `db` (not `localhost`)
- `DEBUG=False` always; only gunicorn via `docker-entrypoint.sh` (no `runserver`)

## Commands

```bash
docker compose exec web python manage.py <command>
docker compose exec web python manage.py test          # tests (vanilla TestCase, no pytest)
docker compose exec web tail -f /var/log/gunicorn/<file>  # logs
```

## API (`/api/v1/yt/`)

| Endpoint | Method | Rate Limit |
|---|---|---|
| `videos/` | POST (submit URL) | 5/m |
| `videos/` | GET (list) | 30/m |
| `videos/<uuid:pk>` | GET (status) | 30/m |
| `categorias/` | POST (create) | 10/m |
| `categorias/` | GET (list) | 30/m |
| `videos-uploaded/` | POST (download) | 5/m |
| `videos-uploaded/` | GET (list) | 30/m |
| `videos-uploaded/<uuid:pk>` | GET (download file) | 10/m |

## Architecture

- **`videos/`** — models, views, serializers, yt-dlp integration
- **`Youtube_dl_vid/`** — project settings, logging config, middleware, URL routing
- **`videos/download.py`** — yt-dlp command builder + subprocess runner + cleanup
- **`docker-entrypoint.sh`** — runs `makemigrations` → `migrate` → `collectstatic` → `gunicorn`
- **`downloads/`** — downloaded MP4 files; old downloads for same URL auto-cleaned
- yt-dlp format: `bestvideo[height<=720]+bestaudio/best[height<=720]/best` merged to MP4 (requires ffmpeg)

## Key facts

- **Rate limiting**: `django-ratelimit~=4.1` with `block=True`, key=`user_or_ip`, LocMemCache, custom 429 view at `videos.views.ratelimited_error`
- **Migrations auto-applied** on container start; create new ones with `docker compose exec web python manage.py makemigrations`
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
