"""
memory.py - writes to memory/ and nowhere else.

One dated markdown file per remembered fact. Never touches vault
folders - that boundary is enforced by simply never
importing or referencing them here.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from agent.paths import MEMORY_DIR, REPO_ROOT

BASE_DIR = REPO_ROOT


def _slug(text: str, max_words: int = 6) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())[:max_words]
    return "-".join(words) or "note"


def write_fact(fact: str) -> dict:
    """
    Writes one fact to its own dated file in memory/. Returns the path
    and the exact text written, so the caller can (must) say it out loud.
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    date_prefix = now.strftime("%Y-%m-%d")
    slug = _slug(fact)
    filename = f"{date_prefix}_{slug}.md"
    path = MEMORY_DIR / filename
    # Avoid clobbering an existing memory from the same day/slug.
    n = 2
    while path.exists():
        path = MEMORY_DIR / f"{date_prefix}_{slug}-{n}.md"
        n += 1

    content = (
        f"---\ndate: {now.isoformat(timespec='seconds')}\n---\n\n{fact.strip()}\n"
    )
    path.write_text(content, encoding="utf-8")
    return {"path": str(path.relative_to(BASE_DIR)), "fact": fact.strip()}


def list_facts() -> list[dict]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    facts = []
    for path in sorted(MEMORY_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        body = text.split("---", 2)[-1].strip() if text.startswith("---") else text.strip()
        facts.append({"path": str(path.relative_to(BASE_DIR)), "fact": body})
    return facts
