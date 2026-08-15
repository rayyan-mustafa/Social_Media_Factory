"""CLI: YouTube OAuth — browser window / account picker for Brand Account.

Dual-channel (does NOT overwrite the other channel's token):
  # Existing napstorian Brand Account → config/youtube_token.json
  .venv/bin/python -m src.cli.youtube_auth --channel napstorian

  # New napping_historian Brand Account → config/youtube_token_napping_historian.json
  .venv/bin/python -m src.cli.youtube_auth --channel napping_historian --print-url
  .venv/bin/python -m src.cli.youtube_auth --channel napping_historian --complete "http://localhost/?code=..."

Shared Desktop OAuth client: config/youtube_client_secrets.json
(optional per-channel override via YOUTUBE_CLIENT_SECRETS_PATH_<CHANNEL>).

Redirect URI is taken from the client secrets file's first redirect_uris entry
when present (e.g. http://localhost with no port); otherwise http://localhost:8080/.

Scopes (AUTH_SCOPES): youtube.upload + youtube.force-ssl + yt-analytics.readonly
  — upload, schedule (status.publishAt), pin comments, SMM CTR/AVD scorecard.
"""

from __future__ import annotations

import json
import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import typer
from rich.console import Console
from rich.panel import Panel

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.publish_youtube import AUTH_SCOPES  # noqa: E402
from src.services.settings import ROOT as PROJ, get_settings  # noqa: E402
from src.services.youtube_channel_auth import (  # noqa: E402
    KNOWN_CHANNELS,
    describe_channel_auth,
    normalize_youtube_channel,
    youtube_client_secrets_path,
    youtube_token_path,
)

app = typer.Typer(
    add_completion=False,
    help="Authorize YouTube upload + schedule + comments (OAuth) per channel",
)
console = Console()

_DEFAULT_REDIRECT = "http://localhost:8080/"


def _pending_path(channel: str) -> Path:
    ch = normalize_youtube_channel(channel)
    if ch == "napstorian":
        return PROJ / "config" / "youtube_oauth_pending.json"
    return PROJ / "config" / f"youtube_oauth_pending_{ch}.json"


