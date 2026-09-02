"""
integrations.py - Gmail, Google Calendar and Slack, read-only, stdlib only.

This is the only file that talks to Google or Slack. Scopes/tokens
requested here are read-only on purpose (gmail.readonly,
calendar.readonly, and Slack's *.history / *.read scopes) - JARVIS's
"never send" guardrail (CLAUDE.md) holds for these integrations too:
nothing here can send an email, create an event, or post a Slack
message.

One-time setup per service (see README.md for the console steps):

    python3 agent/integrations.py google-auth     # opens a browser once,
                                                    # then never again
    python3 agent/integrations.py check           # sanity-checks whatever
                                                    # is configured in .env

Everything else (gmail_unread, calendar_upcoming, slack_recent_messages)
is called from data.py, never directly from the UI.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import datetime
import pathlib
import email.header
import email.utils
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

# Runnable as `python3 agent/integrations.py check` as well as importable,
# which needs the package's parent on the path before the import below.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent import connectors, env  # noqa: E402

from agent.paths import TOKENS_DIR as _TOKENS_PATH

TOKENS_DIR = str(_TOKENS_PATH)
GOOGLE_TOKEN_PATH = os.path.join(TOKENS_DIR, "google.json")
SLACK_STATE_PATH = os.path.join(TOKENS_DIR, "slack_state.json")


def _parse_dt(value: str):
    """ISO-8601 from Google -> aware datetime. Returns None if unparseable."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _decode_header(value: str) -> str:
    """
    "=?UTF-8?B?5pel5pys6Kqe?=" -> "日本語".

    Gmail hands back RFC 2047 headers verbatim, so without this every
    Japanese subject and sender name reaches the UI as mojibake.
    """
    if not value or "=?" not in value:
        return value
    try:
        parts = email.header.decode_header(value)
    except Exception:
        return value
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _sender_name(raw: str) -> str:
    """'Taro <t@x.jp>' -> 'Taro'. Falls back to the address."""
    name, addr = email.utils.parseaddr(_decode_header(raw))
    return name or addr or raw


class IntegrationError(Exception):
    """Raised whenever a configured integration fails at runtime -
    distinct from "not configured at all", which callers in data.py
    treat as None, and report as "not connected"."""


_get = env.get


# Read on every use, not once at import: connecting a service from the UI
# has to take effect without restarting the server.
def google_client() -> tuple[str | None, str | None]:
    return (connectors.get("google", "client_id"),
            connectors.get("google", "client_secret"))


def slack_token() -> str | None:
    return connectors.get("slack", "bot_token")


def slack_channels() -> list[str]:
    raw = connectors.get("slack", "channels") or ""
    return [c.strip() for c in raw.split(",") if c.strip()]

GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/calendar.readonly"
)
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REDIRECT_PORT = 8765
GOOGLE_REDIRECT_URI = f"http://localhost:{GOOGLE_REDIRECT_PORT}/oauth2callback"

TIMEOUT = 20


def google_configured() -> bool:
    return all(google_client())


def slack_configured() -> bool:
    return bool(slack_token() and slack_channels())


def _ensure_tokens_dir():
    os.makedirs(TOKENS_DIR, exist_ok=True)


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: str, data: dict):
    _ensure_tokens_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)


# --------------------------------------------------------------- Google auth

def _pkce_pair():
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    return verifier, challenge


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            self.server.auth_code = qs["code"][0]
            body = b"<html><body>Signed in - you can close this tab.</body></html>"
            self.send_response(200)
        else:
            self.server.auth_error = qs.get("error", ["unknown error"])[0]
            body = f"<html><body>Sign-in failed: {self.server.auth_error}</body></html>".encode()
            self.send_response(400)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the one-time auth flow quiet


