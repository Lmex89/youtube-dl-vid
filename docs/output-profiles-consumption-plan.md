# Output Profiles — Consumption Plan (Frontend & AI)

Backend shipped in commit `fb3852a`. This file is the integration contract
for the two consumers: a **frontend** (web/mobile UI) and an **AI agent**
(function-calling assistant). It is written so both can be implemented
independently without reading the Django source.

## 1. Summary

`POST /api/v1/yt/videos-uploaded/` accepts an optional `output_profile`.
Omitting it (or sending `null`/`""`) keeps the legacy behavior
(`standard_video`). Two opt-in profiles are available:

| Profile | Use when | Output file | MIME |
|---|---|---|---|
| `standard_video` (default) | Archive, generic playback, best quality | `downloads/<id>.mp4` | `video/mp4` |
| `whatsapp_video` | Sending through WhatsApp Cloud API (16 MiB cap) | `downloads/<id>.mp4` | `video/mp4` |
| `audio_m4a` | Music/podcast, audio-only delivery | `downloads/<id>.m4a` | `audio/mp4` |

The download is asynchronous: submit → poll → download.

## 2. API contract (frozen)

- Base URL: `http://<host>:8011/api/v1/yt/`
- Auth: none.
- Body/response format: JSON, except the file download (binary).
- Detail routes have **no trailing slash**: `/videos/<uuid>`,
  `/videos-uploaded/<uuid>`.

### 2.1 Submit

`POST /api/v1/yt/videos-uploaded/` — rate limit **5/m**

```json
{
  "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
  "output_profile": "whatsapp_video"
}
```

`output_profile` is optional. Omitted, `null`, or `""` → `standard_video`.

**202 Accepted**

```json
{
  "id": "0f6b7c1e-1c2a-4f7a-9b3e-5a1c2d3e4f50",
  "status": "pending",
  "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
  "output_profile": "whatsapp_video"
}
```

`id` is the **codecurl id** (job id) used for polling.

**400 Bad Request**

```json
{ "error": "url is required" }
```

```json
{
  "error": "unsupported output_profile",
  "allowed": ["audio_m4a", "standard_video", "whatsapp_video"]
}
```

**429 Too Many Requests**

```json
{ "error": "rate limit exceeded", "detail": "..." }
```

Rate limits use a per-process cache, so a 429 is enforced **per gunicorn
worker**, not globally. Treat 429 as transient and back off; do not retry
in a tight loop.

### 2.2 Poll status

`GET /api/v1/yt/videos/<codecurl_id>` — rate limit **30/m**

```json
{
  "id": "0f6b7c1e-1c2a-4f7a-9b3e-5a1c2d3e4f50",
  "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
  "output_profile": "whatsapp_video",
  "status": 2,
  "progress": 43.5,
  "created_at": "2026-09-15T10:00:00-06:00",
  "updated_at": "2026-09-15T10:00:12-06:00",
  "visible": true
}
```

| `status` | Meaning | Next action |
|---|---|---|
| `2` | Pending / downloading / post-processing | Keep polling |
| `1` | Success, file available | Fetch the upload record (§2.3) |
| `3` | Error | Show failure; resubmit to retry |

`progress` is 0–100. Bands differ by profile because post-processing also
reports progress:

| Profile | 0–75 | 75–90 | 90–100 |
|---|---|---|---|
| `standard_video` | yt-dlp download (0–100) | — | — |
| `whatsapp_video` | yt-dlp download | ffmpeg re-encode | ffmpeg finalizing |
| `audio_m4a` | yt-dlp download (0–90) | audio extract | finalizing |

Progress can sit at a band boundary (e.g. `75`) for a while: the transcode
is CPU-bound and may take as long as the download.

The polled record (`CodecUrls`) is created **immediately** on submit. The
`VideosUploaded` record referenced in §2.3 is created **only after
success**.

### 2.3 Find the upload record

`GET /api/v1/yt/videos-uploaded/` — rate limit **30/m**, no pagination,
ordered newest first.

There is currently no filter-by-job endpoint. Find the record whose
`codecurl` equals the submit `id`:

```json
[
  {
    "id": "9a1f2e3d-...",
    "video": "downloads/9a1f2e3d-....mp4",
    "mime_type": "video/mp4",
    "title": "Me at the zoo",
    "category": null,
    "codecurl": "0f6b7c1e-1c2a-4f7a-9b3e-5a1c2d3e4f50",
    "created_at": "...",
    "updated_at": "...",
    "visible": true
  }
]
```

