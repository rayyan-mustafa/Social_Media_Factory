"""Ops Ledger — local JSON + optional batch digest email (not per-event)."""

from __future__ import annotations

import json
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR, JobRecord, OpsStore
from src.services.settings import ROOT, get_settings

logger = logging.getLogger(__name__)

# Terminal statuses that count as "new finished work" for digest triggers.
DIGEST_TERMINAL_STATUSES = frozenset({"private", "failed", "scheduled", "done"})
# Shown in digest body for overnight context (inflight + waiting).
DIGEST_SNAPSHOT_STATUSES = frozenset(
    {
        "queued",
        "running",
        "farming",
        "scripting",
        "tts",
        "visuals",
        "edit",
        "gate_a",
        "gate_b",
        "private",
        "failed",
        "scheduled",
        "hold",
        "done",
        "ready_for_stills",
        "awaiting_gpu",
    }
)
INFLIGHT_STATUSES = frozenset(
    {
        "farming",
        "scripting",
        "tts",
        "visuals",
        "edit",
        "gate_a",
        "gate_b",
        "running",
    }
)

LAST_DIGEST_JSON = OPS_DIR / "last_digest.json"
LAST_DIGEST_MD = OPS_DIR / "last_digest.md"
DEFAULT_DIGEST_MAX_HOURS = 6.0


