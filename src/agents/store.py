"""Ops store — file-backed job/events ledger (Postgres-ready schema).

Default: JSON under output/ops/ (no Docker required).
Optional: DATABASE_URL=postgresql://... for future SQLAlchemy wiring.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.services.settings import ROOT

OPS_DIR = ROOT / "output" / "ops"
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRecord(BaseModel):
    id: str
    title: str
    status: str = "queued"
    stage: str = "queued"
    sheet_row: int | None = None
    video_id: str | None = None
    watch_url: str | None = None
    job_dir: str | None = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class EventRecord(BaseModel):
    id: str
    job_id: str | None = None
    agent: str
    event_type: str
    severity: str = "info"
    message: str
    action: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)


class PolicySnapshot(BaseModel):
    version: int
    content_hash: str
    fetched_at: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    rule_pack: dict[str, Any] = Field(default_factory=dict)
    compile_ok: bool = True
    path: str = ""


class AlgoInsight(BaseModel):
    id: str
    video_id: str | None = None
    title: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
    vs_benchmark: dict[str, Any] = Field(default_factory=dict)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)


class OpsStore:
    """Thread-safe JSON store mirroring Plan C Postgres tables."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else OPS_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        for name in (
            "jobs.json",
            "events.json",
            "policy_snapshots.json",
            "algo_insights.json",
            "benchmarks.json",
            "spend.json",
            "ledger.json",
        ):
            p = self.root / name
            if not p.exists():
                if name == "benchmarks.json":
                    p.write_text(
                        json.dumps(_default_benchmarks(), indent=2) + "\n",
                        encoding="utf-8",
                    )
                elif name == "spend.json":
                    p.write_text(
                        json.dumps({"month": _month_key(), "items": []}, indent=2)
                        + "\n",
                        encoding="utf-8",
                    )
                else:
                    p.write_text("[]\n", encoding="utf-8")

    def _read(self, name: str) -> Any:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def _write(self, name: str, data: Any) -> None:
        path = self.root / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(path)

    def create_job(self, title: str, **kwargs: Any) -> JobRecord:
        with _LOCK:
            jobs = self._read("jobs.json")
            job = JobRecord(
                id=kwargs.pop("id", None) or f"job_{uuid.uuid4().hex[:12]}",
                title=title,
                **kwargs,
            )
            jobs.append(job.model_dump())
            self._write("jobs.json", jobs)
            return job

    def update_job(self, job_id: str, **updates: Any) -> JobRecord | None:
        with _LOCK:
            jobs = self._read("jobs.json")
            for i, raw in enumerate(jobs):
                if raw.get("id") == job_id:
                    raw.update(updates)
                    raw["updated_at"] = _now()
                    jobs[i] = raw
                    self._write("jobs.json", jobs)
                    return JobRecord.model_validate(raw)
            return None

    def get_job(self, job_id: str) -> JobRecord | None:
        for raw in self._read("jobs.json"):
            if raw.get("id") == job_id:
                return JobRecord.model_validate(raw)
        return None

    def list_jobs(self, *, status: str | None = None) -> list[JobRecord]:
        out = [JobRecord.model_validate(r) for r in self._read("jobs.json")]
        if status:
            out = [j for j in out if j.status == status]
        return out

    def add_event(
        self,
        *,
        agent: str,
        event_type: str,
        message: str,
        job_id: str | None = None,
        severity: str = "info",
        action: str = "",
        payload: dict[str, Any] | None = None,
    ) -> EventRecord:
        with _LOCK:
            events = self._read("events.json")
            ev = EventRecord(
                id=f"evt_{uuid.uuid4().hex[:12]}",
                job_id=job_id,
                agent=agent,
                event_type=event_type,
                severity=severity,
                message=message,
                action=action,
                payload=payload or {},
            )
            events.append(ev.model_dump())
            self._write("events.json", events)
            return ev

    def list_events(
        self, *, job_id: str | None = None, limit: int = 100
    ) -> list[EventRecord]:
        events = [EventRecord.model_validate(r) for r in self._read("events.json")]
        if job_id:
            events = [e for e in events if e.job_id == job_id]
        return events[-limit:]

    def save_policy_snapshot(self, snap: PolicySnapshot) -> PolicySnapshot:
        with _LOCK:
            snaps = self._read("policy_snapshots.json")
            snaps.append(snap.model_dump())
            self._write("policy_snapshots.json", snaps)
            return snap

    def latest_policy_snapshot(self) -> PolicySnapshot | None:
        snaps = self._read("policy_snapshots.json")
        if not snaps:
            return None
        return PolicySnapshot.model_validate(snaps[-1])

    def get_benchmarks(self) -> dict[str, Any]:
        return self._read("benchmarks.json")

    def get_smm_winner_signals(self, channel: str | None = None) -> dict[str, Any]:
        """Read API for Harvest/Trends: SMM winners (optionally per sheet channel).

        Does not refresh winners — SMM / watchdog ``_maybe_smm_scan`` owns writes.
        Lock-free read of the current benchmarks snapshot.
        """
        from src.agents.smm_harvest_bridge import load_smm_winner_signals

        return load_smm_winner_signals(self.get_benchmarks(), channel=channel)

    def save_benchmarks(self, data: dict[str, Any]) -> None:
        with _LOCK:
            self._write("benchmarks.json", data)

    def add_algo_insight(self, insight: AlgoInsight) -> AlgoInsight:
        with _LOCK:
            rows = self._read("algo_insights.json")
            rows.append(insight.model_dump())
            self._write("algo_insights.json", rows)
            return insight

    def list_algo_insights(self, limit: int = 50) -> list[AlgoInsight]:
        rows = [
            AlgoInsight.model_validate(r) for r in self._read("algo_insights.json")
        ]
        return rows[-limit:]

    def record_spend(
        self, *, category: str, amount_usd: float, note: str = "",
        job_id: str | None = None, channel: str | None = None,
    ) -> dict:
        with _LOCK:
            data = self._read("spend.json")
            month = _month_key()
            if data.get("month") != month:
                data = {"month": month, "items": []}
            item = {
                "id": f"sp_{uuid.uuid4().hex[:8]}",
                "category": category,
                "amount_usd": amount_usd,
                "note": note,
                "at": _now(),
            }
            if job_id:
                item["job_id"] = job_id
            if channel:
                item["channel"] = channel
            data["items"].append(item)
            self._write("spend.json", data)
            return item

    def month_spend_total(self) -> float:
        data = self._read("spend.json")
        if data.get("month") != _month_key():
            return 0.0
        return float(
            sum(float(i.get("amount_usd") or 0) for i in data.get("items") or [])
        )

    def append_ledger(self, entry: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            rows = self._read("ledger.json")
            entry = dict(entry)
            entry.setdefault("id", f"led_{uuid.uuid4().hex[:10]}")
            entry.setdefault("created_at", _now())
            rows.append(entry)
            self._write("ledger.json", rows)
            return entry


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _default_benchmarks() -> dict[str, Any]:
    return {
        "audience": "history_nerds_what_if_gallery",
        "avd_pct_min": 40.0,
        "first_60s_retention_pct_min": 70.0,
        "ctr_pct_min": 4.0,
        "browse_suggested_share_min": 0.25,
        "consecutive_fails_before_voice_propose": 2,
        "failing_streak": 0,
        "voice_proposals_pending": [],
        "updated_at": _now(),
    }


POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  sheet_row INT,
  video_id TEXT,
  watch_url TEXT,
  job_dir TEXT,
  error TEXT,
  meta JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  job_id TEXT,
  agent TEXT NOT NULL,
  event_type TEXT NOT NULL,
  severity TEXT,
  message TEXT,
  action TEXT,
  payload JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS policy_snapshots (
  version INT,
  content_hash TEXT,
  fetched_at TIMESTAMPTZ,
  sources JSONB,
  rule_pack JSONB,
  compile_ok BOOLEAN
);
CREATE TABLE IF NOT EXISTS algo_insights (
  id TEXT PRIMARY KEY,
  video_id TEXT,
  title TEXT,
  metrics JSONB,
  vs_benchmark JSONB,
  proposals JSONB,
  created_at TIMESTAMPTZ
);
"""