`video` is the server-side path, **not** a URL. Use the upload `id` for the
download endpoint in §2.4. Do not parse `video`.

### 2.4 Download the file

`GET /api/v1/yt/videos-uploaded/<upload_id>` — rate limit **10/m**

- **200**: binary attachment
  - `Content-Type`: `video/mp4` (standard/WhatsApp) or `audio/mp4` (audio)
  - `Content-Disposition: attachment; filename="<upload_id>.mp4|.m4a"`
- **404**: `{"error": "file not found"}` — record has no file (e.g. deleted)

The filename always uses the upload id plus the profile extension. Ignore
`title` for filenames unless the UI renames client-side.

## 3. Frontend implementation plan

### 3.1 Deliverables

- [ ] Profile selector: 3 options, `standard_video` preselected and labeled
      as the default (e.g. "Standard MP4").
- [ ] API client functions: `submitDownload`, `getJob`, `findUpload`,
      `downloadFile` with typed errors (400/404/429/5xx).
- [ ] Polling hook with cleanup on unmount/route change and backoff.
- [ ] Success state with download button; inline preview when possible.
- [ ] Failure state for `status: 3` and for requests returning 400/429.
- [ ] Unit tests with mocked fetch for: default profile, each profile,
      400 invalid profile, 429, stuck job, success.
- [ ] Analytics events: `download_submitted {profile}`, `download_finished
      {profile, seconds}`, `download_failed {profile, status}`.

### 3.2 TypeScript contract

```ts
type OutputProfile = "standard_video" | "whatsapp_video" | "audio_m4a";
type JobStatus = 1 | 2 | 3;

interface CodecUrlJob {
  id: string;
  url: string;
  output_profile: OutputProfile;
  status: JobStatus;
  progress: number; // 0-100
  created_at: string;
  updated_at: string;
  visible: boolean;
}

interface UploadedVideo {
  id: string;
  video: string;      // server path, do not parse
  mime_type: string;
  title: string;
  category: string | null;
  codecurl: string | null;
  created_at: string;
  updated_at: string;
  visible: boolean;
}
```

### 3.3 Flow

1. User pastes URL, picks a profile (default `standard_video`), submits.
2. `POST videos-uploaded/` → keep `response.id` as the job id.
3. Poll `GET videos/<job_id>` every **1.5 s** (± jitter). Stop when
   `status` is `1` or `3`. Give up after **15 min** and surface a
   "taking too long" state (server caps each subprocess at 300 s).
4. On `status === 1`: `GET videos-uploaded/`, find
   `item.codecurl === job_id`, keep `item.id`.
5. Point the download button at `GET videos-uploaded/<item.id>` (let the
   browser follow `Content-Disposition`; add `download` attribute in the
   UI). For preview: `<video>` for MP4 (WhatsApp output is `+faststart`,
   so it streams), `<audio>` for M4A.
6. On `status === 3`: show error, offer "Try again" (a resubmit is the
   only retry; there is no resume).

### 3.4 UX notes

- WhatsApp profile implies a size cap: if the user needs a larger file,
  steer them to `standard_video`.
- Re-submitting the same URL **with the same profile** deletes the
  previous file for that URL+profile; different profiles coexist.
- A server restart strands in-flight jobs at `status: 2` forever. When a
  job exceeds the 15 min client budget, show a retry action instead of
  polling forever.
- `429` from the 5/m submit limit: disable the submit button and count
  down ~60 s; for poll `429`, double the interval up to ~10 s.

## 4. AI agent implementation plan

The agent calls the same HTTP endpoints through three tools. Model the
tools 1:1 with the contract; do not invent endpoints.

### 4.1 Tool schemas (function calling / MCP)

```json
[
  {
    "name": "submit_video_download",
    "description": "Submit a YouTube URL for background download. Returns a job id for polling.",
    "parameters": {
      "type": "object",
      "properties": {
        "url": { "type": "string", "description": "YouTube watch/share URL" },
        "output_profile": {
          "type": "string",
          "enum": ["standard_video", "whatsapp_video", "audio_m4a"],
          "default": "standard_video",
          "description": "Omit unless the user asks for WhatsApp or audio-only"
        }
      },
      "required": ["url"]
    }
  },
  {
    "name": "get_download_status",
    "description": "Get status and 0-100 progress for a download job.",
    "parameters": {
      "type": "object",
      "properties": { "codecurl_id": { "type": "string", "format": "uuid" } },
      "required": ["codecurl_id"]
    }
  },
  {
    "name": "get_downloaded_file",
    "description": "Resolve the download URL for a finished job (status 1).",
    "parameters": {
      "type": "object",
      "properties": { "codecurl_id": { "type": "string", "format": "uuid" } },
      "required": ["codecurl_id"]
    }
  }
]
```

