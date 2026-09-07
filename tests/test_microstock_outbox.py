"""Tier B staging and manifest schemas."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.microstock import paths
from src.microstock.manifest import ADOBE_COLUMNS, write_manifest
from src.microstock.outbox import stage_batch

META = {
    "asset_id": "abc123",
    "title": "Isometric cloud server rack",
    "category": "Technology",
    "keywords": ["cloud", "server", "rack"],
}


@pytest.fixture()
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(paths, "outbox_for", lambda p, b: tmp_path / "outbox" / p / b)
    svg = tmp_path / "abc123.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
        '<path d="M0 0L9 9Z"/></svg>', encoding="utf-8",
    )
    return stage_batch("adobe_stock", [{**META, "clean_svg": str(svg)}], batch_id="b1")


def test_batch_contains_asset_manifest_and_instructions(staged):
    folder = Path(staged["target"])
    names = {p.name for p in folder.iterdir()}
    assert names == {"abc123.svg", "manifest.csv", "UPLOAD_INSTRUCTIONS.md"}


def test_adobe_manifest_uses_the_adobe_schema(staged):
    with (Path(staged["target"]) / "manifest.csv").open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ADOBE_COLUMNS
        row = next(reader)
    assert row["Filename"] == "abc123.svg"
    assert row["Keywords"] == "cloud, server, rack"


def test_adobe_manifest_declares_ai_generation(staged):
    with (Path(staged["target"]) / "manifest.csv").open(encoding="utf-8") as handle:
        assert next(csv.DictReader(handle))["GeneratedByAI"] == "Yes"


def test_instructions_demand_the_ai_checkbox(staged):
    text = (Path(staged["target"]) / "UPLOAD_INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert "Generative AI" in text and "mandatory" in text


def test_shutterstock_is_refused(tmp_path):
    with pytest.raises(ValueError, match="permanently excluded"):
        stage_batch("shutterstock", [META])


def test_unknown_platform_is_refused():
    with pytest.raises(ValueError, match="unknown platform"):
        stage_batch("myspace", [META])


def test_empty_batch_is_a_noop():
    assert stage_batch("adobe_stock", [])["staged"] == 0


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "outbox_for", lambda p, b: tmp_path / "out" / p / b)
    result = stage_batch("adobe_stock", [META], batch_id="b2", dry_run=True)
    assert result["reason"] == "dry_run"
    assert not (tmp_path / "out").exists()


def test_generic_manifest_used_for_non_adobe(tmp_path):
    path = write_manifest("vecteezy", [{**META, "filename": "x.svg"}], tmp_path / "m.csv")
    with path.open(encoding="utf-8") as handle:
        assert "AI Generated" in (csv.DictReader(handle).fieldnames or [])
