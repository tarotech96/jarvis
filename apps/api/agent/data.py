"""
data.py - THE ONLY FILE THAT TOUCHES TARO'S REAL DATA.

Every other module gets folders, inbox items, calendar events and tasks
through the functions in this file. That means there's exactly one place
to look to know what JARVIS can see.

There is no demo mode. It existed to make the thing safe to screen-record
before it read anything real; once it did, the fixtures were just a second
set of answers that could be mistaken for the truth. A source that isn't
connected now says so instead of inventing something plausible.

Nothing in this codebase writes to vault folders. JARVIS writes in exactly
two places, both its own: memory/ (see memory.py) and .tokens/ (see
connectors.py).
"""
from __future__ import annotations

import datetime
import platform
from pathlib import Path

from agent import env, integrations, vault

# ---------------------------------------------------------------------------
# Vault folders (read-only, recursive, .md/.txt/.pdf, 2MB cap - see vault.py)
# ---------------------------------------------------------------------------

# The same vault opens on more than one machine, so the root is chosen by
# the operating system rather than hard-coded. Override with
# JARVIS_WORKS_DIR in .env when a machine keeps its work somewhere else.
MAC_WORKS_DIR = Path.home() / "Documents" / "Works"
WINDOWS_WORKS_DIR = Path("D:/Works")


def platform_root() -> Path:
    override = env.get("JARVIS_WORKS_DIR")
    if override:
        return Path(override).expanduser()
    system = platform.system()
    if system == "Windows":
        return WINDOWS_WORKS_DIR
    if system == "Darwin":
        return MAC_WORKS_DIR
    return Path.home() / "Works"    # Linux and anything else


PLATFORM_ROOT = platform_root()

# Everything under the machine's own Works folder, by default. Narrow it
# from the Sources panel in the UI (main.py's /api/sources), which can only
# ever select folders inside PLATFORM_ROOT - so a stray request can't point
# JARVIS at, say, ~/Documents/Giấy tờ cá nhân.
VAULT_FOLDERS: list[Path] = [PLATFORM_ROOT]


SOURCE_SCAN_DEPTH = 4


def source_candidates() -> list[Path]:
    """
    Folders the UI may offer: the root, its immediate subfolders, and every
    git repository beneath it.

    Repositories matter because they're the unit the graph buckets by - and
    because the ones worth picking are often nested (sanpaizanmai-api lives
    three levels down, under next-surprise/ishizaka). One flat level of
    subfolders could never reach them.
    """
    root = PLATFORM_ROOT
    if not root.is_dir():
        return []

    found: list[Path] = [root]
    seen = {root}

    def walk(directory: Path, depth: int):
        if depth > SOURCE_SCAN_DEPTH:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda d: d.name.lower())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.name in vault.SKIP_DIR_NAMES:
                continue
            is_repo = (child / ".git").exists()
            if (depth == 1 or is_repo) and child not in seen:
                found.append(child)
                seen.add(child)
            if not is_repo:          # don't descend into a repo's innards
                walk(child, depth + 1)

    walk(root, 1)
    return found


def set_vault_folders(folders: list[Path]) -> list[Path]:
    """
    Swaps the indexed folders at runtime. Rejects anything outside
    PLATFORM_ROOT, and anything that isn't a readable directory.

    Nothing is persisted here: guardrail 2 says memory/ is the only place
    JARVIS writes, so the chosen folders live in the browser instead and
    are re-sent on load (see ui/app.js).
    """
    global VAULT_FOLDERS
    root = PLATFORM_ROOT.resolve()
    cleaned: list[Path] = []
    for f in folders:
        path = Path(f).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        if resolved != root and root not in resolved.parents:
            continue
        if resolved not in cleaned:
            cleaned.append(resolved)
    if not cleaned:
        raise ValueError("no readable folder inside " + str(root))
    VAULT_FOLDERS = cleaned
    return cleaned


# ---------------------------------------------------------------------------
# Inbox, calendar, Slack and tasks. All read-only, all via
# agent/integrations.py, all returning None when nothing is connected -
# which is what makes the tools say "not connected" rather than guess.
# A configured source that then fails raises integrations.IntegrationError,
# so tools.py can report what actually went wrong.
# ---------------------------------------------------------------------------


def _day_window(when: str):
    """
    (start, end) for a named range, in the machine's own timezone.

    "today" starts at midnight, not now: asking what is on today means the
    whole day, including the meeting that just finished.
    """
    now = datetime.datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = datetime.timedelta(days=1)
    if when == "today":
        return midnight, midnight + day
    if when == "tomorrow":
        return midnight + day, midnight + 2 * day
    if when == "week":
        return now, midnight + 7 * day
    return now, None          # "upcoming": from now, open-ended


def get_inbox(when: str | None = None) -> list[dict] | None:
    """
    Unread mail from Gmail's Primary tab, or None if Google isn't connected.
    """
    if not integrations.google_configured():
        return None
    # Gmail's own Primary-tab definition. Override with GMAIL_QUERY in .env
    # if you want a different slice (e.g. add -from:noreply@google.com).
    query = env.get("GMAIL_QUERY", "is:unread category:primary -in:spam -in:trash")
    if when == "today":
        query += " newer_than:1d"
    return integrations.gmail_unread(query=query)


def get_calendar(when: str = "upcoming") -> list[dict] | None:
    """
    Google Calendar, scoped to `when` - today / tomorrow / week / upcoming.
    """
    if not integrations.google_configured():
        return None
    start, end = _day_window(when)
    return integrations.calendar_events(
        start, end, max_results=25,
        # Times alone read better when every event is on the same day.
        with_date=when not in ("today", "tomorrow"),
    )


def get_slack() -> list[dict] | None:
    """Messages posted since the last check, across the chosen channels."""
    if not integrations.slack_configured():
        return None
    return integrations.slack_recent_messages()


def get_tasks() -> list[dict] | None:
    """Open work assigned to Taro, from Jira."""
    if not integrations.jira_configured():
        return None
    return integrations.jira_my_work()


def get_done_tasks(days: int = 7) -> list[dict] | None:
    """Recently finished work, for "what did I get through this week"."""
    if not integrations.jira_configured():
        return None
    return integrations.jira_recently_done(days)