def _redirect_uri_from_secrets(secrets: Path, *, default_port: int = 8080) -> str:
    """First client-secrets redirect_uris entry, else http://localhost:{default_port}/."""
    try:
        data = json.loads(secrets.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"http://localhost:{default_port}/"
    block = data.get("installed") or data.get("web") or {}
    uris = block.get("redirect_uris") or []
    if uris:
        return str(uris[0]).strip()
    return f"http://localhost:{default_port}/"


def _listen_port_for_redirect(redirect_uri: str, *, fallback_port: int = 8080) -> int:
    """Port for run_local_server. Portless http://localhost → 80."""
    parsed = urlparse(redirect_uri)
    if parsed.port is not None:
        return parsed.port
    if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return 443 if parsed.scheme == "https" else 80
    return fallback_port


def _is_portless_localhost(redirect_uri: str) -> bool:
    parsed = urlparse(redirect_uri)
    return (
        parsed.hostname in ("localhost", "127.0.0.1", "::1")
        and parsed.port is None
    )


def _complete_example(redirect_uri: str, channel: str) -> str:
    """Example --complete URL matching the configured redirect base."""
    base = redirect_uri.rstrip("/") + "/"
    return (
        f'.venv/bin/python -m src.cli.youtube_auth --channel {channel} '
        f'--complete "{base}?code=..."'
    )


@app.command()
def main(
    channel: str = typer.Option(
        "napstorian",
        "--channel",
        "-c",
        help="napstorian | napping_historian (separate token files; never cross-wipe)",
    ),
    port: int = typer.Option(
        8080,
        "--port",
        "-p",
        help="Fallback listen port when client secrets have no redirect_uris "
        "(secrets redirect_uris win when present)",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Do not auto-open browser; print URL only",
    ),
    print_url: bool = typer.Option(
        False,
        "--print-url",
        help="Print consent URL, save PKCE pending state, exit (use --complete next)",
    ),
    complete: str = typer.Option(
        "",
        "--complete",
        help="Finish remote re-auth: paste full redirect URL (or raw code= value)",
    ),
    show_paths: bool = typer.Option(
        False,
        "--show-paths",
        help="Print token/secrets paths for --channel and exit",
    ),
) -> None:
    """Authorize one Brand Account into that channel's token file only."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        console.print(
            "[red]Missing libs[/red] — run:\n"
            "  .venv/bin/pip install google-api-python-client "
            "google-auth-oauthlib google-auth-httplib2"
        )
        raise typer.Exit(1) from exc

    ch = normalize_youtube_channel(channel)
    if ch not in KNOWN_CHANNELS:
        console.print(
            f"[yellow]Unknown channel {ch!r}[/yellow] — still using "
            f"config/youtube_token_{ch}.json (known: {', '.join(KNOWN_CHANNELS)})"
        )

    get_settings.cache_clear()
    secrets = youtube_client_secrets_path(ch)
    token_path = youtube_token_path(ch)
    pending = _pending_path(ch)
    info = describe_channel_auth(ch)

    if show_paths:
        console.print_json(data=info)
        return

    console.print(
        Panel.fit(
            f"[bold]Channel:[/bold] {ch}\n"
            f"[bold]Token file:[/bold] {token_path}\n"
            f"[bold]Client secrets:[/bold] {secrets}\n"
            f"[dim]Other channel tokens are never modified by this run.[/dim]",
            title="YouTube OAuth paths",
        )
    )

    if not secrets.exists():
        console.print(f"[red]Missing client secrets:[/red] {secrets}")
        console.print(
            "Google Cloud → APIs → enable YouTube Data API v3 →\n"
            "Credentials → OAuth client ID (Desktop) → download JSON → save as above path.\n"
            "Same Desktop client JSON can authorize both Brand Accounts into separate tokens."
        )
        raise typer.Exit(2)

    redirect_uri = _redirect_uri_from_secrets(secrets, default_port=port)
    listen_port = _listen_port_for_redirect(redirect_uri, fallback_port=port)

    if complete.strip():
        _complete_pending(secrets, token_path, pending, complete.strip())
        return

    console.print(
        Panel.fit(
            "[bold]YouTube Brand Account sign-in[/bold]\n\n"
            "1. A browser window / tab should open (or use the URL below).\n"
            f"2. Choose the [bold]{ch}[/bold] Brand Account when Google asks "
            "(not only personal Gmail).\n"
            "3. Allow YouTube upload + manage (schedule/pin) + Analytics (CTR/AVD).\n"
            "4. Wait for success — token is saved to THIS channel file only.\n\n"
            f"[dim]redirect_uri (from client secrets): {redirect_uri}[/dim]\n"
            f"[dim]Remote VPS: prefer --print-url then --complete "
            f"(or forward localhost:{listen_port}).[/dim]",
            title="OAuth",
        )
    )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), AUTH_SCOPES)

    if print_url:
        flow.redirect_uri = redirect_uri
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(
            json.dumps(
                {
                    "channel": ch,
                    "redirect_uri": flow.redirect_uri,
                    "code_verifier": getattr(flow, "code_verifier", None),
                    "token_path": str(token_path),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        url_file = PROJ / "config" / f"youtube_oauth_url_{ch}.txt"
        if ch == "napstorian":
            url_file = PROJ / "config" / "youtube_oauth_url.txt"
        url_file.write_text(auth_url + "\n", encoding="utf-8")
        console.print("[bold]Open this URL and sign in:[/bold]")
        console.print(auth_url)
        console.print(f"\n[dim]URL also written → {url_file}[/dim]")
        console.print(f"[dim]Pending PKCE state → {pending}[/dim]")
        console.print(f"[dim]redirect_uri:[/dim] {redirect_uri}")
        console.print(
            "\nAfter Allow, browser may land on "
            f"[yellow]{redirect_uri.rstrip('/')}/?code=...[/yellow] "
            "(connection refused is OK).\n"
            "Paste that FULL URL:\n"
            f"  {_complete_example(redirect_uri, ch)}"
        )
        return

    # Portless http://localhost needs root to bind :80 — prefer paste / --print-url.
    if _is_portless_localhost(redirect_uri):
        console.print(
            "[yellow]Client secrets redirect is portless "
            f"({redirect_uri}) — using paste-redirect "
            "(prefer --print-url / --complete on remote VPS).[/yellow]"
        )
        creds = _paste_redirect_flow(
            flow, redirect_uri=redirect_uri, open_browser=not no_browser
        )
    else:
        try:
            flow.redirect_uri = redirect_uri
            creds = flow.run_local_server(
                port=listen_port,
                prompt="consent",
                access_type="offline",
                include_granted_scopes="true",
                open_browser=not no_browser,
            )
        except OSError:
            console.print(
                f"[yellow]Port {listen_port} busy or unreachable — "
                "paste-redirect fallback[/yellow]"
            )
            creds = _paste_redirect_flow(
                flow, redirect_uri=redirect_uri, open_browser=not no_browser
            )

    _save_token(token_path, creds, channel=ch)


def _extract_code(raw: str) -> str:
    raw = (raw or "").strip().strip('"').strip("'")
    if raw.startswith("http://") or raw.startswith("https://"):
        qs = parse_qs(urlparse(raw).query)
        if "code" not in qs or not qs["code"]:
            console.print("[red]No code= in redirect URL[/red]")
            raise typer.Exit(2)
        code = qs["code"][0]
    elif "code=" in raw:
        code = raw.split("code=", 1)[1].split("&", 1)[0]
    else:
        code = raw
    if not code:
        console.print("[red]Empty OAuth code[/red]")
        raise typer.Exit(2)
    return code


def _complete_pending(
    secrets: Path, token_path: Path, pending_path: Path, raw: str
) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not pending_path.exists():
        console.print(
            f"[red]No pending OAuth state[/red] at {pending_path}. "
            "Run --print-url first for this same --channel."
        )
        raise typer.Exit(2)
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    ch = pending.get("channel") or "napstorian"
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), AUTH_SCOPES)
    flow.redirect_uri = (
        pending.get("redirect_uri")
        or _redirect_uri_from_secrets(secrets)
        or _DEFAULT_REDIRECT
    )
    if pending.get("code_verifier"):
        flow.code_verifier = pending["code_verifier"]
    code = _extract_code(raw)
    flow.fetch_token(code=code)
    _save_token(token_path, flow.credentials, channel=ch)
    try:
        pending_path.unlink()
    except OSError:
        pass


def _save_token(token_path: Path, creds, *, channel: str) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        bak = token_path.with_suffix(".json.bak")
        bak.write_text(token_path.read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"[dim]Previous {channel} token backed up → {bak}[/dim]")
    token_path.write_text(creds.to_json(), encoding="utf-8")
    granted = list(getattr(creds, "scopes", None) or AUTH_SCOPES)
    console.print(f"[green]OK[/green] channel={channel} token → {token_path}")
    console.print(f"[dim]scopes:[/dim] {', '.join(granted)}")
    # Prove we did not wipe the other channel
    other = "napping_historian" if channel == "napstorian" else "napstorian"
    other_path = youtube_token_path(other)
    console.print(
        f"[dim]Other channel ({other}) token untouched:[/dim] "
        f"{other_path} exists={other_path.exists()}"
    )
    missing = [s for s in AUTH_SCOPES if s not in set(granted)]
    if missing:
        console.print(f"[yellow]Missing expected scopes:[/yellow] {', '.join(missing)}")
        console.print("Re-run with prompt=consent so Google shows the extra permissions.")
        if any("yt-analytics" in s for s in missing):
            console.print(
                "[yellow]Without yt-analytics.readonly, SMM scorecard CTR/AVD "
                "stays Data-API-only until re-auth.[/yellow]"
            )
    else:
        console.print(
            "[green]All AUTH_SCOPES present[/green] — "
            "schedule + pin + Analytics scorecard unblocked."
        )


def _paste_redirect_flow(flow, *, redirect_uri: str, open_browser: bool):
    """Remote-safe fallback: user pastes the localhost redirect URL that contains ?code=."""
    flow.redirect_uri = redirect_uri
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    console.print("[bold]Open this URL and sign in with your Brand Account:[/bold]")
    console.print(auth_url)
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001
            pass
    console.print()
    example = redirect_uri.rstrip("/") + "/?code=..."
    console.print(
        "After you Allow, the browser may show [yellow]connection refused[/yellow] "
        "on localhost — that is OK.\n"
        f"Copy the FULL URL from the address bar (e.g. {example}) "
        "and paste it here."
    )
    raw = typer.prompt("Paste redirect URL (or just the code= value)").strip()
    code = _extract_code(raw)
    flow.fetch_token(code=code)
    return flow.credentials


if __name__ == "__main__":
    app()
