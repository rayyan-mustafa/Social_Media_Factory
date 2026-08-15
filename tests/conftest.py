"""Test defaults: never hit YouTube/yt-dlp from vod_picker refresh hooks."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _disable_live_vod_library_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests mock the library; opt in with LIVE_VOD_LIBRARY_IN_TESTS=1."""
    if os.environ.get("LIVE_VOD_LIBRARY_IN_TESTS") == "1":
        return
    monkeypatch.setenv("LIVE_VOD_LIBRARY", "0")
