"""
main.py - JARVIS's HTTP server and API. Python standard library only.

Serves the UI from ui/ and exposes a small JSON API:

  GET  /api/graph            -> current vault graph (nodes + edges)
  GET  /api/note?id=...      -> full content for one note (inspector)
  GET  /api/connectors       -> every connector, connected or not
  POST /api/connectors       -> {id, action, fields} -> save / test / sign in / disconnect
  GET  /api/sources          -> which folders are indexed, and what else could be
  POST /api/sources          -> {folders:[...]} -> reindex those folders
  GET  /api/status           -> what's available: model, mic, transcriber
  GET  /api/speak?text=...   -> audio/mpeg, streamed as ElevenLabs produces it
  POST /api/chat             -> {message, history} -> {speech, card, tool}
  POST /api/speak            -> {text} -> audio/mpeg bytes (ElevenLabs TTS)
  POST /api/listen           -> audio bytes -> {text} (ElevenLabs Scribe)
  POST /api/brief            -> brief_me tool, no input needed
  POST /api/plan             -> plan_day tool, no input needed
  POST /api/remember         -> {fact} -> remember tool

Run with:  python3 agent/main.py
"""
from __future__ import annotations

import errno
import json
import mimetypes
import os
import platform
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import connectors, vault, data, tools, memory, paths, voice, integrations  # noqa: E402

