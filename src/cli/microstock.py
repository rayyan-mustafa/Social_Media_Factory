"""CLI for the microstock vector engine.

Examples:
  .venv/bin/python -m src.cli.microstock status
  .venv/bin/python -m src.cli.microstock generate --prompt "flat vector cloud icons" --backend mock
  .venv/bin/python -m src.cli.microstock trace --input output/microstock/raw_png/x.png
  .venv/bin/python -m src.cli.microstock clean --input output/microstock/traced_svg/x.svg
  .venv/bin/python -m src.cli.microstock gate  --input output/microstock/clean_svg/x.svg
  .venv/bin/python -m src.cli.microstock run   --prompt "isometric supply chain" --backend mock
  .venv/bin/python -m src.cli.microstock run   --source-png some.png
  .venv/bin/python -m src.cli.microstock doctor
  .venv/bin/python -m src.cli.microstock beat --dry-run
  .venv/bin/python -m src.cli.microstock harvest --target 60 --no-llm
  .venv/bin/python -m src.cli.microstock plan
  .venv/bin/python -m src.cli.microstock confirm --platform adobe_stock --batch <id>
  .venv/bin/python -m src.cli.microstock crontab
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.microstock import config, paths  # noqa: E402
from src.microstock.ledger import AssetLedger  # noqa: E402

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Microstock vector engine")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command("status")
def status() -> None:
    """Show config gates, ledger counts and per-platform standing."""
    paths.ensure_dirs()
    ledger = AssetLedger()

    table = Table(title="Microstock engine", show_header=False)
    table.add_row("master enabled", "[green]yes[/]" if config.is_enabled() else "[yellow]no (config.enabled=false)[/]")
    table.add_row("distribution", "[green]armed[/]" if config.distribution_enabled() else "[yellow]disarmed[/]")
    table.add_row("generator backend", str(config.section("generator").get("backend")))
    table.add_row("daily target", str(config.load_settings().get("daily_asset_target")))
    table.add_row("output dir", str(paths.OUTPUT_DIR.relative_to(paths.ROOT)))
    console.print(table)

    counts = ledger.counts_by_stage()
    if counts:
        stage_table = Table(title="Assets by stage")
        stage_table.add_column("stage")
        stage_table.add_column("count", justify="right")
        for stage, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            stage_table.add_row(stage, str(count))
        console.print(stage_table)
    else:
        console.print("[dim]no assets in the ledger yet[/]")

    platform_table = Table(title="Platforms")
    for column in ("platform", "tier", "enabled", "cap/day", "uploaded today", "lifetime"):
        platform_table.add_column(column)
    for platform in config.load_platforms(enabled_only=False):
        name = platform["name"]
        platform_table.add_row(
            name,
            platform.get("tier", ""),
            "[green]yes[/]" if platform.get("enabled") else "[dim]no[/]",
            str(platform.get("daily_cap", "-")),
            str(ledger.uploaded_today(name)),
            str(ledger.uploaded_count(name)),
        )
    console.print(platform_table)


@app.command("doctor")
def doctor() -> None:
    """Check dependencies, keys and config before a first real run."""
    import os

    rows: list[tuple[str, bool, str]] = []

    for module, label in (("vtracer", "vtracer"), ("lxml", "lxml"), ("cairosvg", "cairosvg"),
                          ("PIL", "Pillow"), ("numpy", "numpy"), ("httpx", "httpx")):
        try:
            __import__(module)
            rows.append((label, True, "installed"))
        except ImportError as exc:
            rows.append((label, False, str(exc)))

    from src.microstock.generator import gemini_api_key

    backend = str(config.section("generator").get("backend") or "gemini")
    if backend == "gemini":
        key = gemini_api_key()
        rows.append(("GEMINI_API_KEY", bool(key),
                     f"set ({key[:6]}…)" if key else "missing — https://aistudio.google.com/apikey"))
    elif backend == "seedream":
        wavespeed = (os.environ.get("WAVESPEED_API_KEY") or "").strip()
        rows.append(("WAVESPEED_API_KEY", bool(wavespeed), "set" if wavespeed else "missing"))

    for path in (paths.SETTINGS_PATH, paths.NICHES_PATH, paths.PLATFORMS_PATH):
        rows.append((path.name, path.is_file(), "found" if path.is_file() else "MISSING"))

    enabled = [p["name"] for p in config.load_platforms()]
    rows.append(("enabled platforms", bool(enabled), ", ".join(enabled) or "none (distribution inert)"))

    table = Table(title="Preflight")
    table.add_column("check")
    table.add_column("ok")
    table.add_column("detail")
    for label, ok, detail in rows:
        table.add_row(label, "[green]✓[/]" if ok else "[red]✗[/]", detail)
    console.print(table)

    blocking = [label for label, ok, _ in rows if not ok and label.endswith("_KEY")]
    if blocking:
        console.print(f"\n[yellow]Generation is blocked until: {', '.join(blocking)}[/]")


@app.command("generate")
def generate_cmd(
    prompt: str = typer.Option(..., "--prompt", "-p"),
    backend: str | None = typer.Option(None, "--backend", help="gemini | seedream | mock"),
    out: Path | None = typer.Option(None, "--out"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Generate a single raster from a prompt."""
    _setup_logging(verbose)
    from src.microstock.generator import GenerateError, generate

    paths.ensure_dirs()
    try:
        image = generate(prompt, out_path=out, backend=backend)
    except GenerateError as exc:
        console.print(f"[red]generation failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]wrote[/] {image.path} ({image.path.stat().st_size} bytes) "
        f"via {image.backend}/{image.model} cost=${image.cost_usd:.4f}"
    )


