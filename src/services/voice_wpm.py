"""Voice WPM calibration — measured Kokoro words-per-minute for dual pacing."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.settings import CONFIG_DIR, ROOT, get_settings

VOICE_WPM_PATH = CONFIG_DIR / "voice_wpm.json"
DEFAULT_WPM = 150.0

_WORD_RE = re.compile(r"[A-Za-z0-9']+")

# ~60s of calm documentary narration at ~150–180 WPM (~160–180 words).
DEFAULT_CALIBRATION_TEXT = (
    "In the shadowed galleries of Tudor England, a single decision could remake a dynasty. "
    "Candles guttered against cold stone as courtiers waited for news that never came. "
    "What if the fragile thread of succession had held, and the map of Europe itself "
    "had bent around one quiet cradle? History remembers the paths we walked; "
    "tonight we ask what might have flourished down the corridors we never took. "
    "Imagine a court where succession is settled not by blood alone, but by the quiet "
    "courage of advisors who refuse to look away. Maps are redrawn in candlelight. "
    "Treaties are signed with trembling hands. Ordinary people feel the tremor years "
    "before the chronicles admit it. What if the heir had lived, or the fleet had turned, "
    "or the letter had arrived one day earlier. The record would still look familiar at "
    "first glance, then diverge in ways that reshape faith, trade, and the stories a "
    "nation tells itself. We follow that fork carefully, scene by scene, so the alternate "
    "path feels as solid as the one we inherited from the archives and the songs."
)


@dataclass(frozen=True)
class VoiceWpmRecord:
    voice: str
    speed: float
    wpm: float
    measured_at: str
    words: int | None = None
    duration_s: float | None = None
    source: str = "calibrate"
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != ""}


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def wpm_from_words_duration(words: int, duration_s: float) -> float:
    if words <= 0 or duration_s <= 0:
        raise ValueError("words and duration_s must be positive")
    return float(words) / (float(duration_s) / 60.0)


def load_voice_wpm(
    *,
    path: Path | None = None,
    voice: str | None = None,
    speed: float | None = None,
    default: float = DEFAULT_WPM,
) -> float:
    """Return measured WPM when voice/speed match; else ``default`` (150)."""
    rec = load_voice_wpm_record(path=path)
    if rec is None:
        return float(default)
    s = get_settings()
    want_voice = (voice or s.kokoro_voice or "").strip()
    want_speed = float(speed if speed is not None else s.kokoro_speed)
    if rec.voice.strip() != want_voice:
        return float(default)
    if abs(float(rec.speed) - want_speed) > 0.001:
        return float(default)
    return float(rec.wpm)


def load_voice_wpm_record(*, path: Path | None = None) -> VoiceWpmRecord | None:
    p = Path(path) if path else VOICE_WPM_PATH
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict) or "wpm" not in raw:
        return None
    try:
        return VoiceWpmRecord(
            voice=str(raw.get("voice") or ""),
            speed=float(raw.get("speed") or 1.0),
            wpm=float(raw["wpm"]),
            measured_at=str(raw.get("measured_at") or ""),
            words=int(raw["words"]) if raw.get("words") is not None else None,
            duration_s=(
                float(raw["duration_s"]) if raw.get("duration_s") is not None else None
            ),
            source=str(raw.get("source") or "calibrate"),
            notes=str(raw.get("notes") or ""),
        )
    except (TypeError, ValueError):
        return None


def save_voice_wpm(record: VoiceWpmRecord, *, path: Path | None = None) -> Path:
    p = Path(path) if path else VOICE_WPM_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(record.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return p


def phase_b_words_from_wpm(wpm: float, seconds_per_scene: float) -> int:
    """Words for one Phase B beat: round(wpm * seconds/60)."""
    return max(1, int(round(float(wpm) * float(seconds_per_scene) / 60.0)))


def measure_from_audio(
    *,
    text: str,
    duration_s: float,
    voice: str | None = None,
    speed: float | None = None,
    source: str = "manual",
    notes: str = "",
) -> VoiceWpmRecord:
    s = get_settings()
    words = count_words(text)
    wpm = wpm_from_words_duration(words, duration_s)
    return VoiceWpmRecord(
        voice=(voice or s.kokoro_voice or "").strip(),
        speed=float(speed if speed is not None else s.kokoro_speed),
        wpm=round(wpm, 2),
        measured_at=datetime.now(timezone.utc).isoformat(),
        words=words,
        duration_s=round(float(duration_s), 3),
        source=source,
        notes=notes,
    )


def measure_from_job_dir(
    job_dir: Path | str,
    *,
    voice: str | None = None,
    speed: float | None = None,
) -> VoiceWpmRecord:
    """Compute WPM from a completed job's script words ÷ voice total duration."""
    jd = Path(job_dir)
    if not jd.is_absolute():
        jd = ROOT / jd
    script_path = jd / "script" / "script.json"
    if not script_path.exists():
        # Some layouts put script.json at job root or under scripts/
        for cand in (jd / "script.json", jd / "scripts" / "script.json"):
            if cand.exists():
                script_path = cand
                break
    if not script_path.exists():
        raise FileNotFoundError(f"script.json not found under {jd}")

    raw = json.loads(script_path.read_text(encoding="utf-8"))
    scenes = raw.get("scenes") or []
    text = " ".join(str(sc.get("text") or "") for sc in scenes)
    words = count_words(text)
    if words <= 0:
        raise ValueError(f"no spoken words in {script_path}")

    duration_s = _total_audio_duration_s(jd)
    if duration_s is None or duration_s <= 0:
        raise FileNotFoundError(f"no voice duration found under {jd}/audio")

    meta_voice = voice
    meta_speed = speed
    for manifest in (
        jd / "audio" / "mixed" / "voice_manifest_mixed.json",
        jd / "audio" / "voice_manifest.json",
    ):
        if not manifest.exists():
            continue
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
            meta_voice = meta_voice or m.get("voice")
            if meta_speed is None and m.get("speed") is not None:
                meta_speed = float(m["speed"])
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        break

    return measure_from_audio(
        text=text,
        duration_s=duration_s,
        voice=meta_voice,
        speed=meta_speed,
        source="job",
        notes=str(jd),
    )


