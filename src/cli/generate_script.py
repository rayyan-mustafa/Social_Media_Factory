"""CLI: generate a Plan C gallery script (Module 1 only).

Usage:
  python -m src.cli.generate_script "The forgotten libraries of Baghdad"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

# Allow `python -m src.cli.generate_script` from repo root
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.script import ScriptModule, ScriptValidationError  # noqa: E402

app = typer.Typer(
    add_completion=False,
    help="ScriptModule — outline + chapter expansion → sentence scenes",
)
console = Console()


@app.command()
def main(
    topic: str = typer.Argument(..., help="Video topic / working title idea"),
    niche_notes: str = typer.Option(
        "",
        "--niche-notes",
        "-n",
        help="Optional notes for the outline prompt (tone, era, angle)",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Optional output JSON path (default: output/scripts/<timestamp>_*.json)",
    ),
    dry_prompts: bool = typer.Option(
        False,
        "--dry-prompts",
        help="Print rendered prompt placeholders check and exit (no LLM call)",
    ),
    channel: str | None = typer.Option(
        None,
        "--channel",
        "-c",
        help="Sheet channel (napstorian|napping_historian) — selects prompts_dir / profile overlays",
    ),
) -> None:
    """Generate outline → expand chapters → write scenes JSON."""
    ch = (channel or "").strip() or None
    if ch:
        from src.agents.sheet_channels import channel_default_profile
        from src.services.retention_profile import load_retention_profiles
        from src.services.settings import get_settings as _gs

        os.environ["SCRIPT_CHANNEL"] = ch
        os.environ["RETENTION_PROFILE"] = channel_default_profile(ch)
        load_retention_profiles.cache_clear()
        _gs.cache_clear()

    if dry_prompts:
        from src.services.pacing import load_dual_pacing
        from src.services.retention_profile import resolve_prompts_dir
        from src.services.settings import (
            get_settings,
            load_prompt,
            load_script_settings,
            load_style_hint,
            render_prompt,
        )

        s = get_settings()
        cfg = load_script_settings()
        if ch:
            from src.agents.sheet_channels import channel_script_overrides

            cfg = {**cfg, **channel_script_overrides(ch)}
        prompts_dir = resolve_prompts_dir(cfg, channel=ch)
        p = load_dual_pacing(cfg, settings_target_scenes=s.target_scenes)
        outline_t = load_prompt("outline.txt", prompts_dir=prompts_dir)
        expand_t = load_prompt("chapter_expand.txt", prompts_dir=prompts_dir)
        refine_path = prompts_dir / "outline_refine.txt"
        console.print("[bold]Editable prompts loaded[/bold]")
        console.print(f"  channel: {ch or '(none)'}")
        console.print(f"  profile: {cfg.get('_retention_profile')}")
        console.print(f"  prompts_dir: {prompts_dir}")
        console.print(f"  outline.txt chars: {len(outline_t)}")
        console.print(
            f"  outline_refine.txt: "
            + (f"yes ({refine_path.stat().st_size} bytes)" if refine_path.exists() else "no")
        )
        console.print(f"  chapter_expand.txt chars: {len(expand_t)}")
        console.print(f"  style hint: {load_style_hint()[:80]}...")
        console.print(
            f"  dual pacing @ {p.voice_wpm:.0f} WPM: A {p.phase_a_target_scenes}×{p.phase_a_seconds:.0f}s "
            f"+ B {p.phase_b_target_scenes}×{p.phase_b_seconds:.0f}s "
            f"(~{p.phase_b_words} words/B) "
            f"= {p.target_scenes} scenes / ~{(p.phase_a_target_scenes*p.phase_a_seconds + p.phase_b_target_scenes*p.phase_b_seconds)/60:.0f} min"
        )
        sample = render_prompt(
            outline_t,
            {
                "TOPIC": topic,
                "NICHE_NOTES": niche_notes or "(none)",
                "TARGET_DURATION_MIN": cfg.get(
                    "target_duration_min", s.target_duration_min
                ),
                "TARGET_SECONDS_PER_SCENE": p.phase_a_seconds,
                "TARGET_SCENES": p.target_scenes,
                "MIN_SCENES": p.min_scenes,
                "MAX_SCENES": p.max_scenes,
                "CHAPTERS_TARGET": cfg.get("chapters_target", 10),
                "PHASE_A_MAX_WORDS": p.phase_a_max_words,
                "PHASE_A_SCENES": p.phase_a_target_scenes,
                "PHASE_A_SECONDS": p.phase_a_seconds,
                "PHASE_A_WORDS_MIN": p.phase_a_words_min,
                "PHASE_A_WORDS_MAX": p.phase_a_words_max,
                "PHASE_B_SCENES": p.phase_b_target_scenes,
                "PHASE_B_SECONDS": p.phase_b_seconds,
                "PHASE_B_WORDS": p.phase_b_words,
                "VOICE_WPM": round(p.voice_wpm, 1),
            },
        )
        console.print("\n[dim]--- outline prompt preview (first 600 chars) ---[/dim]")
        console.print(sample[:600] + ("…" if len(sample) > 600 else ""))
        raise typer.Exit(0)

    console.print(f"[bold]ScriptModule[/bold] topic: {topic}")
    if ch:
        console.print(f"  channel: {ch}")
    try:
        result = ScriptModule(channel=ch).generate(
            topic, niche_notes=niche_notes, out_path=out
        )
    except ScriptValidationError as exc:
        result = exc.result
        _print_summary(result)
        console.print(
            f"[yellow]Saved with validation errors[/yellow]: {result.meta.get('saved_to')}"
        )
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    _print_summary(result)
    console.print(f"[green]OK[/green] wrote {result.meta.get('saved_to')}")
    console.print(f"       narration: {Path(str(result.meta.get('saved_to'))).with_suffix('.txt')}")


def _print_summary(result) -> None:
    v = result.validation
    table = Table(title="Script validation")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("title", result.title)
    table.add_row("scenes", str(v.scene_count))
    table.add_row("band", f"{v.min_scenes}–{v.max_scenes} (target {v.target_scenes})")
    table.add_row("est duration", f"{v.estimated_duration_s/60:.1f} min")
    table.add_row("median words", f"{v.median_word_count:.1f}" if v.median_word_count else "—")
    table.add_row("ok", "yes" if v.ok else "no")
    console.print(table)
    for w in v.warnings:
        console.print(f"[yellow]warn[/yellow] {w}")
    for e in v.errors:
        console.print(f"[red]error[/red] {e}")
    console.print("\n[dim]First 3 scenes:[/dim]")
    for s in result.scenes[:3]:
        console.print(f"  [{s.index:03d}] {s.text}")
        console.print(f"         visual: {s.visual_prompt[:100]}…")


if __name__ == "__main__":
    app()
