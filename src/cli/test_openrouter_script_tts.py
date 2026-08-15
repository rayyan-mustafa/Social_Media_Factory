"""One-off OpenRouter script generation + Kokoro TTS quality test.

Default TTS mode is hybrid: chapter (or ~60–90s) synth with sentence-only
inner packs under the 510-phoneme cap (trim=False), then scene slices.

Usage:
  .venv-kokoro/bin/python -m src.cli.test_openrouter_script_tts
  .venv-kokoro/bin/python -m src.cli.test_openrouter_script_tts \\
      --reuse-script output/.../script.json --skip-bed
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
import typer
from rich.console import Console

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.domain.models import (  # noqa: E402
    Outline,
    OutlineChapter,
    Scene,
    ScriptResult,
    ScriptValidation,
)
from src.services.audio_bed import AudioBedModule  # noqa: E402
from src.services.openrouter import OpenRouterClient, OpenRouterError  # noqa: E402
from src.services.settings import get_settings  # noqa: E402
from src.services.tts_kokoro import VoiceModule, VoiceValidationError  # noqa: E402
from src.services.tts_sanitize import sanitize_scene_text  # noqa: E402

app = typer.Typer(add_completion=False)
console = Console()

DEFAULT_TOPIC = "What If the Spanish Armada Had Won in 1588?"
TARGET_WORDS = 400
WORD_TOLERANCE = 50
MODEL = "openrouter/free"

_SYSTEM = """You are a historical documentary narrator writing spoken narration only.
Write in a compelling Tudor-era alternate-history "what if" style.
Output plain narration text only — no titles, no markdown, no scene labels, no visual directions.
Each sentence should be a clear beat suitable for text-to-speech (one sentence per line).
Write years as digits (e.g. 1588) and royal names with Roman numerals (e.g. Philip II, Elizabeth I).
Do NOT include camera directions, shot descriptions, or bracketed stage directions."""

_USER_TEMPLATE = """Write a ~{target} word alternate-history narration on this topic:

{topic}