class OpsLedger:
    """Structured ops log. Always writes local ledger; emails only via batch digest."""

    def __init__(self, store: OpsStore | None = None):
        self.store = store or OpsStore()
        self.s = get_settings()

    def write(
        self,
        *,
        agent: str,
        problem: str,
        action: str,
        job_id: str | None = None,
        severity: str = "info",
        publish_status: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        subject = f"[{agent.upper()}] {severity}: {problem[:80]}"
        entry = self.store.append_ledger(
            {
                "agent": agent,
                "severity": severity,
                "problem": problem,
                "action": action,
                "job_id": job_id,
                "publish_status": publish_status,
                "subject": subject,
                "extra": extra or {},
            }
        )
        self.store.add_event(
            agent=agent,
            event_type="ledger",
            message=problem,
            job_id=job_id,
            severity=severity,
            action=action,
            payload={"publish_status": publish_status, **(extra or {})},
        )
        # Intentionally no per-entry email — use maybe_send_digest / email-digest CLI.
        return entry

    def alert_now(self, *, subject: str, body: str) -> dict[str, Any]:
        """Immediate SMTP alert for critical HOLD / money-risk events (not batch digest)."""
        out: dict[str, Any] = {"ok": False, "sent_email": False, "subject": subject}
        path = OPS_DIR / "last_critical_alert.md"
        path.write_text(f"# {subject}\n\n{body}\n", encoding="utf-8")
        out["path"] = str(path)
        if not self.smtp_configured():
            out["send_detail"] = "SMTP not configured — wrote last_critical_alert.md only"
            logger.warning("critical alert (no SMTP): %s", subject)
            return out
        sent, detail = self._send_smtp(subject, body)
        out["ok"] = sent
        out["sent_email"] = sent
        out["send_detail"] = detail
        self.store.add_event(
            agent="ledger",
            event_type="critical_alert",
            message=subject,
            severity="critical",
            action="alert_now",
            payload={"sent_email": sent, "detail": detail},
        )
        return out

    # --- SMTP / digest -------------------------------------------------

    def smtp_configured(self) -> bool:
        to_addr = (getattr(self.s, "ops_ledger_email", None) or "").strip()
        smtp_host = (getattr(self.s, "smtp_host", None) or "").strip()
        return bool(to_addr and smtp_host)

    def load_digest_state(self) -> dict[str, Any]:
        if not LAST_DIGEST_JSON.exists():
            return {
                "last_digest_at": None,
                "job_ids_included": [],
                "sent_email": False,
            }
        try:
            return json.loads(LAST_DIGEST_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {
                "last_digest_at": None,
                "job_ids_included": [],
                "sent_email": False,
            }

    def save_digest_state(self, state: dict[str, Any]) -> None:
        OPS_DIR.mkdir(parents=True, exist_ok=True)
        LAST_DIGEST_JSON.write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8"
        )

    def _farm_log_path(self, job: JobRecord) -> str:
        meta = job.meta or {}
        if meta.get("farm_log"):
            return str(meta["farm_log"])
        return str(ROOT / "output" / "ops" / f"farm_{job.id}.log")

    def _job_cost_usd(self, job: JobRecord) -> float | None:
        """Best-effort cost: spend items tagged with job_id, else retention estimate."""
        try:
            spend = self.store._read("spend.json")  # noqa: SLF001
            items = spend.get("items") or []
            tagged = [
                float(i.get("amount_usd") or 0)
                for i in items
                if i.get("job_id") == job.id
            ]
            if tagged:
                return round(sum(tagged), 2)
        except Exception:  # noqa: BLE001
            pass
        if job.status in DIGEST_TERMINAL_STATUSES | INFLIGHT_STATUSES:
            try:
                from src.agents.cost_guardian import CostGuardian

                return CostGuardian(self.store, self).estimate_video_usd(
                    profile="retention"
                )
            except Exception:  # noqa: BLE001
                return None
        return None

    def _parse_iso(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def collect_digest_jobs(
        self, *, since: datetime | None = None, include_inflight: bool = True
    ) -> dict[str, list[JobRecord]]:
        """Partition jobs for digest body."""
        terminal: list[JobRecord] = []
        inflight: list[JobRecord] = []
        other: list[JobRecord] = []
        for job in self.store.list_jobs():
            if job.status in INFLIGHT_STATUSES or (
                job.status == "queued" and (job.meta or {}).get("farm_pid")
            ):
                inflight.append(job)
            elif job.status in DIGEST_TERMINAL_STATUSES:
                updated = self._parse_iso(job.updated_at)
                if since and updated and updated <= since:
                    continue
                terminal.append(job)
            elif include_inflight and job.status in DIGEST_SNAPSHOT_STATUSES:
                other.append(job)
        return {"terminal": terminal, "inflight": inflight, "other": other}

    def new_terminal_since_digest(
        self, state: dict[str, Any] | None = None
    ) -> list[JobRecord]:
        """Terminal jobs not yet listed in ``last_digest.json`` job_ids_included."""
        state = state or self.load_digest_state()
        already = set(state.get("job_ids_included") or [])
        out: list[JobRecord] = []
        for job in self.store.list_jobs():
            if job.status not in DIGEST_TERMINAL_STATUSES:
                continue
            if job.id in already:
                continue
            out.append(job)
        return out

    def hours_since_digest(self, state: dict[str, Any] | None = None) -> float | None:
        state = state or self.load_digest_state()
        last = self._parse_iso(state.get("last_digest_at"))
        if not last:
            return None
        now = datetime.now(timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds() / 3600.0

    def has_farming_inflight(self) -> bool:
        for job in self.store.list_jobs():
            if job.status in INFLIGHT_STATUSES:
                return True
            if job.status == "queued" and (job.meta or {}).get("farm_pid"):
                return True
        return False

    def should_send_digest(
        self,
        *,
        force: bool = False,
        max_hours: float = DEFAULT_DIGEST_MAX_HOURS,
    ) -> tuple[bool, str]:
        if force:
            return True, "force"
        state = self.load_digest_state()
        new_jobs = self.new_terminal_since_digest(state)
        if not new_jobs:
            return False, "no new private/failed since last digest"
        inflight = self.has_farming_inflight()
        hours = self.hours_since_digest(state)
        if not inflight:
            return True, f"batch idle with {len(new_jobs)} new terminal job(s)"
        if hours is None:
            # Never sent; wait until idle unless somehow stuck farming forever —
            # still allow max_hours gate from "epoch" via treating as large.
            return False, "farming inflight; waiting for idle (no prior digest)"
        if hours >= max_hours:
            return True, (
                f"farming still inflight but {hours:.1f}h since last digest "
                f"(>= {max_hours}h) with {len(new_jobs)} new terminal job(s)"
            )
        return False, (
            f"farming inflight; {hours:.1f}h since last digest "
            f"(need idle or >={max_hours}h)"
        )

    def format_digest_markdown(
        self,
        *,
        reason: str = "",
        force: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        state = self.load_digest_state()
        already = set(state.get("job_ids_included") or [])

        parts = self.collect_digest_jobs(since=None, include_inflight=True)
        # Highlight jobs that are new since last digest for the "batch" section.
        new_terminal = self.new_terminal_since_digest(state)
        if force and not new_terminal:
            # Force: show all current terminal + inflight regardless of digest history.
            new_terminal = [
                j
                for j in self.store.list_jobs()
                if j.status in DIGEST_TERMINAL_STATUSES
            ]

        now = datetime.now(timezone.utc).isoformat()
        lines: list[str] = [
            "# Overnight farm digest",
            "",
            f"- Generated (UTC): `{now}`",
            f"- Trigger: {reason or ('force' if force else 'auto')}",
            f"- Last digest at: `{state.get('last_digest_at') or 'never'}`",
            f"- SMTP configured: `{self.smtp_configured()}`",
            "",
        ]

        def _job_block(job: JobRecord) -> list[str]:
            cost = self._job_cost_usd(job)
            cost_s = f"${cost:.2f}" if cost is not None else "n/a"
            log_p = self._farm_log_path(job)
            return [
                f"### {job.id}",
                f"- **Title:** {job.title}",
                f"- **Status:** `{job.status}` (stage=`{job.stage}`)",
                f"- **video_id:** `{job.video_id or '—'}`",
                f"- **Cost estimate:** {cost_s}",
                f"- **Log:** `{log_p}`",
                f"- **Job dir:** `{job.job_dir or '—'}`",
                f"- **Updated:** `{job.updated_at}`",
                "",
            ]

        lines.append(f"## New private / failed ({len(new_terminal)})")
        lines.append("")
        if not new_terminal:
            lines.append("_None since last digest._")
            lines.append("")
        else:
            for job in sorted(new_terminal, key=lambda j: j.updated_at or ""):
                lines.extend(_job_block(job))

        inflight = parts["inflight"]
        lines.append(f"## Currently farming / inflight ({len(inflight)})")
        lines.append("")
        if not inflight:
            lines.append("_None._")
            lines.append("")
        else:
            for job in inflight:
                lines.extend(_job_block(job))

        # Brief hold/other snapshot (not the spam focus)
        holds = [j for j in self.store.list_jobs() if j.status == "hold"]
        if holds:
            lines.append(f"## On hold ({len(holds)})")
            lines.append("")
            for job in holds:
                lines.append(f"- `{job.id}` — {job.title} — `{job.error or 'hold'}`")
            lines.append("")

        total_est = 0.0
        est_n = 0
        for job in new_terminal + inflight:
            c = self._job_cost_usd(job)
            if c is not None:
                total_est += c
                est_n += 1
        lines.append("## Cost rollup")
        lines.append("")
        lines.append(
            f"- Est. for listed jobs ({est_n}): **${total_est:.2f}** "
            "(retention formula; actual RunPod may vary)"
        )
        try:
            spent = self.store.month_spend_total()
            lines.append(f"- Month spend recorded: **${spent:.2f}**")
        except Exception:  # noqa: BLE001
            pass
        lines.append("")
        lines.append(
            "_One digest email per overnight batch — not per scene/chapter/warn._"
        )
        lines.append("")

        body = "\n".join(lines)
        # Only terminal (private/failed/…) ids advance dedupe — inflight may still
        # appear in the body but must not be marked "already digested" before finish.
        meta = {
            "at": now,
            "reason": reason or ("force" if force else "auto"),
            "new_terminal_ids": [j.id for j in new_terminal],
            "inflight_ids": [j.id for j in inflight],
            "job_ids_included": sorted(already | {j.id for j in new_terminal}),
            "est_usd": round(total_est, 2),
        }
        return body, meta

    def _send_smtp(self, subject: str, body: str) -> tuple[bool, str]:
        to_addr = (getattr(self.s, "ops_ledger_email", None) or "").strip()
        smtp_host = (getattr(self.s, "smtp_host", None) or "").strip()
        if not to_addr or not smtp_host:
            return False, "SMTP not configured (OPS_LEDGER_EMAIL / SMTP_HOST)"
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = getattr(self.s, "smtp_from", None) or to_addr
        msg["To"] = to_addr
        try:
            port = int(getattr(self.s, "smtp_port", 587) or 587)
            user = getattr(self.s, "smtp_user", "") or ""
            password = getattr(self.s, "smtp_password", "") or ""
            with smtplib.SMTP(smtp_host, port, timeout=30) as smtp:
                smtp.starttls()
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(msg)
            return True, f"sent to {to_addr}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("digest email failed: %s", exc)
            return False, str(exc)

    def maybe_send_digest(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        max_hours: float = DEFAULT_DIGEST_MAX_HOURS,
    ) -> dict[str, Any]:
        """Build overnight batch digest; email at most when trigger fires.

        Trigger (unless ``force`` / ``dry_run``):
          new private/failed since ``last_digest_at`` AND
          (no farming inflight OR hours since last digest >= max_hours).

        ``dry_run`` always writes ``last_digest.md`` (preview) without email/state.
        Never raises — farm pipeline must not crash on mail failures.
        """
        out: dict[str, Any] = {
            "ok": True,
            "sent_email": False,
            "wrote_file": False,
            "skipped": False,
        }
        try:
            # dry_run previews current status even with only farming / nothing new
            effective_force = force or dry_run
            should, reason = self.should_send_digest(
                force=effective_force, max_hours=max_hours
            )
            out["reason"] = reason
            if not should:
                out["skipped"] = True
                out["ok"] = True
                logger.info("email digest skipped: %s", reason)
                return out

            body, meta = self.format_digest_markdown(
                reason=reason, force=effective_force
            )
            OPS_DIR.mkdir(parents=True, exist_ok=True)
            LAST_DIGEST_MD.write_text(body, encoding="utf-8")
            out["wrote_file"] = True
            out["path"] = str(LAST_DIGEST_MD)
            out["meta"] = meta

            n_new = len(meta.get("new_terminal_ids") or [])
            n_inf = len(meta.get("inflight_ids") or [])
            subject = (
                f"[YT Factory] Overnight digest — "
                f"{n_new} finished, {n_inf} inflight"
            )
            out["subject"] = subject

            if dry_run:
                out["dry_run"] = True
                out["note"] = (
                    "dry-run: wrote markdown only, did not email or update state"
                )
                logger.info("email digest dry-run wrote %s", LAST_DIGEST_MD)
                return out

            sent = False
            send_detail = "SMTP not configured"
            if self.smtp_configured():
                sent, send_detail = self._send_smtp(subject, body)
            else:
                logger.info(
                    "digest email skipped (SMTP unset); wrote %s", LAST_DIGEST_MD
                )

            out["sent_email"] = sent
            out["send_detail"] = send_detail

            # Advance dedupe state even on SMTP failure so 15m beat doesn't retry-spam;
            # file is the durable record. Force re-send via CLI --force.
            state = {
                "last_digest_at": meta["at"],
                "job_ids_included": meta["job_ids_included"],
                "sent_email": sent,
                "subject": subject,
                "reason": reason,
                "path": str(LAST_DIGEST_MD),
                "send_detail": send_detail,
            }
            self.save_digest_state(state)
            out["state"] = state

            self.store.add_event(
                agent="ledger",
                event_type="email_digest",
                message=subject,
                severity="info",
                action="digest",
                payload={
                    "sent_email": sent,
                    "send_detail": send_detail,
                    "new_terminal_ids": meta.get("new_terminal_ids"),
                    "inflight_ids": meta.get("inflight_ids"),
                },
            )
            logger.info(
                "email digest done sent=%s detail=%s path=%s",
                sent,
                send_detail,
                LAST_DIGEST_MD,
            )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("email digest failed safely: %s", exc)
            out["ok"] = False
            out["error"] = str(exc)
            return out