def calibrate_synthesize(
    *,
    text: str | None = None,
    voice: str | None = None,
    speed: float | None = None,
    out_dir: Path | None = None,
) -> tuple[VoiceWpmRecord, Path]:
    """Synthesize a short calibration clip with current Kokoro voice/speed."""
    from src.services.tts_kokoro import VoiceModule

    s = get_settings()
    use_voice = (voice or s.kokoro_voice or "").strip()
    use_speed = float(speed if speed is not None else s.kokoro_speed)
    cal_text = (text or DEFAULT_CALIBRATION_TEXT).strip()
    if not cal_text:
        raise ValueError("calibration text is empty")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = Path(out_dir) if out_dir else (
        ROOT / "output" / "voice_wpm" / f"{stamp}_{use_voice}"
    )
    dest.mkdir(parents=True, exist_ok=True)
    wav_path = dest / "calibration.wav"

    vm = VoiceModule(voice=use_voice, speed=use_speed)
    engine = vm._ensure_engine()
    import numpy as np
    import soundfile as sf

    from src.services.voice_prosody import strip_ellipsis_for_tts

    spoken = strip_ellipsis_for_tts(cal_text)
    samples, sample_rate = engine.create(
        spoken,
        voice=use_voice,
        speed=use_speed,
        lang=vm.lang,
        trim=False,
    )
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError("calibration TTS returned empty audio")
    sf.write(str(wav_path), samples, int(sample_rate))
    duration_s = float(samples.size) / float(sample_rate)

    record = measure_from_audio(
        text=cal_text,
        duration_s=duration_s,
        voice=use_voice,
        speed=use_speed,
        source="synthesize",
        notes=str(wav_path),
    )
    meta_path = dest / "calibration.json"
    meta_path.write_text(
        json.dumps(
            {**record.as_dict(), "wav": str(wav_path), "text": cal_text},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return record, wav_path


def _total_audio_duration_s(job_dir: Path) -> float | None:
    for manifest in (
        job_dir / "audio" / "mixed" / "voice_manifest_mixed.json",
        job_dir / "audio" / "voice_manifest.json",
    ):
        if not manifest.exists():
            continue
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        scenes = raw.get("scenes") or []
        total = sum(float(sc.get("duration_s") or 0) for sc in scenes)
        if total > 0:
            return total
        validation = raw.get("validation") or {}
        if validation.get("total_duration_s"):
            return float(validation["total_duration_s"])
    return None
