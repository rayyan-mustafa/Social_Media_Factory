#!/usr/bin/env python3
"""Patch VPS YouTube OAuth scopes + youtube_auth --print-url. Run on VPS."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "src" / "services" / "publish_youtube.py").exists():
    ROOT = Path.cwd()

PUB = ROOT / "src" / "services" / "publish_youtube.py"
AUTH = ROOT / "src" / "cli" / "youtube_auth.py"


def patch_publish() -> None:
    text = PUB.read_text(encoding="utf-8")
    old = """# Match token scopes from youtube_auth / uploaded token.
# youtube.upload alone is enough for private insert; requesting extra scopes
# on refresh causes invalid_scope if the refresh token wasn't granted them.
# Load credentials with the token's embedded scopes (not this list) so upload
# keeps working until the user re-auths with AUTH_SCOPES for comments/pin.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
]
# Full auth for youtube_auth CLI — upload + commentThreads (pin / engage).
AUTH_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
"""
    new = """# Scope notes (YouTube Data API v3):
# - youtube.upload: videos.insert (upload) + limited metadata on own uploads
# - youtube.force-ssl: videos.update (status/privacy/publishAt schedule),
#   commentThreads.insert + pin, thumbnails, broader manage
# youtube.upload alone is NOT enough for schedule arm or SMM pin (403 insufficient
# authentication scopes on videos?part=status / commentThreads).
# Refresh cannot upgrade scopes — re-run youtube_auth after AUTH_SCOPES changes.
# _build_youtube_client loads scopes embedded in the token (avoids invalid_scope).
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
]
# Full auth for youtube_auth CLI — required for upload + schedule + pin.
AUTH_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
# Subset that schedule/pin need beyond bare upload.
MANAGE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube",  # accepted alternate
]
"""
    if "MANAGE_SCOPES" in text and "schedule arm or SMM pin" in text:
        print("publish_youtube: scope docs already patched")
    elif old not in text:
        raise SystemExit("publish_youtube: scope block not found")
    else:
        text = text.replace(old, new, 1)
        print("publish_youtube: AUTH_SCOPES docs + MANAGE_SCOPES")

    helper = '''
    def token_scopes(self) -> list[str]:
        """Return scopes embedded in youtube_token.json (may be empty if missing)."""
        import json

        token_path = Path(self.s.youtube_token_path)
        if not token_path.is_absolute():
            token_path = ROOT / token_path
        if not token_path.exists():
            return []
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        scopes = data.get("scopes") or data.get("scope") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        return [str(s).strip() for s in scopes if str(s).strip()]

    def has_manage_scopes(self) -> bool:
        """True if token can schedule status / pin comments (force-ssl or youtube)."""
        have = set(self.token_scopes())
        return bool(have.intersection(MANAGE_SCOPES))

    def require_manage_scopes(self, *, op: str) -> None:
        if self.has_manage_scopes():
            return
        have = self.token_scopes() or ["(none)"]
        raise PublishModuleError(
            f"{op} needs youtube.force-ssl (or youtube) OAuth scope; "
            f"token currently has: {', '.join(have)}. "
            "Re-auth: .venv/bin/python -m src.cli.youtube_auth --print-url"
        )

'''
    needle = "    def schedule_publish_at(self, video_id: str, publish_at_iso: str) -> dict[str, Any]:"
    if "def require_manage_scopes" in text:
        print("publish_youtube: helpers already present")
    elif needle not in text:
        raise SystemExit("publish_youtube: schedule_publish_at not found")
    else:
        text = text.replace(needle, helper + needle, 1)
        print("publish_youtube: added scope helpers")

    old_sched = '''    def schedule_publish_at(self, video_id: str, publish_at_iso: str) -> dict[str, Any]:
        """Set private video to go public at publishAt (YouTube handles the flip).

        publish_at_iso: UTC timestamp like 2026-08-07T23:00:00Z
        Note: YouTube requires privacyStatus=private when publishAt is set.
        """
        youtube = self._build_youtube_client()
'''
    new_sched = '''    def schedule_publish_at(self, video_id: str, publish_at_iso: str) -> dict[str, Any]:
        """Set private video to go public at publishAt (YouTube handles the flip).

        publish_at_iso: UTC timestamp like 2026-08-07T23:00:00Z
        Note: YouTube requires privacyStatus=private when publishAt is set.
        Requires AUTH_SCOPES (youtube.force-ssl) — youtube.upload alone → 403.
        """
        self.require_manage_scopes(op="schedule_publish_at / videos.update status")
        youtube = self._build_youtube_client()
'''
    if "schedule_publish_at / videos.update status" in text:
        print("publish_youtube: schedule gate already present")
    elif old_sched not in text:
        raise SystemExit("publish_youtube: schedule body not found")
    else:
        text = text.replace(old_sched, new_sched, 1)
        print("publish_youtube: gated schedule_publish_at")

    old_promo = '''            raise PublishModuleError(
                "Public promote blocked (Plan C): human approve required after Gate B. "
                "Pass force=True only after you reviewed the private upload."
            )
        youtube = self._build_youtube_client()
        body = {"id": video_id, "status": {"privacyStatus": "public"}}
'''
    new_promo = '''            raise PublishModuleError(
                "Public promote blocked (Plan C): human approve required after Gate B. "
                "Pass force=True only after you reviewed the private upload."
            )
        self.require_manage_scopes(op="promote_public / videos.update status")
        youtube = self._build_youtube_client()
        body = {"id": video_id, "status": {"privacyStatus": "public"}}
'''
    if "promote_public / videos.update status" in text:
        print("publish_youtube: promote gate already present")
    elif old_promo in text:
        text = text.replace(old_promo, new_promo, 1)
        print("publish_youtube: gated promote_public")

    PUB.write_text(text, encoding="utf-8")


def patch_auth() -> None:
    text = AUTH.read_text(encoding="utf-8")
    if "--print-url" not in text:
        old_sig = '''def main(
    port: int = typer.Option(
        8080,
        "--port",
        "-p",
        help="Localhost redirect port (Cursor must forward this on remote SSH)",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Do not auto-open browser; print URL only",
    ),
) -> None:
    """Open Google sign-in (account picker) and save config/youtube_token.json."""
'''
        new_sig = '''def main(
    port: int = typer.Option(
        8080,
        "--port",
        "-p",
        help="Localhost forward port (Cursor Ports panel) OR paste-redirect fallback",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Do not auto-open browser; print URL only",
    ),
    print_url: bool = typer.Option(
        False,
        "--print-url",
        help="Print consent URL and exit (no wait) — for remote re-auth handoff",
    ),
) -> None:
    """Open Google sign-in (account picker) and save config/youtube_token.json.

    Consent covers AUTH_SCOPES: youtube.upload + youtube.force-ssl
    (upload, schedule publishAt/status, pin comments).
    """
'''
        if old_sig not in text:
            raise SystemExit("youtube_auth: main signature not found")
        text = text.replace(old_sig, new_sig, 1)
        print("youtube_auth: added --print-url option")

    insert_after = """    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), AUTH_SCOPES)

    # Prefer browser popup + local redirect (works with Cursor port-forward).
