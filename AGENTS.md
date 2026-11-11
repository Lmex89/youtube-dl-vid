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
- **`videos/download.py`** — async worker orchestration: temp download dir → profile post-process → atomic move → DB record + cleanup; public interface: `start_download_async` (start job) + `get_job_state` (read `JobState` status/progress, `None` if unknown)
- **`Youtube_dl_vid/`** — project settings, logging config, middleware, URL routing
- **`docker-entrypoint.sh`** — runs `makemigrations` → `migrate` → `collectstatic` → `gunicorn`
- **`downloads/<codecurl-uuid>.<ext>`** — final files (`mp4`/`m4a`); re-encoded profiles stage under `downloads/.tmp/<codecurl-uuid>/` and are cleaned on success/failure. Old downloads for same URL **and profile** auto-cleaned. Per-download logs: `logs/<codecurl-uuid>.txt` (yt-dlp) and `logs/<codecurl-uuid>.ffmpeg.txt`
- Downloads run in a daemon thread (`start_download_async`) that updates `CodecUrls.progress` in DB in real time; the thread is tracked in a module-level registry keyed by codecurl id. Restarting the container kills in-flight downloads and leaves `status=2` forever (no retry/resume)
- `Youtube_dl_vid/settings_bkp.py` is a dead Django 3.2 backup — edit `settings.py` only

## Key facts

- **Rate limiting**: `django-ratelimit~=4.1` with `block=True`, key=`user_or_ip`, custom 429 view at `videos.views.ratelimited_error`. LocMemCache is per-process, so limits are enforced **per gunicorn worker**, not globally
- **Running-job protection**: `is_download_running` reads the in-process thread registry, so supersession only protects jobs running in the **same gunicorn worker**; jobs running in another worker are not protected (accepted limit, see `shortcut:` in `DownloadRetention._superseded_ids`)
- **Orphan metrics**: orphans carry no job id, so staging dirs and logs cannot be located; `download_orphans_cleaned` reports only `files_deleted` and `uploads_deleted`
- **Orphan cleanup**: `manage.py cleanup_downloads` deletes `VideosUploaded` rows without a `CodecUrls` (and their files under `downloads/`). It is **manual only** — never scheduled or called from the worker
- **Migrations**: entrypoint runs `makemigrations --no-input` then `migrate` on every start, so missing migrations are auto-generated — still create and commit them with `docker compose exec web python manage.py makemigrations` after model changes
- **Download timeouts**: subprocesses capped at `settings.MEDIA_PROCESS_TIMEOUT=300s` (kills hung yt-dlp/ffmpeg; list orphaned `status=2` jobs with `manage.py list_stale_jobs`); `--socket-timeout 30` guards against network stalls
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

## PR workflow (mandatory)

When using the `implement`, `implement-spec` or `tdd` skills, **always open a PR** after the commits are done. Do not stop at local commits — the deliverable is a pull request.

## SOLID principles (mandatory)

All backend code must follow SOLID:

1. **SRP** — One class/view/serializer = one responsibility.
2. **OCP** — Extend via subclassing or composition, not by modifying existing classes.
3. **LSP** — Subtypes must be substitutable for their base types.
4. **ISP** — Keep interfaces narrow.
5. **DIP** — Depend on abstractions, not concretions. Use DRF's abstract views; pass dependencies via settings (e.g., `settings.DOWNLOADS_DIR`).

## Codegraph (mandatory)

Always use Codegraph tools for codebase exploration, symbol navigation, and impact analysis. Prefer Codegraph over built-in tools (Grep, Glob, Read, explore agent) for any codebase query; fall back to built-in tools only when the Codegraph query does not satisfy the request.

1. **Context & symbol discovery** — Use `codegraph_explore` with a natural-language question or symbol/file names to get verbatim source grouped by file, plus call paths and blast radius.
2. **Impact analysis** — Use `codegraph_impact` before refactoring, renaming, or deleting to see what depends on a symbol.
3. **Callee tracing** — Use `codegraph_callees` to see what a function calls.
4. **Graph health** — Use `codegraph_status` to check index freshness.

## Available skills

38 skills from `mattpocock/skills` are installed in `.agents/skills/` and `.claude/skills/`. Most relevant to this Django project:

- **`code-review`** — Two-axis review (Standards + Spec) of changes since a commit/branch
- **`tdd`** — Test-driven development with red-green-refactor loop
- **`diagnosing-bugs`** — Debugging loop for hard bugs and performance regressions
- **`research`** — Investigate questions against primary sources and capture findings
- **`pr`** — Write PR descriptions
- **`wizard`** — Generate interactive bash wizards for manual provisioning steps

TypeScript-focused skills (not directly relevant here): `setup-ts-deep-modules`, `migrate-to-shoehorn`, `scaffold-exercises`, `writing-fragments`, `writing-beats`, `writing-shape`.

General workflow skills: `grilling`, `grill-me`, `grill-with-docs`, `handoff`, `claude-handoff`, `implement`, `implement-spec`, `prototype`, `to-spec`, `to-tickets`, `to-questionnaire`, `triage`, `retro`, `teach`, `wait-what`, `wayfinder`, `ask-matt`, `chief-of-staff`, `loop-me`, `setup-pre-commit`, `setup-matt-pocock-skills`, `git-guardrails-claude-code`, `improve-codebase-architecture`, `domain-modeling`, `codebase-design`, `writing-for-agents`.

Full skill definitions in `skills-lock.json` and individual `SKILL.md` files.

## Agent skills

### Issue tracker

Issues live in the repo's GitHub Issues (`Lmex89/youtube-dl-vid`), accessed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `GLOSSARY.md` + `docs/adr/` at repo root. See `docs/agents/domain.md`.
