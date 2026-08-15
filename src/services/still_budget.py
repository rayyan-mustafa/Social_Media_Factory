"""Still budget helpers — cap unique Flux gens for long epics (FoC 3–4h).

Maps many narration scenes onto fewer unique stills; compose reuses via Ken Burns
+ chapter holds so GPU hours stay closer to a 90m epic.
"""

from __future__ import annotations

from typing import Any


def resolve_max_unique_stills(
    *,
    profile: dict[str, Any] | None = None,
    format_mode: str | None = None,
    scene_count: int,
    default_cap: int | None = None,
) -> int:
    """Return max unique Flux stills for this job."""
    prof = profile or {}
    cap = prof.get("max_unique_stills")
    if cap is None and default_cap is not None:
        cap = default_cap
    mode = (format_mode or prof.get("_retention_profile") or "").strip().lower()
    if cap is None:
        if mode in {"foc_epic", "primary_source_reading"}:
            cap = 80
        elif mode == "epic":
            cap = 240
        else:
            cap = scene_count
    try:
        cap_i = int(cap)
    except (TypeError, ValueError):
        cap_i = scene_count
    return max(1, min(cap_i, max(1, scene_count)))


def still_reuse_map(
    scene_count: int,
    *,
    max_unique: int,
) -> list[int]:
    """For each scene index 0..n-1, return the unique-still slot to use.

    Spreads unique slots evenly so chapter arcs keep visual variety without
    one-still-per-sentence Flux burns.
    """
    n = max(0, int(scene_count))
    if n == 0:
        return []
    k = max(1, min(int(max_unique), n))
    if k >= n:
        return list(range(n))
    # Map scene i → slot round(i * (k-1) / (n-1))
    out: list[int] = []
    for i in range(n):
        slot = round(i * (k - 1) / (n - 1)) if n > 1 else 0
        out.append(int(slot))
    return out


def unique_scene_indices(scene_count: int, *, max_unique: int) -> list[int]:
    """Scene indices that should receive a fresh Flux gen (others copy)."""
    mapping = still_reuse_map(scene_count, max_unique=max_unique)
    seen: set[int] = set()
    idxs: list[int] = []
    for scene_i, slot in enumerate(mapping):
        if slot not in seen:
            seen.add(slot)
            idxs.append(scene_i)
    return idxs


def foc_epic_length_band_s() -> tuple[int, int]:
    """Canonical 3–4 hour band (seconds)."""
    return 10800, 14400


def format_mode_length_band(
    format_mode: str | None,
    *,
    fallback: tuple[int | None, int | None] = (None, None),
) -> tuple[int | None, int | None]:
    mode = (format_mode or "").strip().lower()
    if mode in {"foc_epic", "foc", "3-4h", "3h", "4h"}:
        return foc_epic_length_band_s()
    if mode == "epic":
        return 4800, 6000
    if mode == "primary_source_reading":
        return 2400, 7200
    return fallback
