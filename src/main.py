"""Moodle DHBW API — serves downloaded lecture files."""

import asyncio
import io
import json
import logging
import os
import random
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, File, HTTPException, Security, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader

from .scraper import DOWNLOAD_DIR, run_sync
from .helpers import sanitize

log = logging.getLogger("moodle_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/.state.json"))
ANKI_DIR = Path(os.environ.get("ANKI_DIR", "/data/anki"))
USERNAME = os.environ["MOODLE_USERNAME"]
PASSWORD = os.environ["MOODLE_PASSWORD"]
_API_KEY = os.environ["SYNC_API_KEY"]

_key_header = APIKeyHeader(name="X-API-Key")

_progress: dict = {"running": False, "current_course": None, "courses_done": 0, "courses_total": 0}


def _require_key(key: str = Security(_key_header)) -> None:
    """Reject requests that don't carry the correct API key."""
    if key != _API_KEY:
        raise HTTPException(403, "Invalid API key")


_executor = ThreadPoolExecutor(max_workers=1)
_sync_lock = asyncio.Lock()


def _load() -> dict:
    """Load persisted state from disk, returning a blank state if missing or corrupt."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_refresh": None, "file_sizes": {}, "courses": []}


def _save(state: dict) -> None:
    """Persist state to disk as JSON."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def _check_path(fp: Path) -> None:
    """Raise 403 if fp escapes the download directory."""
    try:
        fp.resolve().relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(403, "Forbidden")


def _check_anki_path(fp: Path) -> None:
    """Raise 403 if fp escapes the anki directory."""
    try:
        fp.resolve().relative_to(ANKI_DIR.resolve())
    except ValueError:
        raise HTTPException(403, "Forbidden")


def _resolve_course_dir(path: str) -> Path:
    """Return the course directory, falling back to a glob search. Raises 404 if not found."""
    course_dir = DOWNLOAD_DIR / path
    _check_path(course_dir)
    if not course_dir.is_dir():
        matches = [d for d in DOWNLOAD_DIR.glob(f"*/{path}") if d.is_dir()]
        if not matches:
            raise HTTPException(404, "Course not found")
        course_dir = matches[0]
    return course_dir


_APKG_MAGIC = b"PK\x03\x04"

_COLLECTION_NAMES = {"collection.anki2", "collection.anki21"}


def _strip_scheduling(apkg_bytes: bytes) -> bytes:
    """Return a copy of the .apkg with all scheduling data removed.

    Resets every card to 'new' state and deletes the review log so that
    importing the deck never overwrites another user's local progress.
    """
    in_buf = io.BytesIO(apkg_bytes)
    out_buf = io.BytesIO()
    with zipfile.ZipFile(in_buf, "r") as src, zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            raw = src.read(item.filename)
            if item.filename in _COLLECTION_NAMES:
                conn = sqlite3.connect(":memory:")
                conn.deserialize(raw)
                conn.execute(
                    "UPDATE cards SET type=0, queue=0, due=ord, ivl=0, factor=0,"
                    " reps=0, lapses=0, left=0, odue=0, odid=0"
                )
                conn.execute("DELETE FROM revlog")
                row = conn.execute("SELECT decks FROM col LIMIT 1").fetchone()
                if row:
                    decks = json.loads(row[0])
                    real_did = next((int(k) for k in decks if k != "1"), None)
                    if real_did:
                        conn.execute("UPDATE cards SET did=? WHERE did=1", (real_did,))
                conn.commit()
                raw = conn.serialize()
                conn.close()
            dst.writestr(item, raw)
    return out_buf.getvalue()


async def _validate_apkg(file: UploadFile) -> None:
    """Raise 400 if the file is not a valid .apkg (ZIP-based) Anki deck."""
    if not file.filename or not file.filename.lower().endswith(".apkg"):
        raise HTTPException(400, "File must have .apkg extension")
    header = await file.read(4)
    await file.seek(0)
    if header != _APKG_MAGIC:
        raise HTTPException(400, "File is not a valid Anki deck (.apkg must be a ZIP archive)")


