# youtube-dl-vid

A Django 4.2 REST API service for downloading YouTube videos using yt-dlp. Built with Django REST Framework, PostgreSQL, and containerized with Docker.

## Overview

This service provides a RESTful API to submit YouTube URLs, track download status, categorize videos, and serve downloaded files. It uses yt-dlp (the actively maintained fork of youtube-dl) for reliable video extraction and supports downloading videos in MP4 format up to 720p quality.

### Key Features

- **Asynchronous video downloads** — Submit URLs and track download progress via API
- **Automatic format selection** — Downloads best quality video+audio and merges to MP4 (≤720p)
- **Video categorization** — Organize downloaded videos into custom categories
- **Soft delete support** — Records are marked invisible rather than permanently deleted
- **Automatic cleanup** — Old downloads for the same URL are automatically cleaned up
- **CORS enabled** — Ready for frontend integration
- **Production-ready** — Multi-stage Docker build with Alpine Linux

## Quick Start

### Prerequisites

- Docker Engine 20.10+
- Docker Compose 2.0+
- 2GB available disk space (for Docker images and video downloads)

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd youtube-dl-vid
```

2. Start the services:
```bash
docker compose up --build
```

3. Access the services:
   - Web API: http://localhost:8011
   - PostgreSQL: localhost:51432

### Verify Installation

Check if the API is running:
```bash
curl http://localhost:8011/api/v1/yt/videos/
```

Expected response: `[]` (empty array)

## API Documentation

All API endpoints are prefixed with `/api/v1/yt/`.

### Videos (URL Management)

Manage YouTube URLs and track download status.

#### List All URLs
```bash
GET /api/v1/yt/videos/
```

**Response:**
```json
[
  {
    "id": "423a808e-2a07-45f8-86ee-ce9e4e5db8be",
    "url": "https://www.youtube.com/watch?v=...",
    "status": 1,
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T10:35:00Z",
    "visible": true
  }
]
```

**Status codes:**
- `1` — Success (download completed)
- `2` — Pending (download in progress or queued)
- `3` — Error (download failed)

#### Submit New URL
```bash
POST /api/v1/yt/videos/
Content-Type: application/json

