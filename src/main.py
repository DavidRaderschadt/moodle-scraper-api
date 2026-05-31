"""Moodle DHBW API — serves downloaded lecture files."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from scraper import DOWNLOAD_DIR, run_sync, sanitize

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/.state.json"))
USERNAME = os.environ["MOODLE_USERNAME"]
PASSWORD = os.environ["MOODLE_PASSWORD"]

_executor = ThreadPoolExecutor(max_workers=1)
_sync_lock = asyncio.Lock()


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_refresh": None, "files": {}, "courses": []}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


async def _do_sync() -> None:
    if _sync_lock.locked():
        return
    async with _sync_lock:
        loop = asyncio.get_event_loop()
        state = _load()
        await loop.run_in_executor(_executor, lambda: run_sync(USERNAME, PASSWORD, state))
        _save(state)


scheduler = AsyncIOScheduler(timezone="Europe/Berlin")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(
        _do_sync,
        CronTrigger(hour=3, minute=0, timezone="Europe/Berlin"),
        id="sync",
        replace_existing=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Moodle DHBW API", lifespan=lifespan)


@app.get("/ping")
def ping():
    state = _load()
    return {"status": "ok", "last_refresh": state.get("last_refresh")}


@app.get("/courses")
def list_courses():
    return _load().get("courses", [])


@app.get("/courses/{course_id}/files")
def list_files(course_id: str):
    state = _load()
    course = next((c for c in state.get("courses", []) if c["id"] == course_id), None)
    if not course:
        raise HTTPException(404, "Course not found")
    course_dir = DOWNLOAD_DIR / sanitize(course["name"])
    if not course_dir.exists():
        return []
    return [
        {
            "name": f.name,
            "path": str(f.relative_to(DOWNLOAD_DIR)),
            "size": f.stat().st_size,
            "download_url": f"/files/{f.relative_to(DOWNLOAD_DIR)}",
        }
        for f in sorted(course_dir.rglob("*"))
        if f.is_file() and not f.name.startswith(".")
    ]


@app.get("/files/{path:path}")
def download_file(path: str):
    fp = DOWNLOAD_DIR / path
    if not fp.is_file():
        raise HTTPException(404, "File not found")
    # Prevent path traversal
    try:
        fp.resolve().relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(403, "Forbidden")
    return FileResponse(fp, filename=fp.name)


@app.post("/sync")
async def trigger_sync():
    """Trigger an immediate sync outside the scheduled window."""
    if _sync_lock.locked():
        return {"status": "already_running"}
    asyncio.create_task(_do_sync())
    return {"status": "started"}
