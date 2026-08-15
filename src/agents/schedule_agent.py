"""Schedule Agent — daily publishAt in audience timezone (per-channel PKT hours)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.agents.ledger import OpsLedger
from src.agents.store import OpsStore
from src.agents.title_queue import TitleQueue
from src.services.settings import CONFIG_DIR, get_settings

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL_HOURS = {
    "napstorian": 4,  # 04:00 PKT → 23:00 UTC prior day
    "napping_historian": 6,  # 06:00 PKT → 01:00 UTC
}


class ScheduleAgentError(RuntimeError):
    pass


class ScheduleAgent:
    """Assign YouTube publishAt slots: every other day at channel-local PKT hour.

    Defaults: napstorian 04:00 PKT; napping_historian 06:00 PKT (01:00 UTC).
    Cadence is Rayyan-locked at 15/mo (cadence_days=2) unless manually changed.
    SMM always auto-applies ``channels.<name>.preferred_hours`` for both channels
    (no sheet hour columns).
    """

    def __init__(
        self,
        store: OpsStore | None = None,
        ledger: OpsLedger | None = None,
        queue: TitleQueue | None = None,
    ):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)
        self.queue = queue or TitleQueue()
        self.s = get_settings()
        self.cfg = _load_schedule_config()

    @property
    def tz(self) -> ZoneInfo:
        name = (
            getattr(self.s, "publish_timezone", None)
            or self.cfg.get("timezone")
            or "Asia/Karachi"
        )
        return ZoneInfo(str(name))

    def channel_cfg(self, channel: str | None) -> dict[str, Any]:
        from src.services.youtube_channel_auth import normalize_youtube_channel

        ch = normalize_youtube_channel(channel)
        raw = (self.cfg.get("channels") or {}).get(ch) or {}
        return dict(raw) if isinstance(raw, dict) else {}

    def publish_hour_for_channel(
        self,
        channel: str | None = None,
        *,
        row: Any = None,
    ) -> int:
        """Resolve local publish hour for one channel.

        Priority:
        1. Benchmarks ``publish_hour_approved_by_channel[ch]`` (CLI set-publish-hour)
        2. ``channels[ch].preferred_hours[0]`` (SMM always-accept writes)
        3. ``channels[ch].publish_hour_local`` (channel default)
        4. Legacy global benchmarks / settings
        """
        from src.services.youtube_channel_auth import normalize_youtube_channel

        ch = normalize_youtube_channel(channel)
        _ = row  # sheet hour columns removed; keep kwarg for call-site compat

        bench = self.store.get_benchmarks()
        by_ch = bench.get("publish_hour_approved_by_channel") or {}
        if isinstance(by_ch, dict) and by_ch.get(ch) is not None:
            try:
                h = int(by_ch[ch])
                if 0 <= h <= 23:
                    return h
            except (TypeError, ValueError):
                pass

        ch_cfg = self.channel_cfg(ch)
        prefs = ch_cfg.get("preferred_hours")
        if isinstance(prefs, list) and prefs:
            try:
                h = int(prefs[0])
                if 0 <= h <= 23:
                    return h
            except (TypeError, ValueError):
                pass
        if ch_cfg.get("publish_hour_local") is not None:
            try:
                h = int(ch_cfg["publish_hour_local"])
                if 0 <= h <= 23:
                    return h
            except (TypeError, ValueError):
                pass

        if bench.get("publish_hour_approved") is not None:
            try:
                h = int(bench["publish_hour_approved"])
                if 0 <= h <= 23:
                    return h
            except (TypeError, ValueError):
                pass

        if self.cfg.get("publish_hour_local") is not None:
            try:
                return int(self.cfg["publish_hour_local"])
            except (TypeError, ValueError):
                pass

        default = _DEFAULT_CHANNEL_HOURS.get(ch, 4)
        override = getattr(self.s, "publish_hour_local", None)
        if override is not None and str(override) != "" and ch == "napstorian":
            try:
                return int(override)
            except (TypeError, ValueError):
                pass
        return int(default)

    @property
    def publish_hour(self) -> int:
        """Legacy global hour — napstorian / fallback."""
        return self.publish_hour_for_channel("napstorian")

    @property
    def cadence_days(self) -> int:
        return int(
            getattr(self.s, "publish_cadence_days", None)
            or self.cfg.get("cadence_days")
            or 1
        )

    def pkt_to_eastern_note(self, hour: int | None = None) -> str:
        """Document a PKT hour vs America/New_York (EST/EDT)."""
        h = 4 if hour is None else int(hour)
        sample = datetime(2026, 1, 15, h, 0, tzinfo=ZoneInfo("Asia/Karachi"))
        ny = sample.astimezone(ZoneInfo("America/New_York"))
        utc = sample.astimezone(timezone.utc)
        return (
            f"{h:02d}:00 Asia/Karachi (PKT) == {utc.strftime('%H:%M')} UTC == "
            f"{ny.strftime('%H:%M %Z')} America/New_York on {ny.date()}"
        )

    def next_slot(
        self,
        *,
        after: datetime | None = None,
        existing: list[datetime] | None = None,
        channel: str | None = None,
        hour: int | None = None,
    ) -> datetime:
        """Next free daily local slot for ``channel`` (timezone-aware)."""
        from src.services.youtube_channel_auth import normalize_youtube_channel

        ch = normalize_youtube_channel(channel) if channel else None
        minute = int(self.cfg.get("publish_minute_local") or 0)
        use_hour = (
            int(hour)
            if hour is not None
            else self.publish_hour_for_channel(ch or "napstorian")
        )
        tz = self.tz
        now_local = (after or datetime.now(timezone.utc)).astimezone(tz)
        cand = now_local.replace(
            hour=use_hour, minute=minute, second=0, microsecond=0
        )
        if cand <= now_local:
            cand += timedelta(days=1)

        channel_existing = (
            existing
            if existing is not None
            else self._existing_scheduled_datetimes(channel=ch)
        )
        taken = {e.astimezone(tz).date() for e in channel_existing}
        while cand.date() in taken:
            cand += timedelta(days=1)

        scheduled = sorted(channel_existing)
        if scheduled:
            latest = scheduled[-1].astimezone(tz)
            min_next = latest + timedelta(days=self.cadence_days)
            min_next = min_next.replace(
                hour=use_hour, minute=minute, second=0, microsecond=0
            )
            if cand < min_next:
                cand = min_next
            while cand.date() in taken:
                cand += timedelta(days=self.cadence_days)

        return cand

    def can_arm_schedule(
        self, *, job_id: str | None = None, public_approved: bool
    ) -> tuple[bool, str]:
        """All videos this month require public_approved (default n=15)."""
        n = int(self.cfg.get("human_approve_first_n") or 15)
        if self.cfg.get("require_public_approved_for_all", True) and not public_approved:
            return False, f"public_approved required (all {n} videos this month)"
        if not public_approved:
            approved_count = sum(
                1
                for j in self.store.list_jobs()
                if j.status in ("scheduled", "public")
            )
            if approved_count < n:
                return False, f"public_approved required ({approved_count}/{n})"
        return True, "ok"

    def assign_slot_for_job(
        self,
        *,
        job_id: str,
        video_id: str,
        public_approved: bool,
        dry_run: bool = False,
        apply_youtube: bool = True,
    ) -> dict[str, Any]:
        ok, reason = self.can_arm_schedule(job_id=job_id, public_approved=public_approved)
        if not ok:
            self.ledger.write(
                agent="schedule",
                problem=f"cannot arm schedule for {job_id}",
                action=reason,
                job_id=job_id,
                severity="warn",
                publish_status="private",
                extra={"video_id": video_id},
            )
            return {"ok": False, "reason": reason, "video_id": video_id}

        job_row = self.store.get_job(job_id)
        row = self.queue.find_by_job_id(job_id)
        ch = _resolve_job_channel(job_row, row)
        hour = self.publish_hour_for_channel(ch, row=row)
        reserved = _job_reserved_slot_local(job_row, tz=self.tz)
        if reserved is not None:
            slot_local = reserved
            hour = int(slot_local.hour)
        else:
            slot_local = self.next_slot(channel=ch, hour=hour)
        slot_utc = slot_local.astimezone(timezone.utc)
        publish_at_iso = slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        hour_note = self.pkt_to_eastern_note(hour)

        if dry_run:
            return {
                "ok": True,
                "job_id": job_id,
                "video_id": video_id,
                "channel": ch,
                "publish_hour_local": hour,
                "scheduled_at_local": slot_local.isoformat(),
                "publish_at_utc": publish_at_iso,
                "timezone": str(self.tz),
                "pkt_vs_ny": hour_note,
                "dry_run": True,
                "would_apply_youtube": bool(apply_youtube),
            }

        yt_ok = None
        if apply_youtube and not dry_run:
            try:
                from src.services.publish_youtube import PublishModule

                PublishModule(channel=ch).schedule_publish_at(
                    video_id, publish_at_iso, channel=ch
                )
                yt_ok = True
            except Exception as exc:  # noqa: BLE001
                yt_ok = False
                err_s = str(exc)
                self.ledger.write(
                    agent="schedule",
                    problem=f"YouTube publishAt failed: {exc}",
                    action="kept private — retry schedule later",
                    job_id=job_id,
                    severity="error",
                    extra={
                        "video_id": video_id,
                        "publish_at": publish_at_iso,
                        "channel": ch,
                        "publish_hour_local": hour,
                    },
                )
                # Surface failure on the sheet so public_approved≠scheduled isn't silent.
                if row is not None:
                    try:
                        short = err_s
                        if "videoNotFound" in err_s or "cannot be found" in err_s:
                            short = (
                                f"schedule FAIL: YouTube videoNotFound {video_id} "
                                "(deleted or wrong id) — re-upload required"
                            )
                        elif "403" in err_s or "insufficient" in err_s.lower():
                            short = (
                                "schedule FAIL: OAuth insufficient scopes — "
                                "re-auth with force-ssl"
                            )
                        else:
                            short = f"schedule FAIL: {err_s[:180]}"
                        self.queue.update_row(
                            row.row_index,
                            notes=short,
                            channel=getattr(row, "channel", None) or ch,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return {
                    "ok": False,
                    "reason": err_s,
                    "scheduled_at_local": slot_local.isoformat(),
                    "publish_at_utc": publish_at_iso,
                    "video_id": video_id,
                    "channel": ch,
                    "publish_hour_local": hour,
                }

        prev_meta = (
            (job_row.meta if job_row and isinstance(job_row.meta, dict) else {}) or {}
        )
        self.store.update_job(
            job_id,
            status="scheduled",
            stage="scheduled",
            video_id=video_id,
            meta={
                **prev_meta,
                "channel": ch,
                "sheet_tab": prev_meta.get("sheet_tab") or ch,
                "scheduled_at_local": slot_local.isoformat(),
                "publish_at_utc": publish_at_iso,
                "publish_hour_local": hour,
                "timezone": str(self.tz),
            },
        )
        if row:
            drop_info: dict[str, Any] | None = None
            try:
                from src.agents.sheet_hygiene import archive_drop_sheet_row

                # Stamp scheduled fields into the archive payload, then remove
                # from the idea queue (ops job retains publishAt / status).
                drop_info = archive_drop_sheet_row(
                    self.queue,
                    row,
                    channel=getattr(row, "channel", None) or ch,
                    extra={
                        "status": "scheduled",
                        "video_id": video_id,
                        "scheduled_at": slot_local.isoformat(),
                        "hygiene_reason": "schedule_arm",
                    },
                    dry_run=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "schedule_arm sheet drop failed for %s: %s — falling back to status=scheduled",
                    job_id,
                    exc,
                )
                self.queue.update_row(
                    row.row_index,
                    status="scheduled",
                    video_id=video_id,
                    scheduled_at=slot_local.isoformat(),
                    channel=getattr(row, "channel", None) or ch,
                )
                drop_info = {"ok": False, "error": str(exc)[:200]}
        else:
            drop_info = None

        self.ledger.write(
            agent="schedule",
            problem=f"armed publishAt {publish_at_iso}",
            action=f"slot {slot_local.isoformat()} ({self.tz} hour={hour})",
            job_id=job_id,
            severity="info",
            publish_status="scheduled",
            extra={
                "video_id": video_id,
                "channel": ch,
                "publish_hour_local": hour,
                "pkt_vs_ny": hour_note,
                "youtube_updated": yt_ok,
                "sheet_dropped": bool(drop_info and drop_info.get("ok")),
            },
        )
        result = {
            "ok": True,
            "job_id": job_id,
            "video_id": video_id,
            "channel": ch,
            "publish_hour_local": hour,
            "scheduled_at_local": slot_local.isoformat(),
            "publish_at_utc": publish_at_iso,
            "timezone": str(self.tz),
            "pkt_vs_ny": hour_note,
            "dry_run": dry_run,
        }
        if drop_info is not None:
            result["sheet_drop"] = drop_info
        return result

    def status(self) -> dict[str, Any]:
        channels = ("napstorian", "napping_historian")
        by_ch: dict[str, Any] = {}
        for ch in channels:
            existing = self._existing_scheduled_datetimes(channel=ch)
            hour = self.publish_hour_for_channel(ch)
            nxt = self.next_slot(existing=existing, channel=ch, hour=hour)
            by_ch[ch] = {
                "publish_hour_local": hour,
                "preferred_hours": list(
                    (self.channel_cfg(ch).get("preferred_hours") or [hour])
                ),
                "scheduled_count": len(existing),
                "next_slot_local": nxt.isoformat(),
                "pkt_vs_ny": self.pkt_to_eastern_note(hour),
            }
        existing_all = self._existing_scheduled_datetimes()
        return {
            "timezone": str(self.tz),
            "publish_hour_local": self.publish_hour,
            "channels": by_ch,
            "cadence_days": self.cadence_days,
            "human_approve_first_n": self.cfg.get("human_approve_first_n"),
            "require_public_approved_for_all": self.cfg.get(
                "require_public_approved_for_all", True
            ),
            "buffer_target": self.cfg.get("buffer_target"),
            "buffer_count": self.queue.count_buffer(),
            "scheduled_count": len(existing_all),
            "next_slot_local": by_ch["napstorian"]["next_slot_local"],
            "pkt_vs_ny": self.pkt_to_eastern_note(self.publish_hour),
        }

    def apply_approved_hour_override(
        self, hour: int, *, channel: str | None = None
    ) -> dict[str, Any]:
        """Set publish hour for a channel (CLI). Always applies — no sheet approve gate."""
        from src.services.youtube_channel_auth import normalize_youtube_channel

        hour = int(hour)
        if hour < 0 or hour > 23:
            raise ScheduleAgentError("hour must be 0-23")
        bench = self.store.get_benchmarks()
        ch = normalize_youtube_channel(channel) if channel else None
        if ch:
            by_ch = dict(bench.get("publish_hour_approved_by_channel") or {})
            by_ch[ch] = hour
            bench["publish_hour_approved_by_channel"] = by_ch
            proposed = dict(bench.get("publish_hour_proposed_by_channel") or {})
            proposed[ch] = hour
            bench["publish_hour_proposed_by_channel"] = proposed
            self.write_channel_preferred_hours(
                ch, [hour], source="cli_set_publish_hour", soft=False
            )
        else:
            bench["publish_hour_approved"] = hour
            bench["publish_hour_proposed"] = hour
        self.store.save_benchmarks(bench)
        self.ledger.write(
            agent="schedule",
            problem=f"publish hour set to {hour}:00 {self.tz}"
            + (f" channel={ch}" if ch else " (global)"),
            action="future slots use new hour",
            severity="info",
        )
        return {
            "publish_hour_local": hour,
            "channel": ch,
            "timezone": str(self.tz),
        }

    def write_channel_preferred_hours(
        self,
        channel: str,
        hours: list[int],
        *,
        source: str = "smm",
        soft: bool = True,
    ) -> dict[str, Any]:
        """Persist ``preferred_hours`` for one channel into publish_schedule.json."""
        from src.services.youtube_channel_auth import normalize_youtube_channel

        ch = normalize_youtube_channel(channel)
        cleaned: list[int] = []
        for h in hours:
            try:
                hi = int(h)
            except (TypeError, ValueError):
                continue
            if 0 <= hi <= 23 and hi not in cleaned:
                cleaned.append(hi)
        if not cleaned:
            raise ScheduleAgentError("preferred_hours must include 0-23")

        path = CONFIG_DIR / "publish_schedule.json"
        cfg = _load_schedule_config()
        channels = dict(cfg.get("channels") or {})
        entry = dict(channels.get(ch) or {})
        prev = list(entry.get("preferred_hours") or [])
        if soft:
            max_delta = int(cfg.get("smm_hour_soft_max_delta") or 3)
            current = (
                int(prev[0])
                if prev
                else int(
                    entry.get("publish_hour_local")
                    or _DEFAULT_CHANNEL_HOURS.get(ch, 4)
                )
            )
            if abs(cleaned[0] - current) > max_delta:
                return {
                    "ok": False,
                    "applied": False,
                    "reason": "outside_soft_window",
                    "channel": ch,
                    "proposed": cleaned,
                    "current": current,
                    "max_delta": max_delta,
                }

        entry["preferred_hours"] = cleaned
        entry["publish_hour_local"] = cleaned[0]
        entry["preferred_hours_updated_at"] = datetime.now(timezone.utc).isoformat()
        entry["preferred_hours_source"] = source
        channels[ch] = entry
        cfg["channels"] = channels
        # Keep global legacy field aligned with napstorian for older readers
        if ch == "napstorian":
            cfg["publish_hour_local"] = cleaned[0]
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        self.cfg = cfg
        return {
            "ok": True,
            "applied": True,
            "channel": ch,
            "preferred_hours": cleaned,
            "path": str(path),
            "source": source,
        }

    def write_cadence_days(
        self,
        cadence_days: int,
        *,
        source: str = "ceo_competitors",
        channel: str | None = None,
        videos_per_month_target: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Update cadence_days globally and optionally per-channel.

        Cadence is shared in publish_schedule.json; per-channel override is also
        stamped on ``channels.<ch>.cadence_days`` for CEO/SMM readers.

        When ``frequency_locked`` is true (Rayyan 15/mo lock), refuses writes unless
        ``force=True`` or ``source`` starts with ``rayyan``.
        """
        from src.services.youtube_channel_auth import normalize_youtube_channel

        days = max(1, min(14, int(cadence_days)))
        path = CONFIG_DIR / "publish_schedule.json"
        cfg = _load_schedule_config()
        prev = int(cfg.get("cadence_days") or 1)
        locked = bool(cfg.get("frequency_locked"))
        source_s = str(source or "")
        rayyan_ok = source_s.startswith("rayyan")
        if locked and not force and not rayyan_ok:
            return {
                "ok": True,
                "applied": False,
                "reason": "frequency_locked",
                "cadence_days": int(cfg.get("cadence_days") or days),
                "previous": prev,
                "channel": channel,
                "source": cfg.get("cadence_source") or source_s,
                "videos_per_month_target": cfg.get("videos_per_month_target"),
                "frequency_locked": True,
            }
        cfg["cadence_days"] = days
        cfg["cadence_updated_at"] = datetime.now(timezone.utc).isoformat()
        cfg["cadence_source"] = source
        if videos_per_month_target is not None:
            cfg["videos_per_month_target"] = max(1, int(videos_per_month_target))
        if channel:
            ch = normalize_youtube_channel(channel)
            channels = dict(cfg.get("channels") or {})
            entry = dict(channels.get(ch) or {})
            entry["cadence_days"] = days
            entry["cadence_updated_at"] = cfg["cadence_updated_at"]
            entry["cadence_source"] = source
            if videos_per_month_target is not None:
                entry["videos_per_month_target"] = max(1, int(videos_per_month_target))
            channels[ch] = entry
            cfg["channels"] = channels
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        self.cfg = cfg
        return {
            "ok": True,
            "applied": True,
            "cadence_days": days,
            "previous": prev,
            "channel": channel,
            "source": source,
            "videos_per_month_target": cfg.get("videos_per_month_target"),
        }

    def _existing_scheduled_datetimes(
        self, *, channel: str | None = None
    ) -> list[datetime]:
        from src.services.youtube_channel_auth import normalize_youtube_channel

        want = normalize_youtube_channel(channel) if channel else None
        out: list[datetime] = []
        seen: set[str] = set()

        def _add(dt: datetime) -> None:
            key = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if key in seen:
                return
            seen.add(key)
            out.append(dt)

        for row in self.queue.list_rows():
            if want:
                row_ch = normalize_youtube_channel(
                    getattr(row, "channel", None) or None
                )
                if row_ch != want:
                    continue
            if row.scheduled_at:
                try:
                    dt = datetime.fromisoformat(
                        row.scheduled_at.replace("Z", "+00:00")
                    )
                    _add(dt)
                except ValueError:
                    continue
        for job in self.store.list_jobs():
            meta = job.meta or {}
            if want:
                job_ch = normalize_youtube_channel(
                    meta.get("channel") or meta.get("sheet_tab") or None
                )
                if job_ch != want:
                    continue
            raw = (
                meta.get("scheduled_at_local")
                or meta.get("force_publish_at_utc")
                or meta.get("reserved_publish_at_utc")
                or meta.get("publish_at_utc")
            )
            if not raw:
                continue
            try:
                _add(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
            except ValueError:
                continue
        # Ops reserved slots (farm_priority fresh uploads, human holds, …)
        for dt, slot_ch in _load_reserved_publish_slots():
            if want and slot_ch and slot_ch != want:
                continue
            _add(dt)
        return out



_REAUTH_CMD = (
    ".venv/bin/python -m src.cli.youtube_auth --channel <CHANNEL> --print-url"
)
_REAUTH_DOCS = (
    "src/cli/youtube_auth.py — AUTH_SCOPES = youtube.upload + youtube.force-ssl; "
    "videos().update(part=status) / publishAt needs force-ssl (upload-only token → 403). "
    "Per-channel tokens: napstorian→config/youtube_token.json; "
    "napping_historian→config/youtube_token_napping_historian.json."
)
_COALESCE_PATH_NAME = "schedule_arm_coalesce.json"


def _resolve_job_channel(job: Any, row: Any = None) -> str:
    """Prefer job.meta.channel / sheet_tab, then TitleQueue row channel."""
    from src.services.youtube_channel_auth import normalize_youtube_channel

    meta = getattr(job, "meta", None) if job is not None else None
    raw = ""
    if isinstance(meta, dict):
        raw = (meta.get("channel") or meta.get("sheet_tab") or "").strip()
    if not raw and row is not None:
        raw = (getattr(row, "channel", None) or "").strip()
    return normalize_youtube_channel(raw or None)


def _job_reserved_slot_local(
    job: Any, *, tz: ZoneInfo
) -> datetime | None:
    """Fixed publish slot from job meta (farm_priority / human lock).

    Honors ``force_publish_at_utc``, ``reserved_publish_at_utc``, or locked
    ``scheduled_at_local`` / ``publish_at_utc`` when ``schedule_slot_locked``.
    """
    meta = getattr(job, "meta", None) if job is not None else None
    if not isinstance(meta, dict):
        return None
    raw = meta.get("force_publish_at_utc") or meta.get("reserved_publish_at_utc")
    if not raw and meta.get("schedule_slot_locked"):
        raw = meta.get("scheduled_at_local") or meta.get("publish_at_utc")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _load_reserved_publish_slots() -> list[tuple[datetime, str | None]]:
    """Read ``output/ops/reserved_publish_slots.json`` → (dt, channel|None)."""
    from src.agents.store import OPS_DIR
    from src.services.youtube_channel_auth import normalize_youtube_channel

    path = OPS_DIR / "reserved_publish_slots.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    slots = data.get("slots") if isinstance(data, dict) else None
    if not isinstance(slots, list):
        return []
    out: list[tuple[datetime, str | None]] = []
    for row in slots:
        if not isinstance(row, dict):
            continue
        raw = row.get("scheduled_at_local") or row.get("publish_at_utc")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ch_raw = (row.get("channel") or "").strip() or None
        ch = normalize_youtube_channel(ch_raw) if ch_raw else None
        out.append((dt, ch))
    return out


def _oauth_fix_payload(
    err: str, *, channel: str | None = None
) -> dict[str, Any] | None:
    low = (err or "").lower()
    hit = (
        ("insufficient" in low and "scope" in low)
        or ("403" in low and "scope" in low)
        or "force-ssl" in low
        or "re-auth" in low
        or "oauth scope" in low
        or "needs youtube.force-ssl" in low
    )
    if not hit:
        return None
    try:
        from src.services.youtube_channel_auth import (
            normalize_youtube_channel,
            youtube_token_path,
        )

        ch = normalize_youtube_channel(channel)
        token = str(youtube_token_path(ch))
    except Exception:  # noqa: BLE001
        ch = (channel or "napstorian").strip() or "napstorian"
        token = (
            "config/youtube_token.json"
            if ch == "napstorian"
            else f"config/youtube_token_{ch}.json"
        )
    return {
        "oauth_insufficient_scopes": True,
        "channel": ch,
        "token_path": token,
        "reauth_command": (
            f".venv/bin/python -m src.cli.youtube_auth --channel {ch} --print-url"
        ),
        "reauth_docs": _REAUTH_DOCS,
        "fix": (
            f"YouTube OAuth token for {ch} lacks publish/manage scopes for "
            f"videos.update(part=status). Re-auth with AUTH_SCOPES "
            f"(--channel {ch}), then ensure {token} on the VPS is the new token. "
            "Note: napstorian currently often has upload-only; historian already "
            "has force-ssl when auth completed with AUTH_SCOPES."
        ),
    }


def maybe_arm_public_approved_schedule(
    *,
    dry_run: bool = False,
    force: bool = False,
    apply_youtube: bool | None = None,
    coalesce_minutes: float | None = None,
    store: OpsStore | None = None,
    ledger: OpsLedger | None = None,
    queue: TitleQueue | None = None,
) -> dict[str, Any]:
    """Arm publishAt for private jobs that already have public_approved=TRUE.

    Shared by */10 runpod_watchdog and */15 sleep_factory. Never sets
    public_approved — only arms rows/jobs already approved. Coalesces so dual
    beats do not double-hit YouTube for the same videos within ~10 minutes.
    """
    from src.agents.store import OPS_DIR

    store = store or OpsStore()
    ledger = ledger or OpsLedger(store)
    queue = queue or TitleQueue()
    sched = ScheduleAgent(store, ledger, queue)

    if apply_youtube is None:
        apply_youtube = not dry_run
    coalesce_m = (
        float(coalesce_minutes)
        if coalesce_minutes is not None
        else float((sched.cfg or {}).get("arm_coalesce_minutes") or 9)
    )

    coalesce_path = OPS_DIR / _COALESCE_PATH_NAME
    now = datetime.now(timezone.utc)
    if not force and not dry_run and coalesce_m > 0 and coalesce_path.exists():
        try:
            prev = json.loads(coalesce_path.read_text(encoding="utf-8"))
            raw_at = prev.get("at") or prev.get("ts")
            if raw_at:
                at = datetime.fromisoformat(str(raw_at).replace("Z", "+00:00"))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                age = now - at.astimezone(timezone.utc)
                if age < timedelta(minutes=coalesce_m):
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": "quota_coalesce",
                        "coalesce_minutes": coalesce_m,
                        "age_seconds": int(age.total_seconds()),
                        "eligible_count": int(prev.get("eligible_count") or 0),
                        "schedule_status": sched.status(),
                        "last_at": raw_at,
                        "armed": [],
                    }
        except Exception as exc:  # noqa: BLE001
            logger.info("schedule_arm coalesce check failed: %s", exc)

    eligible: list[dict[str, Any]] = []
    for job in store.list_jobs(status="private"):
        meta = job.meta or {}
        row = queue.find_by_job_id(job.id)
        public_ok = bool(meta.get("public_approved")) or (
            bool(getattr(row, "public_approved", False)) if row else False
        )
        if not public_ok or not job.video_id:
            continue
        ch = _resolve_job_channel(job, row)
        eligible.append(
            {
                "job_id": job.id,
                "video_id": job.video_id,
                "title": getattr(job, "title", None) or (meta.get("title") if meta else None),
                "channel": ch,
            }
        )

    status = sched.status()
    out: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "apply_youtube": bool(apply_youtube),
        "eligible_count": len(eligible),
        "eligible": eligible[:20],
        "schedule_status": status,
        "armed": [],
        "armed_ok_by_channel": {},
        "errors": 0,
        "oauth_insufficient_scopes": False,
    }

    if not eligible:
        out["message"] = "no private+public_approved+video_id jobs"
        if not dry_run:
            try:
                coalesce_path.write_text(
                    json.dumps(
                        {
                            "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "eligible_count": 0,
                            "armed_ok": 0,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
        return out

    armed: list[dict[str, Any]] = []
    oauth_hit = False
    for item in eligible:
        try:
            result = sched.assign_slot_for_job(
                job_id=item["job_id"],
                video_id=item["video_id"],
                public_approved=True,
                dry_run=bool(dry_run),
                apply_youtube=bool(apply_youtube) and not dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            result = {
                "ok": False,
                "job_id": item["job_id"],
                "channel": item.get("channel"),
                "error": str(exc)[:400],
            }
        if not result.get("channel"):
            result = {**result, "channel": item.get("channel")}
        hint = _oauth_fix_payload(
            str(result.get("reason") or result.get("error") or ""),
            channel=(result.get("channel") or item.get("channel")),
        )
        if hint:
            oauth_hit = True
            result = {**result, **hint}
            out["oauth_fix"] = hint
            out["reauth_command"] = hint["reauth_command"]
            out["reauth_docs"] = hint["reauth_docs"]
        if result.get("ok") is False or result.get("error"):
            out["errors"] = int(out["errors"]) + 1
        armed.append(result)

    out["armed"] = armed
    out["armed_ok"] = sum(1 for a in armed if a.get("ok") is True)
    by_ch: dict[str, int] = {}
    for a in armed:
        if a.get("ok") is not True:
            continue
        ch = str(a.get("channel") or "napstorian")
        by_ch[ch] = int(by_ch.get(ch) or 0) + 1
    out["armed_ok_by_channel"] = by_ch

    # Consume-replace: +1 invent credit per successful arm (harvest on same/next beat).
    # Credit whenever the job actually moved to scheduled (not dry_run). Local-only
    # arms (apply_youtube=False) still consume a public_approved slot.
    if by_ch and not dry_run:
        try:
            from src.agents.idea_stock import credit_consume_refills

            out["refill_credits"] = credit_consume_refills(by_ch, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("schedule_arm refill credit failed: %s", exc)
            out["refill_credits_error"] = str(exc)[:200]
    elif by_ch and dry_run:
        out["refill_credits_note"] = (
            "dry_run — would credit "
            + ", ".join(f"{k}+{v}" for k, v in sorted(by_ch.items()))
        )

    out["oauth_insufficient_scopes"] = oauth_hit
    if oauth_hit:
        out["ok"] = False
        out["message"] = (
            "schedule arm blocked by YouTube OAuth scopes — "
            f"run `{_REAUTH_CMD}` (see {_REAUTH_DOCS})"
        )
    else:
        out["message"] = (
            f"armed {out['armed_ok']}/{len(eligible)} "
            f"(apply_youtube={bool(apply_youtube) and not dry_run})"
        )

    # Refresh status after any successful local/slot updates
    out["schedule_status"] = sched.status()

    if not dry_run:
        try:
            coalesce_path.write_text(
                json.dumps(
                    {
                        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "eligible_count": len(eligible),
                        "armed_ok": out.get("armed_ok"),
                        "armed_ok_by_channel": by_ch,
                        "errors": out.get("errors"),
                        "oauth_insufficient_scopes": oauth_hit,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("schedule_arm coalesce write failed: %s", exc)

    return out


def _load_schedule_config() -> dict[str, Any]:
    path = CONFIG_DIR / "publish_schedule.json"
    if not path.exists():
        return {
            "timezone": "Asia/Karachi",
            "publish_hour_local": 4,
            "frequency_locked": True,
            "frequency_lock_source": "rayyan_fixed_15_per_month",
            "cadence_days": 2,
            "videos_per_month_target": 15,
            "human_approve_first_n": 15,
            "require_public_approved_for_all": True,
            "smm_hour_soft_max_delta": 3,
            "channels": {
                "napstorian": {
                    "publish_hour_local": 4,
                    "preferred_hours": [4],
                    "frequency_locked": True,
                    "cadence_days": 2,
                    "videos_per_month_target": 15,
                    "cadence_source": "rayyan_fixed_15_per_month",
                },
                "napping_historian": {
                    "publish_hour_local": 6,
                    "preferred_hours": [6],
                    "frequency_locked": True,
                    "cadence_days": 2,
                    "videos_per_month_target": 15,
                    "cadence_source": "rayyan_fixed_15_per_month",
                },
            },
            "cadence_source": "rayyan_fixed_15_per_month",
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    # Ensure channel defaults exist even on older configs
    channels = dict(data.get("channels") or {})
    if "napstorian" not in channels:
        channels["napstorian"] = {
            "publish_hour_local": int(data.get("publish_hour_local") or 4),
            "preferred_hours": [int(data.get("publish_hour_local") or 4)],
        }
    if "napping_historian" not in channels:
        channels["napping_historian"] = {
            "publish_hour_local": 6,
            "preferred_hours": [6],
        }
    data["channels"] = channels
    return data
