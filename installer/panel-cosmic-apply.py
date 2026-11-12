#!/usr/bin/env python3
"""Add the shared cosmic theme stylesheet to the admin page templates."""
from pathlib import Path
import re
import sys


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/multisub-panel")
TEMPLATES = (
    "index.html",
    "user.html",
    "new_user.html",
    "dashboard.html",
    "sources.html",
    "logs.html",
    "bulk.html",
    "bulk-result.html",
    "bulk-add-result.html",
    "trash.html",
)
OLD = '''<link rel="stylesheet" href="{{ url_for('static', filename='panel-v2.css') }}">'''
COSMIC = '''<link rel="stylesheet" href="{{ url_for('static', filename='panel-cosmic.css') }}">'''
VERSIONED = '''<link rel="stylesheet" href="{{ url_for('static', filename='panel-cosmic.css', v=3) }}">'''
COSMIC_LINK = re.compile(
    r'''<link rel="stylesheet" href="\{\{ url_for\('static', filename='panel-cosmic\.css'(?:, v=\d+)?\) \}\}">'''
)
NEW = OLD + "\n" + VERSIONED


def main() -> int:
    for name in TEMPLATES:
        path = ROOT / "templates" / name
        with path.open("r", encoding="utf-8", newline="") as template:
            content = template.read()
        if VERSIONED in content:
            continue
        if COSMIC_LINK.search(content):
            with path.open("w", encoding="utf-8", newline="") as template:
                template.write(COSMIC_LINK.sub(VERSIONED, content, count=1))
            continue
        if OLD not in content:
            raise RuntimeError(f"Expected panel-v2.css link was not found in {path}")
        with path.open("w", encoding="utf-8", newline="") as template:
            template.write(content.replace(OLD, NEW, 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