{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

**Response:**
```json
{
  "id": "423a808e-2a07-45f8-86ee-ce9e4e5db8be",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "status": 2,
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:00Z",
  "visible": true
}
```

The download starts automatically in the background. Poll the detail endpoint to check status.

#### Get URL Status
```bash
GET /api/v1/yt/videos/<uuid:pk>/
```

**Example:**
```bash
curl http://localhost:8011/api/v1/yt/videos/423a808e-2a07-45f8-86ee-ce9e4e5db8be/
```

### Categories

Organize videos into categories.

#### List Categories
```bash
GET /api/v1/yt/categorias/
```

#### Create Category
```bash
POST /api/v1/yt/categorias/
Content-Type: application/json

{
  "title": "Music Videos"
}
```

### Uploaded Videos

Access and download completed video files.

#### List Downloaded Videos
```bash
GET /api/v1/yt/videos-uploaded/
```

**Response:**
```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Video Title",
    "video_path": "/code/downloads/550e8400-e29b-41d4-a716-446655440000.mp4",
    "category": "423a808e-2a07-45f8-86ee-ce9e4e5db8be",
    "codecurl": "550e8400-e29b-41d4-a716-446655440001",
    "created_at": "2024-01-15T10:35:00Z",
    "updated_at": "2024-01-15T10:35:00Z",
    "visible": true
  }
]
```

#### Download Video File
```bash
GET /api/v1/yt/videos-uploaded/<uuid:pk>/
```

This endpoint streams the actual MP4 file using Django's `FileResponse`.

**Example (download to file):**
```bash
curl -O -J http://localhost:8011/api/v1/yt/videos-uploaded/550e8400-e29b-41d4-a716-446655440000/
```

## Complete Workflow Example

Here's a typical workflow for downloading a YouTube video:

```bash
# 1. Create a category
curl -X POST http://localhost:8011/api/v1/yt/categorias/ \
  -H "Content-Type: application/json" \
  -d '{"title": "Tech Talks"}'
# Response: {"id": "cat-uuid", "title": "Tech Talks", ...}

# 2. Submit a YouTube URL for download
curl -X POST http://localhost:8011/api/v1/yt/videos/ \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=example"}'
# Response: {"id": "url-uuid", "status": 2, ...}

# 3. Check download status (poll until status=1)
curl http://localhost:8011/api/v1/yt/videos/url-uuid/

# 4. List completed downloads
curl http://localhost:8011/api/v1/yt/videos-uploaded/

# 5. Download the video file
curl -O -J http://localhost:8011/api/v1/yt/videos-uploaded/video-uuid/
```

## Architecture

### System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Compose Environment                │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────┐         ┌──────────────────────────────┐  │
│  │   PostgreSQL │         │        Django Web            │  │
│  │   db:5432    │◄────────┤    gunicorn:8000             │  │
│  │  (internal)  │         │        │                     │  │
│  └──────────────┘         ├────────┴─────────────────────┤  │
│                           │  ┌──────────┐  ┌──────────┐  │  │
│                           │  │  videos/ │  │Youtube_dl│  │  │
│                           │  │   app    │  │   _vid/  │  │  │
│                           │  └──────────┘  └──────────┘  │  │
│                           │  ┌──────────────────────────┐│  │
│                           │  │   downloads/ (volume)    ││  │
│                           │  └──────────────────────────┘│  │
│                           └──────────────────────────────┘  │
│                                      │                      │
└──────────────────────────────────────┼──────────────────────┘
                                       │
                              ┌────────┴────────┐
                              │  Host Machine   │
                              │  localhost:8011 │
                              │  localhost:51432│
                              └─────────────────┘
```

### Project Structure

```
youtube-dl-vid/
├── docker-compose.yaml          # Docker services configuration
├── Dockerfile                   # Multi-stage Alpine-based image
├── docker-entrypoint.sh         # Container startup script
├── requirements.txt             # Python dependencies
├── downloads/                   # Downloaded video storage (bind mount)
├── staticfiles/                 # Collected static files
├── Youtube_dl_vid/              # Django project settings
│   ├── settings.py             # Main configuration
│   ├── urls.py                 # URL routing
│   ├── wsgi.py                 # WSGI entrypoint
│   └── logging_config.py       # Loguru configuration
├── videos/                      # Main Django application
│   ├── models.py               # Database models (CodecUrls, VideosUploaded, Categorias)
│   ├── views.py                # API views (DRF generic views)
│   ├── serializers.py          # DRF serializers
│   ├── download.py             # yt-dlp integration module
│   └── urls.py                 # App URL routes
└── manage.py                    # Django management script
```

### Data Models

**CodecUrls** — Stores YouTube URLs and download status
- `id`: UUID (primary key)
- `url`: YouTube URL string
- `status`: Enum (1=Success, 2=Pending, 3=Error)
- `created_at`, `updated_at`: Timestamps
- `visible`: Soft delete flag

**VideosUploaded** — Stores completed download metadata
- `id`: UUID (primary key)
- `video_path`: Filesystem path to MP4 file
- `title`: Video title
- `category`: Foreign key to Categorias
- `codecurl`: Foreign key to CodecUrls
- Timestamps and visibility flags

**Categorias** — Video categories
- `id`: UUID (primary key)
- `title`: Category name
- Timestamps and visibility flags

### Download Process Flow

1. Client submits URL via `POST /api/v1/yt/videos/`
2. API creates `CodecUrls` record with status=PENDING
3. Background download process starts via `download.py`
4. yt-dlp extracts video and saves to `downloads/<uuid>.mp4`
5. On success: creates `VideosUploaded` record, updates status=SUCCESS
6. On failure: updates status=ERROR
7. Client polls status endpoint or lists `videos-uploaded/` to get file

## Tech Stack

### Backend
- **Python 3.12** — Modern Python with type hints support
- **Django 4.2** — LTS web framework
- **Django REST Framework 3.15** — API serialization and views
- **PostgreSQL** — Production database
- **psycopg[binary] 3.1** — PostgreSQL adapter with musllinux wheels for Alpine

### Video Processing
- **yt-dlp 2026.03.17** — YouTube video downloader
- **ffmpeg 8.0** — Video/audio format conversion and merging
- Download format: `bestvideo[height<=720]+bestaudio/best[height<=720]` merged to MP4

### Infrastructure
- **Docker** — Containerization with multi-stage build
- **Alpine Linux 3.20** — Minimal base image (~60MB)
- **Gunicorn 22.0** — WSGI HTTP server (3 workers, sync mode)
- **WhiteNoise 6.6** — Static file serving
- **nginx-proxy-manager** — Recommended for production SSL termination

### Development Tools
- **Loguru** — Structured logging to stdout and file
- **django-cors-headers** — CORS handling for frontend integration

## Docker Configuration

### Multi-Stage Build

The Dockerfile uses a multi-stage build pattern for minimal image size:

**Builder Stage:**
- Alpine Linux with build dependencies
- Compiles Python wheels for all packages
- Output: `/wheels` directory with pre-compiled packages

**Runtime Stage:**
- Alpine Linux with runtime dependencies only (ffmpeg, postgresql-client)
- Installs Python packages from local wheels (no compilers needed)
- Creates non-root `appuser` for security
- Total image size: ~300MB (vs ~900MB for Debian-based)

### Services

**db (PostgreSQL)**
- Image: `postgres:latest`
- Database: `youtube_project`
- User: `lmex` / Password: `postgres`
- Port: `51432` (host) → `5432` (container)
- Persistent volume: `postgres_data`

**web (Django)**
- Build: Local Dockerfile
- Port: `8011` (host) → `8000` (container)
- Bind mount: `.:/code` (development mode)
- Environment: Debug=False, gunicorn with 3 workers

### Environment Variables

The following environment variables can be configured in `docker-compose.yaml`:

**Django Settings:**
- `DJANGO_SETTINGS_MODULE` — Django settings module path
- `TZ` — Timezone (default: America/Mexico_City)

**Database:**
- `POSTGRES_NAME` — Database name (default: youtube_project)
- `POSTGRES_USER` — Database user (default: lmex)
- `POSTGRES_PASSWORD` — Database password (default: postgres)
- Note: DB_HOST is hardcoded to `db` (Docker service name)

**Django settings.py:**
- `SECRET_KEY` — Django secret key (currently hardcoded, should use env var in production)
- `DEBUG` — Always False in this configuration

### Volumes

**postgres_data**
- Type: Named Docker volume
- Purpose: Persistent PostgreSQL data storage
- Location: `/var/lib/postgresql/data`

**Bind Mount (.:/code)**
- Type: Bind mount (development only)
- Purpose: Live code reloading during development
- Note: In production, remove this and bake code into image

### Networks

Default Docker bridge network with automatic DNS resolution between containers. Services communicate using service names (`db`, `web`) as hostnames.

## Configuration

### Customizing Download Behavior

Edit `videos/download.py` to modify yt-dlp options:

```python
# Current format selection (720p max, MP4 output)
format_spec = "bestvideo[height<=720]+bestaudio/best[height<=720]"
output_format = "mp4"

# To download 1080p:
format_spec = "bestvideo[height<=1080]+bestaudio/best[height<=1080]"

# To prefer webm:
output_format = "webm"
```

### Changing Timezone

The timezone is set to `America/Mexico_City` by default. To change it:

1. Update `Dockerfile`:
```dockerfile
ENV TZ=Your/Timezone
```

2. Update `docker-compose.yaml`:
```yaml
environment:
  - TZ=Your/Timezone
```

3. Rebuild: `docker compose up --build`

### Adjusting Gunicorn Workers

Edit `docker-entrypoint.sh`:

```bash
# Default: 3 workers, 1 thread each
gunicorn Youtube_dl_vid.wsgi:application \
  --timeout 300 \
  --workers=3 \
  --threads=1 \
  --worker-connections=500 \
  --bind 0.0.0.0:8000

# For high traffic: increase workers (2-4 x CPU cores)
--workers=9
```

## Development Workflow

### Local Development

The bind mount (`.:/code`) enables live code reloading without rebuilding the container:

1. Start services: `docker compose up`
2. Edit Python files locally
3. Django auto-reloads on changes (gunicorn with `--reload` or use development server)

Note: The current configuration uses gunicorn in production mode. For active development with auto-reload, temporarily modify `docker-entrypoint.sh`:

```bash
# For development with auto-reload
gunicorn Youtube_dl_vid.wsgi:application --reload --bind 0.0.0.0:8000

# Or use Django development server (not for production!)
python manage.py runserver 0.0.0.0:8000
```

### Database Migrations

Migrations are applied automatically on container startup via `docker-entrypoint.sh`. To manually create migrations:

```bash
docker compose exec web python manage.py makemigrations
```

To run migrations manually:
```bash
docker compose exec web python manage.py migrate
```

### Viewing Logs

```bash
# All services
docker compose logs

# Web service only (follow mode)
docker compose logs -f web

# PostgreSQL logs
docker compose logs db
```

All application logs are inside the container at `/var/log/gunicorn/` in JSON format:

| File | Content | Level |
|---|---|---|
| `app.log` | Loguru application logs (30-day retention) | INFO+ |
| `django.log` | Django app logs | INFO+ |
| `django_requests.log` | HTTP request/response traces | INFO |
| `django_db.log` | Database query logs | DEBUG |
| `django_slow_queries.log` | Queries exceeding 500ms | WARNING |

**Check logs inside the container:**
```bash
# Tail all Django logs
docker compose exec web tail -f /var/log/gunicorn/django.log

# Watch request logs
docker compose exec web tail -f /var/log/gunicorn/django_requests.log

# Monitor slow queries
docker compose exec web tail -f /var/log/gunicorn/django_slow_queries.log
```

### Running Django Shell

```bash
docker compose exec web python manage.py shell
```

### Creating Superuser

```bash
docker compose exec web python manage.py createsuperuser
```

## Troubleshooting

### Container won't start

**Check logs:**
```bash
docker compose logs web
```

**Common issues:**
- Port 8011 already in use → Change port mapping in docker-compose.yaml
- Port 51432 already in use → Change or remove PostgreSQL host port
- Permission denied on downloads/ → Check directory permissions

### Database connection errors

**Verify PostgreSQL is running:**
```bash
docker compose ps
```

**Check database credentials:**
Ensure `docker-compose.yaml` environment variables match `Youtube_dl_vid/settings.py` DATABASES config.

**Reset database (WARNING: loses all data):**
```bash
docker compose down -v
docker compose up --build
```

### Download fails with "No such file or directory: 'yt-dlp'"

This indicates yt-dlp is not installed. Rebuild the image:
```bash
docker compose down
docker compose up --build --no-cache
```

### Video downloads timeout

Large videos may exceed the default 300-second timeout. Increase in `docker-entrypoint.sh`:
```bash
gunicorn ... --timeout 600
```

Or adjust yt-dlp timeout in `videos/download.py`.

### CORS errors from frontend

Ensure `CORS_ORIGIN_ALLOW_ALL = True` in settings.py (currently enabled) or configure specific origins:
```python
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://yourdomain.com",
]
```

### Permission denied on downloads/

When using bind mounts with non-root users, permission issues can occur. Solutions:
1. Run container as root (current default)
2. Change host directory ownership to match container user
3. Remove bind mount for production deployments

## Security Considerations

### Current Configuration (Development-Friendly)

- `DEBUG = False` (good)
- `ALLOWED_HOSTS = ["*"]` (permissive, restrict in production)
- `CORS_ORIGIN_ALLOW_ALL = True` (permissive, restrict in production)
- No authentication required on API endpoints (open access)
- Hardcoded SECRET_KEY (should use environment variable)

### Production Hardening Checklist

- [ ] Use strong, random SECRET_KEY from environment variable
- [ ] Restrict ALLOWED_HOSTS to your domain
- [ ] Configure CORS_ALLOWED_ORIGINS for your frontend only
- [ ] Add authentication/authorization to API endpoints
- [ ] Enable HTTPS (use nginx-proxy-manager or traefik)
- [ ] Remove bind mount volume (bake code into image)
- [ ] Run as non-root user (enable USER appuser in Dockerfile)
- [ ] Add rate limiting to prevent abuse
- [ ] Implement download quotas/size limits
- [ ] Add input validation on URLs (prevent SSRF)
- [ ] Regular security updates: `docker compose pull && docker compose up --build`

## Performance Optimization

### Gunicorn Tuning

For production workloads:

```bash
# CPU-bound: sync workers, 2-4 x CPU cores
--workers=9 --threads=1

