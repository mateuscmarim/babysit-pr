"""Gitea connection settings shared by poll_review.py and reply_finding.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_URL = os.environ.get("GITEA_BASE_URL", "https://gitea.example.com").rstrip("/")
TEA_CONFIG = Path.home() / ".config" / "tea" / "config.yml"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def tea_token(config_text: str, base_url: str) -> str | None:
    """The token of the tea login whose `url` is `base_url`, or None.

    tea keeps one entry per login under `logins:`. Taking the first `token:`
    line regardless of host -- what this used to do -- sends one server's
    credential to another the moment a second login exists. Read by hand
    rather than with PyYAML so the scripts keep running on a bare python3."""
    want = base_url.rstrip("/")
    logins: list[dict[str, str]] = []
    for line in config_text.splitlines():
        s = line.strip()
        if s.startswith("- "):
            logins.append({})
            s = s[2:].strip()
        if not logins or ":" not in s:
            continue
        key, value = s.split(":", 1)
        if key in ("url", "token"):
            logins[-1][key] = _unquote(value)
    for login in logins:
        if login.get("url", "").rstrip("/") == want and login.get("token"):
            return login["token"]
    return None


def token() -> str:
    if env := os.environ.get("GITEA_TOKEN"):
        return env
    if not TEA_CONFIG.exists():
        sys.exit(f"no token: set GITEA_TOKEN or configure {TEA_CONFIG}")
    if tok := tea_token(TEA_CONFIG.read_text(), BASE_URL):
        return tok
    sys.exit(
        f"no token: no tea login in {TEA_CONFIG} has url {BASE_URL} -- set "
        "GITEA_TOKEN, or GITEA_BASE_URL to match a login"
    )
