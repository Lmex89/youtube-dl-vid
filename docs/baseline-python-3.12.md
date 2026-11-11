# Baseline: Python 3.12 (issue #16)

Reference for comparing later Python upgrades. Branch: `integration/open-issues`
(no `chore/python-3.14` branch created; the integration branch is the working branch).

- Runtime: `Python 3.12.16` (docker compose `web`)
- Date: 2026-11-10

## Tests

`docker compose exec web python manage.py test`

```
Ran 61 tests in 10.796s

OK
```

## Output profiles (real downloads via `POST /api/v1/yt/videos-uploaded/`)

Source: `https://www.w3schools.com/html/mov_bbb.mp4` (generic extractor).

| Profile | Status | Output |
|---|---|---|
| `standard_video` | 1 (success) | `.mp4`, 788 KB |
| `whatsapp_video` | 1 (success) | `.mp4`, 4.0 MB (<= 16 MiB) |
| `audio_m4a` | 1 (success) | `.m4a`, 204 KB |

Caveat: YouTube URLs (`jNQXAC9IVRE`) failed for all profiles with
`This video is unavailable` and a "no supported JavaScript runtime" yt-dlp
warning. That is an environment/YouTube extraction issue, not Python-specific,
so a non-YouTube URL was used. Re-check with YouTube when a JS runtime is available.
