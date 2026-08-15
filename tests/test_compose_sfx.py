"""Channel gating + plan for cinematic compose SFX (napstorian vs historian)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.services.compose_sfx import (
    ensure_sfx_assets,
    plan_compose_sfx,
    resolve_compose_sfx,
)
from src.services.settings import get_settings


@dataclass
class _Clip:
    index: int
    duration_s: float


@pytest.fixture(autouse=True)
def _clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_resolve_sfx_on_napstorian_off_historian(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPOSE_SFX", "1")
    monkeypatch.setenv("COMPOSE_SFX_NAPSTORIAN", "1")
    monkeypatch.setenv("COMPOSE_SFX_HISTORIAN", "0")
    get_settings.cache_clear()
    s = get_settings()
    assert resolve_compose_sfx("napstorian", s) is True
    assert resolve_compose_sfx("napping_historian", s) is False


def test_resolve_sfx_master_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPOSE_SFX", "0")
    monkeypatch.setenv("COMPOSE_SFX_NAPSTORIAN", "1")
    get_settings.cache_clear()
    s = get_settings()
    assert resolve_compose_sfx("napstorian", s) is False
    assert resolve_compose_sfx("napping_historian", s) is False


def test_plan_events_napstorian_has_cold_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPOSE_SFX", "1")
    monkeypatch.setenv("COMPOSE_SFX_NAPSTORIAN", "1")
    monkeypatch.setenv("COMPOSE_SFX_DIR", str(tmp_path / "sfx"))
    monkeypatch.setenv("COMPOSE_SFX_BEAT_INTERVAL_S", "0")
    get_settings.cache_clear()
    s = get_settings()
    ensure_sfx_assets(tmp_path / "sfx")

    clips = [_Clip(0, 5.0), _Clip(1, 5.0), _Clip(2, 5.0)]
    overlays = [
        {"scene_index": 1, "style": "map"},
        {"scene_index": 2, "style": "timeline"},
    ]
    plan = plan_compose_sfx(
        channel="napstorian",
        scene_clips=clips,
        chapter_boundary_after={0},
        overlays=overlays,
        settings=s,
    )
    assert plan.enabled is True
    kinds = {e.kind for e in plan.events}
    assert "cold_whoosh" in kinds
    assert "cold_impact" in kinds
    assert "chapter_hit" in kinds
    assert "infographic_whoosh" in kinds


def test_plan_sfx_throughout_dense_overlays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPOSE_SFX", "1")
    monkeypatch.setenv("COMPOSE_SFX_NAPSTORIAN", "1")
    monkeypatch.setenv("COMPOSE_SFX_DIR", str(tmp_path / "sfx"))
    monkeypatch.setenv("COMPOSE_SFX_MAX_INFOGRAPHIC_WHOOSHES", "24")
    monkeypatch.setenv("COMPOSE_SFX_BEAT_INTERVAL_S", "70")
    get_settings.cache_clear()
    s = get_settings()
    ensure_sfx_assets(tmp_path / "sfx")

    # ~23 min timeline with many overlay plates
    clips = [_Clip(i, 11.0) for i in range(126)]
    overlays = [{"scene_index": i, "style": "timeline"} for i in range(0, 126, 7)]
    plan = plan_compose_sfx(
        channel="napstorian",
        scene_clips=clips,
        chapter_boundary_after={10, 20, 40, 60, 80, 100},
        overlays=overlays,
        settings=s,
    )
    assert plan.enabled is True
    whooshes = [e for e in plan.events if e.kind == "infographic_whoosh"]
    hits = [e for e in plan.events if e.kind == "chapter_hit"]
    assert len(whooshes) >= 12
    assert len(whooshes) <= 24
    # Story beats + chapter boundaries → hits throughout, not only open
    assert len(hits) >= 6
    assert max(e.at_s for e in plan.events if e.kind != "ambient_bed") > 600
    assert plan.meta.get("whoosh_cap") == 24


def test_plan_disabled_for_historian(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPOSE_SFX", "1")
    monkeypatch.setenv("COMPOSE_SFX_NAPSTORIAN", "1")
    monkeypatch.setenv("COMPOSE_SFX_HISTORIAN", "0")
    monkeypatch.setenv("COMPOSE_SFX_DIR", str(tmp_path / "sfx"))
    get_settings.cache_clear()
    s = get_settings()
    plan = plan_compose_sfx(
        channel="napping_historian",
        scene_clips=[_Clip(0, 4.0)],
        chapter_boundary_after=set(),
        overlays=[{"scene_index": 0, "style": "map"}],
        settings=s,
    )
    assert plan.enabled is False
    assert plan.events == []