# Loopback by default: this serves Taro's mail, calendar and files, and
# nothing about it should be reachable from the network unless someone
# deliberately says so. The container sets JARVIS_HOST=0.0.0.0, and Docker
# is what decides who can reach the published port.
HOST = os.environ.get("JARVIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("JARVIS_PORT", "8080"))

UI_DIR = paths.web_dir()

# Built once at startup, rebuilt on demand via /api/graph?refresh=1.
_index_cache: vault.VaultIndex | None = None


def get_index(refresh: bool = False) -> vault.VaultIndex:
    global _index_cache
    if _index_cache is None or refresh:
        _index_cache = vault.build_index(data.VAULT_FOLDERS)
    return _index_cache


def connectors_payload() -> dict:
    """
    Every connector and its live state. Secrets never appear here - only
    whether a field is filled, plus the values that aren't sensitive.
    """
    auth = connectors.auth_state()
    out = []
    for spec in connectors.REGISTRY:
        cid = spec["id"]
        fields = []
        for f in spec["fields"]:
            stored = connectors.get(cid, f["name"])
            fields.append({
                "name": f["name"],
                "label": f["label"],
                "secret": f["secret"],
                "filled": bool(stored),
                # A secret is never echoed back; the rest is, so the form
                # shows what's currently set.
                "value": "" if f["secret"] else (stored or ""),
                "locked": connectors.env_locked(cid, f["name"]),
            })

        if cid == "google":
            has_keys = connectors.has_all("google")
            signed_in = integrations.google_signed_in()
            state = ("connected" if signed_in
                     else "needs_signin" if has_keys else "not_configured")
        elif cid == "slack":
            state = "connected" if integrations.slack_configured() else "not_configured"
        else:
            state = "connected" if integrations.jira_configured() else "not_configured"

        out.append({
            "id": cid, "label": spec["label"], "kind": spec["kind"],
            "reads": spec["reads"], "setup": spec["setup"],
            "setup_url": spec.get("setup_url", ""),
            "paste": spec.get("paste", False),
            "fields": fields, "state": state,
        })
    return {"connectors": out, "auth": auth}


def connector_check(cid: str) -> tuple[bool, str]:
    """A cheap live call, so 'connected' means it actually answered."""
    try:
        if cid == "google":
            integrations.calendar_upcoming(max_results=1)
            return True, "Google is answering."
        if cid == "slack":
            msgs = integrations.slack_recent_messages()
            return True, f"Slack is answering ({len(msgs)} new since last check)."
        if cid == "jira":
            me = integrations.jira_account()
            name = me.get("displayName") or me.get("emailAddress") or "your account"
            return True, f"Jira is answering as {name}."
    except integrations.IntegrationError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Unexpected failure: {e}"
    return False, "Unknown connector."


def sources_payload() -> dict:
    """What the Sources panel needs: the machine's root and what's under it."""
    root = data.PLATFORM_ROOT
    return {
        "platform": platform.system(),
        "root": str(root),
        "root_exists": root.is_dir(),
        "selected": [str(f) for f in data.VAULT_FOLDERS],
        "sources": len(tools._code().files),
        "candidates": [
            {
                "path": str(d),
                "name": str(d) if d == root else str(d.relative_to(root)),
                "is_root": d == root,
            }
            for d in data.source_candidates()
        ],
    }


def json_bytes(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "JARVIS/1.0"

    def log_message(self, fmt, *args):
        # Quieter default logging; keep it to one line per request.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -----------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json_bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_speech(self, text: str):
        """
        Proxies ElevenLabs' streaming TTS straight through to the browser.

        The upstream call is opened first, so an auth/network failure still
        comes back as a normal JSON error - by the time a 200 goes out, audio
        is already flowing. No Content-Length: the connection closing is the
        end-of-stream signal (this server speaks HTTP/1.0), which is exactly
        what an <audio> element wants for progressive playback.
        """
        upstream, err = voice.text_to_speech_stream(text)
        if err:
            self._send_json({"error": err}, 502)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with upstream:
                while True:
                    chunk = upstream.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # The page navigated away or barged in mid-sentence. Expected.
            pass

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _serve_static(self, path: str):
        if path == "/":
            path = "/index.html"
        safe_path = (UI_DIR / path.lstrip("/")).resolve()
        if UI_DIR.resolve() not in safe_path.parents and safe_path != UI_DIR.resolve():
            self._send_json({"error": "not found"}, 404)
            return
        if not safe_path.exists() or not safe_path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        content_type, _ = mimetypes.guess_type(str(safe_path))
        body = safe_path.read_bytes()
        self._send_bytes(body, content_type or "application/octet-stream")

    # -- routing -------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/api/graph":
            refresh = qs.get("refresh", ["0"])[0] == "1"
            idx = get_index(refresh=refresh)
            self._send_json(idx.to_graph_json())
            return

        if parsed.path == "/api/note":
            note_id = qs.get("id", [None])[0]
            idx = get_index()
            note = idx.notes.get(note_id) if note_id else None
            if note is None:
                self._send_json({"error": "not found"}, 404)
                return
            self._send_json({
                "id": note.id, "title": note.title, "type": note.type,
                "repo": note.repo_name,
                "path": str(note.path), "content": note.content,
                "degree": note.degree,
            })
            return

        if parsed.path == "/api/status":
            self._send_json(tools.get_status())
            return

        if parsed.path == "/api/connectors":
            self._send_json(connectors_payload())
            return

        if parsed.path == "/api/sources":
            self._send_json(sources_payload())
            return

        if parsed.path == "/api/speak":
            text = (qs.get("text", [""])[0] or "").strip()
            if not text:
                self._send_json({"error": "no text"}, 400)
                return
            self._stream_speech(text)
            return

        self._serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/chat":
            body = self._read_json_body()
            result = tools.handle_chat(body.get("message", ""), body.get("history", []))
            self._send_json(result)
            return

        if parsed.path == "/api/brief":
            self._send_json(tools.brief_me())
            return

        if parsed.path == "/api/plan":
            self._send_json(tools.plan_day())
            return

        if parsed.path == "/api/connectors":
            body = self._read_json_body()
            cid = body.get("id", "")
            action = body.get("action", "save")
            if cid not in connectors.REGISTRY_BY_ID:
                self._send_json({"error": "unknown connector"}, 400)
                return

            if action == "disconnect":
                connectors.clear(cid)
                self._send_json({"ok": True, "message": "Disconnected.",
                                  **connectors_payload()})
                return

            if action == "list_channels":
                if cid != "slack":
                    self._send_json({"error": "Only Slack lists channels."}, 400)
                    return
                # Save the token first so the list can be fetched with a
                # token that was just typed and not saved yet.
                try:
                    connectors.save(cid, body.get("fields") or {})
                except ValueError as e:
                    self._send_json({"error": str(e)}, 400)
                    return
                if not connectors.get("slack", "bot_token"):
                    self._send_json({"error": "Paste the bot token first."}, 400)
                    return
                try:
                    channels = integrations.slack_channels_available()
                except integrations.IntegrationError as e:
                    self._send_json({"error": str(e)}, 502)
                    return
                joined = sum(1 for c in channels if c["is_member"])
                self._send_json({
                    "ok": True,
                    "channels": channels,
                    "message": f"{len(channels)} channel(s), {joined} the bot is in.",
                    **connectors_payload(),
                })
                return

            if action == "cancel_signin":
                connectors.cancel_auth()
                self._send_json({"ok": True, "message": "Sign-in cancelled.",
                                  **connectors_payload()})
                return

            if action == "signin":
                if cid != "google":
                    self._send_json({"error": "That connector has no sign-in step."}, 400)
                    return
                # Save whatever was typed first. Making the human press Save
                # and *then* Sign in meant typing the credentials, clicking
                # sign-in, and being told to add the credentials.
                try:
                    connectors.save(cid, body.get("fields") or {})
                except ValueError as e:
                    self._send_json({"error": str(e)}, 400)
                    return
                if not connectors.has_all("google"):
                    self._send_json({
                        "error": "Paste the credentials JSON from Google Cloud "
                                  "Console first (or fill in the ID and secret).",
                        **connectors_payload(),
                    }, 400)
                    return
                started, why = connectors.start_auth(integrations.google_auth_flow)
                if not started:
                    self._send_json({"error": why}, 409)
                    return
                self._send_json({"ok": True, "message": "Opening your browser…",
                                  **connectors_payload()})
                return

            # Default: save what was typed, then prove it works.
            try:
                connectors.save(cid, body.get("fields") or {})
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            if not connectors.has_all(cid):
                self._send_json({"ok": True, "message": "Saved. Some fields are still empty.",
                                  **connectors_payload()})
                return
            if cid == "google":
                self._send_json({"ok": True, "message": "Saved. Now sign in.",
                                  **connectors_payload()})
                return
            ok, message = connector_check(cid)
            self._send_json({"ok": ok, "message": message, **connectors_payload()})
            return

        if parsed.path == "/api/sources":
            body = self._read_json_body()
            folders = body.get("folders") or []
            try:
                chosen = data.set_vault_folders(folders)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            # Both caches hold notes from the old folders; drop them together
            # or the graph and the search tools disagree about what exists.
            idx = get_index(refresh=True)
            tools.refresh_index()
            self._send_json({
                "selected": [str(f) for f in chosen],
                "notes": len(idx.notes),
                "edges": len(idx.edges),
                "sources": len(tools._code().files),
            })
            return

        if parsed.path == "/api/remember":
            body = self._read_json_body()
            self._send_json(tools.remember(body.get("fact", "")))
            return

        if parsed.path == "/api/speak":
            body = self._read_json_body()
            text = body.get("text", "")
            if not text.strip():
                self._send_json({"error": "no text"}, 400)
                return
            audio, err = voice.text_to_speech(text)
            if err:
                self._send_json({"error": err}, 502)
                return
            self._send_bytes(audio, "audio/mpeg")
            return

        if parsed.path == "/api/listen":
            audio = self._read_raw_body()
            if not audio:
                self._send_json({"error": "no audio"}, 400)
                return
            transcript, err = voice.speech_to_text(
                audio, self.headers.get("Content-Type", "audio/webm"))
            if err:
                self._send_json({"error": err}, 502)
                return
            self._send_json({"text": transcript})
            return

        self._send_json({"error": "not found"}, 404)


def main():
    get_index()  # warm the cache and fail fast if folders are misconfigured
    if not UI_DIR.is_dir():
        print(f"WARNING: no UI at {UI_DIR} - the API will answer but every "
              f"page will 404. Set JARVIS_WEB_DIR to the folder holding "
              f"index.html.")
    if not data.PLATFORM_ROOT.is_dir():
        print(f"WARNING: {data.PLATFORM_ROOT} doesn't exist on this machine - "
              f"the graph will be empty. Set JARVIS_WORKS_DIR in .env to point "
              f"at your work folder.")
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        # "Address already in use" as a twelve-line traceback tells you
        # nothing you can act on. Almost always this is `docker compose up`
        # already holding the port, so say that and how to get past it.
        print(f"Port {PORT} is already in use, so JARVIS can't start.", file=sys.stderr)
        print(f"  Something is already listening on {HOST}:{PORT} - usually the "
              f"container from `docker compose up`.", file=sys.stderr)
        print("  Either stop it:      docker compose down", file=sys.stderr)
        print(f"  or use another port: JARVIS_PORT={PORT + 1} "
              f"{'yarn dev' if os.environ.get('TURBO_HASH') else 'python3 agent/main.py'}",
              file=sys.stderr)
        print(f"  (see what has it:    lsof -nP -iTCP:{PORT} -sTCP:LISTEN)", file=sys.stderr)
        raise SystemExit(1)

    print(f"JARVIS running at http://{HOST}:{PORT}  "
          f"(reading {data.PLATFORM_ROOT})")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