def google_auth_flow():
    """
    Interactive, one-time OAuth flow. Opens a browser tab for Google
    sign-in, catches the redirect on a temporary local server, exchanges
    the code for tokens, and saves them to .tokens/google.json. Run this
    once from a terminal (python3 agent/integrations.py google-auth) -
    never called from a web request, so it never blocks the UI.
    """
    if not google_configured():
        raise IntegrationError(
            "Add a Google client ID and secret first."
        )

    client_id, client_secret = google_client()
    verifier, challenge = _pkce_pair()
    params = {
        "client_id": client_id,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)

    server = HTTPServer(("localhost", GOOGLE_REDIRECT_PORT), _OAuthCallbackHandler)
    server.auth_code = None
    server.auth_error = None
    server.timeout = 5

    print("Opening a browser to sign in to Google...")
    print(f"If it doesn't open automatically, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    waited = 0
    cancelled = False
    while server.auth_code is None and server.auth_error is None and waited < 180:
        # Checked between requests rather than by closing the socket from
        # another thread: handle_request() has a 5s timeout, so a cancel
        # lands within five seconds and nothing races on the socket.
        if connectors.auth_cancelled():
            cancelled = True
            break
        server.handle_request()
        waited += server.timeout
    server.server_close()

    if cancelled:
        raise IntegrationError("Google sign-in cancelled.")
    if server.auth_error:
        raise IntegrationError(f"Google sign-in failed: {server.auth_error}")
    if server.auth_code is None:
        raise IntegrationError("Timed out waiting for Google sign-in.")

    body = urllib.parse.urlencode({
        "code": server.auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode("utf-8")
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=body, method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise IntegrationError(f"Token exchange failed: {e.read().decode(errors='ignore')}")

    payload["obtained_at"] = time.time()
    _write_json(GOOGLE_TOKEN_PATH, payload)
    print("Google sign-in complete. Token saved to .tokens/google.json")


def google_signed_in() -> bool:
    """
    Credentials configured is not the same as signed in - the OAuth dance
    still has to have happened, and it's the refresh token that proves it.
    """
    return bool(_read_json(GOOGLE_TOKEN_PATH).get("refresh_token"))


def _google_access_token() -> str:
    if not google_configured():
        raise IntegrationError("Google isn't connected yet.")
    token = _read_json(GOOGLE_TOKEN_PATH)
    if not token.get("refresh_token"):
        raise IntegrationError(
            "Not signed in yet - run: python3 agent/integrations.py google-auth"
        )

    obtained_at = token.get("obtained_at", 0)
    expires_in = token.get("expires_in", 0)
    if token.get("access_token") and time.time() < obtained_at + expires_in - 60:
        return token["access_token"]

    body = urllib.parse.urlencode({
        "client_id": google_client()[0],
        "client_secret": google_client()[1],
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=body, method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            fresh = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise IntegrationError(f"Couldn't refresh Google token: "
                                f"{e.read().decode(errors='ignore')}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise IntegrationError(f"Couldn't reach Google to refresh the token: {e}")

    token["access_token"] = fresh["access_token"]
    token["expires_in"] = fresh.get("expires_in", 3600)
    token["obtained_at"] = time.time()
    _write_json(GOOGLE_TOKEN_PATH, token)
    return token["access_token"]


def _google_get(url: str, access_token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise IntegrationError(f"Google API error ({e.code}): "
                                f"{e.read().decode(errors='ignore')[:200]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise IntegrationError(f"Couldn't reach Google: {e}")


# ------------------------------------------------------------------- Gmail

def gmail_unread(max_results: int = 10, query: str | None = None) -> list[dict]:
    """
    Unread mail from the Primary tab only. Raises IntegrationError.

    `is:unread` on its own returns whatever is unread, which on a real
    account means newsletters, receipts and promotions - Taro asked to see
    the Main tab and nothing else. category:primary is the same split Gmail
    shows in its own UI, and the -in: clauses cover the rest.
    """
    token = _google_access_token()
    q = query or "is:unread category:primary -in:spam -in:trash"
    listing = _google_get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        f"?q={urllib.parse.quote(q)}&maxResults={max_results}",
        token,
    )
    items = []
    for ref in listing.get("messages", []):
        msg = _google_get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{ref['id']}"
            "?format=metadata&metadataHeaders=From&metadataHeaders=Subject"
            "&metadataHeaders=Date",
            token,
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = _decode_header(headers.get("Subject", "")) or "(no subject)"
        when = email.utils.parsedate_to_datetime(headers["Date"]) if headers.get("Date") else None
        items.append({
            "from": _sender_name(headers.get("From", "unknown")),
            "subject": subject,
            "date": when.astimezone().strftime("%d/%m %H:%M") if when else "",
            "preview": _decode_header(msg.get("snippet", "")),
            "topic_hint": subject,
        })
    return items


# ---------------------------------------------------------------- Calendar

def _event_display(ev: dict, with_date: bool) -> dict | None:
    """One Google event -> the shape data.py hands the rest of JARVIS."""
    if ev.get("status") == "cancelled":
        return None
    # Something Taro declined is not on his schedule.
    for att in ev.get("attendees", []) or []:
        if att.get("self") and att.get("responseStatus") == "declined":
            return None

    start, end = ev.get("start", {}), ev.get("end", {})
    all_day = "date" in start and "dateTime" not in start
    sdt, edt = _parse_dt(start.get("dateTime", "")), _parse_dt(end.get("dateTime", ""))

    if all_day:
        when = start.get("date", "")
        if with_date and when:
            d = _parse_dt(when + "T00:00:00+00:00")
            when = d.strftime("%d/%m") + " all day" if d else when
        else:
            when = "all day"
    elif sdt:
        local = sdt.astimezone()
        when = local.strftime("%d/%m %H:%M") if with_date else local.strftime("%H:%M")
        if edt:
            when += "-" + edt.astimezone().strftime("%H:%M")
    else:
        when = ""

    return {
        "time": when,
        "title": ev.get("summary", "(untitled)"),
        "location": ev.get("location", ""),
        "all_day": all_day,
        "start": start.get("dateTime") or start.get("date") or "",
    }


def calendar_events(time_min: datetime.datetime, time_max: datetime.datetime | None = None,
                    max_results: int = 20, with_date: bool = True) -> list[dict]:
    """
    Events in a window. Raises IntegrationError.

    The window is the whole point: asking "what's on today" used to send
    only timeMin=now with maxResults=5, so the answer happily ran on into
    next week's events and read as rambling and wrong.
    """
    token = _google_access_token()
    params = {
        "timeMin": time_min.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maxResults": str(max_results),
        "singleEvents": "true",     # expand recurring series into occurrences
        "orderBy": "startTime",
        "showDeleted": "false",
    }
    if time_max is not None:
        params["timeMax"] = time_max.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = _google_get(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events?"
        + urllib.parse.urlencode(params),
        token,
    )
    out = []
    for ev in payload.get("items", []):
        shaped = _event_display(ev, with_date=with_date)
        if shaped:
            out.append(shaped)
    return out


def calendar_upcoming(max_results: int = 5) -> list[dict]:
    """Next few events from now, whenever they are."""
    return calendar_events(datetime.datetime.now().astimezone(),
                            max_results=max_results, with_date=True)


# -------------------------------------------------------------------- Slack

# Slack answers failures with a bare code. "not_in_channel" in particular
# is the single most common way this goes wrong - the bot exists, the token
# is right, and nobody invited it - so say what to do about it.
_SLACK_ERRORS = {
    "not_in_channel": "the bot isn't in {channel} - invite it with "
                       "`/invite @your-app` in that channel",
    "channel_not_found": "no channel {channel} - check the ID (it starts with "
                          "C for public channels, and is not the channel name)",
    "missing_scope": "the app is missing a permission scope",
    "invalid_auth": "Slack rejected the token - copy the Bot User OAuth Token "
                     "(it starts with xoxb-) from OAuth & Permissions",
    "not_authed": "no token was sent - paste the bot token again",
    "account_inactive": "that token's app has been removed from the workspace",
    "token_revoked": "that token has been revoked - reinstall the app and "
                      "copy the new one",
    "ratelimited": "Slack is rate-limiting; try again in a moment",
}


def _slack_error(code: str, params: dict, payload: dict | None = None) -> str:
    channel = params.get("channel", "that channel")
    hint = _SLACK_ERRORS.get(code)
    message = "Slack: " + hint.format(channel=channel) if hint else f"Slack API error: {code}"

    # missing_scope comes back with `needed` and `provided`. Naming the exact
    # scope beats a guessed list - the guess said channels:history when what
    # was actually missing was something else entirely.
    payload = payload or {}
    needed = payload.get("needed")
    if needed:
        message += f" - add `{needed}`"
        provided = payload.get("provided")
        if provided:
            message += f" (it currently has: {provided})"
        message += ", then reinstall the app under OAuth & Permissions"
    return message


def _slack_call(method: str, params: dict) -> dict:
    token = slack_token()
    if not token:
        raise IntegrationError("Slack isn't connected yet.")
    url = f"https://slack.com/api/{method}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise IntegrationError(f"Couldn't reach Slack: {e}")
    if not payload.get("ok"):
        raise IntegrationError(
            _slack_error(payload.get("error", "unknown"), params, payload))
    return payload


_slack_name_cache: dict[str, str] = {}


def _slack_channel_name(channel_id: str) -> str:
    if channel_id not in _slack_name_cache:
        try:
            info = _slack_call("conversations.info", {"channel": channel_id})
            _slack_name_cache[channel_id] = "#" + info["channel"]["name"]
        except IntegrationError:
            _slack_name_cache[channel_id] = channel_id
    return _slack_name_cache[channel_id]


def _slack_user_name(user_id: str) -> str:
    key = f"user:{user_id}"
    if key not in _slack_name_cache:
        try:
            info = _slack_call("users.info", {"user": user_id})
            profile = info["user"].get("profile", {})
            _slack_name_cache[key] = profile.get("real_name") or info["user"].get("name", user_id)
        except IntegrationError:
            _slack_name_cache[key] = user_id
    return _slack_name_cache[key]


def slack_channels_available(limit: int = 200) -> list[dict]:
    """
    Channels this bot can see, so nobody has to go hunting for an ID.

    A Slack channel ID isn't shown anywhere obvious - it's at the bottom of
    the channel-details dialog, or buried in the URL - and typing one wrong
    fails with "channel_not_found". Asking Slack for the list instead turns
    the whole step into ticking a box.

    `is_member` is the one that matters: a bot only reads channels it has
    been invited to, so the rest are listed but flagged.
    """
    params = {
        "types": "public_channel,private_channel",
        "exclude_archived": "true",
        "limit": limit,
    }
    try:
        payload = _slack_call("conversations.list", params)
    except IntegrationError as e:
        # Private channels need groups:read on top of channels:read. Asking
        # for both and failing outright would hide every public channel over
        # a scope the public ones never needed.
        if "missing_scope" not in str(e) and "add `groups:read`" not in str(e):
            raise
        params["types"] = "public_channel"
        payload = _slack_call("conversations.list", params)
    channels = [
        {
            "id": c.get("id", ""),
            "name": "#" + c.get("name", ""),
            "is_member": bool(c.get("is_member")),
            "is_private": bool(c.get("is_private")),
        }
        for c in payload.get("channels", [])
    ]
    # Ones the bot is already in first - those are the ones that will work.
    channels.sort(key=lambda c: (not c["is_member"], c["name"]))
    return channels


def slack_recent_messages(limit_per_channel: int = 10) -> list[dict]:
    """
    Messages posted since the last time this was called, across
    the configured channels (a bot only sees channels it has been invited to -
    that's a Slack constraint, not a JARVIS one). "Since last check" is
    used instead of a fabricated unread count, because bot tokens don't
    have a real one. Raises IntegrationError.
    """
    if not slack_configured():
        raise IntegrationError(
            "Slack isn't connected - add a bot token and channel IDs."
        )
    state = _read_json(SLACK_STATE_PATH)
    items = []
    for channel_id in slack_channels():
        oldest = state.get(channel_id)
        params = {"channel": channel_id, "limit": limit_per_channel}
        if oldest:
            params["oldest"] = oldest
        payload = _slack_call("conversations.history", params)
        messages = [m for m in payload.get("messages", []) if m.get("type") == "message"
                    and not m.get("subtype")]
        if messages:
            state[channel_id] = max(m["ts"] for m in messages)
        channel_name = _slack_channel_name(channel_id)
        for m in messages:
            items.append({
                "channel": channel_name,
                "from": _slack_user_name(m.get("user", "")) if m.get("user") else "unknown",
                "text": m.get("text", ""),
                "ts": m.get("ts"),
            })
    _write_json(SLACK_STATE_PATH, state)
    items.sort(key=lambda m: m["ts"], reverse=True)
    return items


# ------------------------------------------------------------------------ CLI

# --------------------------------------------------------------------- Jira

def jira_site() -> str | None:
    """Accepts 'mycompany' or a full URL and normalises to the host."""
    raw = (connectors.get("jira", "site") or "").strip()
    if not raw:
        return None
    raw = raw.replace("https://", "").replace("http://", "").strip("/")
    return raw if "." in raw else f"{raw}.atlassian.net"


def jira_configured() -> bool:
    return bool(jira_site() and connectors.get("jira", "email")
                and connectors.get("jira", "api_token"))


def _jira_get(path: str, params: dict | None = None) -> dict:
    site = jira_site()
    email = connectors.get("jira", "email")
    token = connectors.get("jira", "api_token")
    if not (site and email and token):
        raise IntegrationError("Jira isn't connected yet.")

    url = f"https://{site}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:200]
        if e.code in (401, 403):
            raise IntegrationError(
                "Jira rejected the credentials - check the email and API token."
            )
        raise IntegrationError(f"Jira error ({e.code}): {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise IntegrationError(f"Couldn't reach Jira: {e}")


def jira_account() -> dict:
    """Who the token belongs to. Used to verify a fresh connection."""
    return _jira_get("/rest/api/3/myself")


_JIRA_PRIORITY = {
    "highest": "high", "high": "high",
    "medium": "medium",
    "low": "low", "lowest": "low",
}


def _shape_issue(issue: dict, site: str) -> dict:
    fields = issue.get("fields", {}) or {}
    status = fields.get("status", {}) or {}
    category = (status.get("statusCategory", {}) or {}).get("key", "")
    priority = ((fields.get("priority") or {}).get("name") or "").lower()
    return {
        # The four keys data.py's task shape promises, so plan_day and
        # brief_me keep working untouched...
        "title": fields.get("summary", "(no summary)"),
        "project": (fields.get("project", {}) or {}).get("key", ""),
        "priority": _JIRA_PRIORITY.get(priority, "medium"),
        "status": "done" if category == "done" else "open",
        # ...plus what only Jira can say.
        "key": issue.get("key", ""),
        "status_name": status.get("name", ""),
        "category": category,           # new | indeterminate | done
        "url": f"https://{site}/browse/{issue.get('key', '')}",
        "updated": (fields.get("updated") or "")[:10],
    }


def jira_issues(jql: str, max_results: int = 50) -> list[dict]:
    """
    Issues matching a JQL query, shaped like data.py's tasks.

    Atlassian replaced /rest/api/3/search with /rest/api/3/search/jql on
    Jira Cloud and the old path now 410s on newer sites, so try the new one
    and fall back - which of the two a site answers depends on when it was
    provisioned.
    """
    site = jira_site()
    params = {
        "jql": jql,
        "maxResults": str(max_results),
        "fields": "summary,status,priority,project,updated",
    }
    try:
        payload = _jira_get("/rest/api/3/search/jql", params)
    except IntegrationError as first:
        # Sniffing the message for "404" also fired on a wrong site name.
        # Just try the older path, and if it fails too, report the original
        # failure - that's the one that describes the real problem.
        try:
            payload = _jira_get("/rest/api/3/search", params)
        except IntegrationError:
            raise first
    return [_shape_issue(i, site) for i in payload.get("issues", [])]


def jira_my_work(max_results: int = 50) -> list[dict]:
    """Everything assigned to you that isn't finished, most recent first."""
    return jira_issues(
        "assignee = currentUser() AND statusCategory != Done "
        "ORDER BY updated DESC",
        max_results,
    )


def jira_recently_done(days: int = 7, max_results: int = 20) -> list[dict]:
    return jira_issues(
        f"assignee = currentUser() AND statusCategory = Done "
        f"AND updated >= -{days}d ORDER BY updated DESC",
        max_results,
    )


def _cli_check():
    print(f"Google configured: {google_configured()}")
    if google_configured():
        try:
            _google_access_token()
            print("  token OK")
        except IntegrationError as e:
            print(f"  {e}")
    print(f"Slack configured: {slack_configured()}")
    if slack_configured():
        try:
            msgs = slack_recent_messages()
            print(f"  OK - {len(msgs)} message(s) since last check")
        except IntegrationError as e:
            print(f"  {e}")
    print(f"Jira configured: {jira_configured()}")
    if jira_configured():
        try:
            me = jira_account()
            print(f"  OK - {me.get('displayName') or me.get('emailAddress')}")
        except IntegrationError as e:
            print(f"  {e}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "google-auth":
        google_auth_flow()
    elif cmd == "check":
        _cli_check()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python3 agent/integrations.py [google-auth|check]")
