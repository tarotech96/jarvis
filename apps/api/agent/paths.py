"""
paths.py - where the repository root is, decided once.

Six modules used to work this out for themselves as "the folder above
agent/". That held exactly as long as the layout did: moving the Python
into apps/api/ would have quietly relocated .env, .tokens/ and memory/
along with it, and the first symptom would have been JARVIS forgetting its
own credentials.

Resolution order:
  1. JARVIS_ROOT in the environment - how the Docker image points at /app.
  2. The nearest ancestor holding turbo.json (the monorepo marker).
  3. The folder above this package, which is the plain-checkout layout.
"""
from __future__ import annotations

import os
from pathlib import Path

MARKER = "turbo.json"


def _find_root() -> Path:
    override = os.environ.get("JARVIS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / MARKER).exists():
            return parent
    return here.parent.parent


REPO_ROOT = _find_root()

ENV_PATH = REPO_ROOT / ".env"
TOKENS_DIR = REPO_ROOT / ".tokens"
MEMORY_DIR = REPO_ROOT / "memory"


def web_dir() -> Path:
    """
    The static UI. JARVIS_WEB_DIR wins (Docker copies it somewhere flat).

    Otherwise it's found relative to THIS FILE, not to REPO_ROOT: the UI
    ships beside the code, while REPO_ROOT is where the data lives, and the
    two are only the same by coincidence. Deriving it from REPO_ROOT meant
    that setting JARVIS_ROOT - which the Dockerfile does - pointed the
    server at a directory with no UI in it, and every page 404'd with
    nothing in the log to say why.
    """
    override = os.environ.get("JARVIS_WEB_DIR")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve().parent.parent      # apps/api
    for candidate in (here.parent / "web" / "public",  # apps/web/public
                      here.parent.parent / "ui"):      # older flat layout
        if candidate.is_dir():
            return candidate
    return here.parent / "web" / "public"