`get_downloaded_file` performs §2.3 (list + match `codecurl`) and returns
`/api/v1/yt/videos-uploaded/<upload_id>` plus `mime_type`. It must fail
with a clear message if no matching record exists yet.

### 4.2 Routing rules

| User intent | `output_profile` |
|---|---|
| "download this video", "save the video", unspecified | `standard_video` |
| "send to WhatsApp", "WhatsApp-ready", "small enough for WhatsApp" | `whatsapp_video` |
| "extract audio", "MP3", "just the song/podcast" | `audio_m4a` |
| "best quality", "full video" | `standard_video` |

Never send an `output_profile` the user did not ask for; the default
preserves legacy behavior. If the user asks for a format that does not
exist (e.g. FLAC), explain the three options instead of guessing.

### 4.3 Guardrails

- Validate URLs before submit (expect `youtube.com`/`youtu.be`; reject
  other hosts unless the product explicitly allows them).
- Max one submit per user request; the submit limit is 5/m. If the submit
  returns 429, wait ~60 s and retry once.
- Poll at most every 1.5 s and stop on `status` 1/3 or after ~15 min.
- Never fabricate job/upload ids; pass server values through untouched.
- On `status: 3`, report the failure; retry only if the user explicitly
  asks, and never more than once.
- Treat 400 as a programming error (missing url / unknown profile):
  surface `allowed` and stop, do not retry.
- Do not download the binary into the model context; return the URL to
  the user.

### 4.4 Example tool loop

```
user: "download this and send it to whatsapp: https://youtu.be/jNQXAC9IVRw"
-> submit_video_download(url, output_profile="whatsapp_video")
   { "id": "0f6b...", "status": "pending", "output_profile": "whatsapp_video" }
-> sleep 2s; get_download_status("0f6b...")   # repeat while status == 2
   { "status": 2, "progress": 61.2 }
   { "status": 1, "progress": 100 }
-> get_downloaded_file("0f6b...")
   { "download_url": "/api/v1/yt/videos-uploaded/9a1f...", "mime_type": "video/mp4", "filename": "9a1f....mp4" }
   (assistant tells the user the file is ready and offers the link)
```

## 5. Backend follow-ups (optional, not required to ship consumers)

1. `GET /api/v1/yt/output-profiles/` returning name, label, extension,
   MIME, `max_bytes` and `audio_only` so clients stop hardcoding the list.
2. `GET /api/v1/yt/videos-uploaded/?codecurl=<uuid>` (or include the
   upload id in the poll payload) to remove the list-and-match step.
3. Watchdog to mark stranded `status: 2` jobs as `3` after a restart.
4. SSE/WebSocket progress channel to replace polling.
5. Global rate limiting (Redis cache) instead of per-worker LocMemCache.

## 6. Acceptance criteria

| # | Given | When | Then |
|---|---|---|---|
| 1 | URL only | submit | 202, `output_profile == "standard_video"` |
| 2 | `output_profile: "hacked"` | submit | 400, `allowed` lists the 3 profiles |
| 3 | Job running | poll | `status` 2, `progress` monotonic 0–100 |
| 4 | Job succeeded | poll | `status` 1, then upload record matches `codecurl` |
| 5 | WhatsApp job | download | `.mp4`, `video/mp4`, H.264 Main/AAC, ≤16 MiB, previewable |
| 6 | Audio job | download | `.m4a`, `audio/mp4`, plays in `<audio>` |
| 7 | 6th submit in a minute | submit | 429 handled with backoff, no crash |
| 8 | Job stuck > 15 min | poll | UI/agent stops and offers retry |

## Appendix — smoke test

```bash
BASE=http://localhost:8011/api/v1/yt
JOB=$(curl -s -X POST "$BASE/videos-uploaded/" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "output_profile": "whatsapp_video"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s "$BASE/videos/$JOB"
curl -s "$BASE/videos-uploaded/"            # find item.codecurl == $JOB
curl -sI "$BASE/videos-uploaded/<upload_id>" # Content-Type / Disposition
```