@app.command("trace")
def trace_cmd(
    input_png: Path = typer.Option(..., "--input", "-i"),
    out: Path | None = typer.Option(None, "--out"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Vectorise a raster into a multi-colour SVG."""
    _setup_logging(verbose)
    from src.microstock.vectorizer import VectorizeError, vectorize

    paths.ensure_dirs()
    try:
        dest = vectorize(input_png, out)
    except VectorizeError as exc:
        console.print(f"[red]trace failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]traced[/] {dest} ({dest.stat().st_size} bytes)")


@app.command("clean")
def clean_cmd(
    input_svg: Path = typer.Option(..., "--input", "-i"),
    out: Path | None = typer.Option(None, "--out"),
    epsilon: float | None = typer.Option(None, "--epsilon", help="Override Douglas-Peucker tolerance"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Sanitise, normalise and simplify an SVG."""
    _setup_logging(verbose)
    from src.microstock.svg_cleaner import SvgCleanError, clean_svg

    overrides = {"simplify_epsilon": epsilon} if epsilon is not None else None
    try:
        report = clean_svg(input_svg, out, overrides=overrides)
    except SvgCleanError as exc:
        console.print(f"[red]clean failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print_json(data=report.as_dict())


@app.command("gate")
def gate_cmd(
    input_svg: Path = typer.Option(..., "--input", "-i"),
    reference: Path | None = typer.Option(None, "--reference", help="Pre-simplification trace, enables the fidelity check"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run Gate V against a cleaned SVG."""
    _setup_logging(verbose)
    from src.microstock.qa_gate import check_svg

    result = check_svg(input_svg, reference_svg=reference)
    console.print_json(data=result.as_dict())
    if not result.passed:
        raise typer.Exit(1)


@app.command("run")
def run_cmd(
    prompt: str | None = typer.Option(None, "--prompt", "-p"),
    source_png: Path | None = typer.Option(None, "--source-png", help="Skip generation, vectorise this raster"),
    niche: str = typer.Option("", "--niche"),
    backend: str | None = typer.Option(None, "--backend"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run one asset through the whole chain: generate -> trace -> clean -> Gate V."""
    _setup_logging(verbose)
    if not prompt and not source_png:
        console.print("[red]need --prompt or --source-png[/]")
        raise typer.Exit(2)

    from src.microstock.pipeline import process_asset

    result = process_asset(
        prompt or f"(from {source_png})", niche=niche, backend=backend, source_png=source_png
    )
    console.print_json(data=result.as_dict())
    if not result.passed:
        raise typer.Exit(1)


@app.command("beat")
def beat_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan every step, change nothing"),
    max_assets: int | None = typer.Option(None, "--max-assets"),
    backend: str | None = typer.Option(None, "--backend"),
    skip_gates: bool = typer.Option(False, "--skip-gates", help="Bypass enable/disk/politeness gates (testing)"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Use template briefs instead of the LLM"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run one unattended cycle: harvest -> produce -> gate -> tag -> drip."""
    _setup_logging(verbose)
    from src.microstock.stock_factory import run_beat

    result = run_beat(
        max_assets=max_assets, backend=backend, dry_run=dry_run,
        skip_gates=skip_gates, use_llm=not no_llm,
    )
    console.print_json(data=result)


@app.command("harvest")
def harvest_cmd(
    target: int | None = typer.Option(None, "--target"),
    no_llm: bool = typer.Option(False, "--no-llm"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Top the brief stock back up to target."""
    _setup_logging(verbose)
    from src.microstock import brief_harvest

    console.print_json(data=brief_harvest.maybe_refill(
        target=target, use_llm=not no_llm, dry_run=dry_run
    ))


@app.command("tag")
def tag_cmd(
    image: Path = typer.Option(..., "--input", "-i"),
    backend: str | None = typer.Option(None, "--backend", help="openrouter | mock"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Generate stock metadata for one raster."""
    _setup_logging(verbose)
    from src.microstock.ai_tagger import TagError, tag_image

    try:
        metadata = tag_image(image, backend=backend)
    except TagError as exc:
        console.print(f"[red]tagging failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print_json(data=metadata.as_dict())


@app.command("plan")
def plan_cmd() -> None:
    """Show what each platform would receive on the next release."""
    from src.microstock import drip

    rows = drip.plan()
    if not rows:
        console.print("[yellow]no enabled platforms — distribution is inert[/]")
        return
    table = Table(title="Drip plan (highest royalty first)")
    for column in ("platform", "tier", "pending", "allowance", "releasing", "backlog"):
        table.add_column(column, justify="right" if column != "platform" else "left")
    for row in rows:
        table.add_row(
            row["platform"], str(row["tier"]), str(row["pending"]),
            str(row["allowance"]), str(row["releasing"]), str(row["backlog"]),
        )
    console.print(table)


@app.command("release")
def release_cmd(
    dry_run: bool = typer.Option(True, "--dry-run/--live", help="--live actually uploads"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Release one beat's allowance to each platform."""
    _setup_logging(verbose)
    from src.microstock import drip

    console.print_json(data=drip.release(dry_run=dry_run))


@app.command("confirm")
def confirm_cmd(
    platform: str = typer.Option(..., "--platform"),
    batch: str | None = typer.Option(None, "--batch", help="Outbox batch id; omit to pass --asset-id"),
    asset_id: list[str] = typer.Option([], "--asset-id"),
) -> None:
    """Record that you actually submitted a staged manual batch."""
    from src.microstock import drip, paths

    ids = list(asset_id)
    if batch:
        folder = paths.outbox_for(platform, batch)
        if not folder.is_dir():
            console.print(f"[red]no such batch:[/] {folder}")
            raise typer.Exit(1)
        ids += [p.stem for p in folder.glob("*.svg")]
    if not ids:
        console.print("[red]need --batch or --asset-id[/]")
        raise typer.Exit(2)
    console.print_json(data=drip.confirm_manual_upload(platform, sorted(set(ids))))


@app.command("crontab")
def crontab_cmd() -> None:
    """Print the cron line for unattended operation."""
    line = (
        f"*/30 * * * * cd {paths.ROOT} && .venv/bin/python -m src.cli.microstock beat "
        f">> {paths.OPS_DIR.relative_to(paths.ROOT)}/beat.log 2>&1"
    )
    console.print("[bold]Add this to crontab -e:[/]\n")
    console.print(f"  {line}\n")
    console.print(
        "[dim]Deliberately separate from the video farm beats so a microstock\n"
        "failure can never stall video production. Do not install until\n"
        "settings.json -> enabled is true and doctor passes.[/]"
    )


if __name__ == "__main__":
    app()
