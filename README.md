# moodle-scraper-api

Scrapes lecture files from DHBW Mannheim Moodle and serves them over a REST API. Runs as a Docker container, syncs automatically at 03:00 Berlin time, and makes files available for download immediately after each course finishes — no need to wait for the full sync to complete.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/ping` | — | Health check + sync status |
| `GET` | `/courses` | — | List of discovered lecture courses |
| `GET` | `/courses/{id}/files` | — | File listing for a course |
| `GET` | `/files/{path}` | — | Download a file |
| `POST` | `/sync` | `X-API-Key` | Trigger an immediate sync |

### `/ping` response

While idle:
```json
{
  "status": "ok",
  "last_refresh": "2026-05-31T03:45:22",
  "sync": { "running": false }
}
```

While syncing:
```json
{
  "status": "ok",
  "last_refresh": "2026-05-30T03:41:10",
  "sync": {
    "running": true,
    "current_course": "Grundlagen Data Science und KI",
    "courses_done": 2,
    "courses_total": 5
  }
}
```

## Setup

Copy `.env.example` to `.env` and fill in your credentials:

```env
MOODLE_USERNAME=sXXXXXXX
MOODLE_PASSWORD=your_password
SYNC_API_KEY=your_secret_key
```

Generate a key with:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Run with Docker / Podman

```bash
podman build -t moodle-api .
podman run -d \
  --name moodle-api \
  -p 8000:8000 \
  --env-file .env \
  -v moodle-api-data:/data \
  moodle-api
```

Or with Compose:
```bash
podman-compose up -d   # or: docker compose up -d
```

Files persist in the `moodle-api-data` volume at `/data/files`.

## Course discovery

On each sync the scraper logs in, fetches `/my/courses.php`, and keeps only courses whose name matches a cohort code (e.g. `WDSKI24A`, `MA-WDSKI24A`). Institutional courses like "Studieren an der DHBW Mannheim" are excluded automatically.

Override the filter regex via env var:
```env
COURSE_FILTER_PATTERN=[A-Z]{2,}[\-_]?[A-Z]*\d{2}[A-Z]
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MOODLE_USERNAME` | yes | — | DHBW Moodle username |
| `MOODLE_PASSWORD` | yes | — | DHBW Moodle password |
| `SYNC_API_KEY` | yes | — | Key required for `POST /sync` |
| `DOWNLOAD_DIR` | no | `/data/files` | Where files are stored |
| `STATE_FILE` | no | `/data/.state.json` | Sync state / hash cache |
| `COURSE_FILTER_PATTERN` | no | see above | Regex to select courses |

## Dev with Nix

```bash
nix develop
uvicorn src.main:app --reload
```