async def _do_sync() -> None:
    """Run a full sync in the thread executor, updating _progress throughout."""
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
            loop = asyncio.get_running_loop()
            state = _load()

            def on_course_start(name: str, index: int, total: int) -> None:
                _progress["current_course"] = name
                _progress["courses_total"] = total
                log.info("syncing course %d/%d: %s", index + 1, total, name)

            def on_course_done(st: dict, name: str, _done: int, _total: int) -> None:
                _progress["courses_done"] += 1
                course = next((c for c in st.get("courses", []) if c["name"] == name), None)
                if course:
                    st.setdefault("course_synced", {})[course["id"]] = datetime.now().isoformat()
                log.info("done: %s", name)

            summary = await loop.run_in_executor(
                _executor,
                lambda: run_sync(
                    USERNAME, PASSWORD, state,
                    on_course_start=on_course_start,
                    on_course_done=on_course_done,
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
        CronTrigger(hour=3, minute=random.randint(0, 59), timezone="Europe/Berlin"),
        id="sync",
        replace_existing=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Moodle DHBW API", lifespan=lifespan, root_path=os.environ.get("ROOT_PATH", ""))


@app.get("/ping")
def ping():
    """Return service status and current sync progress."""
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
    """List all courses that have a local download directory."""
    state = _load()
    synced = state.get("course_synced", {})
    result = []
    for c in state.get("courses", []):
        course_dir = DOWNLOAD_DIR / sanitize(c["name"])
        if not course_dir.exists():
            continue
        result.append({"id": c["id"], "name": c["name"], "last_synced": synced.get(c["id"])})
    return result


@app.get("/courses/{path:path}/files")
def list_files(path: str):
    """List all files under a course directory, resolved by name if the path is ambiguous."""
    course_dir = _resolve_course_dir(path)
    result = []
    for f in sorted(course_dir.rglob("*")):
        if f.is_file() and not f.name.startswith("."):
            st = f.stat()
            result.append({
                "name": f.name,
                "path": str(f.relative_to(DOWNLOAD_DIR)),
                "size": st.st_size,
                "last_downloaded": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "download_url": f"/files/{f.relative_to(DOWNLOAD_DIR)}",
            })
    return result


@app.get("/files/{path:path}")
def download_file(path: str):
    """Serve a file from the download directory."""
    fp = DOWNLOAD_DIR / path
    if not fp.is_file():
        raise HTTPException(404, "File not found")
    _check_path(fp)
    return FileResponse(fp, filename=fp.name)


@app.post("/sync", dependencies=[Depends(_require_key)])
async def trigger_sync():
    """Manually trigger a sync. Returns immediately if one is already running."""
    if _sync_lock.locked():
        return {"status": "already_running"}
    asyncio.create_task(_do_sync())
    return {"status": "started"}


@app.post("/courses/{path:path}/anki", status_code=201)
async def upload_anki_deck(path: str, file: UploadFile = File(...)):
    """Upload an Anki deck (.apkg) for a course or lecture."""
    _resolve_course_dir(path)
    await _validate_apkg(file)

    deck_dir = ANKI_DIR / path
    deck_dir.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize(file.filename)
    fp = deck_dir / safe_name
    _check_anki_path(fp)

    fp.write_bytes(_strip_scheduling(await file.read()))
    st = fp.stat()
    return {
        "name": safe_name,
        "path": str(fp.relative_to(ANKI_DIR)),
        "size": st.st_size,
        "uploaded_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
        "download_url": f"/anki/{fp.relative_to(ANKI_DIR)}",
    }


@app.get("/courses/{path:path}/anki")
def list_anki_decks(path: str):
    """List all Anki decks uploaded for a course or lecture."""
    _resolve_course_dir(path)
    deck_dir = ANKI_DIR / path
    if not deck_dir.is_dir():
        return []
    result = []
    for f in sorted(deck_dir.glob("*.apkg")):
        st = f.stat()
        result.append({
            "name": f.name,
            "path": str(f.relative_to(ANKI_DIR)),
            "size": st.st_size,
            "uploaded_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "download_url": f"/anki/{f.relative_to(ANKI_DIR)}",
        })
    return result


@app.get("/anki/{path:path}")
def download_anki_deck(path: str):
    """Download an Anki deck file."""
    fp = ANKI_DIR / path
    if not fp.is_file():
        raise HTTPException(404, "Deck not found")
    _check_anki_path(fp)
    return FileResponse(fp, filename=fp.name, media_type="application/zip")


@app.patch("/anki/{path:path}")
async def patch_anki_deck(path: str, file: UploadFile = File(...)):
    """Replace an existing Anki deck with a new upload."""
    fp = ANKI_DIR / path
    if not fp.is_file():
        raise HTTPException(404, "Deck not found")
    _check_anki_path(fp)
    await _validate_apkg(file)
    fp.write_bytes(_strip_scheduling(await file.read()))
    st = fp.stat()
    return {
        "name": fp.name,
        "path": str(fp.relative_to(ANKI_DIR)),
        "size": st.st_size,
        "uploaded_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
        "download_url": f"/anki/{fp.relative_to(ANKI_DIR)}",
    }


@app.delete("/anki/{path:path}", dependencies=[Depends(_require_key)], status_code=204)
def delete_anki_deck(path: str):
    """Delete an Anki deck file."""
    fp = ANKI_DIR / path
    if not fp.is_file():
        raise HTTPException(404, "Deck not found")
    _check_anki_path(fp)
    fp.unlink()
