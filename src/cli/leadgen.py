"""CLI: agency lead-gen hunter (hunt → score → draft → phantom). No auto-send.

Examples:
  .venv/bin/python -m src.cli.leadgen hunt --region us --vertical salon --city "Austin, TX" --limit 25
  .venv/bin/python -m src.cli.leadgen audit --run-dir output/leadgen/RUN --lead-id abc123
  .venv/bin/python -m src.cli.leadgen phantom --run-dir output/leadgen/RUN --lead-id abc123
  .venv/bin/python -m src.cli.leadgen report --run-dir output/leadgen/RUN
  .venv/bin/python -m src.cli.leadgen email-check --domain yourdomain.com
  .venv/bin/python -m src.cli.leadgen watchdog
  .venv/bin/python -m src.cli.leadgen smm
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command("hunt")
def hunt_cmd(
    region: str = typer.Option("us", "--region", "-r"),
    vertical: str = typer.Option("salon", "--vertical", "-v"),
    city: str = typer.Option("Austin, TX", "--city", "-c"),
    limit: int = typer.Option(25, "--limit", "-n"),
    no_fetch: bool = typer.Option(False, "--no-fetch", help="Skip homepage fetch (faster, weaker scores)"),
    no_sheet: bool = typer.Option(False, "--no-sheet"),
    no_phantom: bool = typer.Option(False, "--no-phantom"),
) -> None:
    """Hunt public listings, score gaps, write drafts. You send by hand."""
    from src.agents.leadgen.cost import can_afford, forecast, remaining_month_usd, render_sheet
    from src.agents.leadgen.places import places_api_key
    from src.agents.leadgen.run import hunt_leads, write_run
    from src.agents.leadgen.watchdog import preflight

    gate = preflight(limit=limit)
    if not gate.get("ok"):
        console.print("[red]leadgen watchdog blocked this hunt[/red]")
        console.print_json(data=gate)
        raise typer.Exit(2)
    for w in gate.get("warnings") or []:
        console.print(f"[yellow]watchdog:[/yellow] {w}")

    # BEFORE: Places forecast when key present; else Nominatim ($0)
    assumed = "places" if places_api_key() else "nominatim"
    before = forecast(limit=limit, source=assumed)
    ok_pay, pay_msg = can_afford(before)
    console.print(render_sheet(before, title="COST SHEET — BEFORE"))
    console.print(pay_msg)
    if not ok_pay:
        raise typer.Exit(2)

    hunt = hunt_leads(
        region_id=region,
        vertical_id=vertical,
        city=city,
        limit=limit,
        fetch_sites=not no_fetch,
    )
    summary = write_run(
        hunt,
        phantom_top=not no_phantom,
        write_sheet=not no_sheet,
        cost_before=before,
    )
    after = summary.get("cost_after") or {}
    console.print(render_sheet(after, title="COST SHEET — AFTER"))
    console.print(f"remaining_month_usd: {remaining_month_usd()}")
    table = Table(title=f"Leadgen {summary.get('run_id')}  source={summary.get('source')}")
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("track")
    table.add_column("name")
    table.add_column("gap")
    for i, row in enumerate(summary.get("top") or [], start=1):
        table.add_row(
            str(i),
            str(row.get("score")),
            str(row.get("track") or ""),
            str(row.get("name") or "")[:36],
            "; ".join(row.get("reasons") or [])[:50],
        )
    console.print(table)
    if summary.get("places_error"):
        console.print(f"[yellow]Places:[/yellow] {summary['places_error']}")
    console.print(f"folder: {summary.get('out_dir')}")
    console.print(f"csv:    {summary.get('csv')}")
    console.print(f"drafts: {summary.get('drafts')}")
    if summary.get("audits_dir"):
        console.print(f"audits: {summary.get('audits_dir')}  ({summary.get('audits_n')} HTML check-ups)")
    if summary.get("phantom"):
        console.print(f"phantom: {summary.get('phantom')}")
    if summary.get("cost_sheet"):
        console.print(f"cost:   {summary.get('cost_sheet')}")
    console.print("[dim]No emails were sent. Copy drafts.md and send the ones you like.[/dim]")
    console.print_json(data={k: v for k, v in summary.items() if k != "top"} | {"top": summary.get("top")})


@app.command("watchdog")
def watchdog_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Check only; still writes last JSON"),
) -> None:
    """SOP / cap check. Never kills YouTube pods or starts RunPod."""
    from src.agents.leadgen.watchdog import tick

    out = tick(dry_run=dry_run)
    console.print_json(data=out)
    if not out.get("ok"):
        raise typer.Exit(2)


@app.command("email-check")
def email_check_cmd(
    domain: str = typer.Option("", "--domain", "-d", help="Agency domain (or LEADGEN_SEND_DOMAIN)"),
) -> None:
    """SPF + DKIM + DMARC DNS check. Does not send mail. VPS must not be the mail host."""
    from src.agents.leadgen.email_auth import check_domain

    out = check_domain(domain or None)
    console.print_json(data=out)
    if out.get("ok"):
        console.print("[green]SPF, DKIM, and DMARC look published. Send by hand from that domain.[/green]")
    else:
        console.print("[red]Not ready to send outreach from this domain.[/red]")
        console.print("SOP: config/sop/leadgen_email_sop.md")
        raise typer.Exit(2)


@app.command("smm")
def smm_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Scorecard only; skip writing digest files"),
) -> None:
    """Lead-gen growth scorecard (not YouTube ceo_smm)."""
    from src.agents.leadgen.smm import run_beat

    out = run_beat(dry_run=dry_run)
    if out.get("digest_md"):
        console.print(f"digest: {out['digest_md']}")
    console.print_json(data=out)


def _lead_from_csv(run_dir: Path, lead_id: str) -> dict[str, str] | None:
    import csv

    csv_path = run_dir / "leads.csv"
    if not csv_path.is_file():
        return None
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("lead_id") == lead_id:
                lead = dict(row)
                flags = str(lead.get("flags") or "")
                lead["flags"] = [p for p in flags.split(";") if p]
                reasons = str(lead.get("reasons") or "")
                lead["reasons"] = [p.strip() for p in reasons.split(";") if p.strip()]
                return lead
    return None


@app.command("audit")
def audit_cmd(
    run_dir: Path = typer.Option(..., "--run-dir"),
    lead_id: str = typer.Option(..., "--lead-id"),
) -> None:
    """Rebuild the plain-language HTML check-up for one lead (no send)."""
    from src.agents.leadgen.audit import write_audit
    from src.agents.leadgen.config import load_region, load_vertical

    lead = _lead_from_csv(run_dir, lead_id)
    if not lead:
        console.print(f"[red]lead {lead_id} not in {run_dir / 'leads.csv'}[/red]")
        raise typer.Exit(1)
    region = load_region(lead.get("region") or "us")
    vertical = load_vertical(lead.get("vertical") or "salon")
    path = write_audit(lead, run_dir / "audits", vertical=vertical, region=region)
    console.print(str(path.resolve()))


@app.command("phantom")
def phantom_cmd(
    run_dir: Path = typer.Option(..., "--run-dir"),
    lead_id: str = typer.Option(..., "--lead-id"),
) -> None:
    """Rebuild phantom HTML for one lead in an existing run."""
    from src.agents.leadgen.config import load_region, load_vertical
    from src.agents.leadgen.phantom import slug, write_phantom

    lead = _lead_from_csv(run_dir, lead_id)
    if not lead:
        console.print(f"[red]lead {lead_id} not in {run_dir / 'leads.csv'}[/red]")
        raise typer.Exit(1)
    region = load_region(lead.get("region") or "us")
    vertical = load_vertical(lead.get("vertical") or "salon")
    payload = {
        "name": lead.get("name"),
        "city": lead.get("city"),
        "address": lead.get("address"),
        "phone": lead.get("phone"),
    }
    dest = run_dir / "phantom" / slug(str(lead.get("name") or lead_id))
    path = write_phantom(payload, dest, vertical=vertical, region=region)
    console.print(str(path.resolve()))


@app.command("draft")
def draft_cmd(
    run_dir: Path = typer.Option(..., "--run-dir"),
    lead_id: str | None = typer.Option(None, "--lead-id"),
) -> None:
    """Print stored drafts (no send)."""
    md = run_dir / "drafts.md"
    csv_path = run_dir / "leads.csv"
    if lead_id and csv_path.is_file():
        import csv

        with csv_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("lead_id") == lead_id:
                    console.print(row.get("subject") or "")
                    console.print(row.get("draft_primary") or "")
                    return
        console.print(f"[red]lead {lead_id} not found[/red]")
        raise typer.Exit(1)
    if md.is_file():
        console.print(md.read_text(encoding="utf-8"))
        return
    console.print("[red]no drafts.md[/red]")
    raise typer.Exit(1)


@app.command("report")
def report_cmd(run_dir: Path = typer.Option(..., "--run-dir")) -> None:
    summary = run_dir / "summary.json"
    if not summary.is_file():
        console.print(f"[red]missing {summary}[/red]")
        raise typer.Exit(1)
    console.print_json(data=json.loads(summary.read_text(encoding="utf-8")))


if __name__ == "__main__":
    app()
