"""CLI: Weird Human Biology script factory (research → beat-sheet → validate).

Usage:
  python -m src.cli.generate_weird_biology_script "Why do we get goosebumps"
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.weird_biology_research import (  # noqa: E402
    SourcePackInsufficient,
    build_source_pack,
    save_source_pack,
)
from src.services.weird_biology_script import (  # noqa: E402
    SCRIPT_MODEL_DEFAULT,
    WeirdBiologyScriptError,
    WeirdBiologyScriptWriter,
)
from src.services.weird_biology_claim_fix import (  # noqa: E402
    WeirdBiologyClaimFixError,
    fix_unverified_claims,
)
from src.services.weird_biology_validate import (  # noqa: E402
    WeirdBiologyValidationError,
    WeirdBiologyValidator,
    save_validation,
)

app = typer.Typer(
    add_completion=False,
    help="Weird Human Biology — sources → beat-sheet script → claim validation",
)
console = Console()

CHANNEL = "weird_human_biology"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(topic: str, *, max_len: int = 48) -> str:
    s = _SLUG_RE.sub("_", topic.lower()).strip("_")
    return (s[:max_len] or "topic").rstrip("_")


def _run_dir(topic: str, out_root: Path | None) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = out_root or (ROOT / "output" / "scripts" / CHANNEL)
    path = base / f"{ts}_{_slug(topic)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


@app.command()
def main(
    topic: str = typer.Argument(..., help="Video topic / working title idea"),
    out_dir: Path | None = typer.Option(
        None,
        "--out-dir",
        "-o",
        help="Optional run directory (default: output/scripts/weird_human_biology/<ts>_*)",
    ),
    min_sources: int = typer.Option(
        3,
        "--min-sources",
        help="Minimum usable source excerpts required before writing",
    ),
    extra_url: list[str] = typer.Option(
        [],
        "--url",
        help="Extra allowlisted source URL to fetch (repeatable)",
    ),
    skip_validate: bool = typer.Option(
        False,
        "--skip-validate",
        help="Write script only (no claim validator) — not for production",
    ),
    research_only: bool = typer.Option(
        False,
        "--research-only",
        help="Fetch and save source_pack.json then exit",
    ),
    script_model: str = typer.Option(
        SCRIPT_MODEL_DEFAULT,
        "--script-model",
        help=(
            "Script-writer model id (default: google/gemini-2.5-flash via "
            "WaveSpeed). Prefix openrouter/ to use OpenRouterClient "
            "(e.g. openrouter/free)."
        ),
    ),
) -> None:
    """Research → beat-sheet → claim validation → optional one-shot claim-fix."""
    console.print(f"[bold]Weird Human Biology[/bold] topic: {topic}")
    run_path = out_dir if out_dir is not None else _run_dir(topic, None)
    if out_dir is not None:
        run_path.mkdir(parents=True, exist_ok=True)
    console.print(f"  out: {run_path}")
    console.print(f"  script-model: {script_model}")

    try:
        pack = build_source_pack(
            topic,
            min_usable=min_sources,
            extra_urls=list(extra_url) or None,
        )
    except SourcePackInsufficient as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        if exc.pack is not None:
            save_source_pack(exc.pack, run_path / "source_pack.json")
        (run_path / "meta.json").write_text(
            json.dumps(
                {
                    "channel": CHANNEL,
                    "topic": topic,
                    "error": str(exc),
                    "run_dir": str(run_path),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        raise typer.Exit(3) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Research error:[/red] {exc}")
        raise typer.Exit(1) from exc

    save_source_pack(pack, run_path / "source_pack.json")
    console.print(
        f"  sources: {len(pack.usable_items())} usable / {len(pack.items)} total"
    )

    if research_only:
        console.print("[green]OK[/green] research-only complete")
        raise typer.Exit(0)

    try:
        script_result = WeirdBiologyScriptWriter(model=script_model).generate(
            topic, pack
        )
    except WeirdBiologyScriptError as exc:
        console.print(f"[red]Script parse error:[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Script error:[/red] {exc}")
        raise typer.Exit(1) from exc

    (run_path / "script_sections.md").write_text(
        script_result.script_sections_md, encoding="utf-8"
    )
    (run_path / "vo_raw.txt").write_text(
        script_result.vo_raw.strip() + "\n", encoding="utf-8"
    )

    band_table = Table(title="Section word bands")
    band_table.add_column("Section")
    band_table.add_column("Words", justify="right")
    band_table.add_column("Band")
    band_table.add_column("OK")
    for c in script_result.band_checks:
        band_table.add_row(
            c.name,
            str(c.words),
            f"{c.min_words}-{c.max_words}",
            "[green]yes[/green]" if c.ok else "[yellow]no[/yellow]",
        )
    console.print(band_table)

    validation_payload: dict | None = None
    validation_before_fix: dict | None = None
    claim_fix_attempted = False
    claim_fix_model: str | None = None
    claim_fix_payload: dict | None = None
    exit_code = 0
    script_md = script_result.script_sections_md
    vo_raw = script_result.vo_raw
    if skip_validate:
        console.print("[yellow]skip-validate[/yellow] — no claim check")
    else:
        try:
            validator = WeirdBiologyValidator()
            val = validator.validate(
                script_md,
                pack,
                also_check_vo=vo_raw,
            )
            save_validation(
                val,
                md_path=run_path / "validation.md",
                json_path=run_path / "validation.json",
            )
            validation_payload = val.to_dict()
            if val.ok:
                console.print(f"[green]{val.status_line}[/green] via {val.model_used}")
            else:
                console.print(f"[red]{val.status_line}[/red] via {val.model_used}")
                # One repair round: surgical claim-fix → re-validate once
                console.print("[yellow]claim-fix[/yellow] attempting surgical edit…")
                claim_fix_attempted = True
                validation_before_fix = val.to_dict()
                try:
                    fix = fix_unverified_claims(
                        script_md,
                        pack,
                        val.report_md,
                        also_vo=vo_raw,
                    )
                    claim_fix_model = fix.model_used
                    claim_fix_payload = fix.to_dict()
                    script_md = fix.script_sections_md
                    vo_raw = fix.vo_raw.strip() + "\n"
                    (run_path / "script_sections.md").write_text(
                        script_md, encoding="utf-8"
                    )
                    (run_path / "vo_raw.txt").write_text(vo_raw, encoding="utf-8")
                    console.print(
                        f"  claim-fix via {fix.model_used} "
                        f"({len(fix.claims_targeted)} claim(s) targeted)"
                    )
                    val2 = validator.validate(
                        script_md,
                        pack,
                        also_check_vo=vo_raw,
                    )
                    save_validation(
                        val2,
                        md_path=run_path / "validation_after_fix.md",
                        json_path=run_path / "validation_after_fix.json",
                    )
                    validation_payload = val2.to_dict()
                    if val2.ok:
                        console.print(
                            f"[green]{val2.status_line}[/green] "
                            f"after fix via {val2.model_used}"
                        )
                        exit_code = 0
                    else:
                        console.print(
                            f"[red]{val2.status_line}[/red] "
                            f"after fix via {val2.model_used} — fail closed"
                        )
                        exit_code = 4
                except WeirdBiologyClaimFixError as fix_exc:
                    console.print(f"[red]Claim-fix error:[/red] {fix_exc}")
                    exit_code = 4
                except WeirdBiologyValidationError as reval_exc:
                    console.print(
                        f"[red]Re-validation error after fix:[/red] {reval_exc}"
                    )
                    (run_path / "validation_after_fix.md").write_text(
                        str(reval_exc) + "\n", encoding="utf-8"
                    )
                    exit_code = 5
        except WeirdBiologyValidationError as exc:
            console.print(f"[red]Validation error:[/red] {exc}")
            (run_path / "validation.md").write_text(str(exc) + "\n", encoding="utf-8")
            exit_code = 5
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Validation error:[/red] {exc}")
            exit_code = 5

    meta = {
        "channel": CHANNEL,
        "topic": topic,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_dir": str(run_path),
        "script_model": script_result.model,
        "rewrite_attempted": script_result.rewrite_attempted,
        "claim_fix_attempted": claim_fix_attempted,
        "claim_fix_model": claim_fix_model,
        "claim_fix": claim_fix_payload,
        "bands_ok": script_result.bands_ok,
        "band_checks": [
            {
                "name": c.name,
                "words": c.words,
                "min": c.min_words,
                "max": c.max_words,
                "ok": c.ok,
                "detail": c.detail,
            }
            for c in script_result.band_checks
        ],
        "source_usable_count": len(pack.usable_items()),
        "validation": validation_payload,
        "validation_before_fix": validation_before_fix,
        "script_meta": script_result.meta,
    }
    (run_path / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if exit_code == 0:
        console.print(f"[green]OK[/green] wrote {run_path}")
    else:
        console.print(f"[yellow]DONE WITH ERRORS[/yellow] artifacts in {run_path}")
    raise typer.Exit(exit_code)


if __name__ == "__main__":
    app()
