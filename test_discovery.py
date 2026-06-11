#!/usr/bin/env python3
"""Live discovery test — exercises MoodleScraper.discover_courses() against the real Moodle."""

import os
import sys
from pathlib import Path

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, _, v = _line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("EXCLUDED_FILE", str(Path(__file__).parent / "config" / "excluded.txt"))
sys.path.insert(0, str(Path(__file__).parent))

from src.scraper import MoodleScraper

USERNAME = os.environ["MOODLE_USERNAME"]
PASSWORD = os.environ["MOODLE_PASSWORD"]


def main() -> None:
    scraper = MoodleScraper(USERNAME, PASSWORD, {})
    print("Logging in... ", end="", flush=True)
    if not scraper.login():
        print("FAILED")
        sys.exit(1)
    print("OK\n")

    courses = scraper.discover_courses()
    print(f"Courses ({len(courses)}):")
    for c in courses:
        print(f"  {c['name']}")


if __name__ == "__main__":
    main()