# I/O-bound: async workers with gevent
--worker-class=gevent --worker-connections=1000

# Mixed workload: threads
--workers=3 --threads=4
```

### Database Optimization

- Add database indexes on frequently queried fields
- Consider read replicas for heavy read workloads
- Use connection pooling (pgbouncer)

### Download Optimization

- Implement download queue with Celery + Redis for async processing
- Add caching layer for video metadata
- Use CDN for serving downloaded files

## API Rate Limits

Currently no rate limiting is implemented. For production, add Django Ratelimit or Nginx rate limiting:

```nginx
# nginx example
limit_req_zone $binary_remote_addr zone=api:10m rate=10r/m;
location /api/ {
    limit_req zone=api burst=20 nodelay;
    proxy_pass http://web:8000;
}
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes with tests
4. Submit a pull request

### Code Style

- No linting configuration currently exists
- Follow PEP 8 for Python code
- Use type hints where applicable

### Testing

Currently minimal test coverage. Run tests with:
```bash
docker compose exec web python manage.py test
```

## License

[Add your license here]

## Acknowledgments

- yt-dlp team for the excellent video downloader
- Django and DRF communities for the robust web framework
- CFE (Coding For Entrepreneurs) for the Dockerfile patterns

## Support

For issues and questions:
- Check the Troubleshooting section above
- Review application logs: `docker compose logs web`
- Check yt-dlp documentation for download issues

---

**Note:** This README reflects the current development setup. Production deployment requires additional security hardening as outlined in the Security Considerations section.