"""
    insert_block = r'''    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), AUTH_SCOPES)

    if print_url:
        flow.redirect_uri = f"http://localhost:{int(port)}/"
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
        console.print(
            Panel.fit(
                "[bold]Re-auth required[/bold]\n\n"
                "Token must include youtube.force-ssl for schedule + pin.\n"
                "Open the URL below → pick Brand Account → Allow.\n"
                "Then either:\n"
                f"  • Forward localhost:{port} and re-run without --print-url, or\n"
                "  • Use paste-redirect fallback and paste the full redirect URL.\n\n"
                f"[cyan]{auth_url}[/cyan]",
                title="OAuth URL",
            )
        )
        console.print(auth_url)
        raise typer.Exit(0)

    # Prefer browser popup + local redirect (works with Cursor port-forward).
'''

    if "if print_url:" in text:
        print("youtube_auth: print_url branch already present")
    elif insert_after not in text:
        raise SystemExit("youtube_auth: flow init block not found")
    else:
        text = text.replace(insert_after, insert_block, 1)
        print("youtube_auth: added print_url branch")

    old_ok = r'''    token_path.write_text(creds.to_json(), encoding="utf-8")
    console.print(f"[green]OK[/green] token → {token_path}")
    console.print(
        "Next: upload with\n"
        "  .venv/bin/python -m src.cli.publish_video PATH/final.mp4 --allow-short"
    )
'''
    new_ok = r'''    token_path.write_text(creds.to_json(), encoding="utf-8")
    granted = list(getattr(creds, "scopes", None) or AUTH_SCOPES)
    console.print(f"[green]OK[/green] token → {token_path}")
    console.print(f"[dim]scopes:[/dim] {', '.join(granted)}")
    missing = [s for s in AUTH_SCOPES if s not in set(granted)]
    if missing:
        console.print(f"[yellow]Missing expected scopes:[/yellow] {', '.join(missing)}")
        console.print("Re-run with prompt=consent so Google shows the extra permissions.")
    console.print(
        "Next: upload with\n"
        "  .venv/bin/python -m src.cli.publish_video PATH/final.mp4 --allow-short"
    )
'''
    if "Missing expected scopes" in text:
        print("youtube_auth: scope summary already present")
    elif old_ok not in text:
        raise SystemExit("youtube_auth: ok print block not found")
    else:
        text = text.replace(old_ok, new_ok, 1)
        print("youtube_auth: prints granted scopes after save")

    # Force consent so scope upgrades re-prompt (select_account alone may skip)
    if 'prompt="consent"' not in text.split("run_local_server", 1)[-1][:500]:
        text2 = text.replace(
            '            prompt="select_account",\n            access_type="offline",',
            '            prompt="consent",\n            access_type="offline",',
            1,
        )
        if text2 == text:
            print("youtube_auth: WARN could not set run_local_server prompt=consent")
        else:
            text = text2
            print("youtube_auth: run_local_server prompt=consent")
    else:
        print("youtube_auth: local server already uses consent")

    if (
        'prompt="consent"'
        not in text.split("_paste_redirect_flow", 1)[-1].split("authorization_url", 1)[-1][
            :400
        ]
    ):
        text2 = text.replace(
            '        prompt="select_account",\n        include_granted_scopes="true",',
            '        prompt="consent",\n        include_granted_scopes="true",',
            1,
        )
        if text2 == text:
            print("youtube_auth: WARN could not set paste-flow prompt=consent")
        else:
            text = text2
            print("youtube_auth: paste-flow prompt=consent")
    else:
        print("youtube_auth: paste-flow already uses consent")

    AUTH.write_text(text, encoding="utf-8")


def main() -> None:
    if not PUB.exists() or not AUTH.exists():
        raise SystemExit(f"missing files under {ROOT}")
    bak_pub = PUB.with_suffix(".py.bak_scopes")
    bak_auth = AUTH.with_suffix(".py.bak_scopes")
    if not bak_pub.exists():
        bak_pub.write_text(PUB.read_text(encoding="utf-8"), encoding="utf-8")
    if not bak_auth.exists():
        bak_auth.write_text(AUTH.read_text(encoding="utf-8"), encoding="utf-8")
    patch_publish()
    patch_auth()
    # sanity import
    import sys

    sys.path.insert(0, str(ROOT))
    from src.services.publish_youtube import AUTH_SCOPES, MANAGE_SCOPES

    print("AUTH_SCOPES=", AUTH_SCOPES)
    print("MANAGE_SCOPES=", MANAGE_SCOPES)
    print("OK")


if __name__ == "__main__":
    main()
