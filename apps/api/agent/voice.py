"""
voice.py - ElevenLabs speech in and out. This is the only file that
talks to ElevenLabs, and the only file that reads ELEVENLABS_API_KEY.

The key never reaches the browser: the page posts text to /api/speak and
raw audio to /api/listen (see main.py), and this module makes the actual
calls to ElevenLabs from the server side, returning bytes or a transcript.

Uses urllib only (stdlib) - no requests library, no SDK.
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

from agent import env

_get = env.get


API_KEY = _get("ELEVENLABS_API_KEY")
# Default voice: "Adam", a premade ElevenLabs US male voice. Override with
# ELEVENLABS_VOICE_ID in .env if Taro wants a different one.
VOICE_ID = _get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
TTS_MODEL = _get("ELEVENLABS_TTS_MODEL", "eleven_turbo_v2_5")
STT_MODEL = "scribe_v1"

# Lower bitrate = fewer bytes on the wire = speech starts sooner. Voice-only
# audio at 64kbps is indistinguishable from 128 through laptop speakers.
TTS_OUTPUT_FORMAT = _get("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_64")

TTS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
TTS_STREAM_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream"
STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"

# Scribe infers the codec from the filename it's given, so the browser's
# Content-Type has to survive the trip through main.py to here.
_EXT_BY_CONTENT_TYPE = {
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
}

TIMEOUT = 30


def _api_error_detail(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return detail.get("message", str(detail))
        return str(detail) if detail else raw.decode("utf-8", errors="ignore")[:200]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="ignore")[:200]


def text_to_speech(text: str):
    """Returns (audio_bytes, error_string). Exactly one is truthy."""
    if not API_KEY:
        return None, "No ElevenLabs API key configured - voice output is off."

    body = json.dumps({
        "text": text,
        "model_id": TTS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode("utf-8")

    req = urllib.request.Request(
        TTS_URL, data=body, method="POST",
        headers={
            "xi-api-key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as e:
        return None, f"ElevenLabs TTS error ({e.code}): {_api_error_detail(e.read())}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"Couldn't reach ElevenLabs for speech output: {e}"


def text_to_speech_stream(text: str):
    """
    Opens ElevenLabs' streaming TTS endpoint and returns (response, error) -
    exactly one is truthy. The response is a live file-like object: read it in
    chunks and forward them as they arrive, so playback can start before the
    whole clip exists. Any HTTP/network failure surfaces here, BEFORE the
    caller has sent a single byte downstream, so errors stay reportable.
    """
    if not API_KEY:
        return None, "No ElevenLabs API key configured - voice output is off."

    url = (f"{TTS_STREAM_URL}?optimize_streaming_latency=3"
           f"&output_format={urllib.parse.quote(TTS_OUTPUT_FORMAT)}")
    body = json.dumps({
        "text": text,
        "model_id": TTS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "xi-api-key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT), None
    except urllib.error.HTTPError as e:
        return None, f"ElevenLabs TTS error ({e.code}): {_api_error_detail(e.read())}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"Couldn't reach ElevenLabs for speech output: {e}"


def _encode_multipart(fields: dict, file_field: str, filename: str,
                       file_bytes: bytes, content_type: str):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    parts.append(
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
         f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
        + file_bytes + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def speech_to_text(audio_bytes: bytes, audio_content_type: str = "audio/webm"):
    """Returns (transcript, error_string). Exactly one is truthy."""
    if not API_KEY:
        return None, "No ElevenLabs API key configured - voice input is off."

    audio_content_type = (audio_content_type or "audio/webm").split(";")[0].strip().lower()
    ext = _EXT_BY_CONTENT_TYPE.get(audio_content_type, "webm")

    body, content_type = _encode_multipart(
        fields={"model_id": STT_MODEL},
        file_field="file", filename=f"audio.{ext}",
        file_bytes=audio_bytes, content_type=audio_content_type,
    )
    req = urllib.request.Request(
        STT_URL, data=body, method="POST",
        headers={"xi-api-key": API_KEY, "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("text", "").strip(), None
    except urllib.error.HTTPError as e:
        return None, f"ElevenLabs Scribe error ({e.code}): {_api_error_detail(e.read())}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"Couldn't reach ElevenLabs for transcription: {e}"
