"""Dual gallery pacing helpers (Phase A short beats → Phase B long beats)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.services.voice_wpm import (
    DEFAULT_WPM,
    load_voice_wpm,
    phase_b_words_from_wpm,
)


# Default 10-section word/duration ratios (must sum ≈ 1.0)
DEFAULT_SECTION_RATIOS = (
    0.075,  # Hook
    0.10,   # Historical Context
    0.075,  # Divergence
    0.125,  # Immediate Aftermath
    0.15,   # Mid-Term Shift
    0.10,   # Pattern Interrupt
    0.15,   # Long-term
    0.075,  # Counter-Argument
    0.10,   # Final Summary
    0.05,   # Community Question
)


@dataclass(frozen=True)
class DualPacing:
    enabled: bool
    total_words: int
    phase_a_max_words: int
    phase_a_seconds: float
    phase_a_words_min: int
    phase_a_words_max: int
    phase_a_target_scenes: int
    phase_b_seconds: float
    phase_b_words: int
    phase_b_words_min: int
    phase_b_words_max: int
    phase_b_target_scenes: int
    target_scenes: int
    min_scenes: int
    max_scenes: int
    voice_wpm: float = DEFAULT_WPM

    @property
    def phase_a_avg_words(self) -> float:
        return (self.phase_a_words_min + self.phase_a_words_max) / 2.0


def load_dual_pacing(file_cfg: dict[str, Any], *, settings_target_scenes: int) -> DualPacing:
    enabled = bool(file_cfg.get("dual_pacing", True))
    phase_a_max = int(file_cfg.get("phase_a_max_words", 1500))
    a_wmin = int(file_cfg.get("phase_a_words_min", file_cfg.get("words_per_sentence_min", 6)))
    a_wmax = int(file_cfg.get("phase_a_words_max", file_cfg.get("words_per_sentence_max", 12)))
    a_sec = float(file_cfg.get("phase_a_seconds_per_scene", 3))
    b_sec = float(file_cfg.get("phase_b_seconds_per_scene", 60))

    wpm = float(file_cfg.get("voice_wpm") or load_voice_wpm())
    if wpm <= 0:
        wpm = DEFAULT_WPM

    # Phase B words from measured WPM (plan: round(wpm * seconds/60))
    b_words = phase_b_words_from_wpm(wpm, b_sec)
    # Profile min/max are documentation for the seeded WPM; recenter ±10% if WPM drifted
    if "phase_b_words_min" in file_cfg and "phase_b_words_max" in file_cfg:
        b_wmin = int(file_cfg["phase_b_words_min"])
        b_wmax = int(file_cfg["phase_b_words_max"])
        if b_words < b_wmin or b_words > b_wmax:
            b_wmin = max(1, int(round(b_words * 0.90)))
            b_wmax = int(round(b_words * 1.10))
    else:
        b_wmin = max(1, int(round(b_words * 0.90)))
        b_wmax = int(round(b_words * 1.10))

    a_scenes = int(file_cfg.get("phase_a_target_scenes") or round(phase_a_max / ((a_wmin + a_wmax) / 2)))
    # Remaining runtime after phase A word budget at measured WPM
    duration_min = int(file_cfg.get("target_duration_min", 20))
    phase_a_min = phase_a_max / wpm  # words / WPM → minutes
    phase_b_min = max(0.0, duration_min - phase_a_min)
    b_scenes = int(
        file_cfg.get("phase_b_target_scenes")
        or max(1, round((phase_b_min * 60.0) / b_sec))
    )

    target = int(file_cfg.get("target_scenes") or (a_scenes + b_scenes) or settings_target_scenes)
    min_s = int(file_cfg.get("min_scenes") or max(1, int(target * 0.8)))
    max_s = int(file_cfg.get("max_scenes") or int(target * 1.3))

    return DualPacing(
        enabled=enabled,
        total_words=phase_a_max + b_scenes * b_words,
        phase_a_max_words=phase_a_max,
        phase_a_seconds=a_sec,
        phase_a_words_min=a_wmin,
        phase_a_words_max=a_wmax,
        phase_a_target_scenes=a_scenes,
        phase_b_seconds=b_sec,
        phase_b_words=b_words,
        phase_b_words_min=b_wmin,
        phase_b_words_max=b_wmax,
        phase_b_target_scenes=b_scenes,
        target_scenes=target,
        min_scenes=min_s,
        max_scenes=max_s,
        voice_wpm=wpm,
    )


def assign_chapter_phases(
    chapter_count: int,
    pacing: DualPacing,
    ratios: tuple[float, ...] = DEFAULT_SECTION_RATIOS,
) -> list[str]:
    """Return 'a'/'b' per chapter using cumulative word midpoints vs phase_a_max_words."""
    if chapter_count <= 0:
        return []
    if not pacing.enabled:
        return ["a"] * chapter_count

    # Use provided ratios; fall back to equal split
    if len(ratios) >= chapter_count:
        r = list(ratios[:chapter_count])
    else:
        r = [1.0 / chapter_count] * chapter_count
    s = sum(r) or 1.0
    r = [x / s for x in r]

    phases: list[str] = []
    cum = 0.0
    for frac in r:
        start = cum
        end = cum + frac
        mid = (start + end) / 2.0
        mid_words = mid * pacing.total_words
        phases.append("a" if mid_words < pacing.phase_a_max_words else "b")
        cum = end
    # Ensure at least one chapter in each phase when dual pacing is on
    if "a" not in phases:
        phases[0] = "a"
    if "b" not in phases:
        phases[-1] = "b"
    return phases


def scale_phase_targets(
    raw_targets: list[int],
    phases: list[str],
    pacing: DualPacing,
) -> list[int]:
    """Rescale chapter sentence targets so A-sum≈phase_a_target and B-sum≈phase_b_target."""
    n = len(raw_targets)
    if n == 0:
        return []
    if len(phases) != n:
        phases = assign_chapter_phases(n, pacing)

    out = [max(1, int(t)) for t in raw_targets]
    for phase, goal in (("a", pacing.phase_a_target_scenes), ("b", pacing.phase_b_target_scenes)):
        idxs = [i for i, p in enumerate(phases) if p == phase]
        if not idxs:
            continue
        current = sum(out[i] for i in idxs) or len(idxs)
        allocated = 0
        for j, i in enumerate(idxs):
            if j == len(idxs) - 1:
                out[i] = max(1, goal - allocated)
            else:
                out[i] = max(1, round(out[i] / current * goal))
                allocated += out[i]
        # Fix drift on last
        drift = goal - sum(out[i] for i in idxs)
        if drift != 0:
            last_i = idxs[-1]
            out[last_i] = max(1, out[last_i] + drift)
    return out


def estimate_duration_s(scenes: list[dict[str, Any]] | list[Any], pacing: DualPacing) -> float:
    total = 0.0
    for s in scenes:
        phase = getattr(s, "pacing_phase", None)
        if phase is None and isinstance(s, dict):
            phase = s.get("pacing_phase")
        if phase == "b":
            total += pacing.phase_b_seconds
        else:
            total += pacing.phase_a_seconds
    return total


def phase_for_running_words(words_before: int, pacing: DualPacing) -> str:
    if not pacing.enabled:
        return "a"
    return "a" if words_before < pacing.phase_a_max_words else "b"
