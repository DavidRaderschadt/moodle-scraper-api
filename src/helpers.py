import logging
import os
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("moodle_api")

_EXCLUDED_FILE = Path(os.environ.get("EXCLUDED_FILE", "/app/config/excluded.txt"))
_SANITIZE_TABLE = str.maketrans('<>:"/\\|?*', "_________")


def sanitize(name: str) -> str:
    """Strip filesystem-unsafe characters and decode HTML entities from a filename."""
    return name.translate(_SANITIZE_TABLE).replace("&amp;", "&").strip()


def _normalize(s: str) -> str:
    """Lowercase and normalize curly quotes for consistent string comparison."""
    return s.lower().replace("‘", "'").replace("’", "'")


def _load_excluded() -> set[str]:
    """Load normalized exclusion terms from the configured excluded.txt file."""
    if not _EXCLUDED_FILE.exists():
        log.warning("excluded.txt not found at %s", _EXCLUDED_FILE)
        return set()
    lines = _EXCLUDED_FILE.read_text(encoding="utf-8").splitlines()
    return {_normalize(line.strip()) for line in lines if line.strip() and not line.startswith("#")}


EXCLUDED = _load_excluded()


def resolve_filename(name: str, url: str, ct: str) -> str:
    """Sanitize a filename and infer a missing extension from the content-type header."""
    name = sanitize(name)
    if len(name) < 3:
        name = urlparse(url).path.rstrip("/").split("/")[-1] or "file"
    if "." not in name:
        if "pdf" in ct:
            name += ".pdf"
        elif "zip" in ct:
            name += ".zip"
        elif "wordprocessingml" in ct or "msword" in ct:
            name += ".docx"
        elif "presentationml" in ct or "powerpoint" in ct:
            name += ".pptx"
    return name
