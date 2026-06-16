"""Moodle DHBW scraper — login, discover courses, download files."""

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from .helpers import sanitize, _normalize, EXCLUDED, resolve_filename

MOODLE_URL = "https://moodle.dhbw-mannheim.de"
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/data/files"))

log = logging.getLogger("moodle_api")


class MoodleScraper:
    def __init__(self, username: str, password: str, file_sizes: dict) -> None:
        """Set up the HTTP session and seed file_sizes from disk if the state is empty."""
        self._base = MOODLE_URL.rstrip("/")
        self._s = requests.Session()
        self._s.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0"
        )
        self._username = username
        self._password = password
        self._page_cache: dict[str, BeautifulSoup] = {}
        self._file_sizes = file_sizes
        if not file_sizes and DOWNLOAD_DIR.exists():
            for f in DOWNLOAD_DIR.rglob("*"):
                if f.is_file() and not f.name.startswith("."):
                    key = str(f.relative_to(DOWNLOAD_DIR))
                    file_sizes[key] = str(f.stat().st_size)

    def login(self) -> bool:
        """Authenticate against Moodle. Returns True on success."""
        page = self._s.get(f"{self._base}/login/index.php", timeout=15)
        soup = BeautifulSoup(page.text, "html.parser")
        token = soup.find("input", {"name": "logintoken"})
        data: dict = {
            "username": self._username,
            "password": self._password,
            "rememberusername": "1",
        }
        if token:
            data["logintoken"] = token["value"]
        resp = self._s.post(
            f"{self._base}/login/index.php", data=data, allow_redirects=True, timeout=15
        )
        return "logout" in resp.text.lower()

    def discover_courses(self) -> list[dict]:
        """Return all courses, expanding Vorlesungsunterlagen into one entry per section."""
        raw = self._scrape_course_links()
        result = []
        for course in raw.values():
            if "vorlesungsunterlagen" in course["name"].lower():
                result.extend(self._expand_vorlesungsunterlagen(course))
            else:
                result.append(course)
        log.info("discovered %d courses: %s", len(result), [c["name"] for c in result])
        return result

    def _scrape_course_links(self) -> dict[str, dict]:
        """Scrape /my/courses.php and return a mapping of course_id to course dict."""
        seen: dict[str, dict] = {}
        try:
            page = self._s.get(f"{self._base}/my/courses.php", timeout=15)
        except requests.RequestException:
            return {}
        soup = BeautifulSoup(page.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if "/course/view.php?id=" not in href:
                continue
            course_id = parse_qs(urlparse(href).query).get("id", [None])[0]
            name = a.get_text(strip=True)
            if not course_id or len(name) < 3:
                continue
            if any(term in _normalize(name) for term in EXCLUDED):
                continue
            if course_id not in seen:
                seen[course_id] = {
                    "id": course_id,
                    "name": name,
                    "url": urljoin(self._base, href),
                }
        return seen

    def _expand_vorlesungsunterlagen(self, course: dict) -> list[dict]:
        """Fetch a Vorlesungsunterlagen course page and return one entry per section."""
        result = []
        try:
            page = self._s.get(course["url"], timeout=15)
            soup = BeautifulSoup(page.text, "html.parser")
            for section in soup.find_all("li", {"data-sectionname": True}):
                section_name = section.get("data-sectionname", "").strip()
                if not section_name or any(term in _normalize(section_name) for term in EXCLUDED):
                    continue
                result.append({"id": section_name, "name": section_name, "url": course["url"]})
        except Exception:
            log.warning("failed to expand %s", course["name"])
        return result

    def collect_files(self, course: dict) -> list[dict]:
        """Return all downloadable files for a course as a list of {name, url} dicts."""
        if course["url"] not in self._page_cache:
            page = self._s.get(course["url"], timeout=15)
            self._page_cache[course["url"]] = BeautifulSoup(page.text, "html.parser")
        soup = self._page_cache[course["url"]]

        section = soup.find("li", {"data-sectionname": course["name"]})
        sections = [section] if section else soup.find_all("li", {"data-sectionname": True})

        files = []
        for sec in sections:
            for activity in sec.find_all("div", class_="activity-item"):
                name = activity.get("data-activityname", "")
                link = activity.find("a", href=True)
                if not link:
                    continue
                href = link["href"]
                full_url = urljoin(self._base, href)
                if "/mod/folder/view.php" in href:
                    files.extend(self._collect_folder(full_url, name or "folder"))
                elif "/mod/resource/view.php" in href or "/pluginfile.php/" in href:
                    files.append({"name": name, "url": full_url})
        return files

    def _collect_folder(
        self,
        url: str,
        name: str,
        prefix: str = "",
        visited: set | None = None,
        depth: int = 0,
    ) -> list[dict]:
        """Recursively collect files from a Moodle folder activity (max depth 5)."""
        if visited is None:
            visited = set()
        if depth >= 5 or url in visited:
            return []
        visited.add(url)
        folder_prefix = f"{prefix}{sanitize(name)}/"
        files = []
        try:
            page = self._s.get(url, timeout=15)
            soup = BeautifulSoup(page.text, "html.parser")
            container = (
                soup.find("div", class_="filemanager")
                or soup.find("div", class_="fp-content")
                or soup.find("div", attrs={"role": "main"})
                or soup.find("div", id="region-main")
                or soup
            )
            for a in container.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if not text or "?lang=" in href or "&lang=" in href:
                    continue
                full = urljoin(self._base, href)
                if "/pluginfile.php/" in href:
                    files.append({"name": f"{folder_prefix}{text}", "url": full})
                elif "/mod/folder/view.php" in href and full not in visited:
                    files.extend(self._collect_folder(full, text, folder_prefix, visited, depth + 1))
        except Exception:
            log.warning("failed to fetch folder %s", url, exc_info=True)
        return files

    def sync_course(self, course: dict) -> tuple[int, int]:
        """Download all new or updated files for a course. Returns (downloaded, skipped)."""
        course_dir = DOWNLOAD_DIR / sanitize(course["name"])
        dl = skip = 0
        for f in self.collect_files(course):
            fp = Path(f["name"])
            target = course_dir / fp.parent if fp.parent != Path(".") else course_dir
            if self._download_file(f["url"], fp.name, target):
                dl += 1
            else:
                skip += 1
            time.sleep(0.1)
        return dl, skip

    def _fetch_head(self, url: str) -> tuple[str | None, str]:
        """Return (content-length, content-type) via HEAD request, or (None, '') on failure."""
        try:
            head = self._s.head(url, allow_redirects=True, timeout=10)
            return head.headers.get("content-length"), head.headers.get("content-type", "")
        except Exception:
            return None, ""

    def _download_file(self, url: str, name: str, target: Path) -> bool:
        """Download a file to target, skipping if the remote size is unchanged. Returns True if downloaded."""
        remote_size, ct = self._fetch_head(url)
        name = resolve_filename(name, url, ct)

        target.mkdir(parents=True, exist_ok=True)
        fp = target / name
        key = str(fp.relative_to(DOWNLOAD_DIR))

        if fp.exists() and (remote_size is None or self._file_sizes.get(key) == remote_size):
            return False

        try:
            resp = self._s.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            with open(fp, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            self._file_sizes[key] = resp.headers.get("content-length") or remote_size or str(fp.stat().st_size)
            return True
        except Exception:
            return False


def _sync_courses(
    scraper: MoodleScraper,
    courses: list[dict],
    state: dict,
    on_course_start,
    on_course_done,
) -> dict:
    """Iterate courses, fire progress callbacks, and return a per-course download summary."""
    summary = {}
    for i, course in enumerate(courses):
        if on_course_start:
            on_course_start(course["name"], i, len(courses))
        dl, skip = scraper.sync_course(course)
        summary[course["id"]] = {"name": course["name"], "downloaded": dl, "skipped": skip}
        if on_course_done:
            on_course_done(state, course["name"], i + 1, len(courses))
    return summary


def run_sync(username: str, password: str, state: dict, *, on_course_start=None, on_course_done=None) -> dict:
    """Log in, discover courses, sync files, and update state. Returns a per-course download summary."""
    state.setdefault("file_sizes", {})
    scraper = MoodleScraper(username, password, state["file_sizes"])
    if not scraper.login():
        return {"error": "login_failed"}
    courses = scraper.discover_courses()
    state["courses"] = courses
    summary = _sync_courses(scraper, courses, state, on_course_start, on_course_done)
    state["last_refresh"] = datetime.now().isoformat()
    return summary
