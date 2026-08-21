"""CLI: Weird Human Biology end-to-end video (whole VO + stickman timeline).

Usage:
  python -m src.cli.generate_weird_biology_video \\
    --script-dir output/scripts/weird_human_biology/<run>
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.weird_biology_compositor import (  # noqa: E402
    VISUAL_BACKEND,
    render_composited_clip,
    render_composited_still_file,
    visual_backend_from_config,
)
from src.services.weird_biology_compose import (  # noqa: E402
    WeirdBiologyComposeError,
    compose_weird_biology,
)
from src.services.weird_biology_scenes import (  # noqa: E402
    build_visual_timeline,
    save_visual_timeline,
)
from src.services.weird_biology_shot_plan import (  # noqa: E402
    WeirdBiologyShotPlanner,
    shots_to_specs,
)
from src.services.weird_biology_prop_synthesizer import PropSynthesizer  # noqa: E402
from src.services.weird_biology_tts import synthesize_full_vo  # noqa: E402

app = typer.Typer(
    add_completion=False,
    help="Weird Biology — full VO + SVG stickman v5 → final.mp4",
)
console = Console()


def _load_script_dir(script_dir: Path) -> tuple[str, str, dict]:
    vo_path = script_dir / "vo_raw.txt"
    sections_path = script_dir / "script_sections.md"
    if not vo_path.exists():
        raise FileNotFoundError(f"missing {vo_path}")
    vo = vo_path.read_text(encoding="utf-8")
    sections = (
        sections_path.read_text(encoding="utf-8") if sections_path.exists() else ""
    )
    meta: dict = {}
    meta_path = script_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # Prefer validated scripts
    val = script_dir / "validation.json"
    val_after = script_dir / "validation_after_fix.json"
    for vp in (val_after, val):
        if vp.exists():
            try:
                v = json.loads(vp.read_text(encoding="utf-8"))
                if v.get("ok") is False:
                    console.print(
                        f"[yellow]warning[/yellow] {vp.name} not OK — continuing anyway"
                    )
            except Exception:  # noqa: BLE001
                pass
    return vo, sections, meta


def _script_validation_ok(script_dir: Path) -> bool:
    """Use the newest available validator result as the prop-synthesis gate."""
    for path in (
        script_dir / "validation_after_fix.json",
        script_dir / "validation.json",
    ):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8")).get("ok") is True
            except (OSError, json.JSONDecodeError):
                return False
    return False


@app.command()
def main(
    script_dir: Path = typer.Option(
        ...,
        "--script-dir",
        "-s",
        exists=True,
        file_okay=False,
        help="Script factory run dir (vo_raw.txt + script_sections.md)",
    ),
    out_dir: Path | None = typer.Option(
        None,
        "--out-dir",
        "-o",
        help="Job dir (default: <script-dir>/video_job)",
    ),
    voice: str | None = typer.Option(None, "--voice", help="Kokoro voice id"),
    speed: float | None = typer.Option(None, "--speed", help="Kokoro speed"),
    no_resume: bool = typer.Option(False, "--no-resume", help="Regenerate all stages"),
    skip_tts: bool = typer.Option(
        False, "--skip-tts", help="Use existing audio/full_vo.wav"
    ),
    skip_plan: bool = typer.Option(
        False, "--skip-plan", help="Use existing shot_plan.json"
    ),
    compositor: bool = typer.Option(
        False,
        "--compositor",
        help="Use asset-compositing backend (PNG layers) instead of SVG stickman",
    ),
    compositor_sample: bool = typer.Option(
        False,
        "--compositor-sample",
        help="Render compositor still + short motion demo only (no full video)",
    ),
) -> None:
    """Whole-script Kokoro → shot plan (openrouter/free ×2, then flash-lite) → mux."""
    resume = not no_resume
    job_dir = out_dir or (script_dir / "video_job")
    job_dir.mkdir(parents=True, exist_ok=True)
    visual_backend = VISUAL_BACKEND if compositor else visual_backend_from_config()
    audio_dir = job_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Weird Biology video[/bold] script: {script_dir}")
    console.print(f"  job: {job_dir}")
    console.print(f"  visual_backend: {visual_backend}")

    if compositor_sample:
        from src.services.weird_biology_stickman import ShotSpec

        preview_dir = job_dir / "preview_stills"
        preview_dir.mkdir(parents=True, exist_ok=True)
        shot = ShotSpec(layout="crib_door_scene", bubble_bars=3, seed=42)
        still = preview_dir / "compositor_sample.jpg"
        clip = preview_dir / "compositor_sample.mp4"
        render_composited_still_file(shot, still)
        render_composited_clip(shot, clip, duration_s=2.5)
        console.print(f"[green]OK[/green] compositor sample still: {still}")
        console.print(f"[green]OK[/green] compositor sample clip: {clip}")
        return

    vo_raw, sections_md, script_meta = _load_script_dir(script_dir)
    topic = str(script_meta.get("topic") or script_dir.name)

    # Expand the persistent prop library from this validated script before planning.
    if _script_validation_ok(script_dir):
        console.print("  props: scanning validated script and expanding library...")
        synthesized_props = PropSynthesizer().auto_expand_props_for_script(
            sections_md or vo_raw,
            topic=topic,
        )
        if synthesized_props:
            console.print(f"  props: ready ({', '.join(synthesized_props)})")
        else:
            console.print("  props: existing library covers this script")
    else:
        console.print("  props: skipped (script validation is missing or not OK)")

    # 1) Full VO
    full_vo = audio_dir / "full_vo.wav"
    try:
        if skip_tts and full_vo.exists():
            from src.services.tts_kokoro import _wav_meta

            dur, sr = _wav_meta(full_vo)
            console.print(f"  VO: skip-tts ({dur:.1f}s)")
            vo_duration = dur
        else:
            # Prefer kokoro venv if available when invoked under .venv
            console.print("  VO: synthesizing full_vo.wav (Kokoro whole script)…")
            result = synthesize_full_vo(
                vo_raw,
                out_path=full_vo,
                voice=voice,
                speed=speed,
                resume=resume,
            )
            vo_duration = result.duration_s
            console.print(f"  VO: {vo_duration:.1f}s → {full_vo}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]TTS error:[/red] {exc}")
        console.print(
            "[dim]Hint: run with .venv-kokoro/bin/python -m src.cli.generate_weird_biology_video …[/dim]"
        )
        raise typer.Exit(1) from exc

    # 2) Visual timeline
    tl_path = job_dir / "visual_timeline.json"
    if resume and tl_path.exists() and skip_plan:
        tl_data = json.loads(tl_path.read_text(encoding="utf-8"))
        from src.services.weird_biology_scenes import VisualBeat, VisualTimeline

        beats = [VisualBeat(**b) for b in tl_data.get("beats") or []]
        tl = VisualTimeline(
            beats=beats,
            full_vo_duration_s=float(tl_data.get("full_vo_duration_s") or vo_duration),
            topic=str(tl_data.get("topic") or topic),
        )
    else:
        tl = build_visual_timeline(
            vo_raw,
            script_sections_md=sections_md,
            full_vo_duration_s=vo_duration,
            topic=topic,
        )
        save_visual_timeline(tl, tl_path)
    console.print(f"  timeline: {len(tl.beats)} beats")

    # 3) Shot plan
    plan_path = job_dir / "shot_plan.json"
    if skip_plan and plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        console.print(f"  shots: skip-plan ({len(plan.get('shots') or [])})")
    else:
        console.print("  shots: planning (openrouter/free ×2, then flash-lite)…")
        plan = WeirdBiologyShotPlanner().plan(tl)
        plan_path.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        console.print(
            f"  shots: {len(plan.get('shots') or [])} ({plan.get('source')}) "
            f"model={plan.get('model')}"
        )

    specs = shots_to_specs(plan)
    if len(specs) != len(tl.beats):
        # Align by beat_index
        by_i = {
            int(s.get("beat_index", i)): spec
            for i, (s, spec) in enumerate(zip(plan.get("shots") or [], specs))
        }
        from src.services.weird_biology_stickman import ShotSpec

        specs = [by_i.get(b.index) or ShotSpec(seed=b.index) for b in tl.beats]

    # 4) Render + mux
    try:
        console.print("  compose: render beats + mux full VO…")
        composed = compose_weird_biology(
            tl=tl,
            shots=specs,
            full_vo_wav=full_vo,
            job_dir=job_dir,
            resume=resume,
            visual_backend=visual_backend,
        )
    except WeirdBiologyComposeError as exc:
        console.print(f"[red]Compose error:[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Compose error:[/red] {exc}")
        raise typer.Exit(2) from exc

    meta = {
        "channel": "weird_human_biology",
        "topic": topic,
        "script_dir": str(script_dir),
        "job_dir": str(job_dir),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "full_vo_duration_s": vo_duration,
        "beat_count": len(tl.beats),
        "planner_model": plan.get("model"),
        "shot_plan_source": plan.get("source"),
        "final": str(composed.final_path),
        "compose": composed.meta,
        "tts_mode": "whole_vo",
        "visual_backend": visual_backend,
    }
    (job_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    console.print(f"[green]OK[/green] {composed.final_path}")


if __name__ == "__main__":
    app()
