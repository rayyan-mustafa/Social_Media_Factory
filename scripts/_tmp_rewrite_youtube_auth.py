#!/usr/bin/env python3
"""Add youtube_auth --complete handoff for PKCE remote re-auth."""

from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()
AUTH = ROOT / "src" / "cli" / "youtube_auth.py"

PENDING = "config/youtube_oauth_pending.json"

NEW_AUTH = r'''"""CLI: YouTube OAuth — browser window / account picker for Brand Account.

Usage (run in Cursor terminal so the browser can open / port-forward works):
  .venv/bin/python -m src.cli.youtube_auth

Remote two-step (no hang on print):
  .venv/bin/python -m src.cli.youtube_auth --print-url
  # open URL, Allow, copy redirect URL from address bar (connection refused is OK)
  .venv/bin/python -m src.cli.youtube_auth --complete "http://localhost:8080/?code=..."

When Google asks, pick your Brand Account (not only the personal Gmail).
Requires Desktop OAuth client in config/youtube_client_secrets.json.

Scopes (AUTH_SCOPES): youtube.upload + youtube.force-ssl
  — upload, schedule (status.publishAt), pin comments.
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

app = typer.Typer(
    add_completion=False,
    help="Authorize YouTube upload + schedule + comments (OAuth)",
)
console = Console()

PENDING_PATH = PROJ / "config" / "youtube_oauth_pending.json"


@app.command()
def main(
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
        help="Print consent URL, save PKCE pending state, exit (use --complete next)",
    ),
    complete: str = typer.Option(
        "",
        "--complete",
        help="Finish remote re-auth: paste full redirect URL (or raw code= value)",
    ),
) -> None:
    """Open Google sign-in (account picker) and save config/youtube_token.json.

    Consent covers AUTH_SCOPES: youtube.upload + youtube.force-ssl
    (upload, schedule publishAt/status, pin comments).
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        console.print(
            "[red]Missing libs[/red] — run:\n"
            "  .venv/bin/pip install google-api-python-client "
            "google-auth-oauthlib google-auth-httplib2"
        )
        raise typer.Exit(1) from exc

    get_settings.cache_clear()
    s = get_settings()
    secrets = Path(s.youtube_client_secrets_path)
    token_path = Path(s.youtube_token_path)
    if not secrets.is_absolute():
        secrets = PROJ / secrets
    if not token_path.is_absolute():
        token_path = PROJ / token_path

    if not secrets.exists():
        console.print(f"[red]Missing client secrets:[/red] {secrets}")
        console.print(
            "Google Cloud → APIs → enable YouTube Data API v3 →\n"
            "Credentials → OAuth client ID (Desktop) → download JSON → save as above path."
        )
        raise typer.Exit(2)

    if complete.strip():
        _complete_pending(secrets, token_path, complete.strip())
        return

    console.print(
        Panel.fit(
            "[bold]YouTube Brand Account sign-in[/bold]\n\n"
            "1. A browser window / tab should open (or use the URL below).\n"
            "2. Choose your [bold]Brand Account[/bold] when Google asks "
            "(not only personal Gmail).\n"
            "3. Allow YouTube upload + manage (schedule status + pin comments).\n"
            "4. Wait for success — token is saved automatically.\n\n"
            f"[dim]Remote VPS: Cursor must forward localhost:{port} "
            f"(Ports panel → forward {port}).[/dim]\n"
            "[dim]Or use --print-url then --complete with the redirect URL.[/dim]",
            title="OAuth",
        )
    )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), AUTH_SCOPES)

    if print_url:
        flow.redirect_uri = f"http://localhost:{int(port)}/"
        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
        pending = {
            "redirect_uri": flow.redirect_uri,
            "state": state,
            "code_verifier": getattr(flow, "code_verifier", None),
            "scopes": list(AUTH_SCOPES),
            "client_config_path": str(secrets),
        }
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_PATH.write_text(
            json.dumps(pending, indent=2) + "\n", encoding="utf-8"
        )
        console.print(
            Panel.fit(
                "[bold]Re-auth required[/bold]\n\n"
                "Token must include youtube.force-ssl for schedule + pin.\n"
                "Open the URL below → pick Brand Account → Allow.\n"
                "Browser may show connection refused on localhost — OK.\n"
                "Copy the FULL address-bar URL, then run:\n"
                '  .venv/bin/python -m src.cli.youtube_auth --complete '
                '"http://localhost:8080/?code=..."\n\n'
                f"[cyan]{auth_url}[/cyan]",
                title="OAuth URL",
            )
        )
        console.print(auth_url)
        console.print(f"[dim]PKCE pending saved → {PENDING_PATH}[/dim]")
        raise typer.Exit(0)

    # Prefer browser popup + local redirect (works with Cursor port-forward).
    # prompt=consent forces re-grant when upgrading scopes.
    try:
        creds = flow.run_local_server(
            host="localhost",
            port=int(port),
            open_browser=not no_browser,
            prompt="consent",
            access_type="offline",
            authorization_prompt_message=(
                "\nIf no window popped up, open this URL:\n{url}\n\n"
                "Sign in → pick Brand Account → Allow.\n"
            ),
            success_message=(
                "YouTube auth OK — you can close this tab and return to the terminal."
            ),
        )
    except OSError as exc:
        console.print(f"[yellow]Local server on :{port} failed ({exc}).[/yellow]")
        console.print("Falling back to paste-redirect-URL mode…")
        creds = _paste_redirect_flow(flow, open_browser=not no_browser)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Browser flow failed ({exc}).[/yellow]")
        console.print("Falling back to paste-redirect-URL mode…")
        creds = _paste_redirect_flow(flow, open_browser=not no_browser)

    _save_token(token_path, creds)


def _extract_code(raw: str) -> str:
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        raise typer.Exit(2)
    code = raw
    if raw.startswith("http://") or raw.startswith("https://"):
        qs = parse_qs(urlparse(raw).query)
        if "code" not in qs or not qs["code"]:
            console.print("[red]No code= in that URL[/red]")
            raise typer.Exit(2)
        code = qs["code"][0]
    elif "code=" in raw:
        code = raw.split("code=", 1)[1].split("&", 1)[0]
    return code


def _complete_pending(secrets: Path, token_path: Path, raw: str) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not PENDING_PATH.exists():
        console.print(
            f"[red]No pending OAuth state[/red] at {PENDING_PATH}. "
            "Run --print-url first in this same project."
        )
        raise typer.Exit(2)
    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), AUTH_SCOPES)
    flow.redirect_uri = pending.get("redirect_uri") or "http://localhost:8080/"
    if pending.get("code_verifier"):
        flow.code_verifier = pending["code_verifier"]
    code = _extract_code(raw)
    flow.fetch_token(code=code)
    _save_token(token_path, flow.credentials)
    try:
        PENDING_PATH.unlink()
    except OSError:
        pass


def _save_token(token_path: Path, creds) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        bak = token_path.with_suffix(".json.bak")
        bak.write_text(token_path.read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"[dim]Previous token backed up → {bak}[/dim]")
    token_path.write_text(creds.to_json(), encoding="utf-8")
    granted = list(getattr(creds, "scopes", None) or AUTH_SCOPES)
    console.print(f"[green]OK[/green] token → {token_path}")
    console.print(f"[dim]scopes:[/dim] {', '.join(granted)}")
    missing = [s for s in AUTH_SCOPES if s not in set(granted)]
    if missing:
        console.print(f"[yellow]Missing expected scopes:[/yellow] {', '.join(missing)}")
        console.print("Re-run with prompt=consent so Google shows the extra permissions.")
    else:
        console.print("[green]All AUTH_SCOPES present[/green] — schedule + pin unblocked.")
    console.print(
        "Next: upload with\n"
        "  .venv/bin/python -m src.cli.publish_video PATH/final.mp4 --allow-short"
    )


def _paste_redirect_flow(flow, *, open_browser: bool):
    """Remote-safe fallback: user pastes the localhost redirect URL that contains ?code=."""
    flow.redirect_uri = "http://localhost:8080/"
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
    console.print(
        "After you Allow, the browser may show [yellow]connection refused[/yellow] "
        "on localhost — that is OK.\n"
        "Copy the FULL URL from the address bar (starts with http://localhost:8080/...) "
        "and paste it here."
    )
    raw = typer.prompt("Paste redirect URL (or just the code= value)").strip()
    code = _extract_code(raw)
    flow.fetch_token(code=code)
    return flow.credentials


if __name__ == "__main__":
    app()
'''


def main() -> None:
    AUTH.write_text(NEW_AUTH, encoding="utf-8")
    # compile check
    compile(NEW_AUTH, str(AUTH), "exec")
    print("wrote", AUTH)
    print("OK")


if __name__ == "__main__":
    main()
