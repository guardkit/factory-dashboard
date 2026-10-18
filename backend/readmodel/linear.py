"""Each project's Linear link, read from the Linear door's own file (design "Linear front door",
section 11.3, 18 October 2026).

The door writes ``linear-projects.json`` into its state directory after each good read of Linear's
projects: ``{"written_at": ..., "projects": {"<owner>/<name>": {"name": ..., "url": ...}}}``. The
board mounts that directory read-only (docker-compose.linear.yml) and reads the file on each page.

No directory, no file, a file that is not that shape, or no entry for a project: no link, and the
pages render nothing for it. Only ``https://linear.app/`` addresses are kept, here in the read model,
so a template never has to judge one; anything else is dropped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: Where the board finds the Linear door's state directory (mounted read-only).
LINEAR_DIR_ENV = "FACTORY_LINEAR_DOOR_STATE_DIR"
DEFAULT_LINEAR_DIR = "/linear"
LINEAR_FILE = "linear-projects.json"

_PREFIX = "https://linear.app/"
_MAX_URL = 2048
_MAX_FILE = 1_000_000


def linear_dir() -> Path:
    return Path(os.environ.get(LINEAR_DIR_ENV) or DEFAULT_LINEAR_DIR)


def _good_url(url: object) -> bool:
    return (isinstance(url, str) and url.startswith(_PREFIX) and len(url) <= _MAX_URL
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "\"'<>\\`" for c in url))


def linear_links(directory: Path | None = None) -> dict[str, str]:
    """``{"owner/name": "https://linear.app/..."}`` for every project with a usable link, else {}."""
    path = (directory or linear_dir()) / LINEAR_FILE
    try:
        if path.stat().st_size > _MAX_FILE:
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    projects = raw.get("projects") if isinstance(raw, dict) else None
    if not isinstance(projects, dict):
        return {}
    out: dict[str, str] = {}
    for repo, entry in projects.items():
        url = entry.get("url") if isinstance(entry, dict) else None
        if isinstance(repo, str) and repo.count("/") == 1 and _good_url(url):
            out[repo] = str(url)
    return out
