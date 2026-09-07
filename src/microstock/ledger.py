"""Asset ledger — the microstock ``processed_history.json``.

Every asset is keyed by the SHA-256 of its source PNG, so re-running any stage is
idempotent: a crashed beat resumes instead of duplicating work, and an asset can
never be uploaded twice to the same platform.

State lives under ``output/microstock/ops/`` via an ``OpsStore`` rooted there, so
microstock events, spend and digests are completely separate from the video farm's.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.microstock import paths

_LOCK = threading.Lock()

# Pipeline stages, in order. `stage` only ever moves forward.
STAGES = (
    "brief",
    "generated",
    "traced",
    "cleaned",
    "gate_passed",
    "gate_failed",
    "tagged",
    "queued",
    "distributed",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | Path, *, chunk: int = 1 << 20) -> str:
    """Streaming content hash — the asset's stable identity across the pipeline."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


class AssetLedger:
    """File-backed asset record store with atomic writes.

    Mirrors ``src/agents/store.py::OpsStore`` write semantics (tmp file + replace
    under a lock) rather than inventing new persistence.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.ASSET_LEDGER_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    # ---- persistence -------------------------------------------------
    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # ---- records -----------------------------------------------------
    def get(self, asset_id: str) -> dict[str, Any] | None:
        return self._read().get(asset_id)

    def upsert(self, asset_id: str, **fields: Any) -> dict[str, Any]:
        """Create or merge an asset record. Returns the stored record."""
        with _LOCK:
            data = self._read()
            record = data.get(asset_id) or {
                "asset_id": asset_id,
                "created_at": _now(),
                "stage": "brief",
                "platforms_uploaded": [],
                "platforms_pending": [],
                "cost_usd": 0.0,
            }
            record.update(fields)
            record["updated_at"] = _now()
            data[asset_id] = record
            self._write(data)
            return record

    def set_stage(self, asset_id: str, stage: str, **fields: Any) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
        return self.upsert(asset_id, stage=stage, **fields)

    def list_by_stage(self, stage: str) -> list[dict[str, Any]]:
        return [r for r in self._read().values() if r.get("stage") == stage]

    def all(self) -> list[dict[str, Any]]:
        return list(self._read().values())

    # ---- idempotency guards -----------------------------------------
    def already_processed(self, asset_id: str, stage: str) -> bool:
        """True if the asset already reached ``stage`` or later."""
        record = self.get(asset_id)
        if not record:
            return False
        current = record.get("stage")
        if current not in STAGES or stage not in STAGES:
            return False
        return STAGES.index(current) >= STAGES.index(stage)

    def already_uploaded(self, asset_id: str, platform: str) -> bool:
        """True if this exact asset already went to this platform. Never re-send."""
        record = self.get(asset_id)
        return bool(record and platform in (record.get("platforms_uploaded") or []))

    def mark_uploaded(self, asset_id: str, platform: str) -> dict[str, Any]:
        with _LOCK:
            data = self._read()
            record = data.get(asset_id)
            if record is None:
                raise KeyError(f"unknown asset {asset_id!r}")
            uploaded = list(record.get("platforms_uploaded") or [])
            if platform not in uploaded:
                uploaded.append(platform)
            record["platforms_uploaded"] = uploaded
            record["platforms_pending"] = [
                p for p in (record.get("platforms_pending") or []) if p != platform
            ]
            if not record["platforms_pending"]:
                record["stage"] = "distributed"
            record["updated_at"] = _now()
            data[asset_id] = record
            self._write(data)
            return record

    def queue_for_platforms(self, asset_id: str, platforms: list[str]) -> dict[str, Any]:
        """Enter the drip queue — pending minus anything already delivered."""
        record = self.get(asset_id) or {}
        uploaded = set(record.get("platforms_uploaded") or [])
        pending = [p for p in platforms if p not in uploaded]
        return self.upsert(asset_id, stage="queued", platforms_pending=pending)

    def pending_for_platform(self, platform: str) -> list[dict[str, Any]]:
        """Assets waiting on this platform, oldest first (FIFO backlog drain)."""
        rows = [
            r
            for r in self._read().values()
            if platform in (r.get("platforms_pending") or [])
            and platform not in (r.get("platforms_uploaded") or [])
        ]
        return sorted(rows, key=lambda r: r.get("created_at", ""))

    def uploaded_count(self, platform: str) -> int:
        """Lifetime uploads to a platform — enforces Freepik's Tier-1 cap."""
        return sum(
            1 for r in self._read().values() if platform in (r.get("platforms_uploaded") or [])
        )

    def uploaded_today(self, platform: str, *, day: str | None = None) -> int:
        """Uploads to a platform today (UTC) — enforces per-platform daily caps."""
        today = day or datetime.now(timezone.utc).date().isoformat()
        count = 0
        for record in self._read().values():
            if platform not in (record.get("platforms_uploaded") or []):
                continue
            if str(record.get("updated_at", "")).startswith(today):
                count += 1
        return count

    def counts_by_stage(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._read().values():
            counts[record.get("stage", "unknown")] = counts.get(record.get("stage", "unknown"), 0) + 1
        return counts
