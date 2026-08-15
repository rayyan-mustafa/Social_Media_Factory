"""Watchdog — stage supervisor from topic → publish."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from src.agents.ledger import OpsLedger
from src.agents.store import JobRecord, OpsStore
from src.services.settings import CONFIG_DIR

logger = logging.getLogger(__name__)


class WatchdogAgent:
    """Close eye on all stages; instant remediation + ledger."""

    def __init__(self, store: OpsStore | None = None, ledger: OpsLedger | None = None):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)
        self.cfg = _load_agents_settings()

    def on_stage(
        self,
        job_id: str,
        stage: str,
        *,
        status: str | None = None,
        error: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> JobRecord | None:
        updates: dict[str, Any] = {"stage": stage}
        # Prefer explicit status. On error default to HOLD (resumable), never
        # silently overwrite an explicit hold/failed with the other.
        if status:
            updates["status"] = status
        if error:
            updates["error"] = error
            if not status:
                updates["status"] = "hold"
        if meta:
            job = self.store.get_job(job_id)
            merged = dict((job.meta if job else {}) or {})
            merged.update(meta)
            updates["meta"] = merged
        job = self.store.update_job(job_id, **updates)
        final_status = str(updates.get("status") or (job.status if job else "") or "")
        self.store.add_event(
            agent="watchdog",
            event_type="stage",
            message=f"{stage}" + (f" ERROR: {error}" if error else ""),
            job_id=job_id,
            severity="error" if error else "info",
            action="record_stage",
            payload=meta or {},
        )
        if error:
            self.ledger.write(
                agent="watchdog",
                problem=f"stage={stage} failed: {error}",
                action="HOLD / fail job — no silent public",
                job_id=job_id,
                severity="error",
                publish_status=final_status or "hold",
            )
        return job

    def scan_stuck_jobs(self) -> list[dict[str, Any]]:
        """Find jobs stuck past stage timeouts; mark HOLD + alert.

        Skips private/scheduled — those wait on human public_approved / publishAt.
        For ``queued``: try spawn / heartbeat instead of false HOLD when the farm
        is only waiting on capacity or GREEN stills.
        """
        timeouts = self.cfg.get("job_stage_timeouts_s") or {}
        actions: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for job in self.store.list_jobs():
            # Waiting on human or already terminal — do not HOLD
            if job.status in (
                "failed",
                "public",
                "done",
                "hold",
                "private",
                "scheduled",
                "ready_for_stills",
                "awaiting_gpu",
            ):
                continue
            stage = job.stage or job.status
            # Prefer status-keyed timeout for farming wrapper
            limit = float(
                timeouts.get(job.status)
                or timeouts.get(stage)
                or 7200
            )
            try:
                updated = datetime.fromisoformat(job.updated_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            age = (now - updated).total_seconds()
            if age <= limit:
                continue

            # Queued past timeout: prefer spawn/heartbeat over crash HOLD.
            if job.status == "queued":
                handled = self._handle_stuck_queued(job, age=age, limit=limit)
                if handled:
                    actions.append(handled)
                continue

            err = f"stuck in {stage} for {age:.0f}s > {limit:.0f}s"
            try:
                from src.agents.farm import hold_farm_job, infer_last_good_stage

                hold_farm_job(
                    job.id,
                    error=err,
                    store=self.store,
                    watchdog=None,  # already writing ledger below
                    ledger=None,
                    hold_class="process_crash",
                    last_good_stage=infer_last_good_stage(
                        job.job_dir, fallback=str(stage or "scripting")
                    ),
                    reason="stuck_stage_timeout",
                    sync_sheet=True,
                )
            except Exception:  # noqa: BLE001
                self.store.update_job(job.id, status="hold", error=err)
                try:
                    from src.agents.title_queue import TitleQueue

                    row = TitleQueue().find_by_job_id(job.id)
                    if row:
                        TitleQueue().update_row(
                            row.row_index,
                            status="hold",
                            notes=err[:500],
                            channel=getattr(row, "channel", None) or None,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("title queue sync on hold failed: %s", exc)
            entry = self.ledger.write(
                agent="watchdog",
                problem=f"job stuck in {stage} ({age:.0f}s)",
                action="set status=hold + alert",
                job_id=job.id,
                severity="critical",
                publish_status="hold",
            )
            actions.append({"job_id": job.id, "stage": stage, "ledger": entry["id"]})
        return actions

    def _handle_stuck_queued(
        self, job: JobRecord, *, age: float, limit: float
    ) -> dict[str, Any] | None:
        """Spawn or heartbeat queued jobs; HOLD only when farm PID is dead."""
        meta = dict(job.meta or {})
        from src.agents.farm import pid_alive, spawn_farm_job

        pid = meta.get("farm_pid")
        if pid_alive(pid):
            # Process alive but status stale — touch heartbeat, do not HOLD.
            self.store.update_job(job.id, meta={**meta, "queued_heartbeat_at": datetime.now(timezone.utc).isoformat()})
            return {
                "job_id": job.id,
                "stage": "queued",
                "action": "heartbeat_alive_pid",
                "age_s": age,
            }

        if meta.get("skip_auto_farm"):
            return None

        try:
            spawned = spawn_farm_job(job.id, store=self.store, watchdog=self)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stuck queued spawn failed job=%s: %s", job.id, exc)
            spawned = {"ok": False, "error": str(exc)}

        if spawned.get("spawned") or spawned.get("already_running"):
            self.ledger.write(
                agent="watchdog",
                problem=f"job stuck in queued ({age:.0f}s) — spawned/resumed",
                action="spawn_farm_job",
                job_id=job.id,
                severity="warning",
                publish_status="queued",
            )
            return {
                "job_id": job.id,
                "stage": "queued",
                "action": "spawned" if spawned.get("spawned") else "already_running",
                "age_s": age,
            }

        err_s = str(spawned.get("error") or "")
        # Capacity / GREEN / prep lock — heartbeat so we do not false-HOLD.
        defer_markers = (
            "green",
            "gpu",
            "capacity",
            "max concurrent",
            "encoding",
            "slot",
            "awaiting",
            "not ready",
            "prep",
            "lock",
        )
        if any(m in err_s.lower() for m in defer_markers) or not err_s:
            meta["queued_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
            meta["queued_defer_reason"] = err_s[:300]
            self.store.update_job(job.id, meta=meta)
            self.ledger.write(
                agent="watchdog",
                problem=f"job queued {age:.0f}s > {limit:.0f}s — waiting capacity/GREEN",
                action="heartbeat (no HOLD); retry spawn next tick",
                job_id=job.id,
                severity="info",
                publish_status="queued",
            )
            return {
                "job_id": job.id,
                "stage": "queued",
                "action": "heartbeat_deferred",
                "age_s": age,
                "error": err_s[:200],
            }

        # Hard spawn failure with dead/missing PID → HOLD resumable.
        err = f"stuck in queued for {age:.0f}s > {limit:.0f}s; spawn={err_s[:180]}"
        try:
            from src.agents.farm import hold_farm_job, infer_last_good_stage

            hold_farm_job(
                job.id,
                error=err,
                store=self.store,
                watchdog=None,
                ledger=None,
                hold_class="process_crash",
                last_good_stage=infer_last_good_stage(
                    job.job_dir, fallback="scripting"
                ),
                reason="stuck_queued_spawn_failed",
                sync_sheet=True,
            )
        except Exception:  # noqa: BLE001
            self.store.update_job(job.id, status="hold", error=err)
        entry = self.ledger.write(
            agent="watchdog",
            problem=f"job stuck in queued ({age:.0f}s)",
            action="HOLD process_crash after spawn failure",
            job_id=job.id,
            severity="critical",
            publish_status="hold",
        )
        return {
            "job_id": job.id,
            "stage": "queued",
            "action": "hold",
            "ledger": entry["id"],
            "age_s": age,
        }

    def handle_gate_failure(
        self, job_id: str, gate: str, errors: list[str], *, retry: bool = False
    ) -> None:
        action = "requeue once" if retry else "HOLD — fix content"
        if retry:
            self.store.update_job(job_id, status="queued", stage="queued", error=None)
        else:
            self.store.update_job(
                job_id, status="hold", error=f"{gate}: {'; '.join(errors)}"
            )
        self.ledger.write(
            agent="watchdog",
            problem=f"{gate} failed: {'; '.join(errors)}",
            action=action,
            job_id=job_id,
            severity="error",
            publish_status="hold",
        )

    def report_publish(
        self,
        job_id: str,
        *,
        video_id: str | None,
        watch_url: str | None,
        privacy: str,
    ) -> None:
        self.store.update_job(
            job_id,
            status=privacy if privacy in ("private", "public") else "private",
            stage="private" if privacy == "private" else "public",
            video_id=video_id,
            watch_url=watch_url,
            error=None,
        )
        self.ledger.write(
            agent="watchdog",
            problem=f"publish {privacy}",
            action=f"recorded video_id={video_id}",
            job_id=job_id,
            severity="info",
            publish_status=privacy,
            extra={"watch_url": watch_url},
        )


def _load_agents_settings() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