Requirements:
- Approximately {target} words (acceptable range {lo}–{hi})
- Engaging hook, clear narrative arc, strong closer
- Short-to-medium sentences; one idea per sentence
- No visual or director language in the spoken text
- Historical tone with vivid but speakable detail
- Return ONLY the narration paragraphs (no JSON, no headings)"""


def _split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return []
    parts = re.split(r"(?<=[.!?…])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+(?:['']\w+)?\b", text))


def _build_script_result(
    *,
    topic: str,
    title: str,
    sentences: list[str],
    model: str,
) -> ScriptResult:
    scenes: list[Scene] = []
    for i, raw in enumerate(sentences):
        cleaned, _warnings = sanitize_scene_text(raw)
        if not cleaned:
            continue
        scenes.append(
            Scene(
                index=i,
                text=cleaned,
                visual_prompt=f"placeholder still for scene {i}",
                chapter_id=1,
                word_count=_word_count(cleaned),
                pacing_phase="b",
            )
        )

    total_words = sum(s.word_count for s in scenes)
    outline = Outline(
        title=title,
        hook=sentences[0] if sentences else "",
        estimated_sentence_budget=len(scenes),
        chapters=[
            OutlineChapter(
                id=1,
                title="Main narrative",
                goal="OpenRouter TTS test narration",
                target_sentences=len(scenes),
                pacing_phase="b",
            )
        ],
        closer=scenes[-1].text if scenes else "",
    )

    validation = ScriptValidation(
        ok=True,
        scene_count=len(scenes),
        min_scenes=1,
        max_scenes=max(len(scenes), 200),
        target_scenes=len(scenes),
        estimated_duration_s=len(scenes) * 8.0,
        median_word_count=(
            float(total_words) / len(scenes) if scenes else None
        ),
        warnings=[],
        errors=[],
    )

    return ScriptResult(
        topic=topic,
        title=title,
        hook=outline.hook,
        outline=outline,
        scenes=scenes,
        validation=validation,
        meta={
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "test": "openrouter_script_tts",
            "raw_word_count": total_words,
        },
    )


def _concat_wavs(wav_paths: list[Path], out_path: Path) -> float:
    chunks: list[np.ndarray] = []
    sample_rate: int | None = None
    for p in wav_paths:
        data, sr = sf.read(str(p), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sample_rate is None:
            sample_rate = int(sr)
        elif int(sr) != sample_rate:
            raise RuntimeError(f"sample rate mismatch: {p} ({sr} vs {sample_rate})")
        chunks.append(data)
    if not chunks or sample_rate is None:
        return 0.0
    combined = np.concatenate(chunks)
    sf.write(str(out_path), combined, sample_rate)
    return float(combined.shape[0]) / float(sample_rate)


def _write_readme(
    path: Path,
    *,
    topic: str,
    model: str,
    word_count: int,
    scene_count: int,
    voice: str,
    speed: float,
    lang: str,
    total_audio_s: float,
    out_dir: Path,
    first_sentences: list[str],
    tts_mode: str = "hybrid",
    chapter_count: int = 0,
    inner_splits: bool = False,
) -> None:
    lines = [
        "# OpenRouter Script + TTS Test",
        "",
        f"- **Topic:** {topic}",
        f"- **Model:** `{model}`",
        f"- **Word count:** {word_count}",
        f"- **Scenes:** {scene_count}",
        f"- **TTS mode:** `{tts_mode}`",
        f"- **Chapter chunks:** {chapter_count}",
        f"- **Inner phoneme splits:** {'yes' if inner_splits else 'no'}",
        f"- **Voice:** `{voice}` @ {speed}x ({lang})",
        f"- **Total audio:** {total_audio_s:.1f}s ({total_audio_s/60:.2f} min)",
        f"- **Generated:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Files",
        "",
        "- `narration.txt` — full spoken narration",
        "- `script.json` — scene-level script (one sentence per scene)",
        "- `audio/chapters/chXX_YY.wav` — hybrid chapter (or ~60–90s) WAVs",
        "- `audio/chapters/chapters_manifest.json` — chapter + inner-batch meta",
        "- `audio/scene_XXX.wav` — scene slices mapped from chapter audio",
        "- `audio/mixed/scene_XXX.wav` — voice + ambient bed mix (if bed enabled)",
        "- `audio/narration_full.wav` — concatenated mix (bed + voice) or raw",
        "- `audio/narration_raw_full.wav` — concat of chapter WAVs (preferred listen)",
        "- `audio/voice_manifest.json` — VoiceModule manifest",
        "",
        "## Listen",
        "",
        "```bash",
        f"ffplay -nodisp -autoexit {out_dir / 'audio' / 'narration_raw_full.wav'}",
        f"ffplay -nodisp -autoexit {out_dir / 'audio' / 'narration_full.wav'}",
        "```",
        "",
        "## First sentences",
        "",
    ]
    for i, s in enumerate(first_sentences[:3], 1):
        lines.append(f"{i}. {s}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


@app.command()
def main(
    topic: str = typer.Argument(DEFAULT_TOPIC, help="Alternate-history topic"),
    out_dir: Path | None = typer.Option(
        None,
        "--out-dir",
        "-o",
        help="Output folder (default: output/script_tts_test_<timestamp>/)",
    ),
    target_words: int = typer.Option(
        TARGET_WORDS, "--words", "-w", help="Target narration word count"
    ),
    skip_tts: bool = typer.Option(
        False, "--skip-tts", help="Generate script only, skip Kokoro TTS"
    ),
    skip_bed: bool = typer.Option(
        False, "--skip-bed", help="Skip audio bed mix (prosody-only test)"
    ),
    reuse_script: Path | None = typer.Option(
        None,
        "--reuse-script",
        help="Reuse existing script.json (skip OpenRouter); TTS only",
    ),
    tts_mode: str = typer.Option(
        "hybrid",
        "--tts-mode",
        help="Kokoro strategy: hybrid (chapter + sentence packs) or scene",
    ),
    target_chunk_words: int | None = typer.Option(
        None,
        "--chunk-words",
        help="Optional outer ~60–90s word budget (default: one chunk per chapter)",
    ),
) -> None:
    """Generate ~400-word narration via OpenRouter free model, then run full human stack."""
    settings = get_settings()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = (tts_mode or "hybrid").strip().lower()
    if mode not in ("hybrid", "scene"):
        console.print(f"[red]Invalid --tts-mode {tts_mode!r}; use hybrid|scene[/red]")
        raise typer.Exit(2)

    if out_dir is None:
        safe = re.sub(r"[^\w\-]+", "_", topic)[:40].strip("_")
        out_dir = ROOT / "output" / f"script_tts_test_{stamp}_{safe}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(exist_ok=True)

    console.print(f"[bold]OpenRouter script test[/bold] topic={topic!r}")
    console.print(f"tts_mode={mode} target_words={target_words}")

    script_path = out_dir / "script.json"
    model_label = MODEL

    if reuse_script is not None:
        src = Path(reuse_script)
        if not src.exists():
            console.print(f"[red]reuse-script not found:[/red] {src}")
            raise typer.Exit(1)
        raw = json.loads(src.read_text(encoding="utf-8"))
        script = ScriptResult.model_validate(raw)
        topic = script.topic or topic
        model_label = str(script.meta.get("model") or MODEL)
        script_path.write_text(
            json.dumps(script.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (out_dir / "narration.txt").write_text(
            script.narration_text() + "\n", encoding="utf-8"
        )
        # Preserve original raw narration when present beside the source script.
        src_raw = src.parent / "narration_raw.txt"
        if src_raw.exists():
            (out_dir / "narration_raw.txt").write_text(
                src_raw.read_text(encoding="utf-8"), encoding="utf-8"
            )
        word_count = _word_count(script.narration_text())
        console.print(
            f"[green]Reused script[/green] {word_count} words, "
            f"{len(script.scenes)} scenes ← {src}"
        )
    else:
        console.print(f"model={MODEL} (OpenRouter)")
        client = OpenRouterClient(settings, default_model=MODEL)
        user_prompt = _USER_TEMPLATE.format(
            topic=topic,
            target=target_words,
            lo=target_words - WORD_TOLERANCE,
            hi=target_words + WORD_TOLERANCE,
        )

        try:
            raw_narration = client.chat_text(
                system=_SYSTEM,
                user=user_prompt,
                temperature=0.8,
                timeout_s=180.0,
                model=MODEL,
                reasoning_enabled=True,
            )
        except OpenRouterError as exc:
            console.print(f"[red]OpenRouter failed:[/red] {exc}")
            raise typer.Exit(1) from exc

        (out_dir / "narration_raw.txt").write_text(
            raw_narration.strip() + "\n", encoding="utf-8"
        )

        sentences = _split_sentences(raw_narration)
        narration_clean = "\n".join(
            s.text
            for s in _build_script_result(
                topic=topic, title=topic, sentences=sentences, model=MODEL
            ).scenes
        )
        word_count = _word_count(narration_clean)

        if (
            word_count < target_words - WORD_TOLERANCE
            or word_count > target_words + WORD_TOLERANCE
        ):
            console.print(
                f"[yellow]Word count {word_count} outside {target_words - WORD_TOLERANCE}–"
                f"{target_words + WORD_TOLERANCE}; using generated text anyway[/yellow]"
            )

        title = topic if topic.endswith("?") else f"{topic}?"
        script = _build_script_result(
            topic=topic, title=title, sentences=sentences, model=MODEL
        )
        script_path.write_text(
            json.dumps(script.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (out_dir / "narration.txt").write_text(
            script.narration_text() + "\n", encoding="utf-8"
        )
        console.print(
            f"[green]Script[/green] {word_count} words, {len(script.scenes)} scenes"
        )

    console.print(f"       → {script_path}")

    total_audio_s = 0.0
    voice = settings.kokoro_voice
    speed = float(settings.kokoro_speed)
    lang = settings.kokoro_lang
    chapter_count = 0
    inner_splits = False

    compose_manifest: Path | None = None

    if not skip_tts:
        label = "hybrid chapter+sentence" if mode == "hybrid" else "per-scene"
        console.print(f"[bold]Kokoro TTS ({label})[/bold] …")
        try:
            voice_result = VoiceModule().synthesize_script(
                script_path,
                out_dir=audio_dir,
                resume=False,
                mode=mode,  # type: ignore[arg-type]
                target_chunk_words=target_chunk_words,
            )
        except VoiceValidationError as exc:
            voice_result = exc.result
            console.print(f"[yellow]Voice validation issues:[/yellow] {exc}")
        total_audio_s = voice_result.validation.total_duration_s
        voice = voice_result.voice
        speed = voice_result.speed
        lang = voice_result.lang
        chapter_count = int(voice_result.meta.get("chapter_count") or 0)
        inner_splits = bool(voice_result.meta.get("inner_splits_fired"))
        if mode == "hybrid":
            console.print(
                f"       chapters={chapter_count} "
                f"inner_splits={'yes' if inner_splits else 'no'} "
                f"(extra_batches={voice_result.meta.get('inner_extra_batches', 0)})"
            )

        compose_manifest = audio_dir / "voice_manifest.json"

        # Prefer continuous chapter concat for raw listen; fall back to scenes.
        chapter_wavs = sorted((audio_dir / "chapters").glob("ch*.wav"))
        if chapter_wavs:
            raw_full = audio_dir / "narration_raw_full.wav"
            raw_s = _concat_wavs(chapter_wavs, raw_full)
            console.print(f"[green]Raw chapter concat[/green] {raw_full} ({raw_s:.1f}s)")
        else:
            raw_wavs = sorted(audio_dir.glob("scene_*.wav"))
            if raw_wavs:
                raw_full = audio_dir / "narration_raw_full.wav"
                raw_s = _concat_wavs(raw_wavs, raw_full)
                console.print(f"[green]Raw concat[/green] {raw_full} ({raw_s:.1f}s)")

        if not skip_bed and get_settings().audio_bed_enabled:
            console.print("[bold]Audio bed mix[/bold] …")
            bed = AudioBedModule().mix_voice_manifest(
                compose_manifest,
                script_path=script_path,
            )
            compose_manifest = bed.mixed_manifest
            console.print(f"       → {compose_manifest}")

        wav_paths = sorted(
            compose_manifest.parent.glob("scene_*.wav")
            if compose_manifest
            else audio_dir.glob("scene_*.wav")
        )
        if wav_paths:
            full_path = audio_dir / "narration_full.wav"
            total_audio_s = _concat_wavs(wav_paths, full_path)
            console.print(f"[green]Combined[/green] {full_path} ({total_audio_s:.1f}s)")

    first_three = [s.text for s in script.scenes[:3]]
    _write_readme(
        out_dir / "README.md",
        topic=topic,
        model=model_label,
        word_count=word_count,
        scene_count=len(script.scenes),
        voice=voice,
        speed=speed,
        lang=lang,
        total_audio_s=total_audio_s,
        out_dir=out_dir,
        first_sentences=first_three,
        tts_mode=mode,
        chapter_count=chapter_count,
        inner_splits=inner_splits,
    )

    console.print(f"\n[bold]Done[/bold] → {out_dir}")
    listen = audio_dir / "narration_raw_full.wav"
    if not listen.exists():
        listen = audio_dir / "narration_full.wav"
    console.print(f"Listen: ffplay -nodisp -autoexit {listen}")
    for i, s in enumerate(first_three, 1):
        console.print(f"  {i}. {s[:100]}{'…' if len(s) > 100 else ''}")


if __name__ == "__main__":
    app()
