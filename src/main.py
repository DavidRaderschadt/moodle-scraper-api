"""Moodle DHBW API — serves downloaded lecture files."""

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader

from .scraper import DOWNLOAD_DIR, run_sync, sanitize

log = logging.getLogger("moodle_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/.state.json"))
USERNAME = os.environ["MOODLE_USERNAME"]
PASSWORD = os.environ["MOODLE_PASSWORD"]
_API_KEY = os.environ["SYNC_API_KEY"]

_key_header = APIKeyHeader(name="X-API-Key")

_progress: dict = {"running": False, "current_course": None, "courses_done": 0, "courses_total": 0}


def _require_key(key: str = Security(_key_header)) -> None:
    if key != _API_KEY:
        raise HTTPException(403, "Invalid API key")


_executor = ThreadPoolExecutor(max_workers=1)
_sync_lock = asyncio.Lock()


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_refresh": None, "file_sizes": {}, "courses": []}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def _on_course_start(name: str, index: int, total: int) -> None:
    _progress["current_course"] = name
    _progress["courses_done"] = index
    _progress["courses_total"] = total
    log.info("syncing course %d/%d: %s", index + 1, total, name)


def _on_course_done(state: dict, name: str, done: int, total: int) -> None:
    _progress["courses_done"] = done
    _save(state)
    log.info("done %d/%d: %s", done, total, name)


async def _do_sync() -> None:
    if _sync_lock.locked():
        log.info("sync already running, skipping")
        return
    async with _sync_lock:
        _progress["running"] = True
        _progress["courses_done"] = 0
        _progress["courses_total"] = 0
        _progress["current_course"] = None
        log.info("sync started")
        try:
            loop = asyncio.get_event_loop()
            state = _load()
            summary = await loop.run_in_executor(
                _executor,
                lambda: run_sync(
                    USERNAME, PASSWORD, state,
                    on_course_start=_on_course_start,
                    on_course_done=_on_course_done,
                ),
            )
            _save(state)
            log.info("sync complete: %s", summary)
        except Exception:
            log.exception("sync failed")
        finally:
            _progress["running"] = False
            _progress["current_course"] = None


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
    resp: dict = {"status": "ok", "last_refresh": state.get("last_refresh"), "sync": {"running": False}}
    if _progress["running"]:
        resp["sync"] = {
            "running": True,
            "current_course": _progress["current_course"],
            "courses_done": _progress["courses_done"],
            "courses_total": _progress["courses_total"],
        }
    return resp


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
    try:
        fp.resolve().relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(403, "Forbidden")
    return FileResponse(fp, filename=fp.name)


@app.post("/sync", dependencies=[Depends(_require_key)])
async def trigger_sync():
    if _sync_lock.locked():
        return {"status": "already_running"}
    asyncio.create_task(_do_sync())
    return {"status": "started"}
