"""Live Gate R enforce flag — prefer on-disk ``.env`` over stale process env.

Long-lived ``run_farm_job`` PIDs may still have ``GATE_R_ENFORCE=true`` in
``os.environ`` from process start. Publish must re-read ``.env`` at check time
so flipping the file to soft/advisory applies without killing those farms.
"""

from __future__ import annotations

import os
from pathlib import Path

from src.services.settings import ROOT

_TRUTHY = frozenset({"1", "true", "yes"})


def dotenv_value(key: str, *, env_file: Path | None = None) -> str | None:
    """Read ``key`` from a dotenv file (last assignment). Ignores process env."""
    path = Path(env_file) if env_file is not None else (ROOT / ".env")
    if not path.is_file():
        return None
    try:
        from dotenv import dotenv_values

        vals = dotenv_values(path)
    except Exception:  # noqa: BLE001
        return None
    raw = vals.get(key)
    if raw is None:
        return None
    return str(raw).strip()


def gate_r_enforce_enabled(
    *,
    env_name: str = "GATE_R_ENFORCE",
    env_file: Path | None = None,
) -> bool:
    """Whether Gate R should hard-block / HOLD publish.

    Prefers the current on-disk ``.env`` value. When the file defines the key,
    syncs it into ``os.environ`` so later ``os.getenv`` readers see soft mode too.
    """
    file_val = dotenv_value(env_name, env_file=env_file)
    if file_val is not None:
        os.environ[env_name] = file_val
        raw = file_val
    else:
        raw = (os.getenv(env_name) or "false").strip()
    return raw.lower() in _TRUTHY
