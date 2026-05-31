"""Moodle DHBW scraper — login, discover courses, download files."""

import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

MOODLE_URL = "https://moodle.dhbw-mannheim.de"
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/data/files"))

# Cohort codes: WDSKI24A, MA-WDSKI24A, TIT23B, etc.
_filter_str = os.environ.get("COURSE_FILTER_PATTERN", r"[A-Z]{2,}[\-_]?[A-Z]*\d{2}[A-Z]")
COURSE_PATTERN = re.compile(_filter_str)

# Course names that are institutional, not lecture courses
_EXCLUDED = {
    "studieren an der dhbw",
    "welcome",
    "willkommen",
    "allgemein",
}


class MoodleScraper:
    def __init__(self, username: str, password: str) -> None:
        self._base = MOODLE_URL.rstrip("/")
        self._s = requests.Session()
        self._s.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0"
        )
        self._username = username
        self._password = password

    def login(self) -> bool:
        page = self._s.get(f"{self._base}/login/index.php", timeout=15)
        soup = BeautifulSoup(page.text, "lxml")
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
        """Return lecture courses the user is enrolled in, filtered by cohort pattern."""
        seen: dict[str, dict] = {}
        for url in (f"{self._base}/my/", f"{self._base}/my/courses.php"):
            try:
                page = self._s.get(url, timeout=15)
            except requests.RequestException:
                continue
            soup = BeautifulSoup(page.text, "lxml")
            for a in soup.find_all("a", href=True):
                href: str = a["href"]
                if "/course/view.php?id=" not in href:
                    continue
                course_id = parse_qs(urlparse(href).query).get("id", [None])[0]
                name = a.get_text(strip=True)
                if not course_id or len(name) < 3:
                    continue
                if any(term in name.lower() for term in _EXCLUDED):
                    continue
                if not COURSE_PATTERN.search(name):
                    continue
                if course_id not in seen:
                    seen[course_id] = {
                        "id": course_id,
                        "name": name,
                        "url": urljoin(self._base, href),
                    }
        return list(seen.values())

    def sync_course(self, course: dict, file_hashes: dict) -> tuple[int, int]:
        """Download all files for one course. Returns (downloaded, skipped)."""
        course_dir = DOWNLOAD_DIR / sanitize(course["name"])
        page = self._s.get(course["url"], timeout=15)
        soup = BeautifulSoup(page.text, "lxml")

        total_dl = total_skip = 0
        for section in soup.find_all("li", {"data-sectionname": True}):
            section_name = sanitize(section.get("data-sectionname", "").strip())
            if not section_name:
                continue
            section_dir = course_dir / section_name
            for activity in section.find_all("div", class_="activity-item"):
                name = activity.get("data-activityname", "")
                link = activity.find("a", href=True)
                if not link:
                    continue
                href = link["href"]
                full_url = urljoin(self._base, href)
                if "/mod/folder/view.php" in href:
                    d, s = self._sync_folder(full_url, name or "folder", section_dir, file_hashes)
                    total_dl += d
                    total_skip += s
                elif "/mod/resource/view.php" in href or "/pluginfile.php/" in href:
                    if self._dl(full_url, name, section_dir, file_hashes):
                        total_dl += 1
                    else:
                        total_skip += 1
                time.sleep(0.3)
        return total_dl, total_skip

    def _sync_folder(
        self,
        url: str,
        name: str,
        parent: Path,
        file_hashes: dict,
        visited: set | None = None,
        depth: int = 0,
    ) -> tuple[int, int]:
        if visited is None:
            visited = set()
        if depth >= 5 or url in visited:
            return 0, 0
        visited.add(url)
        folder_dir = parent / sanitize(name)
        dl = skip = 0
        try:
            page = self._s.get(url, timeout=15)
            soup = BeautifulSoup(page.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if not text or "◀︎" in text or "▶︎" in text:
                    continue
                if "?lang=" in href or "&lang=" in href:
                    continue
                full = urljoin(self._base, href)
                if "/pluginfile.php/" in href:
                    if self._dl(full, text, folder_dir, file_hashes):
                        dl += 1
                    else:
                        skip += 1
                elif "/mod/folder/view.php" in href and full not in visited:
                    d, s = self._sync_folder(full, text, folder_dir, file_hashes, visited, depth + 1)
                    dl += d
                    skip += s
                time.sleep(0.2)
        except Exception:
            pass
        return dl, skip

    def _dl(self, url: str, name: str, target: Path, file_hashes: dict) -> bool:
        """Download a single file. Returns True if newly downloaded."""
        name = sanitize(name)
        if len(name) < 3:
            name = urlparse(url).path.rstrip("/").split("/")[-1] or "file"
        if "." not in name:
            try:
                head = self._s.head(url, allow_redirects=True, timeout=10)
                ct = head.headers.get("content-type", "")
                if "pdf" in ct:
                    name += ".pdf"
                elif "zip" in ct:
                    name += ".zip"
                elif "wordprocessingml" in ct or "msword" in ct:
                    name += ".docx"
                elif "presentationml" in ct or "powerpoint" in ct:
                    name += ".pptx"
            except Exception:
                pass
        target.mkdir(parents=True, exist_ok=True)
        fp = target / name
        key = str(fp.relative_to(DOWNLOAD_DIR))
        if fp.exists() and file_hashes.get(key) == _md5(fp):
            return False
        try:
            resp = self._s.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            with open(fp, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            file_hashes[key] = _md5(fp)
            return True
        except Exception:
            return False


def run_sync(username: str, password: str, state: dict) -> dict:
    """Run a full sync. Mutates state in-place. Returns per-course summary."""
    scraper = MoodleScraper(username, password)
    if not scraper.login():
        return {"error": "login_failed"}
    courses = scraper.discover_courses()
    state["courses"] = courses
    state.setdefault("files", {})
    summary = {}
    for course in courses:
        dl, skip = scraper.sync_course(course, state["files"])
        summary[course["id"]] = {
            "name": course["name"],
            "downloaded": dl,
            "skipped": skip,
        }
    state["last_refresh"] = datetime.now().isoformat()
    return summary


def sanitize(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name.replace("&amp;", "&").strip()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()
