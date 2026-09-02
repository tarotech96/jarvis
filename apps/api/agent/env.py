"""
env.py - the one place that reads .env.

This used to be three byte-identical copies of _load_env(), one each in
voice.py, tools.py and integrations.py - and data.py, which had none, so it
read os.environ directly and silently ignored everything the file said.
A setting that is quietly dropped is worse than one that isn't supported,
so there is now exactly one reader.

Precedence is unchanged: a real environment variable beats .env, and an
empty value counts as unset.
"""
from __future__ import annotations

import os

from agent.paths import ENV_PATH  # noqa: F401  (re-exported for callers)


def load() -> dict:
    env: dict[str, str] = {}
    if not ENV_PATH.exists():
        return env
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


_ENV = load()


def get(key: str, default: str | None = None) -> str | None:
    """Environment first, then .env, then the default. Empty means unset."""
    return os.environ.get(key) or _ENV.get(key) or default
