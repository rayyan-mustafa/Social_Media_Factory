#!/usr/bin/env python3
"""Broaden OAuth insufficient-scope detection in schedule arm helper."""
from __future__ import annotations

from pathlib import Path

SCHEDULE = Path("/home/ubuntu/new_yt_automation/src/agents/schedule_agent.py")


def main() -> None:
    text = SCHEDULE.read_text(encoding="utf-8")
    text = text.replace(
        '_REAUTH_CMD = ".venv/bin/python -m src.cli.youtube_auth"',
        '_REAUTH_CMD = ".venv/bin/python -m src.cli.youtube_auth --print-url"',
        1,
    )
    old = '''def _oauth_fix_payload(err: str) -> dict[str, Any] | None:
    low = (err or "").lower()
    if ("insufficient" in low and "scope" in low) or (
        "403" in low and "scope" in low
    ):
        return {
            "oauth_insufficient_scopes": True,
            "reauth_command": _REAUTH_CMD,
            "reauth_docs": _REAUTH_DOCS,
            "fix": (
                "YouTube OAuth token lacks publish/manage scopes for "
                "videos.update(part=status). Re-auth with AUTH_SCOPES, then ensure "
                "config/youtube_token.json on the VPS is the new token."
            ),
        }
    return None
'''
    new = '''def _oauth_fix_payload(err: str) -> dict[str, Any] | None:
    low = (err or "").lower()
    hit = (
        ("insufficient" in low and "scope" in low)
        or ("403" in low and "scope" in low)
        or "force-ssl" in low
        or "re-auth" in low
        or "oauth scope" in low
        or "needs youtube.force-ssl" in low
    )
    if not hit:
        return None
    return {
        "oauth_insufficient_scopes": True,
        "reauth_command": _REAUTH_CMD,
        "reauth_docs": _REAUTH_DOCS,
        "fix": (
            "YouTube OAuth token lacks publish/manage scopes for "
            "videos.update(part=status). Re-auth with AUTH_SCOPES, then ensure "
            "config/youtube_token.json on the VPS is the new token."
        ),
    }
'''
    if old not in text:
        if "force-ssl" in text and "def _oauth_fix_payload" in text:
            print("ALREADY_UPDATED")
            return
        raise SystemExit("oauth block missing")
    SCHEDULE.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("UPDATED")


if __name__ == "__main__":
    main()
