"""
connectors.py - the outside services JARVIS can read, and how they're wired.

One place that knows what a connector needs, whether it's connected, and
where its credentials live. Everything that talks to an outside service
(integrations.py) asks here rather than reading os.environ itself.

Two rules this file exists to enforce:

  Credentials are read LAZILY. They used to be module-level constants
  evaluated at import, so connecting Slack from the UI changed a file on
  disk and nothing else until the server was restarted.

  Secrets never reach the browser. status() returns booleans and the
  non-secret fields (a Jira site, a channel list); an API token that went
  out to the page once is a token you have to rotate.

Read-only, still: every scope requested here is a read scope. There is no
code in this project that sends mail, posts to Slack, or writes an issue.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from agent import env

from agent.paths import TOKENS_DIR

STORE_PATH = TOKENS_DIR / "connectors.json"

# field -> the .env name that still overrides it, so an existing setup
# keeps working untouched and .env stays the way to script this.
FIELDS = {
    "google": {
        "client_id": "GOOGLE_CLIENT_ID",
        "client_secret": "GOOGLE_CLIENT_SECRET",
    },
    "slack": {
        "bot_token": "SLACK_BOT_TOKEN",
        "channels": "SLACK_CHANNELS",
    },
    "jira": {
        "site": "JIRA_SITE",
        "email": "JIRA_EMAIL",
        "api_token": "JIRA_API_TOKEN",
    },
}

SECRET_FIELDS = {"client_secret", "bot_token", "api_token"}


def parse_google_credentials(text: str) -> dict | None:
    """
    Pulls the client ID and secret out of the JSON Google Cloud Console
    hands you, so the whole file can be pasted instead of picking two long
    strings out of it by hand. Returns None if it isn't that file.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    node = data.get("installed") or data.get("web") or data
    if not isinstance(node, dict):
        return None
    client_id = node.get("client_id")
    client_secret = node.get("client_secret")
    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret}
    return None

REGISTRY = [
    {
        "id": "google",
        "label": "Google",
        "kind": "oauth",
        "reads": "Unread mail in your Primary tab, and your calendar.",
        "setup": "Create an OAuth client of type Desktop app, download its "
                 "JSON, and paste the file below.",
        "setup_url": "https://console.cloud.google.com/apis/credentials",
        "paste": True,
        "fields": [
            {"name": "client_id", "label": "Client ID", "secret": False},
            {"name": "client_secret", "label": "Client secret", "secret": True},
        ],
    },
    {
        "id": "slack",
        "label": "Slack",
        "kind": "token",
        "reads": "New messages in the channels you list, since the last check.",
        "setup": "OAuth & Permissions → add bot scopes channels:history, "
                 "channels:read, users:read → Install to Workspace → copy the "
                 "Bot User OAuth Token. Then invite the bot to each "
                 "channel. Not the Client ID/Secret from Basic Information.",
        "setup_url": "https://api.slack.com/apps",
        "fields": [
            {"name": "bot_token", "label": "Bot User OAuth Token", "secret": True},
            {"name": "channels", "label": "Channel IDs", "secret": False},
        ],
    },
    {
        "id": "jira",
        "label": "Jira",
        "kind": "token",
        "reads": "Issues assigned to you: what's open, in progress and done.",
        "setup": "Create an API token, then fill in your site and email. "
                 "Site is just the subdomain, e.g. mycompany.",
        "setup_url": "https://id.atlassian.com/manage-profile/security/api-tokens",
        "fields": [
            {"name": "site", "label": "Site", "secret": False},
            {"name": "email", "label": "Atlassian account email", "secret": False},
            {"name": "api_token", "label": "API token", "secret": True},
        ],
    },
]

REGISTRY_BY_ID = {c["id"]: c for c in REGISTRY}


# ------------------------------------------------------------------ store

def _read_store() -> dict:
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_store(store: dict) -> None:
    TOKENS_DIR.mkdir(exist_ok=True)
    STORE_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")
    try:
        os.chmod(STORE_PATH, 0o600)   # tokens, not world-readable
    except OSError:
        pass


def get(connector: str, field: str) -> str | None:
    """Environment first (so .env still wins), then what the UI saved."""
    env_name = FIELDS.get(connector, {}).get(field)
    if env_name:
        from_env = env.get(env_name)
        if from_env:
            return from_env
    value = _read_store().get(connector, {}).get(field)
    return value or None


def save(connector: str, values: dict) -> None:
    if connector not in FIELDS:
        raise ValueError(f"unknown connector: {connector}")

    values = dict(values or {})
    pasted = (values.pop("credentials_json", "") or "").strip()
    if pasted:
        parsed = parse_google_credentials(pasted)
        if parsed is None:
            raise ValueError(
                "That isn't a Google credentials file - download the JSON for "
                "your OAuth client and paste the whole thing."
            )
        values.update(parsed)

    store = _read_store()
    current = store.get(connector, {})
    for field in FIELDS[connector]:
        if field in values:
            new = (values[field] or "").strip()
            # An empty secret means "leave what's there" - the UI never
            # receives the old value, so it can't send it back.
            if not new and field in SECRET_FIELDS:
                continue
            current[field] = new
    store[connector] = current
    _write_store(store)


def clear(connector: str) -> None:
    store = _read_store()
    store.pop(connector, None)
    _write_store(store)
    if connector == "google":
        for path in (TOKENS_DIR / "google.json",):
            try:
                path.unlink()
            except OSError:
                pass


def has_all(connector: str) -> bool:
    return all(get(connector, f) for f in FIELDS.get(connector, {}))


def env_locked(connector: str, field: str) -> bool:
    """True when .env supplies this field, so the UI shouldn't offer to edit it."""
    env_name = FIELDS.get(connector, {}).get(field)
    return bool(env_name and env.get(env_name))


# ------------------------------------------------- in-flight OAuth (google)

_auth_lock = threading.Lock()
_auth_state = {"running": False, "error": None}
_auth_cancel = threading.Event()


def auth_state() -> dict:
    with _auth_lock:
        return dict(_auth_state)


def auth_cancelled() -> bool:
    return _auth_cancel.is_set()


def cancel_auth() -> None:
    """
    Abandon a sign-in that was started and never finished.

    Without this the panel sits on "signing in…" with its button disabled
    for the full three-minute timeout, and closing the browser tab doesn't
    help - there was no way to say "never mind".
    """
    _auth_cancel.set()
    with _auth_lock:
        _auth_state["running"] = False
        _auth_state["error"] = None


def start_auth(runner) -> tuple[bool, str | None]:
    """
    Runs a blocking browser sign-in on a background thread.

    It has to be off the request thread: the flow waits up to three minutes
    for a human to click through Google, and the page needs to stay
    responsive and poll for the result.
    """
    with _auth_lock:
        if _auth_state["running"]:
            return False, "A sign-in is already in progress."
        _auth_state["running"] = True
        _auth_state["error"] = None
    _auth_cancel.clear()

    def work():
        error = None
        try:
            runner()
        except Exception as e:                    # surfaced verbatim to the UI
            error = str(e)
        with _auth_lock:
            # A cancel already moved the UI on; don't drag it back.
            if not _auth_cancel.is_set():
                _auth_state["running"] = False
                _auth_state["error"] = error

    threading.Thread(target=work, daemon=True).start()
    return True, None
