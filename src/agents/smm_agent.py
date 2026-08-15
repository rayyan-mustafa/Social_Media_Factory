"""Social Media Manager — public farm videos + channel-winner learning.

Under SMM command (from config/agents_settings.json):
- watch_only_public=true → ignore private; start when YT privacy=public
- pin_comment_on_upload=true → pin engagement comment on public go-live
- learn_from_channel_winners → pull top channel videos into benchmarks (positive)
- smm.evergreen_bias → tag winners evergreen vs ephemeral; harvest prefers lasting themes
- auto_reply_comments=false → draft only
- voice_change_requires_approve=true → propose voice only
- smm.auto_apply_soft_packaging → rewrite packaging when CTR fails (live metrics)
- smm.auto_apply_schedule_hours → always write preferred_hours into publish_schedule.json
  (per-channel; proposals are always accepted — no publish_hour_approved gate)
- smm.daily_scorecard → once/day YT quality digest (CTR/AVD/views/Live)
- smm.emit_image_reuse_packs → Pinterest/IG/FB stills packs (QUALITY FREEZE side track)
- smm.reuse_freeze_video_heavy → block podcast/shorts-video derivative enqueue
- smm.enforce_new_format_sop → soft SOP compliance alerts (type sop_compliance);
  smm.sop_block_publish=false by default (never blocks Live mid-stream)
- smm.sop_stage_gates=true → per-stage SOP audit + ops/SOP_COMPLIANCE.md;
  smm.sop_stage_gates_hard=false by default (soft remediation / resend_stage only)

Wired from: sleep_factory */15 + runpod_watchdog */10 (scan_and_act) +
stream_beat */1 (watch_live_streams every ~5m).
Quota coalesce (~10m) shared across both beats. Quality alerts ->
output/ops/smm_quality_alerts.md. Daily scorecard ->
output/ops/smm_yt_scorecard.md (+ jsonl). Not started on private upload.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from src.agents.ledger import OpsLedger
from src.agents.store import AlgoInsight, JobRecord, OpsStore
from src.services.settings import CONFIG_DIR, get_settings

logger = logging.getLogger(__name__)

DEFAULT_PIN = (
    "What alternate timeline should we explore next? Drop your 'What If…' below "
    "— the best idea may become the next documentary."
)


def _iso8601_duration_seconds(raw: str | None) -> float | None:
    """Parse YouTube contentDetails.duration (ISO-8601) → seconds."""
    if not raw or not isinstance(raw, str):
        return None
    import re

    m = re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.\d+)?S)?",
        raw.strip().upper(),
    )
    if not m:
        return None
    days, hours, mins, secs = (int(x or 0) for x in m.groups())
    return float(days * 86400 + hours * 3600 + mins * 60 + secs)


def _agents_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None



def _smm_last_scan_path() -> Path:
    from src.agents.store import OPS_DIR

    return OPS_DIR / "smm_last_scan.json"


def _smm_quality_alerts_path() -> Path:
    from src.agents.store import OPS_DIR

    return OPS_DIR / "smm_quality_alerts.md"


def _smm_yt_scorecard_path() -> Path:
    from src.agents.store import OPS_DIR

    return OPS_DIR / "smm_yt_scorecard.md"


def _smm_yt_scorecard_jsonl_path() -> Path:
    from src.agents.store import OPS_DIR

    return OPS_DIR / "smm_yt_scorecard.jsonl"


def _smm_scorecard_stamp_path() -> Path:
    from src.agents.store import OPS_DIR

    return OPS_DIR / "smm_yt_scorecard_last_day.txt"


def _smm_pinned_videos_path() -> Path:
    """Durable once-per-video claim so pin comments never spam on re-onboard."""
    from src.agents.store import OPS_DIR

    return OPS_DIR / "smm_pinned_videos.json"


def _load_pinned_videos() -> dict[str, Any]:
    path = _smm_pinned_videos_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _pin_already_claimed(video_id: str) -> dict[str, Any] | None:
    vid = (video_id or "").strip()
    if not vid:
        return None
    entry = _load_pinned_videos().get(vid)
    return entry if isinstance(entry, dict) else None


def _claim_pin_video(
    video_id: str,
    *,
    channel: str = "",
    comment_id: str | None = None,
    text: str = "",
    status: str = "claimed",
) -> dict[str, Any]:
    """Record a pin attempt/result for video_id (idempotent overwrite of same key)."""
    from src.agents.store import OPS_DIR

    vid = (video_id or "").strip()
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    path = _smm_pinned_videos_path()
    data = _load_pinned_videos()
    entry = {
        "video_id": vid,
        "channel": channel or "",
        "comment_id": comment_id,
        "text": (text or "")[:500],
        "status": status,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    prev = data.get(vid) if isinstance(data.get(vid), dict) else None
    if prev and prev.get("comment_id") and not comment_id:
        entry["comment_id"] = prev.get("comment_id")
    data[vid] = entry
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return entry


class SocialMediaManager:
    """Watch public farm videos; learn winners; soft packaging when armed.

    Dual-channel parity: napstorian + napping_historian use the same Live
    top-view / underperform-swap job with per-channel OAuth token,
    competitors file, sheet tab, and ``playlist_<channel>.txt``.
    """

    def __init__(self, store: OpsStore | None = None, ledger: OpsLedger | None = None):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)
        self.s = get_settings()
        self.cfg = _agents_cfg()
        self.smm_cfg = dict(self.cfg.get("smm") or {})

    # --------------------------------------------------------- dual-channel
    def _smm_live_channels(self) -> tuple[str, ...]:
        """Channels that get Live watch + top-view swap (same job each)."""
        try:
            from src.streaming.vod_loop import CHANNELS

            return tuple(CHANNELS)
        except Exception:  # noqa: BLE001
            pass
        try:
            from src.agents.sheet_channels import configured_sheet_channels

            return tuple(configured_sheet_channels())
        except Exception:  # noqa: BLE001
            return ("napstorian", "napping_historian")

    @staticmethod
    def _normalize_channel(channel: str | None) -> str:
        from src.services.youtube_channel_auth import normalize_youtube_channel

        return normalize_youtube_channel(channel)

    def _job_channel(self, job: JobRecord | None) -> str:
        meta = dict((job.meta if job else {}) or {})
        return self._normalize_channel(
            meta.get("channel") or meta.get("sheet_tab") or meta.get("youtube_channel")
        )

    def _youtube_channel_id_for(self, channel: str | None) -> str:
        """Resolve Brand Account UC… id for a sheet/Live channel (no secrets)."""
        ch = self._normalize_channel(channel)
        # 1) sheet_channels[].youtube_channel_id
        try:
            from src.agents.sheet_channels import channel_youtube_id

            cid = (channel_youtube_id(ch) or "").strip()
            if cid:
                return cid
        except Exception:  # noqa: BLE001
            pass
        # 2) smm.youtube_channel_ids map
        ids = self.smm_cfg.get("youtube_channel_ids") or {}
        if isinstance(ids, dict):
            cid = str(ids.get(ch) or "").strip()
            if cid:
                return cid
        # 3) legacy single id for napstorian only
        if ch in {"napstorian", "default"}:
            cid = str(self.smm_cfg.get("youtube_channel_id") or "").strip()
            if cid:
                return cid
        # 4) env override
        env_cid = (
            os.getenv(f"YOUTUBE_CHANNEL_ID_{ch.upper().replace('-', '_')}") or ""
        ).strip()
        if env_cid:
            return env_cid
        # 5) ops cache (public UC ids written after OAuth mine=True)
        try:
            from src.agents.store import OPS_DIR

            cache = OPS_DIR / "smm_youtube_channel_ids.json"
            if cache.is_file():
                data = json.loads(cache.read_text(encoding="utf-8"))
                cid = str((data.get("channels") or {}).get(ch) or "").strip()
                if cid:
                    return cid
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _cache_youtube_channel_id(self, channel: str, channel_id: str) -> None:
        cid = (channel_id or "").strip()
        if not cid:
            return
        ch = self._normalize_channel(channel)
        try:
            from src.agents.store import OPS_DIR

            OPS_DIR.mkdir(parents=True, exist_ok=True)
            path = OPS_DIR / "smm_youtube_channel_ids.json"
            data: dict[str, Any] = {"channels": {}}
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    data = {"channels": {}}
            channels = dict(data.get("channels") or {})
            if channels.get(ch) == cid:
                return
            channels[ch] = cid
            data = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "channels": channels,
                "note": "Public YouTube channel IDs only (no tokens).",
            }
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.info("smm channel id cache failed: %s", exc)

    def _build_youtube(self, channel: str | None = None):
        """OAuth YouTube client for the given Brand Account token file."""
        from src.services.publish_youtube import PublishModule

        return PublishModule(channel=self._normalize_channel(channel))._build_youtube_client()

    def _channel_winners_payload(self, channel: str | None) -> dict[str, Any]:
        """Per-channel winners; fall back to legacy ``channel_winners`` for napstorian."""
        ch = self._normalize_channel(channel)
        bench = self.store.get_benchmarks()
        by_ch = bench.get("channel_winners_by_channel") or {}
        if isinstance(by_ch, dict):
            row = by_ch.get(ch)
            if isinstance(row, dict) and (row.get("top") or row.get("updated_at")):
                return row
        legacy = bench.get("channel_winners") or {}
        if ch in {"napstorian", "default"} and isinstance(legacy, dict):
            return legacy
        return {}

    # ------------------------------------------------------------------ beat
    def scan_and_act(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Beat hook: learn winners, then watch farm jobs that are live-public.

        Quota coalesce (~10m default) shared by */10 watchdog + */15 sleep_factory
        so both beats do not double-spend YouTube quota.
        """
        if self.smm_cfg.get("enabled", True) is False:
            return [{"skipped": True, "reason": "smm disabled"}]

        coalesce_m = float(self.smm_cfg.get("quota_coalesce_minutes") or 10)
        last_path = _smm_last_scan_path()
        if not force and coalesce_m > 0:
            try:
                prev = (
                    json.loads(last_path.read_text(encoding="utf-8"))
                    if last_path.exists()
                    else {}
                )
                last_at = _parse_iso(prev.get("at"))
                if last_at is not None:
                    age = datetime.now(timezone.utc) - last_at
                    if age < timedelta(minutes=coalesce_m):
                        return [
                            {
                                "skipped": True,
                                "reason": "quota_coalesce",
                                "last_at": prev.get("at"),
                                "coalesce_minutes": coalesce_m,
                                "age_seconds": int(age.total_seconds()),
                            }
                        ]
            except Exception as exc:  # noqa: BLE001
                logger.info("smm coalesce check failed: %s", exc)

        results: list[dict[str, Any]] = []
        try:
            results.append({"live_streams": self.watch_live_streams()})
        except Exception as exc:  # noqa: BLE001
            logger.exception("smm live stream watch failed")
            results.append({"live_streams": {"ok": False, "error": str(exc)}})
        try:
            results.append({"channel_winners": self.maybe_learn_channel_winners()})
        except Exception as exc:  # noqa: BLE001
            logger.exception("smm winner learn failed")
            results.append({"channel_winners": {"ok": False, "error": str(exc)}})

        for job in self.store.list_jobs():
            if job.status not in {"private", "scheduled", "public"}:
                continue
            if not job.video_id:
                continue
            try:
                results.append(self._process_job(job))
            except Exception as exc:  # noqa: BLE001
                logger.exception("smm scan failed job=%s", job.id)
                results.append({"job_id": job.id, "ok": False, "error": str(exc)})

        try:
            results.append({"image_reuse": self.maybe_emit_image_reuse_packs()})
        except Exception as exc:  # noqa: BLE001
            logger.exception("smm image reuse packs failed")
            results.append({"image_reuse": {"ok": False, "error": str(exc)}})

        try:
            results.append({"daily_scorecard": self.maybe_write_daily_yt_scorecard()})
        except Exception as exc:  # noqa: BLE001
            logger.exception("smm daily scorecard failed")
            results.append({"daily_scorecard": {"ok": False, "error": str(exc)}})

        try:
            touched = sum(1 for r in results if isinstance(r, dict) and r.get("job_id"))
            last_path.parent.mkdir(parents=True, exist_ok=True)
            last_path.write_text(
                json.dumps(
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "jobs_touched": touched,
                        "n_results": len(results),
                        "source": "scan_and_act",
                    },
                    indent=2,
                )
                + chr(10),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("smm last_scan persist failed: %s", exc)
        return results

    # ------------------------------------------------------------------ Live
    def watch_live_streams(self) -> dict[str, Any]:
        """Check vod_loop health / performance; nudge repair + re-live when needed.

        Called from stream_beat (~15m) and scan_and_act. Never prints RTMP keys.
        Restarts prefer systemd ``vod-loop@CHANNEL`` via ``relive_channel``.
        Soft Live VOD underperform → next most-viewed after dwell/cooldown.
        """
        from src.agents.store import OPS_DIR
        from src.streaming import vod_loop

        status = vod_loop.run_status(ensure_playlist=False)
        issues: list[dict[str, Any]] = []
        healthy: list[str] = []
        repairs: list[dict[str, Any]] = []
        for ch, row in (status.get("channels") or {}).items():
            rtmp = bool(row.get("rtmp_ready"))
            alive = bool(row.get("alive"))
            supervise = bool(row.get("supervisor_alive"))
            perf = vod_loop.performance_issues(ch)
            ff_n = int(perf.get("ffmpeg_count") or 0)
            if rtmp and alive and not perf.get("needs_repair"):
                healthy.append(ch)
                continue
            if rtmp and ff_n > 1:
                issues.append(
                    {
                        "channel": ch,
                        "problem": "dual_ingest",
                        "ffmpeg_count": ff_n,
                        "supervisor_alive": supervise,
                    }
                )
            elif rtmp and not alive:
                issues.append(
                    {
                        "channel": ch,
                        "problem": "rtmp_ready_but_not_alive",
                        "supervisor_alive": supervise,
                        "perf_issues": perf.get("issues") or [],
                    }
                )
            elif rtmp and alive and perf.get("needs_repair"):
                issues.append(
                    {
                        "channel": ch,
                        "problem": "poor_performance",
                        "supervisor_alive": supervise,
                        "perf_issues": perf.get("issues") or [],
                    }
                )
            # Healthcheck owns ensure_single / stall kill / relive (systemd).
            try:
                hc = vod_loop.healthcheck_channel(ch, dry_run=False)
                repairs.append(
                    {
                        "channel": ch,
                        "action": hc.action,
                        "message": (hc.message or "")[:160],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                if issues:
                    issues[-1]["nudge_error"] = str(exc)
                else:
                    issues.append(
                        {
                            "channel": ch,
                            "problem": "healthcheck_error",
                            "nudge_error": str(exc),
                        }
                    )

        views = self.refresh_vod_views_cache()

        vod_judgments: list[dict[str, Any]] = []
        vod_swaps: list[dict[str, Any]] = []
        for ch in self._smm_live_channels():
            try:
                judgment = self.judge_live_vod_performance(ch)
                vod_judgments.append(judgment)
                if judgment.get("underperforming") and judgment.get("should_swap"):
                    vod_swaps.append(
                        self.swap_underperforming_live_vod(
                            ch,
                            reason=str(judgment.get("reason") or "underperforming"),
                        )
                    )
                    # Re-judge after swap so featured ops JSON reflects new head.
                    try:
                        vod_judgments[-1] = self.judge_live_vod_performance(ch)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                logger.exception("smm live vod judge failed channel=%s", ch)
                vod_judgments.append({"channel": ch, "ok": False, "error": str(exc)})

        featured_path = None
        try:
            featured_path = self._persist_featured_vods(vod_judgments)
        except Exception as exc:  # noqa: BLE001
            logger.info("smm featured vod persist failed: %s", exc)

        policy = self._live_vod_soft_policy()
        alert_path = OPS_DIR / "smm_live_alerts.md"
        lines = [
            "# SMM Live stream watch",
            "",
            f"_Updated: `{datetime.now(timezone.utc).isoformat()}`_",
            "",
            f"- healthy: `{', '.join(healthy) or 'none'}`",
            f"- issues: `{len(issues)}`",
            f"- repairs: `{len(repairs)}`",
            f"- vod_underperforming: "
            f"`{sum(1 for j in vod_judgments if j.get('underperforming'))}`",
            f"- vod_swaps: `{len(vod_swaps)}`",
            f"- views_cache_ids: `{views.get('n_video_ids', 0)}`",
            f"- featured_ops: `{featured_path}`",
            "",
            "## Soft Live underperform policy (loosened)",
            "",
            f"- views_vs_winner soft bar: `{policy['views_vs_winner_soft']}×` "
            f"longform winner median (looser than farm `0.35×` / AVD 40% / CTR 4%)",
            f"- next-best views gap: `≥ {policy['next_views_mult']}×` current "
            f"(clear weakness only)",
            f"- score gap: `≥ {policy['score_mult']}×` and Δscore `≥ {policy['score_delta_min']}`",
            f"- min dwell on same VOD: `{int(policy['min_dwell_sec'])}s` (~25m)",
            f"- swap cooldown: `{int(policy['swap_cooldown_sec'])}s`",
            f"- check cadence: ~`{int(policy['check_interval_hint_sec'])}s` "
            f"(stream_beat SMM watch; pick ~30m)",
            f"- watch-time proxy: Live concurrentViewers ≤ "
            f"`{int(policy.get('concurrent_weak_max') or 1)}` + next ≥ "
            f"`{float(policy.get('watch_time_next_views_mult') or 1.25):g}×` "
            f"views → swap to next most-viewed",
            "- health soft: bitrate_collapse / stall / reconnect_storm + better next candidate",
            "- never swap mid-stall / reconnect storm (healthcheck owns repair)",
            "- rank: **most-viewed owned longform first** (≥180s); no Shorts-as-main; no Meta cookies",
            "- encode: infinite `-stream_loop -1` on playlist **head** (featured); "
            "tails = next candidates",
            "",
            "## Issues",
            "",
        ]
        for issue in issues:
            lines.append(
                f"- **{issue.get('channel')}**: {issue.get('problem')} "
                f"(supervise={issue.get('supervisor_alive')}"
                f"{', ffmpeg_n=' + str(issue['ffmpeg_count']) if issue.get('ffmpeg_count') else ''}"
                f"{', perf=' + ','.join(issue.get('perf_issues') or []) if issue.get('perf_issues') else ''})"
            )
        if not issues:
            lines.append("- All configured Live channels encoding OK (or no RTMP keys).")
        if repairs:
            lines.extend(["", "## Repair actions", ""])
            for rep in repairs:
                lines.append(
                    f"- **{rep.get('channel')}**: `{rep.get('action')}` — {rep.get('message')}"
                )
        lines.extend(["", "## VOD performance vs soft benchmarks", ""])
        for j in vod_judgments:
            if j.get("error"):
                lines.append(f"- **{j.get('channel')}**: error `{j.get('error')}`")
                continue
            flag = "UNDER" if j.get("underperforming") else "ok"
            lines.append(
                f"- **{j.get('channel')}**: `{flag}` — {j.get('reason') or 'within band'} "
                f"(current_views=`{j.get('current_views')}` next_views=`{j.get('next_views')}` "
                f"winner_median=`{j.get('winner_longform_median')}` "
                f"concurrent=`{j.get('concurrent_viewers')}` "
                f"dwell=`{j.get('dwell_detail')}` "
                f"safe=`{j.get('swap_safe_detail')}` "
                f"competitor_pressure=`{j.get('competitor_pressure')}`)"
            )
        if vod_swaps:
            lines.extend(["", "## VOD swaps", ""])
            for s in vod_swaps:
                lines.append(
                    f"- **{s.get('channel')}**: `{s.get('action')}` why=`{s.get('reason')}` "
                    f"nudge=`{(s.get('nudge') or {}).get('action')}`"
                )
        lines.extend(
            [
                "",
                "## Dual-ingest note",
                "",
                "YouTube Studio \"More than one ingestion…\" = **two encodes on primary**. "
                "On VPS this was usually orphan `start_channel` supervise beside systemd "
                "`vod-loop@CHANNEL`, flock unlink race, or false PID matches. "
                "When unit owns channel: systemctl only; lock-first supervise flock; "
                "`ensure_single_ingest` keeps **one** ffmpeg per channel. "
                "Keys for napstorian vs historian must differ. Repair notes: "
                "`vod_loop_repair_notes.md`.",
                "",
                "_Longform-only Live. No Meta cookies. Never logs RTMP keys. "
                "Underperforming VOD → `vod_picker.refresh_playlists(exclude_current)` "
                "+ single ffmpeg nudge (no dual-ingest). Featured VOD: "
                "`smm_live_featured.json`._",
                "",
            ]
        )
        alert_path.parent.mkdir(parents=True, exist_ok=True)
        alert_path.write_text("\n".join(lines), encoding="utf-8")

        return {
            "ok": len(issues) == 0,
            "healthy": healthy,
            "issues": issues,
            "repairs": repairs,
            "vod_judgments": vod_judgments,
            "vod_swaps": vod_swaps,
            "policy": policy,
            "featured_path": str(featured_path) if featured_path else None,
            "views_cache": {
                "n_video_ids": views.get("n_video_ids"),
                "n_paths": views.get("n_paths"),
                "path": views.get("path"),
            },
            "alert_path": str(alert_path),
        }

    def _live_vod_swap_cooldown_sec(self) -> float:
        # Soft band: ~25m after a swap (aligned with min dwell) — not hair-trigger.
        return max(
            300.0,
            float(self.smm_cfg.get("live_vod_swap_cooldown_sec") or 1500),
        )

    def _live_vod_min_dwell_sec(self) -> float:
        """Min time the same VOD must stay featured before underperform can rotate."""
        return max(
            600.0,
            float(self.smm_cfg.get("live_vod_min_dwell_sec") or 1500),
        )

    def _live_vod_soft_policy(self) -> dict[str, Any]:
        """Loosened Live underperform bars (not farm AVD 40% / CTR 4%)."""
        return {
            "views_vs_winner_soft": float(
                self.smm_cfg.get("live_vod_views_vs_winner_soft") or 0.20
            ),
            "next_views_mult": float(
                self.smm_cfg.get("live_vod_next_views_mult") or 2.0
            ),
            "score_mult": float(self.smm_cfg.get("live_vod_score_mult") or 1.30),
            "score_delta_min": float(
                self.smm_cfg.get("live_vod_score_delta_min") or 40
            ),
            "concurrent_weak_max": int(
                self.smm_cfg.get("live_vod_concurrent_weak_max") or 1
            ),
            "watch_time_next_views_mult": float(
                self.smm_cfg.get("live_vod_watch_time_next_views_mult") or 1.25
            ),
            "min_dwell_sec": self._live_vod_min_dwell_sec(),
            "swap_cooldown_sec": self._live_vod_swap_cooldown_sec(),
            "check_interval_hint_sec": 900,
            "note": (
                "Live SMM soft bars — looser than farm AVD 40% / CTR 4% / 0.35× winner. "
                "Watch-time proxy = Live concurrentViewers when available; "
                "weak concurrent → next most-viewed after dwell. "
                "Also bitrate/health + clear views gap vs next candidate."
            ),
        }

    def _live_vod_swap_allowed(self, channel: str) -> tuple[bool, str]:
        from src.agents.store import OPS_DIR

        path = OPS_DIR / f"smm_live_vod_swap_{channel}.ts"
        if not path.is_file():
            return True, "no_marker"
        try:
            age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        except OSError:
            return True, "marker_unreadable"
        cool = self._live_vod_swap_cooldown_sec()
        if age < cool:
            return False, f"cooldown_{int(cool - age)}s"
        return True, f"cooled_{int(age)}s"

    def _live_vod_swap_safe_now(self, channel: str) -> tuple[bool, str]:
        """Never swap mid-stall / reconnect storm — let healthcheck own repair."""
        from src.streaming import vod_loop

        perf = vod_loop.performance_issues(channel)
        issues = list(perf.get("issues") or [])
        blocked = {"stall", "reconnect_storm", "dual_ingest", "dead_encode"}
        hit = [i for i in issues if i in blocked]
        if hit:
            return False, "unsafe_" + "+".join(hit)
        return True, "encode_stable"

    def _mark_live_vod_swap(self, channel: str, reason: str) -> None:
        from src.agents.store import OPS_DIR

        OPS_DIR.mkdir(parents=True, exist_ok=True)
        (OPS_DIR / f"smm_live_vod_swap_{channel}.ts").write_text(
            f"{datetime.now(timezone.utc).isoformat()}\nreason={reason}\n",
            encoding="utf-8",
        )

    def _load_featured_vods(self) -> dict[str, Any]:
        from src.agents.store import OPS_DIR

        path = OPS_DIR / "smm_live_featured.json"
        if not path.is_file():
            return {"channels": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"channels": {}}
        if not isinstance(data, dict):
            return {"channels": {}}
        data.setdefault("channels", {})
        return data

    def _featured_dwell_ok(
        self, channel: str, current_path: str | None
    ) -> tuple[bool, str, float]:
        """Return (ok, detail, age_sec) for min dwell on currently featured VOD."""
        from pathlib import Path

        if not current_path:
            return True, "empty_head", 0.0
        featured = self._load_featured_vods()
        row = (featured.get("channels") or {}).get(channel) or {}
        prev = str(row.get("featured_path") or "")
        since = str(row.get("featured_since") or "")
        now = datetime.now(timezone.utc).timestamp()
        if not prev or str(Path(prev).resolve()) != str(Path(current_path).resolve()):
            # Newly featured / unknown — start dwell clock; block swap until dwell.
            return False, "dwell_new_featured", 0.0
        if not since:
            return False, "dwell_missing_since", 0.0
        try:
            epoch = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        except Exception:
            return False, "dwell_bad_since", 0.0
        age = max(0.0, now - epoch)
        need = self._live_vod_min_dwell_sec()
        if age < need:
            return False, f"dwell_{int(need - age)}s_left", age
        return True, f"dwell_ok_{int(age)}s", age

    def _persist_featured_vods(
        self,
        judgments: list[dict[str, Any]],
        *,
        force_since: dict[str, str] | None = None,
    ) -> Path:
        """Track currently featured Live VOD per channel in ops JSON."""
        from pathlib import Path

        from src.agents.store import OPS_DIR

        OPS_DIR.mkdir(parents=True, exist_ok=True)
        path = OPS_DIR / "smm_live_featured.json"
        prev = self._load_featured_vods()
        prev_ch = dict(prev.get("channels") or {})
        now_iso = datetime.now(timezone.utc).isoformat()
        force_since = force_since or {}
        channels: dict[str, Any] = {}
        for j in judgments:
            ch = str(j.get("channel") or "")
            if not ch:
                continue
            feat = j.get("current_path")
            old = prev_ch.get(ch) or {}
            old_path = str(old.get("featured_path") or "")
            same = bool(
                feat
                and old_path
                and str(Path(old_path).resolve()) == str(Path(str(feat)).resolve())
            )
            if ch in force_since:
                since = force_since[ch]
            elif same and old.get("featured_since"):
                since = str(old.get("featured_since"))
            else:
                since = now_iso
            channels[ch] = {
                "featured_path": feat,
                "featured_views": j.get("current_views"),
                "featured_views_src": j.get("current_views_src"),
                "featured_since": since,
                "next_path": j.get("next_path"),
                "next_views": j.get("next_views"),
                "underperforming": bool(j.get("underperforming")),
                "should_swap": bool(j.get("should_swap")),
                "reason": j.get("reason"),
                "updated_at": now_iso,
            }
        # Keep any channel not in this judgment batch.
        for ch, row in prev_ch.items():
            if ch not in channels and isinstance(row, dict):
                channels[ch] = row
        payload = {
            "updated_at": now_iso,
            "module": "smm_live_featured",
            "policy": self._live_vod_soft_policy(),
            "channels": channels,
            "note": "featured_path = Live concat playlist head (most-viewed-ranked).",
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def _competitor_live_pressure(self, channel: str | None = None) -> dict[str, Any]:
        """Soft competitor pressure from channel-scoped competitors.json."""
        from src.agents.competitors_agent import competitors_path

        path = competitors_path(channel)
        if not path.is_file():
            return {"ok": False, "pressure": False, "reason": "no_competitors_json"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "pressure": False, "reason": str(exc)}
        comps = [c for c in (data.get("competitors") or []) if isinstance(c, dict)]
        eligible = [
            c
            for c in comps
            if c.get("eligible_for_titles")
            or c.get("active_last_7_days")
            or c.get("active_in_window")
        ]
        medians: list[float] = []
        for c in eligible or comps:
            try:
                med = float(c.get("median_recent_views") or 0)
            except (TypeError, ValueError):
                med = 0.0
            if med > 0:
                medians.append(med)
        if not medians:
            return {
                "ok": True,
                "pressure": False,
                "eligible_n": len(eligible),
                "path": str(path),
                "reason": "no_median_views",
            }
        medians.sort()
        competitor_median = medians[len(medians) // 2]
        return {
            "ok": True,
            "pressure": competitor_median >= 5000,
            "competitor_median_recent_views": int(competitor_median),
            "eligible_n": len(eligible),
            "active_7d": sum(1 for c in comps if c.get("active_last_7_days")),
            "path": str(path),
        }

    def _own_live_concurrent_viewers(self, channel: str | None = None) -> int | None:
        """Best-effort concurrent viewers for this channel's live broadcast."""
        ch = self._normalize_channel(channel)
        channel_id = self._youtube_channel_id_for(ch)
        if not channel_id:
            channel_id = self._infer_channel_id_from_farm(ch) or ""
        if not channel_id:
            # OAuth mine=True for this Brand Account token.
            try:
                youtube = self._build_youtube(ch)
                resp = (
                    youtube.channels()
                    .list(part="id", mine=True)
                    .execute()
                )
                items = resp.get("items") or []
                if items:
                    channel_id = str(items[0].get("id") or "").strip()
                    if channel_id:
                        self._cache_youtube_channel_id(ch, channel_id)
            except Exception as exc:  # noqa: BLE001
                logger.info("smm concurrent channel resolve failed ch=%s: %s", ch, exc)
        if not channel_id:
            return None
        api_key = getattr(self.s, "youtube_api_key", None) or ""
        if not api_key:
            return None
        try:
            import httpx

            search = httpx.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "id",
                    "channelId": channel_id,
                    "eventType": "live",
                    "type": "video",
                    "maxResults": 5,
                    "key": api_key,
                },
                timeout=20.0,
            )
            if search.status_code != 200:
                return None
            ids = [
                (it.get("id") or {}).get("videoId")
                for it in (search.json().get("items") or [])
                if (it.get("id") or {}).get("videoId")
            ]
            if not ids:
                return 0
            vids = httpx.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "part": "liveStreamingDetails",
                    "id": ",".join(ids[:5]),
                    "key": api_key,
                },
                timeout=20.0,
            )
            if vids.status_code != 200:
                return None
            best = 0
            for item in vids.json().get("items") or []:
                details = item.get("liveStreamingDetails") or {}
                try:
                    n = int(details.get("concurrentViewers") or 0)
                except (TypeError, ValueError):
                    n = 0
                best = max(best, n)
            return best
        except Exception as exc:  # noqa: BLE001
            logger.info("smm live concurrent fetch failed ch=%s: %s", ch, exc)
            return None

    def _longform_winner_median_views(self, channel: str | None = None) -> int:
        """Median views among that channel's winners that look like longform."""
        cw = self._channel_winners_payload(channel)
        top = cw.get("top") or []
        vals: list[int] = []
        for row in top:
            title = str(row.get("title") or "")
            if title.count("#") >= 2 and len(title) < 90:
                continue
            try:
                vals.append(int(row.get("views") or 0))
            except (TypeError, ValueError):
                continue
        if not vals:
            try:
                return int(float(cw.get("channel_median_views") or 0))
            except (TypeError, ValueError):
                return 0
        vals.sort()
        return int(vals[len(vals) // 2])

    def judge_live_vod_performance(self, channel: str) -> dict[str, Any]:
        """Compare Live playlist head vs next-best / soft winner floor / health.

        Softened vs prior 0.35× winner / 1.5× next / tight score gaps so swaps
        are not hair-trigger. Still fires when clearly weak after min dwell.
        """
        from pathlib import Path

        from src.streaming import vod_loop
        from src.streaming.vod_picker import (
            default_pick_limit,
            rank_vods,
            read_playlist_entries,
            resolve_views_for_path,
        )

        policy = self._live_vod_soft_policy()
        current_entries = read_playlist_entries(channel)
        current_path = current_entries[0] if current_entries else None
        current_views = 0
        current_src = "none"
        if current_path:
            current_views, current_src = resolve_views_for_path(Path(current_path))

        ranked = rank_vods(channel, limit=default_pick_limit())
        next_best = None
        current_rank = 0.0
        for cand in ranked:
            if current_path and str(Path(cand.path).resolve()) == str(
                Path(current_path).resolve()
            ):
                current_rank = float(cand.rank_metric or 0.0)
                continue
            if next_best is None:
                next_best = cand
        next_views = 0
        next_src = "none"
        next_rank = 0.0
        if next_best is not None:
            next_views, next_src = resolve_views_for_path(Path(next_best.path))
            next_rank = float(next_best.rank_metric or 0.0)
            if current_rank <= 0.0 and current_path:
                # Head not in ranked pool — approximate from views (AVD may be missing).
                current_rank = float(current_views or 0)

        winner_med = self._longform_winner_median_views(channel)
        competitor = self._competitor_live_pressure(channel)
        concurrent = self._own_live_concurrent_viewers(channel)
        perf = vod_loop.performance_issues(channel)
        perf_issues = list(perf.get("issues") or [])
        health_soft = bool(
            set(perf_issues)
            & {"bitrate_collapse", "stall", "reconnect_storm", "rtmp_io_error"}
        )

        reasons: list[str] = []
        under = False
        views_mult = float(policy["next_views_mult"])
        winner_soft = float(policy["views_vs_winner_soft"])
        score_mult = float(policy["score_mult"])
        score_delta = float(policy["score_delta_min"])

        # Clear views / views×AVD gap vs next ranked candidate (softened: 2.0×, was 1.5×).
        if (
            next_best is not None
            and next_views > 0
            and next_views
            >= max(80, int(current_views * views_mult) if current_views else 80)
            and str(next_src).startswith("views_cache")
        ):
            under = True
            reasons.append(
                f"next_best_views_{next_views}_gt_current_{current_views}_x{views_mult:g}"
            )
        elif (
            next_best is not None
            and next_rank > 0
            and next_rank
            >= max(80.0, float(current_rank) * views_mult if current_rank else 80.0)
            and (
                getattr(next_best, "avd_pct", None) is not None
                or str(next_src).startswith("views_cache")
            )
        ):
            under = True
            reasons.append(
                f"next_best_rank_{next_rank:.1f}_gt_current_{current_rank:.1f}_x{views_mult:g}"
            )

        current_score = 0.0
        if current_path:
            for cand in ranked:
                if str(Path(cand.path).resolve()) == str(Path(current_path).resolve()):
                    current_score = float(cand.score)
                    break
        # Score gap only when clearly large (softened: 1.30× + Δ40, was 1.15 / 25).
        if (
            next_best is not None
            and current_path
            and float(next_best.score) >= max(1.0, current_score * score_mult)
            and float(next_best.score) - current_score >= score_delta
        ):
            under = True
            reasons.append(
                f"next_best_score_{next_best.score}_vs_current_{current_score}"
            )

        # Content floor: looser 0.20× longform winner median (was 0.35×).
        if (
            winner_med > 0
            and current_views > 0
            and str(current_src).startswith("views_cache")
            and current_views < max(20, int(winner_med * winner_soft))
        ):
            under = True
            reasons.append(
                f"below_winner_soft_{winner_soft:g}x_median_{winner_med}_at_{current_views}"
            )

        # Prefer health soft signals + a better next candidate (not health alone).
        if health_soft and next_best is not None and (
            next_views > current_views or float(next_best.score) > current_score
        ):
            under = True
            reasons.append(
                "health_soft_"
                + "+".join(
                    i
                    for i in perf_issues
                    if i
                    in {
                        "bitrate_collapse",
                        "stall",
                        "reconnect_storm",
                        "rtmp_io_error",
                    }
                )
            )

        # Watch-time proxy: Live concurrentViewers (API). Weak after dwell + better
        # next longform → soft-swap to next most-viewed (both channels).
        weak_max = int(policy.get("concurrent_weak_max") or 1)
        wt_mult = float(policy.get("watch_time_next_views_mult") or 1.25)
        if (
            concurrent is not None
            and concurrent <= weak_max
            and next_best is not None
            and next_views
            >= max(80, int(current_views * wt_mult) if current_views else 80)
        ):
            under = True
            reasons.append(
                f"weak_watch_time_concurrent_{concurrent}_next_views_{next_views}"
            )

        # Competitor pressure + weak concurrent (kept as extra signal).
        if (
            competitor.get("pressure")
            and concurrent is not None
            and concurrent <= weak_max
            and next_best is not None
            and next_views >= max(80, int(current_views * 1.25) if current_views else 80)
        ):
            under = True
            reasons.append(
                "weak_concurrent_"
                f"{concurrent}_comp_med={competitor.get('competitor_median_recent_views')}"
            )

        if not current_path:
            under = True
            reasons.append("empty_playlist_head")

        allowed, cool = self._live_vod_swap_allowed(channel)
        dwell_ok, dwell_detail, dwell_age = self._featured_dwell_ok(channel, current_path)
        safe_ok, safe_detail = self._live_vod_swap_safe_now(channel)
        should_swap = bool(
            under and allowed and dwell_ok and safe_ok and next_best is not None
        )
        reason = "; ".join(reasons) if reasons else "within_band"
        if under and not dwell_ok:
            reason = f"{reason}; swap_blocked={dwell_detail}"
        if under and not allowed:
            reason = f"{reason}; swap_blocked={cool}"
        if under and not safe_ok:
            reason = f"{reason}; swap_blocked={safe_detail}"

        return {
            "ok": True,
            "channel": channel,
            "underperforming": under,
            "should_swap": should_swap,
            "reason": reason,
            "cooldown": cool,
            "dwell_ok": dwell_ok,
            "dwell_detail": dwell_detail,
            "dwell_age_sec": dwell_age,
            "swap_safe": safe_ok,
            "swap_safe_detail": safe_detail,
            "health_soft": health_soft,
            "perf_issues": perf_issues,
            "policy": policy,
            "current_path": current_path,
            "current_views": current_views,
            "current_views_src": current_src,
            "current_score": current_score,
            "next_path": next_best.path if next_best else None,
            "next_views": next_views,
            "next_views_src": next_src,
            "next_score": float(next_best.score) if next_best else None,
            "winner_longform_median": winner_med,
            "concurrent_viewers": concurrent,
            "competitor_pressure": bool(competitor.get("pressure")),
            "competitor": {
                k: competitor.get(k)
                for k in (
                    "competitor_median_recent_views",
                    "eligible_n",
                    "active_7d",
                )
            },
        }

    def swap_underperforming_live_vod(
        self, channel: str, *, reason: str = "underperforming"
    ) -> dict[str, Any]:
        """Rewrite playlist excluding current head; nudge encode once (no dual-ingest)."""
        from src.agents.store import OPS_DIR
        from src.streaming import vod_loop
        from src.streaming.vod_picker import (
            default_pick_limit,
            read_playlist_entries,
            refresh_playlists,
        )

        safe_ok, safe_detail = self._live_vod_swap_safe_now(channel)
        if not safe_ok:
            return {
                "ok": False,
                "channel": channel,
                "action": "blocked_unsafe",
                "reason": reason,
                "safe_detail": safe_detail,
                "nudge": {"action": "skipped_unsafe"},
            }

        from src.streaming.vod_picker import read_playlist_force_lock

        force_lock = read_playlist_force_lock()
        if force_lock:
            return {
                "ok": True,
                "channel": channel,
                "action": "blocked_force_lock",
                "reason": reason,
                "force_lock_reason": force_lock.get("reason"),
                "nudge": {"action": "skipped_force_lock"},
            }

        before = read_playlist_entries(channel)
        dog_excludes = self._retention_dog_exclude_paths(channel)
        pick = refresh_playlists(
            channels=[channel],
            limit=default_pick_limit(),
            dry_run=False,
            exclude_current=True,
            exclude_paths_by_channel=dog_excludes or None,
        )
        ch_row = (pick.get("channels") or {}).get(channel) or {}
        after = list(ch_row.get("entries") or [])
        changed = bool(after) and (not before or after[0] != before[0])
        nudge: dict[str, Any] = {"action": "skipped_unchanged"}
        if changed:
            try:
                nudge = vod_loop.nudge_encode_for_playlist_swap(channel, dry_run=False)
            except Exception as exc:  # noqa: BLE001
                nudge = {"action": "nudge_error", "error": str(exc)}
            self._mark_live_vod_swap(channel, reason)
            try:
                self.ledger.write(
                    agent="smm",
                    problem=f"Live VOD underperforming on {channel}",
                    action=f"playlist_swap nudge={nudge.get('action')}",
                    severity="warn",
                    extra={
                        "channel": channel,
                        "reason": reason,
                        "before_head": before[0] if before else None,
                        "new_head": after[0] if after else None,
                        "nudge": nudge,
                    },
                )
            except Exception:  # noqa: BLE001
                pass
            # Reset featured dwell clock for the new head.
            try:
                judgment = self.judge_live_vod_performance(channel)
                now_iso = datetime.now(timezone.utc).isoformat()
                self._persist_featured_vods(
                    [judgment], force_since={channel: now_iso}
                )
            except Exception:  # noqa: BLE001
                pass

        try:
            OPS_DIR.mkdir(parents=True, exist_ok=True)
            (OPS_DIR / "smm_live_vod_swap_last.json").write_text(
                json.dumps(
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "channel": channel,
                        "reason": reason,
                        "changed": changed,
                        "before_head": before[0] if before else None,
                        "new_head": after[0] if after else None,
                        "nudge": nudge,
                        "policy": self._live_vod_soft_policy(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

        return {
            "ok": bool(ch_row.get("ok")),
            "channel": channel,
            "action": "swapped" if changed else "unchanged",
            "reason": reason,
            "before_head": before[0] if before else None,
            "new_head": after[0] if after else None,
            "excluded": ch_row.get("excluded"),
            "excludes_cleared": bool(ch_row.get("excludes_cleared")),
            "exclusion_exhausted": bool(ch_row.get("exclusion_exhausted")),
            "nudge": nudge,
            "picker": {"ok": pick.get("ok"), "ts": pick.get("ts")},
        }

    def refresh_vod_views_cache(self) -> dict[str, Any]:
        """Build ``vod_views_cache.json`` for Live vod_picker (top views ranking).

        Sources: **both** Brand Accounts' public uploads + farm publish_manifest
        video_ids (OAuth stats when available, including private).
        """
        from src.agents.store import OPS_DIR
        from src.streaming.vod_picker import _load_publish_index

        by_id: dict[str, int] = {}
        by_path: dict[str, int] = {}
        titles_by_id: dict[str, str] = {}
        channel_by_video_id: dict[str, str] = {}
        per_channel: dict[str, int] = {}

        # Public winners from each Live/sheet channel (correct token / UC id).
        for ch in self._smm_live_channels():
            n = 0
            for v in self._list_own_channel_videos(channel=ch, max_n=40) or []:
                vid = str(v.get("video_id") or "").strip()
                if not vid:
                    continue
                try:
                    views = int(v.get("views") or 0)
                except (TypeError, ValueError):
                    views = 0
                by_id[vid] = max(views, by_id.get(vid, 0))
                titles_by_id[vid] = str(v.get("title") or "")
                # First listing wins (do not overwrite if already tagged).
                channel_by_video_id.setdefault(vid, ch)
                n += 1
            per_channel[ch] = n

        # Exact farm finals via publish manifests.
        pub = _load_publish_index()
        farm_ids = [
            str(meta.get("video_id") or "").strip()
            for meta in pub.values()
            if str(meta.get("video_id") or "").strip()
        ]
        farm_ids = list(dict.fromkeys(farm_ids))
        if farm_ids:
            fetched = self._fetch_video_views(farm_ids)
            for vid, views in fetched.items():
                by_id[vid] = max(int(views), by_id.get(vid, 0))

        for path, meta in pub.items():
            vid = str(meta.get("video_id") or "").strip()
            if vid and vid in by_id:
                by_path[path] = by_id[vid]

        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "by_video_id": by_id,
            "by_path": by_path,
            "titles_by_id": titles_by_id,
            "channel_by_video_id": channel_by_video_id,
            "n_video_ids": len(by_id),
            "n_paths": len(by_path),
            "per_channel_listed": per_channel,
            "source": "smm.refresh_vod_views_cache",
        }
        out = OPS_DIR / "vod_views_cache.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        payload["path"] = str(out)
        return payload

    def _fetch_video_views(
        self, video_ids: list[str], *, channel: str | None = None
    ) -> dict[str, int]:
        """Fetch view counts for ids (public or private) via OAuth, else API key.

        Tries the named channel token first, then every Live channel token, then
        API key (public-only).
        """
        ids = [v for v in video_ids if v]
        if not ids:
            return {}
        out: dict[str, int] = {}

        order: list[str] = []
        if channel:
            order.append(self._normalize_channel(channel))
        for ch in self._smm_live_channels():
            if ch not in order:
                order.append(ch)

        for ch in order:
            try:
                youtube = self._build_youtube(ch)
                for i in range(0, len(ids), 50):
                    chunk = ids[i : i + 50]
                    vresp = (
                        youtube.videos()
                        .list(part="statistics", id=",".join(chunk))
                        .execute()
                    )
                    for item in vresp.get("items") or []:
                        vid = item.get("id") or ""
                        st = item.get("statistics") or {}
                        out[vid] = int(st.get("viewCount") or 0)
                if len(out) >= len(ids):
                    return out
            except Exception as exc:  # noqa: BLE001
                logger.info("smm oauth views fetch failed ch=%s: %s", ch, exc)

        missing = [v for v in ids if v not in out]
        if not missing:
            return out
        try:
            import httpx

            from src.agents.youtube_data import YouTubeDataClient

            api_key = getattr(self.s, "youtube_api_key", None) or ""
            if not api_key:
                return out
            yt = YouTubeDataClient(httpx.Client(timeout=30.0), api_key)
            stats = yt.videos_stats(missing)
            for vid, st in (stats or {}).items():
                out[vid] = int(st.get("views") or st.get("viewCount") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.info("smm api-key views fetch failed: %s", exc)
        return out

    def _process_job(self, job: JobRecord) -> dict[str, Any]:
        meta = dict(job.meta or {})
        out: dict[str, Any] = {
            "job_id": job.id,
            "video_id": job.video_id,
            "status": job.status,
            "actions": [],
        }

        privacy = self._yt_privacy(job.video_id, channel=self._job_channel(job))
        out["yt_privacy"] = privacy

        # SOP compliance (soft by default) — runs for private+public so overlays
        # are flagged before go-live. Never blocks Live mid-stream.
        try:
            sop = self.maybe_enforce_new_format_sop(
                job, for_public=(privacy == "public")
            )
            if sop and not sop.get("skipped"):
                out["sop_compliance"] = sop
                out["actions"].append(
                    {
                        "action": "sop_compliance",
                        "ok": bool(sop.get("ok")),
                        "n_failures": sop.get("n_failures"),
                        "n_warnings": sop.get("n_warnings"),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("smm sop compliance failed job=%s: %s", job.id, exc)

        watch_only_public = bool(self.smm_cfg.get("watch_only_public", True))
        if watch_only_public and privacy != "public":
            # privacy=None is an API miss — do NOT clear watching (that re-onboarded
            # and re-pinned every beat). Only pause watching on known non-public.
            if privacy is None:
                out["skipped"] = "privacy_unknown"
                return out
            if meta.get("smm_watching"):
                meta["smm_watching"] = False
                self.store.update_job(job.id, meta=meta)
            out["skipped"] = "not_public_yet"
            return out

        # Promote store status when YT is already public
        if job.status != "public":
            self.store.update_job(job.id, status="public", stage="public")
            job = self.store.get_job(job.id) or job
            meta = dict(job.meta or {})
            out["status"] = "public"
            out["promoted_to_public"] = True
            try:
                purge = self._purge_local_media_after_public(job)
                if purge:
                    out["local_media_purge"] = purge
                    out["actions"].append(
                        {
                            "action": "local_media_purge",
                            "ok": bool(purge.get("ok", True)),
                            "bytes_freed": purge.get("bytes_freed"),
                            "skipped": purge.get("skipped"),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 — never block SMM on disk cleanup
                logger.info("smm local media purge failed job=%s: %s", job.id, exc)

        # First public onboard only once. Restoring smm_watching must NOT re-pin.
        if not meta.get("smm_onboarded_at"):
            onboard = self.on_public_live(
                job_id=job.id,
                video_id=job.video_id or "",
                title=job.title,
                job_dir=job.job_dir,
            )
            out["onboard"] = onboard
            out["actions"].extend(onboard.get("actions") or [])
            # evaluate already done inside onboard for first pass
            if onboard.get("insight_id"):
                out["insight_id"] = onboard["insight_id"]
                out["proposals"] = onboard.get("proposals") or []
            if onboard.get("sop_compliance"):
                out["sop_compliance"] = onboard["sop_compliance"]
            return out

        if not meta.get("smm_watching"):
            meta["smm_watching"] = True
            self.store.update_job(job.id, meta=meta)

        # Never re-post: pin_engagement_comment claims video_id once. Meta-only
        # sync if a prior claim exists but job meta lost the comment id.
        if (
            bool(self.cfg.get("pin_comment_on_upload", True))
            and not meta.get("smm_pin_comment_id")
            and not meta.get("smm_pin_needs_reauth")
            and not meta.get("smm_pin_attempted_at")
            and job.video_id
        ):
            claimed = _pin_already_claimed(job.video_id)
            if claimed:
                if claimed.get("comment_id"):
                    meta["smm_pin_comment_id"] = claimed["comment_id"]
                meta["smm_pin_attempted_at"] = claimed.get("at") or datetime.now(
                    timezone.utc
                ).isoformat()
                self.store.update_job(job.id, meta=meta)
                out["actions"].append(
                    {
                        "ok": True,
                        "action": "pin_comment",
                        "skipped": True,
                        "reason": "already_claimed_for_video",
                        "video_id": job.video_id,
                        "comment_id": claimed.get("comment_id"),
                    }
                )
            else:
                pin = self.pin_engagement_comment(
                    job.video_id,
                    title=job.title,
                    channel=self._job_channel(job),
                )
                out["actions"].append(pin)
                meta["smm_pin_attempted_at"] = datetime.now(timezone.utc).isoformat()
                if pin.get("ok") and pin.get("comment_id"):
                    meta["smm_pin_comment_id"] = pin["comment_id"]
                    meta.pop("smm_pin_error", None)
                elif pin.get("skipped"):
                    if pin.get("comment_id"):
                        meta["smm_pin_comment_id"] = pin["comment_id"]
                elif pin.get("error"):
                    err = str(pin["error"])
                    meta["smm_pin_error"] = err[:300]
                    if "insufficient" in err.lower() or "403" in err:
                        meta["smm_pin_needs_reauth"] = True
                self.store.update_job(job.id, meta=meta)

        metrics = self._fetch_or_stub_metrics(
            job.video_id, privacy="public", channel=self._job_channel(job)
        )
        apply = metrics.get("source") not in {"stub", "private_pending"}
        insight = self.evaluate_video(
            video_id=job.video_id,
            title=job.title,
            metrics=metrics,
            apply_actions=apply,
            job=job,
        )
        out["insight_id"] = insight.id
        out["proposals"] = insight.proposals
        out["metrics_source"] = metrics.get("source")
        out["applied"] = list(getattr(insight, "_applied_actions", []) or [])
        return out

    def maybe_enforce_new_format_sop(
        self,
        job: JobRecord,
        *,
        for_public: bool | None = None,
    ) -> dict[str, Any]:
        """Run every-video SOP check; soft-alert by default (no Live block)."""
        from src.agents.smm_sop import (
            check_new_format_sop,
            enforce_new_format_sop_enabled,
            sop_block_publish_enabled,
            stamp_stage_gate_meta,
        )

        if not enforce_new_format_sop_enabled(self.smm_cfg):
            return {"skipped": True, "reason": "enforce_new_format_sop=false"}

        result = check_new_format_sop(
            job,
            smm_cfg=self.smm_cfg,
            agents_cfg=self.cfg,
            for_public=for_public,
        )
        soft = bool(self.smm_cfg.get("sop_soft_alert", True))
        result["soft"] = soft
        result["block_publish"] = bool(
            sop_block_publish_enabled(self.smm_cfg) and not result.get("ok")
        )

        # Per-stage gate stamp + compliance table (soft; Live never hard-blocked here).
        try:
            from src.agents.smm_sop import (
                audit_stage_complete,
                sop_stage_gates_enabled,
                write_sop_compliance_md,
            )

            if sop_stage_gates_enabled(self.smm_cfg):
                stage_name = "live" if for_public or job.status == "public" else "compose"
                if job.status in {"private", "public"} and (
                    Path(str(job.job_dir or "")) / "youtube_meta"
                ).exists():
                    # Prefer package/publish when packaging exists
                    if job.status == "public":
                        stage_name = "live"
                    elif (Path(str(job.job_dir or "")) / "publish_manifest.json").exists():
                        stage_name = "publish"
                    else:
                        stage_name = "package"
                stage_dig = audit_stage_complete(
                    job,
                    completed_stage=stage_name,
                    smm_cfg=self.smm_cfg,
                    agents_cfg=self.cfg,
                    hard=False,
                )
                result["stage_gate"] = {
                    "ok": stage_dig.get("ok"),
                    "stage": stage_dig.get("stage"),
                    "resend_stage": (stage_dig.get("remediation") or {}).get(
                        "resend_stage"
                    ),
                    "compliance_path": stage_dig.get("compliance_path"),
                }
                if stage_dig.get("proposals"):
                    # Merge stage proposals into alert stream (dedupe by code later)
                    result.setdefault("proposals", [])
                    existing = {
                        (p.get("code"), p.get("stage"))
                        for p in result["proposals"]
                        if isinstance(p, dict)
                    }
                    for p in stage_dig["proposals"]:
                        key = (p.get("code"), p.get("stage"))
                        if key not in existing:
                            result["proposals"].append(p)
            else:
                write_sop_compliance_md(
                    job, smm_cfg=self.smm_cfg, agents_cfg=self.cfg
                )
        except Exception as stage_exc:  # noqa: BLE001
            logger.info("smm sop stage/compliance write failed: %s", stage_exc)

        proposals = list(result.get("proposals") or [])
        if not proposals:
            # Clean — clear sticky block flag if previously set; keep stage stamp.
            meta = dict(job.meta or {})
            dirty = False
            if meta.pop("sop_block_publish", None) is not None:
                dirty = True
            meta["smm_sop_last_fp"] = result.get("fingerprint") or "clean"
            meta["smm_sop_checked_at"] = datetime.now(timezone.utc).isoformat()
            meta["smm_sop_ok"] = True
            if result.get("stage_gate"):
                meta = stamp_stage_gate_meta(
                    meta,
                    {
                        "ok": result["stage_gate"].get("ok"),
                        "stage": result["stage_gate"].get("stage"),
                        "fingerprint": result.get("fingerprint") or "clean",
                        "remediation": {
                            "resend_stage": result["stage_gate"].get("resend_stage")
                        },
                        "compliance_path": result["stage_gate"].get("compliance_path"),
                        "blocked": False,
                    },
                )
                dirty = True
            if dirty or result.get("stage_gate"):
                self.store.update_job(job.id, meta=meta)
            return result

        try:
            self._write_quality_alerts(
                video_id=job.video_id,
                title=job.title,
                job=job,
                proposals=proposals,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("smm sop alert write failed: %s", exc)

        meta = dict(job.meta or {})
        fp = str(result.get("fingerprint") or "")
        prev_fp = str(meta.get("smm_sop_last_fp") or "")
        meta["smm_sop_checked_at"] = datetime.now(timezone.utc).isoformat()
        meta["smm_sop_last_fp"] = fp
        meta["smm_sop_ok"] = bool(result.get("ok"))
        meta["smm_sop_n_failures"] = int(result.get("n_failures") or 0)
        meta["smm_sop_n_warnings"] = int(result.get("n_warnings") or 0)
        if result.get("stage_gate"):
            meta = stamp_stage_gate_meta(meta, {
                "ok": result["stage_gate"].get("ok"),
                "stage": result["stage_gate"].get("stage"),
                "fingerprint": result.get("fingerprint"),
                "remediation": {
                    "resend_stage": result["stage_gate"].get("resend_stage")
                },
                "compliance_path": result["stage_gate"].get("compliance_path"),
                "blocked": False,
            })
        if result.get("block_publish"):
            meta["sop_block_publish"] = True
        else:
            meta.pop("sop_block_publish", None)
        self.store.update_job(job.id, meta=meta)

        # Ledger note — warn loudly; skip duplicate fingerprints to limit spam.
        if fp != prev_fp or not soft:
            n_f = int(result.get("n_failures") or 0)
            n_w = int(result.get("n_warnings") or 0)
            codes = [
                p.get("code") for p in proposals if isinstance(p, dict) and p.get("code")
            ]
            self.ledger.write(
                agent="smm",
                problem=(
                    f"SOP compliance {'FAIL' if n_f else 'WARN'} "
                    f"job={job.id} ch={result.get('channel')} "
                    f"failures={n_f} warnings={n_w}"
                ),
                action=(
                    f"sop_compliance soft_alert={soft} "
                    f"block_publish={bool(result.get('block_publish'))} "
                    f"codes={','.join(str(c) for c in codes[:12])}"
                ),
                severity="warn" if (n_f or n_w) else "info",
                publish_status=job.status,
                job_id=job.id,
                extra={
                    "type": "sop_compliance",
                    "fingerprint": fp,
                    "failures": result.get("failures"),
                    "warnings": result.get("warnings"),
                    "soft": soft,
                },
            )
            logger.warning(
                "smm sop_compliance job=%s ok=%s failures=%s warnings=%s soft=%s",
                job.id,
                result.get("ok"),
                n_f,
                n_w,
                soft,
            )
        return result

    def _purge_local_media_after_public(self, job: JobRecord) -> dict[str, Any]:
        """Idempotent post-public VPS cleanup — heavy media gone, thin meta kept."""
        meta = dict(job.meta or {})
        if meta.get("local_media_purged_at"):
            jdir = Path(str(job.job_dir or meta.get("job_dir") or ""))
            if jdir.is_dir():
                heavy = any(jdir.glob("**/final.mp4")) or any(
                    jdir.glob("**/_xfade_tmp")
                )
                if not heavy:
                    return {
                        "skipped": True,
                        "message": "already purged",
                        "ok": True,
                        "bytes_freed": 0,
                    }
        if not job.job_dir and not meta.get("job_dir"):
            return {"skipped": True, "message": "no job_dir", "ok": True}
        from src.runpod.disk_guard import cleanup_public_job_media

        jdir = Path(str(job.job_dir or meta.get("job_dir")))
        out = cleanup_public_job_media(
            jdir,
            dry_run=False,
            require_public_status=True,
            job_status="public",
        )
        if not out.get("skipped"):
            meta["local_media_purged_at"] = datetime.now(timezone.utc).isoformat()
            meta["local_media_bytes_freed"] = int(out.get("bytes_freed") or 0)
            try:
                self.store.update_job(job.id, meta=meta)
            except Exception as exc:  # noqa: BLE001
                logger.info("smm purge meta update failed: %s", exc)
            try:
                ops = jdir / "ops"
                ops.mkdir(parents=True, exist_ok=True)
                (ops / "local_media_purged.json").write_text(
                    json.dumps(
                        {
                            "at": meta["local_media_purged_at"],
                            "bytes_freed": out.get("bytes_freed"),
                            "deleted_count": out.get("deleted_count"),
                            "video_id": job.video_id,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("smm purge marker write failed: %s", exc)
        return out

    def on_public_live(
        self,
        *,
        job_id: str,
        video_id: str,
        title: str,
        job_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Called when a farm video is confirmed public on YouTube."""
        out: dict[str, Any] = {"job_id": job_id, "video_id": video_id, "actions": []}
        job = self.store.get_job(job_id)
        meta = dict((job.meta if job else {}) or {})

        if bool(self.cfg.get("pin_comment_on_upload", True)) and not meta.get(
            "smm_pin_comment_id"
        ):
            if meta.get("smm_pin_needs_reauth"):
                out["actions"].append(
                    {
                        "ok": False,
                        "action": "pin_comment",
                        "skipped": True,
                        "reason": (
                            "youtube.force-ssl missing — re-run "
                            f"youtube_auth --channel {self._job_channel(job)}"
                        ),
                    }
                )
            elif meta.get("smm_pin_attempted_at") or _pin_already_claimed(video_id):
                claimed = _pin_already_claimed(video_id) or {}
                if claimed.get("comment_id"):
                    meta["smm_pin_comment_id"] = claimed["comment_id"]
                if not meta.get("smm_pin_attempted_at"):
                    meta["smm_pin_attempted_at"] = claimed.get("at") or datetime.now(
                        timezone.utc
                    ).isoformat()
                out["actions"].append(
                    {
                        "ok": True,
                        "action": "pin_comment",
                        "skipped": True,
                        "reason": "already_claimed_for_video",
                        "video_id": video_id,
                        "comment_id": claimed.get("comment_id"),
                    }
                )
            else:
                pin = self.pin_engagement_comment(
                    video_id,
                    title=title,
                    channel=self._job_channel(job),
                )
                out["actions"].append(pin)
                meta["smm_pin_attempted_at"] = datetime.now(timezone.utc).isoformat()
                if pin.get("ok") and pin.get("comment_id"):
                    meta["smm_pin_comment_id"] = pin["comment_id"]
                    meta["smm_pin_text"] = pin.get("text")
                    meta.pop("smm_pin_error", None)
                    meta.pop("smm_pin_needs_reauth", None)
                elif pin.get("skipped") and pin.get("comment_id"):
                    meta["smm_pin_comment_id"] = pin["comment_id"]
                elif pin.get("error"):
                    err = str(pin["error"])
                    meta["smm_pin_error"] = err[:300]
                    if "insufficient" in err.lower() or "403" in err:
                        meta["smm_pin_needs_reauth"] = True

        # Job 5: ensure Chapters timestamps on public description when available.
        if job is not None and not meta.get("smm_chapters_ensured_at"):
            chapters = self.ensure_chapters_on_public(job, video_id=video_id)
            out["actions"].append(chapters)
            refreshed = self.store.get_job(job_id)
            if refreshed is not None:
                meta = dict(refreshed.meta or {})

        meta["smm_onboarded_at"] = datetime.now(timezone.utc).isoformat()
        meta["smm_watching"] = True
        if job_dir:
            meta["job_dir"] = str(job_dir)
        self.store.update_job(job_id, meta=meta)

        # Drop heavy local media once public (keep thin manifests / video_id).
        try:
            job_for_purge = self.store.get_job(job_id) or job
            if job_for_purge is not None:
                purge = self._purge_local_media_after_public(job_for_purge)
                if purge:
                    out["local_media_purge"] = purge
                    out["actions"].append(
                        {
                            "action": "local_media_purge",
                            "ok": bool(purge.get("ok", True)),
                            "bytes_freed": purge.get("bytes_freed"),
                            "skipped": purge.get("skipped"),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            logger.info("smm local media purge on_public_live failed: %s", exc)

        # Refresh job after pin/chapters so SOP sees latest public path evidence.
        job = self.store.get_job(job_id) or job
        if job is not None:
            try:
                sop = self.maybe_enforce_new_format_sop(job, for_public=True)
                if sop and not sop.get("skipped"):
                    out["sop_compliance"] = sop
                    out["actions"].append(
                        {
                            "action": "sop_compliance",
                            "ok": bool(sop.get("ok")),
                            "n_failures": sop.get("n_failures"),
                            "n_warnings": sop.get("n_warnings"),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                logger.info("smm sop on_public_live failed: %s", exc)

        winners = (self._channel_winners_payload(self._job_channel(job)).get("top")) or []
        metrics = self._fetch_or_stub_metrics(
            video_id, privacy="public", channel=self._job_channel(job)
        )
        metrics["winner_refs"] = [
            {"title": w.get("title"), "views": w.get("views")} for w in winners[:5]
        ]
        insight = self.evaluate_video(
            video_id=video_id,
            title=title,
            metrics=metrics,
            apply_actions=False,  # first public beat: observe; next beat can apply
            job=job,
        )
        out["insight_id"] = insight.id
        out["proposals"] = insight.proposals
        self.ledger.write(
            agent="smm",
            problem=f"watching public farm video {title[:50]}",
            action=f"actions={len(out['actions'])} watching=true",
            severity="info",
            publish_status="public",
            job_id=job_id,
            extra=out,
        )
        return out

    # Back-compat alias (old call sites / tests)
    def on_private_upload(self, **kwargs: Any) -> dict[str, Any]:
        logger.warning("on_private_upload is deprecated — SMM watches public only")
        return {
            "skipped": True,
            "reason": "SMM watches public farm videos only",
            **{k: kwargs.get(k) for k in ("job_id", "video_id")},
        }

    # ------------------------------------------------------------------ winners
    def maybe_learn_channel_winners(self, *, force: bool = False) -> dict[str, Any]:
        """Pull top videos for BOTH Brand Accounts into benchmarks."""
        if not bool(self.smm_cfg.get("learn_from_channel_winners", True)):
            return {"skipped": True, "reason": "learn_from_channel_winners=false"}

        bench = self.store.get_benchmarks()
        by_ch = dict(bench.get("channel_winners_by_channel") or {})
        refresh_h = float(self.smm_cfg.get("winner_refresh_hours") or 24)
        channels = list(self._smm_live_channels())
        if not force and channels:
            all_fresh = True
            for ch in channels:
                row = by_ch.get(ch) if isinstance(by_ch.get(ch), dict) else {}
                if not row:
                    # legacy napstorian blob counts as fresh for that channel only
                    if ch == "napstorian":
                        row = dict(bench.get("channel_winners") or {})
                    else:
                        all_fresh = False
                        break
                last = _parse_iso(row.get("updated_at"))
                if (
                    last is None
                    or datetime.now(timezone.utc) - last >= timedelta(hours=refresh_h)
                ):
                    all_fresh = False
                    break
            if all_fresh:
                return {
                    "skipped": True,
                    "reason": "fresh",
                    "channels": channels,
                    "updated_at": {
                        ch: (by_ch.get(ch) or {}).get("updated_at")
                        for ch in channels
                        if isinstance(by_ch.get(ch), dict)
                    },
                }

        learned = self.learn_channel_winners()
        return learned

    def learn_channel_winners(self) -> dict[str, Any]:
        """Rank each Brand Account's uploads; store per-channel + legacy blob."""
        channels = list(self._smm_live_channels())
        by_channel: dict[str, Any] = {}
        per: list[dict[str, Any]] = []
        for ch in channels:
            one = self._learn_winners_for_channel(ch)
            per.append({"channel": ch, **one})
            if one.get("ok"):
                by_channel[ch] = {k: v for k, v in one.items() if k != "ok"}

        bench = self.store.get_benchmarks()
        existing = dict(bench.get("channel_winners_by_channel") or {})
        existing.update(by_channel)
        bench["channel_winners_by_channel"] = existing
        # Legacy single blob = napstorian (back-compat for older harvest readers).
        if "napstorian" in by_channel:
            bench["channel_winners"] = dict(by_channel["napstorian"])
            med = ((by_channel["napstorian"].get("patterns") or {}).get(
                "median_winner_views"
            ))
            if med:
                bench["winner_views_median"] = med
        elif by_channel and "channel_winners" not in bench:
            first = next(iter(by_channel.values()))
            bench["channel_winners"] = dict(first)
        bench["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.save_benchmarks(bench)

        schedule_hours: list[dict[str, Any]] = []
        for ch, payload in by_channel.items():
            try:
                schedule_hours.append(
                    self.propose_schedule_hours_for_channel(ch, learned=payload)
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("smm schedule hour propose failed ch=%s: %s", ch, exc)
                schedule_hours.append(
                    {"ok": False, "channel": ch, "error": str(exc)[:200]}
                )

        ok_n = sum(1 for p in per if p.get("ok"))
        self.ledger.write(
            agent="smm",
            problem="learned channel winner patterns (dual-channel)",
            action=f"ok_channels={ok_n}/{len(channels)} channels={channels}",
            severity="info",
            publish_status="public",
            extra={"per_channel": [
                {
                    "channel": p.get("channel"),
                    "ok": p.get("ok"),
                    "top_n": len(p.get("top") or []),
                    "error": p.get("error"),
                }
                for p in per
            ], "schedule_hours": schedule_hours},
        )
        return {
            "ok": ok_n > 0,
            "channels": channels,
            "by_channel": by_channel,
            "per_channel": per,
            "schedule_hours": schedule_hours,
        }

    def _learn_winners_for_channel(self, channel: str) -> dict[str, Any]:
        """Rank one Brand Account's public uploads by views."""
        ch = self._normalize_channel(channel)
        sample = max(5, min(int(self.smm_cfg.get("winner_sample") or 30), 50))
        top_n = max(3, min(int(self.smm_cfg.get("winner_top_n") or 8), sample))

        videos = self._list_own_channel_videos(channel=ch, max_n=sample)
        if not videos:
            return {"ok": False, "error": "no channel videos found", "top": [], "channel": ch}

        ranked = sorted(videos, key=lambda v: int(v.get("views") or 0), reverse=True)
        view_list = [int(v.get("views") or 0) for v in ranked]
        med = int(median(view_list)) if view_list else 0
        winners = [
            v
            for v in ranked[:top_n]
            if int(v.get("views") or 0) >= max(med, 1) or v is ranked[0]
        ]

        title_lens = [len(str(w.get("title") or "")) for w in winners]
        what_if_n = sum(
            1
            for w in winners
            if str(w.get("title") or "").lower().startswith("what if")
        )
        hour_hist = self._publish_hour_histogram(ranked, tz_name="Asia/Karachi")
        best_hours = [h for h, _ in hour_hist[:3]]
        top_rows = [
            {
                "video_id": w.get("video_id"),
                "title": w.get("title"),
                "views": int(w.get("views") or 0),
                "likes": int(w.get("likes") or 0),
                "comments": int(w.get("comments") or 0),
                "published_at": w.get("published_at"),
            }
            for w in winners
        ]
        # Evergreen vs ephemeral tags — harvest prefers lasting search demand.
        from src.agents.smm_harvest_bridge import load_evergreen_cfg, tag_winner_evergreen

        eg_cfg = load_evergreen_cfg(self.smm_cfg)
        eg_tagged = tag_winner_evergreen(top_rows, channel=ch, evergreen_cfg=eg_cfg)
        top_rows = eg_tagged.get("top") or top_rows
        # Prefer evergreen hooks first when bias is on.
        hooks = [self._title_hook(str(w.get("title") or "")) for w in winners[:5]]
        if bool(eg_cfg.get("evergreen_bias", True)) and eg_tagged.get("evergreen_hooks"):
            eg_hooks = [
                self._title_hook(str(h)) for h in eg_tagged["evergreen_hooks"][:5]
            ]
            hooks = list(dict.fromkeys([*eg_hooks, *hooks]))[:5]
        patterns = {
            "avg_title_len": int(sum(title_lens) / len(title_lens)) if title_lens else 0,
            "median_winner_views": int(median([int(w.get("views") or 0) for w in winners]))
            if winners
            else 0,
            "what_if_title_share": round(what_if_n / max(len(winners), 1), 2),
            "title_hooks": hooks,
            "evergreen_share": eg_tagged.get("evergreen_share"),
            "ephemeral_share": eg_tagged.get("ephemeral_share"),
            "evergreen_hooks": eg_tagged.get("evergreen_hooks") or [],
            "publish_hour_histogram": [
                {"hour_local": h, "weight": w} for h, w in hour_hist[:8]
            ],
            "best_publish_hours_local": best_hours,
            "note": (
                f"Positive {ch} signals — packaging inspiration for that sheet's "
                "farm public videos; do not auto-edit non-farm uploads. "
                "Evergreen bias prefers lasting themes over newsjacking."
            ),
        }
        return {
            "ok": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "channel": ch,
            "sample": len(ranked),
            "channel_id": ranked[0].get("channel_id") if ranked else None,
            "channel_median_views": med,
            "top": top_rows,
            "patterns": patterns,
        }

    @staticmethod
    def _publish_hour_histogram(
        videos: list[dict[str, Any]], *, tz_name: str = "Asia/Karachi"
    ) -> list[tuple[int, float]]:
        """View-weighted histogram of upload hours in audience TZ from public videos.

        Uses YouTube Data API ``publishedAt`` already fetched for channel winners —
        no Analytics API. Weight = views (min 1) so stronger uploads dominate.
        """
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        weights = [0.0] * 24
        for v in videos:
            raw = str(v.get("published_at") or "").strip()
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local_h = int(dt.astimezone(tz).hour)
            w = float(max(int(v.get("views") or 0), 1))
            weights[local_h] += w
        ranked = sorted(
            ((h, weights[h]) for h in range(24) if weights[h] > 0),
            key=lambda x: x[1],
            reverse=True,
        )
        return ranked

    def propose_schedule_hours_for_channel(
        self, channel: str, *, learned: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Propose / soft-apply preferred publish hours from own-upload histogram."""
        from src.agents.schedule_agent import ScheduleAgent

        ch = self._normalize_channel(channel)
        payload = learned or self._channel_winners_payload(ch)
        patterns = (payload.get("patterns") or {}) if isinstance(payload, dict) else {}
        best = list(patterns.get("best_publish_hours_local") or [])
        min_n = int(
            (ScheduleAgent().cfg or {}).get("smm_min_videos_before_hour_propose")
            or self.smm_cfg.get("smm_min_videos_before_hour_propose")
            or 2
        )
        sample_n = int(payload.get("sample") or len(payload.get("top") or []) or 0)
        if sample_n < min_n or not best:
            return {
                "ok": False,
                "channel": ch,
                "skipped": True,
                "reason": "insufficient_hour_signal",
                "sample": sample_n,
                "min": min_n,
            }

        sched = ScheduleAgent(store=self.store, ledger=self.ledger)
        current = sched.publish_hour_for_channel(ch)
        proposed = int(best[0])
        if proposed == current:
            return {
                "ok": True,
                "channel": ch,
                "skipped": True,
                "reason": "already_at_best_hour",
                "hour": current,
            }

        # Always accept SMM hour proposals (no human publish_hour_approved gate).
        auto = bool(self.smm_cfg.get("auto_apply_schedule_hours", True))
        if not auto:
            auto = True

        bench = self.store.get_benchmarks()
        proposed_map = dict(bench.get("publish_hour_proposed_by_channel") or {})
        proposed_map[ch] = proposed
        bench["publish_hour_proposed_by_channel"] = proposed_map
        if ch == "napstorian":
            bench["publish_hour_proposed"] = proposed
        self.store.save_benchmarks(bench)

        out: dict[str, Any] = {
            "ok": True,
            "channel": ch,
            "current_hour": current,
            "proposed_hour": proposed,
            "best_hours": best[:3],
            "histogram": patterns.get("publish_hour_histogram") or [],
            "auto_apply": True,
            "applied": False,
            "requires_column": None,
        }
        result = sched.write_channel_preferred_hours(
            ch, best[:3], source="smm_upload_histogram", soft=False
        )
        out["apply_result"] = result
        out["applied"] = bool(result.get("applied"))
        self.ledger.write(
            agent="smm",
            problem=f"schedule hour for {ch}: propose={proposed} current={current}",
            action=(
                "wrote preferred_hours (always-accept)"
                if out["applied"]
                else result.get("reason") or "proposed only"
            ),
            severity="info",
            extra=out,
        )
        return out

    @staticmethod
    def _title_hook(title: str) -> str:
        t = (title or "").strip()
        if not t:
            return ""
        # Keep short hook fragment for packaging inspiration
        return t if len(t) <= 72 else t[:69] + "…"

    def _list_own_channel_videos(
        self, *, channel: str | None = None, max_n: int = 30
    ) -> list[dict[str, Any]]:
        """List recent public uploads for one Brand Account.

        Resolves UC id via config map → farm video_id → OAuth mine=True.
        Prefers API-key listing when channel_id is known (no extra OAuth scopes).
        """
        ch = self._normalize_channel(channel)
        channel_id = self._youtube_channel_id_for(ch)
        if not channel_id:
            channel_id = self._infer_channel_id_from_farm(ch) or ""

        if channel_id:
            via_key = self._list_channel_videos_api_key(channel_id, max_n=max_n)
            if via_key:
                self._cache_youtube_channel_id(ch, channel_id)
                return via_key

        # OAuth mine=True needs youtube.force-ssl / youtube.readonly (re-auth)
        try:
            youtube = self._build_youtube(ch)
            if channel_id:
                resp = (
                    youtube.channels()
                    .list(part="contentDetails,snippet", id=channel_id)
                    .execute()
                )
            else:
                resp = (
                    youtube.channels()
                    .list(part="contentDetails,snippet", mine=True)
                    .execute()
                )
            items = resp.get("items") or []
            if not items:
                return []

            item0 = items[0]
            resolved_channel = item0.get("id") or channel_id
            if resolved_channel:
                self._cache_youtube_channel_id(ch, str(resolved_channel))
            related = (item0.get("contentDetails") or {}).get("relatedPlaylists") or {}
            uploads_id = (related.get("uploads") or "").strip()
            if not uploads_id:
                return []

            pl = (
                youtube.playlistItems()
                .list(
                    part="snippet,contentDetails",
                    playlistId=uploads_id,
                    maxResults=max_n,
                )
                .execute()
            )
            vids: list[str] = []
            meta_by_id: dict[str, dict[str, Any]] = {}
            for it in pl.get("items") or []:
                sn = it.get("snippet") or {}
                cd = it.get("contentDetails") or {}
                vid = (
                    cd.get("videoId")
                    or (sn.get("resourceId") or {}).get("videoId")
                    or ""
                ).strip()
                if not vid:
                    continue
                vids.append(vid)
                meta_by_id[vid] = {
                    "title": (sn.get("title") or "").strip(),
                    "published_at": (
                        cd.get("videoPublishedAt") or sn.get("publishedAt") or ""
                    ).strip(),
                }

            out: list[dict[str, Any]] = []
            for i in range(0, len(vids), 50):
                chunk = vids[i : i + 50]
                vresp = (
                    youtube.videos()
                    .list(part="snippet,statistics,status", id=",".join(chunk))
                    .execute()
                )
                for item in vresp.get("items") or []:
                    st = item.get("statistics") or {}
                    sn = item.get("snippet") or {}
                    status = item.get("status") or {}
                    if (status.get("privacyStatus") or "") != "public":
                        continue
                    vid = item.get("id") or ""
                    out.append(
                        {
                            "video_id": vid,
                            "title": (
                                sn.get("title")
                                or meta_by_id.get(vid, {}).get("title")
                                or ""
                            ).strip(),
                            "published_at": (
                                sn.get("publishedAt")
                                or meta_by_id.get(vid, {}).get("published_at")
                                or ""
                            ).strip(),
                            "channel_id": resolved_channel,
                            "sheet_channel": ch,
                            "views": int(st.get("viewCount") or 0),
                            "likes": int(st.get("likeCount") or 0),
                            "comments": int(st.get("commentCount") or 0),
                        }
                    )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("smm list own channel via oauth failed ch=%s: %s", ch, exc)
            return []

    def _infer_channel_id_from_farm(self, channel: str | None = None) -> str | None:
        """Use a farm video_id for this sheet channel + API key → Brand UC id."""
        want = self._normalize_channel(channel) if channel else None
        vid = None
        for job in self.store.list_jobs():
            if not job.video_id:
                continue
            if want:
                job_ch = self._job_channel(job)
                if job_ch != want:
                    continue
            vid = job.video_id
            break
        if not vid and want is None:
            for job in self.store.list_jobs():
                if job.video_id:
                    vid = job.video_id
                    break
        if not vid:
            return None
        try:
            import httpx

            from src.agents.youtube_data import YouTubeDataClient

            api_key = getattr(self.s, "youtube_api_key", None) or ""
            if not api_key:
                return None
            yt = YouTubeDataClient(httpx.Client(timeout=30.0), api_key)
            st = yt.videos_stats([vid]).get(vid) or {}
            cid = (st.get("channel_id") or "").strip()
            if cid and want:
                self._cache_youtube_channel_id(want, cid)
            return cid or None
        except Exception as exc:  # noqa: BLE001
            logger.info("smm infer channel_id failed: %s", exc)
            return None

    def _list_channel_videos_api_key(
        self, channel_id: str, *, max_n: int = 30
    ) -> list[dict[str, Any]]:
        if not channel_id:
            return []
        try:
            import httpx

            from src.agents.youtube_data import YouTubeDataClient

            api_key = getattr(self.s, "youtube_api_key", None) or ""
            if not api_key:
                return []
            yt = YouTubeDataClient(httpx.Client(timeout=30.0), api_key)
            bundle = yt.channels_bundle([channel_id]).get(channel_id) or {}
            uploads = yt.recent_uploads(
                bundle.get("uploads_playlist_id") or "", max_n=max_n
            )
            stats = yt.videos_stats([u["video_id"] for u in uploads if u.get("video_id")])
            out: list[dict[str, Any]] = []
            for u in uploads:
                vid = u.get("video_id") or ""
                st = stats.get(vid) or {}
                out.append(
                    {
                        "video_id": vid,
                        "title": st.get("title") or u.get("title"),
                        "published_at": st.get("published_at") or u.get("published_at"),
                        "channel_id": channel_id,
                        "views": int(st.get("views") or 0),
                        "likes": int(st.get("likes") or 0),
                        "comments": int(st.get("comments") or 0),
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("smm api-key channel list failed: %s", exc)
            return []

    # ------------------------------------------------------------------ evaluate / apply
    def evaluate_video(
        self,
        *,
        video_id: str | None,
        title: str,
        metrics: dict[str, Any] | None = None,
        apply_actions: bool = False,
        job: JobRecord | None = None,
    ) -> AlgoInsight:
        """Compare metrics to benchmarks; propose (and optionally apply) actions."""
        metrics = metrics or self._fetch_or_stub_metrics(video_id)
        bench = self.store.get_benchmarks()
        vs: dict[str, Any] = {}
        proposals: list[dict[str, Any]] = []
        failing = False
        source = metrics.get("source")
        if source is None:
            if any(
                metrics.get(k) is not None
                for k in ("avd_pct", "first_60s_retention_pct", "ctr_pct")
            ):
                source = "provided"
            else:
                source = "stub"
        source = str(source)
        live = source not in {"stub", "private_pending"}

        def _num(key: str) -> float | None:
            v = metrics.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        avd = _num("avd_pct")
        ret60 = _num("first_60s_retention_pct")
        ctr = _num("ctr_pct")
        soft_auto = bool(self.smm_cfg.get("auto_apply_soft_packaging", False))
        job_ch = self._job_channel(job) if job is not None else self._normalize_channel(None)
        # Locked winners: never thrash packaging / open new lever experiments
        video_locked = False
        try:
            from src.services.smm_pipeline_ab import is_video_locked

            video_locked = bool(video_id and is_video_locked(video_id))
        except Exception:  # noqa: BLE001
            video_locked = False
        if video_locked:
            soft_auto = False
            vs["locked_winner"] = {
                "ok": True,
                "value": True,
                "note": "winner locked — skip lever thrash / soft packaging",
            }
        cw_payload = self._channel_winners_payload(job_ch)
        winner_hooks = (
            ((cw_payload.get("patterns") or {}).get("title_hooks"))
            or ((bench.get("channel_winners") or {}).get("patterns") or {}).get(
                "title_hooks"
            )
            or []
        )

        if live and ctr is not None:
            ok = ctr >= float(
                self.smm_cfg.get("ctr_pct_min") or bench.get("ctr_pct_min") or 4
            )
            vs["ctr_pct"] = {
                "value": ctr,
                "min": self.smm_cfg.get("ctr_pct_min") or bench.get("ctr_pct_min"),
                "ok": ok,
            }
            if not ok:
                change = "rewrite title + description + tags + thumbnail for stronger CTR"
                if winner_hooks:
                    change += f" — inspire from winners: {', '.join(winner_hooks[:3])}"
                proposals.append(
                    {
                        "level": "soft",
                        "type": "packaging",
                        "change": change,
                        "winner_hooks": winner_hooks[:5],
                        "auto_apply": soft_auto,
                    }
                )
                failing = True
        else:
            vs["ctr_pct"] = {
                "value": ctr,
                "min": bench.get("ctr_pct_min"),
                "ok": None,
                "skipped": True,
                "reason": source,
            }

        # Surface tag / policy-risk from prior publish failures (observe -> alerts file)
        if job is not None:
            meta = dict(job.meta or {})
            blob = " ".join(
                str(meta.get(k) or "")
                for k in (
                    "last_error",
                    "publish_error",
                    "smm_pin_error",
                    "error",
                    "yt_error",
                )
            ).lower()
            if (
                "invalidtag" in blob
                or "invalid tag" in blob
                or ("policy" in blob and "reject" in blob)
            ):
                proposals.append(
                    {
                        "level": "soft",
                        "type": "policy_risk",
                        "change": (
                            "tag/title policy-risk signal in job meta — "
                            "sanitize tags before soft packaging"
                        ),
                        "auto_apply": False,
                    }
                )

        if live and (ret60 is not None or avd is not None):
            ok_avd = avd is None or avd >= float(
                self.smm_cfg.get("avd_pct_min") or bench.get("avd_pct_min") or 40
            )
            vs["avd_pct"] = {
                "value": avd,
                "min": self.smm_cfg.get("avd_pct_min") or bench.get("avd_pct_min"),
                "ok": None if avd is None else ok_avd,
                "auto_apply": False,
                "policy": "advisory_only",
            }
            if ret60 is not None:
                ok60 = ret60 >= float(
                    self.smm_cfg.get("first_60s_retention_pct_min")
                    or bench.get("first_60s_retention_pct_min")
                    or 70
                )
                vs["first_60s"] = {
                    "value": ret60,
                    "min": self.smm_cfg.get("first_60s_retention_pct_min")
                    or bench.get("first_60s_retention_pct_min"),
                    "ok": ok60,
                    "auto_apply": False,
                    "source": metrics.get("first_60s_source")
                    or "youtube_analytics_audience_retention",
                }
                if not ok60:
                    proposals.append(
                        {
                            "level": "medium",
                            "type": "hook_failure",
                            "change": (
                                "rewrite cold open (first 15–30s) — retention cliff "
                                "in opening; apply config/prompts/hook_cold_open.txt "
                                f"for channel={job_ch}"
                            ),
                            "auto_apply": False,
                        }
                    )
                    failing = True
            else:
                vs["first_60s"] = {
                    "value": None,
                    "ok": None,
                    "skipped": True,
                    "reason": metrics.get("first_60s_note")
                    or "audience_retention_unavailable",
                    "auto_apply": False,
                }
            if avd is not None and not ok_avd:
                proposals.append(
                    {
                        "level": "medium",
                        "type": "prompts_pacing",
                        "change": "tighten mid-video pacing + pattern interrupts",
                        "auto_apply": False,
                    }
                )
                failing = True
        else:
            vs["first_60s"] = {
                "value": ret60,
                "ok": None,
                "skipped": True,
                "reason": "metrics_not_live",
                "auto_apply": False,
            }
            vs["avd_pct"] = {
                "value": avd,
                "ok": None,
                "skipped": True,
                "auto_apply": False,
                "policy": "advisory_only",
            }

        # View proxy vs channel winners (positive bar) when we only have Data API stats
        views = _num("views")
        winner_med = float(
            ((cw_payload.get("patterns") or {}).get("median_winner_views"))
            or bench.get("winner_views_median")
            or 0
        )
        if live and views is not None and winner_med > 0 and ctr is None:
            vs["views_vs_winners"] = {
                "value": views,
                "winner_median": winner_med,
                "ok": views >= winner_med * 0.35,  # early / soft vs mature winners
            }

        streak = int(bench.get("failing_streak") or 0)
        if live and ctr is not None:
            streak = streak + 1 if failing else 0
            bench["failing_streak"] = streak
            bench["updated_at"] = datetime.now(timezone.utc).isoformat()

        need = int(bench.get("consecutive_fails_before_voice_propose") or 2)
        voice_requires = bool(self.cfg.get("voice_change_requires_approve", True))
        if live and failing and streak >= need and ctr is not None:
            suggested = (
                "am_puck" if self.s.kokoro_voice == "am_michael" else "am_michael"
            )
            voice_prop = {
                "level": "hard",
                "type": "voice",
                "change": (
                    f"CEO auto KOKORO_VOICE trial → {suggested}"
                    if not voice_requires
                    else "propose KOKORO_VOICE trial — requires voice_change_approved"
                ),
                "current_voice": self.s.kokoro_voice,
                "suggested_voice": suggested,
                "auto_apply": not voice_requires,
                "requires_column": None if not voice_requires else "voice_change_approved",
            }
            proposals.append(voice_prop)
            pending = list(bench.get("voice_proposals_pending") or [])
            pending.append(voice_prop)
            bench["voice_proposals_pending"] = pending[-5:]
            if not voice_requires:
                # Persist suggested voice onto job meta for next farm prep
                if job is not None:
                    meta = dict(job.meta or {})
                    meta["smm_suggested_voice"] = suggested
                    meta["ceo_voice_auto_at"] = datetime.now(timezone.utc).isoformat()
                    try:
                        self.store.update_job(job.id, meta=meta)
                    except Exception:  # noqa: BLE001
                        pass
                bench["ceo_active_voice_trial"] = suggested
                bench["updated_at"] = datetime.now(timezone.utc).isoformat()

        peak = metrics.get("peak_live_hour_local")
        if peak is None:
            # Fall back to channel winner upload-hour histogram (Data API publishedAt)
            best = (cw_payload.get("patterns") or {}).get("best_publish_hours_local") or []
            if best:
                peak = best[0]
        if peak is not None:
            try:
                from src.agents.schedule_agent import ScheduleAgent

                peak_h = int(peak)
                sched = ScheduleAgent(store=self.store, ledger=self.ledger)
                current_h = sched.publish_hour_for_channel(job_ch)
                if abs(peak_h - current_h) >= 2:
                    hour_prop = {
                        "level": "medium",
                        "type": "publish_hour",
                        "change": (
                            f"auto-apply publish_hour_local={peak_h} for {job_ch} "
                            f"(audience/upload peak) vs current={current_h}"
                        ),
                        "suggested_hour": peak_h,
                        "channel": job_ch,
                        "timezone": getattr(self.s, "publish_timezone", "Asia/Karachi"),
                        "auto_apply": True,
                        "requires_column": None,
                    }
                    proposals.append(hour_prop)
                    proposed_map = dict(
                        bench.get("publish_hour_proposed_by_channel") or {}
                    )
                    proposed_map[job_ch] = peak_h
                    bench["publish_hour_proposed_by_channel"] = proposed_map
                    bench["publish_hour_proposed"] = peak_h
            except (TypeError, ValueError):
                pass

        if live and ctr is not None:
            self.store.save_benchmarks(bench)

        applied: list[dict[str, Any]] = []
        if apply_actions and proposals:
            applied = self._apply_allowed_proposals(
                proposals, video_id=video_id, title=title, job=job
            )

        insight = AlgoInsight(
            id=f"algo_{uuid.uuid4().hex[:10]}",
            video_id=video_id,
            title=title,
            metrics=metrics,
            vs_benchmark=vs,
            proposals=proposals,
        )
        setattr(insight, "_applied_actions", applied)
        self.store.add_algo_insight(insight)
        try:
            alert_props = list(proposals)
            if job is not None:
                from src.agents.smm_sop import (
                    check_new_format_sop,
                    enforce_new_format_sop_enabled,
                )

                if enforce_new_format_sop_enabled(self.smm_cfg):
                    sop = check_new_format_sop(
                        job,
                        smm_cfg=self.smm_cfg,
                        agents_cfg=self.cfg,
                        for_public=(job.status == "public"),
                    )
                    alert_props.extend(list(sop.get("proposals") or []))
            self._write_quality_alerts(
                video_id=video_id,
                title=title,
                job=job,
                proposals=alert_props,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("smm quality alerts failed: %s", exc)

        self.ledger.write(
            agent="smm",
            problem=f"metrics eval for {title[:60]} (streak={streak} source={source})",
            action=(
                f"{len(proposals)} proposals, applied={len(applied)}"
                if proposals
                else "benchmarks OK / watching"
            ),
            severity="warn" if proposals else "info",
            publish_status="public" if live else "private",
            extra={
                "video_id": video_id,
                "proposals": proposals,
                "applied": applied,
                "vs": vs,
            },
        )
        return insight

    def _apply_allowed_proposals(
        self,
        proposals: list[dict[str, Any]],
        *,
        video_id: str | None,
        title: str,
        job: JobRecord | None,
    ) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        try:
            from src.services.smm_pipeline_ab import is_video_locked

            if video_id and is_video_locked(video_id):
                return [
                    {
                        "type": "skipped_locked_winner",
                        "ok": True,
                        "video_id": video_id,
                        "note": "winner locked — no packaging/lever apply",
                    }
                ]
        except Exception:  # noqa: BLE001
            pass
        for prop in proposals:
            if not prop.get("auto_apply"):
                continue
            ptype = prop.get("type")
            try:
                if ptype == "packaging" and video_id and job and job.job_dir:
                    if self._job_is_shorts(job):
                        applied.append(
                            {
                                "type": "packaging",
                                "ok": True,
                                "skipped": True,
                                "reason": "shorts_funnel — do not rewrite parent longform",
                            }
                        )
                        continue
                    result = self._apply_soft_packaging(job, video_id=video_id)
                    applied.append({"type": "packaging", **result})
                elif ptype == "voice" and not bool(
                    self.cfg.get("voice_change_requires_approve", True)
                ):
                    meta = dict(job.meta or {}) if job else {}
                    meta["smm_suggested_voice"] = prop.get("suggested_voice")
                    if job:
                        self.store.update_job(job.id, meta=meta)
                    applied.append(
                        {
                            "type": "voice",
                            "ok": True,
                            "suggested": prop.get("suggested_voice"),
                            "note": "recorded suggestion only",
                        }
                    )
                elif ptype == "publish_hour" and prop.get("suggested_hour") is not None:
                    from src.agents.schedule_agent import ScheduleAgent

                    ch = self._normalize_channel(
                        prop.get("channel")
                        or (self._job_channel(job) if job else None)
                    )
                    hour = int(prop["suggested_hour"])
                    result = ScheduleAgent(
                        store=self.store, ledger=self.ledger
                    ).write_channel_preferred_hours(
                        ch,
                        [hour],
                        source="smm_eval_always_accept",
                        soft=False,
                    )
                    applied.append({"type": "publish_hour", "channel": ch, **result})
            except Exception as exc:  # noqa: BLE001
                logger.exception("smm apply failed type=%s", ptype)
                applied.append({"type": ptype, "ok": False, "error": str(exc)})
        return applied

    def _write_quality_alerts(
        self,
        *,
        video_id: str | None,
        title: str,
        job: JobRecord | None,
        proposals: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Write packaging / tag / policy-risk proposals to ops digest markdown.

        Observe-first surface — does not auto-apply. Voice/reply stay human-gated.
        """
        alert_props: list[dict[str, Any]] = []
        for prop in proposals:
            if not isinstance(prop, dict):
                continue
            ptype = str(prop.get("type") or "").lower()
            change = str(prop.get("change") or "").lower()
            if ptype in {
                "packaging",
                "tags",
                "policy_risk",
                "title",
                "hook_failure",
                "prompts_pacing",
                "daily_scorecard",
                "sop_compliance",
            }:
                alert_props.append(prop)
            elif "tag" in change or "policy" in change or "invalidtag" in change:
                alert_props.append(prop)
            elif "hook" in change or "retention" in change or "cold open" in change:
                alert_props.append(prop)
            elif "sop" in change or "overlay" in change:
                alert_props.append(prop)
        if not alert_props:
            return None

        path = _smm_quality_alerts_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        at = datetime.now(timezone.utc).isoformat()
        jid = job.id if job else None
        nl = chr(10)
        has_sop = any(
            str(p.get("type") or "").lower() == "sop_compliance" for p in alert_props
        )
        lines = [
            "# SMM quality alerts",
            "",
            f"- At: `{at}`",
            f"- Job: `{jid}`",
            f"- Video: `{video_id}`",
            f"- Title: {(title or '')[:120]}",
            "",
            (
                "## Proposals (SOP compliance / packaging / retention / scorecard)"
                if has_sop
                else "## Proposals (packaging / retention / scorecard)"
            ),
            "",
            "| level | type | auto_apply | change |",
            "|---|---|---|---|",
        ]
        for prop in alert_props:
            change = str(prop.get("change") or "").replace("|", "/")[:180]
            lines.append(
                f"| {prop.get('level')} | {prop.get('type')} | "
                f"{prop.get('auto_apply')} | {change} |"
            )
        lines.extend(
            [
                "",
                "_Quality-first: soft packaging applies only when CTR fails and "
                "`auto_apply_soft_packaging=true`. Voice / auto-reply stay human-gated. "
                "SOP compliance (`type=sop_compliance`) is a soft alert by default "
                "(`enforce_new_format_sop=true`, `sop_block_publish=false`, "
                "`sop_stage_gates=true` / `sop_stage_gates_hard=false`) — "
                "stage gates soft-audit by default; never blocks Live mid-stream._",
                "",
            ]
        )
        path.write_text(nl.join(lines), encoding="utf-8")
        logger.info(
            "smm quality alerts written n=%s path=%s video=%s",
            len(alert_props),
            path,
            video_id,
        )
        return {"ok": True, "n": len(alert_props), "path": str(path)}

    def _apply_soft_packaging(self, job: JobRecord, *, video_id: str) -> dict[str, Any]:
        from src.services.publish_youtube import PublishModule
        from src.services.youtube_meta import load_youtube_meta
        from src.services.youtube_meta_generate import generate_youtube_pack

        ch = self._job_channel(job)
        job_dir = Path(job.job_dir)
        pack = generate_youtube_pack(
            job_dir,
            script_path=job_dir / "script" / "script.json",
            seed_title=job.title,
            force=True,
            channel=ch,
        )
        meta = load_youtube_meta(pack.meta_dir)
        out = PublishModule(channel=ch).update_packaging(
            video_id,
            title=meta["title"],
            description=meta["description"],
            tags=meta["tags"],
            thumbnail_path=meta.get("thumbnail_path"),
        )
        self.ledger.write(
            agent="smm",
            problem=f"auto soft packaging applied for {job.title[:50]}",
            action=f"updated YT title/description/tags/thumbnail channel={ch}",
            severity="warn",
            job_id=job.id,
            extra={"video_id": video_id, "title": meta["title"], "channel": ch},
        )
        return {"ok": True, "title": meta["title"], "update": out, "channel": ch}

    # ------------------------------------------------------------------ comments
    def draft_comment_replies(
        self, comments: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """Draft only — never auto-send unless auto_reply_comments=true (still off by default)."""
        drafts = []
        for c in comments:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            drafts.append(
                {
                    "comment_id": c.get("id") or "",
                    "original": text,
                    "draft_reply": (
                        "Thanks for watching — what alternate timeline would you explore next? "
                        "(draft — human send required)"
                    ),
                }
            )
        self.ledger.write(
            agent="smm",
            problem=f"drafted {len(drafts)} comment replies",
            action=(
                "auto-send enabled"
                if bool(self.cfg.get("auto_reply_comments"))
                else "NO auto-send — human approve only"
            ),
            severity="info",
        )
        return drafts

    def pin_comment_plan(self, pinned_text: str) -> dict[str, str]:
        return {
            "action": "pin_comment",
            "text": pinned_text,
            "auto": "true",
            "note": "SMM pins on public go-live; replies stay manual",
        }

    def pin_engagement_comment(
        self,
        video_id: str,
        *,
        title: str = "",
        text: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Post at most one engagement comment per video_id (no title prefix)."""
        _ = title  # kept for call-site compat; never prepended to the body
        vid = (video_id or "").strip()
        ch = self._normalize_channel(channel)
        by_ch = self.smm_cfg.get("pin_comment_text_by_channel") or {}
        channel_pin = ""
        if isinstance(by_ch, dict):
            channel_pin = str(by_ch.get(ch) or "").strip()
        body = (
            text
            or channel_pin
            or self.smm_cfg.get("pin_comment_text")
            or DEFAULT_PIN
        ).strip()
        # Strip legacy "Watching '…'?" spam prefix if somehow still present.
        if body.lower().startswith("watching "):
            q = body.find("? ")
            if q != -1:
                body = body[q + 2 :].strip() or body
        plan = self.pin_comment_plan(body)

        prior = _pin_already_claimed(vid)
        if prior:
            return {
                "ok": True,
                "action": "pin_comment",
                "skipped": True,
                "reason": "already_claimed_for_video",
                "video_id": vid,
                "comment_id": prior.get("comment_id"),
                "text": prior.get("text") or body,
                "plan": plan,
            }

        # Claim BEFORE insert so a successful post with a weird response shape
        # (or a later meta wipe) cannot spam another comment on the next beat.
        _claim_pin_video(vid, channel=ch, text=body, status="claimed")

        try:
            youtube = self._build_youtube(ch)
            resp = (
                youtube.commentThreads()
                .insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "videoId": vid,
                            "topLevelComment": {
                                "snippet": {"textOriginal": body[:9000]}
                            },
                        }
                    },
                )
                .execute()
            )
            comment_id = (
                ((resp.get("snippet") or {}).get("topLevelComment") or {}).get("id")
                or resp.get("id")
            )
            pinned = False
            pin_err = None
            if comment_id:
                try:
                    youtube.comments().setModerationStatus(
                        id=comment_id, moderationStatus="published"
                    ).execute()
                except Exception:  # noqa: BLE001
                    pass
                pinned = True
            _claim_pin_video(
                vid,
                channel=ch,
                comment_id=str(comment_id) if comment_id else None,
                text=body,
                status="posted",
            )
            result = {
                "ok": True,
                "action": "pin_comment",
                "video_id": vid,
                "comment_id": comment_id,
                "text": body,
                "pinned": pinned,
                "pin_error": pin_err,
                "plan": plan,
            }
            self.ledger.write(
                agent="smm",
                problem=f"posted engagement comment on {vid}",
                action=f"comment_id={comment_id}",
                severity="info",
                extra=result,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            logger.warning("smm pin comment failed video=%s: %s", vid, exc)
            _claim_pin_video(
                vid, channel=ch, text=body, status=f"failed:{err[:120]}"
            )
            self.ledger.write(
                agent="smm",
                problem=f"pin comment failed on {vid}",
                action="soft-fail — upload unaffected (check YouTube OAuth scopes)",
                severity="warn",
                extra={"error": err, "plan": plan},
            )
            return {"ok": False, "action": "pin_comment", "error": err, "plan": plan}

    # ------------------------------------------------------------------ metrics / privacy
    def _yt_privacy(
        self, video_id: str | None, *, channel: str | None = None
    ) -> str | None:
        """Return YouTube privacyStatus. API key sees public; private → None."""
        if not video_id:
            return None
        ch = self._normalize_channel(channel)
        # API key path (works for public videos; avoids upload-only OAuth gaps)
        try:
            import httpx

            api_key = getattr(self.s, "youtube_api_key", None) or ""
            if api_key:
                resp = httpx.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={
                        "part": "status",
                        "id": video_id,
                        "key": api_key,
                    },
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    items = resp.json().get("items") or []
                    if items:
                        return (items[0].get("status") or {}).get("privacyStatus")
                    # Empty items ⇒ private/unlisted-not-listed under API key
                    return "private"
        except Exception as exc:  # noqa: BLE001
            logger.info("smm privacy api-key check failed %s: %s", video_id, exc)

        try:
            youtube = self._build_youtube(ch)
            resp = youtube.videos().list(part="status", id=video_id).execute()
            items = resp.get("items") or []
            if not items:
                return None
            return (items[0].get("status") or {}).get("privacyStatus")
        except Exception as exc:  # noqa: BLE001
            logger.info("smm privacy oauth check failed %s ch=%s: %s", video_id, ch, exc)
            return None

    def _fetch_or_stub_metrics(
        self,
        video_id: str | None,
        *,
        privacy: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Prefer Analytics CTR/AVD + Data API stats; fall back to stub / private_pending."""
        ch = self._normalize_channel(channel)
        if not video_id:
            return {
                "video_id": None,
                "avd_pct": None,
                "first_60s_retention_pct": None,
                "ctr_pct": None,
                "impressions": None,
                "source": "stub",
            }
        if privacy == "private":
            return {
                "video_id": video_id,
                "avd_pct": None,
                "first_60s_retention_pct": None,
                "ctr_pct": None,
                "impressions": None,
                "source": "private_pending",
                "note": "private — SMM waits until public",
            }

        base: dict[str, Any] = {
            "video_id": video_id,
            "avd_pct": None,
            "first_60s_retention_pct": None,
            "ctr_pct": None,
            "impressions": None,
            "source": "stub",
        }
        duration_s: float | None = None

        try:
            youtube = self._build_youtube(ch)
            resp = (
                youtube.videos()
                .list(part="statistics,status,contentDetails", id=video_id)
                .execute()
            )
            items = resp.get("items") or []
            if not items:
                raise RuntimeError("video not found")
            stats = items[0].get("statistics") or {}
            status = items[0].get("status") or {}
            details = items[0].get("contentDetails") or {}
            yt_privacy = status.get("privacyStatus")
            duration_s = _iso8601_duration_seconds(details.get("duration"))
            if yt_privacy == "private":
                return {
                    "video_id": video_id,
                    "avd_pct": None,
                    "first_60s_retention_pct": None,
                    "ctr_pct": None,
                    "impressions": None,
                    "privacyStatus": yt_privacy,
                    "source": "private_pending",
                }
            base = {
                "video_id": video_id,
                "views": int(stats.get("viewCount") or 0),
                "likes": int(stats.get("likeCount") or 0),
                "comments": int(stats.get("commentCount") or 0),
                "privacyStatus": yt_privacy,
                "duration_s": duration_s,
                "avd_pct": None,
                "first_60s_retention_pct": None,
                "ctr_pct": None,
                "impressions": None,
                "source": "youtube_data_stats",
                "note": (
                    "Data API stats only — Analytics CTR/AVD pending or unavailable"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.info("smm metrics data-api fallback stub: %s", exc)
            base = {
                "video_id": video_id,
                "avd_pct": None,
                "first_60s_retention_pct": None,
                "ctr_pct": None,
                "impressions": None,
                "source": "stub",
                "note": f"metrics unavailable: {exc}",
            }

        analytics = self._fetch_youtube_analytics_metrics(
            video_id, channel=ch, duration_s=duration_s or base.get("duration_s")
        )
        if analytics:
            for key in (
                "ctr_pct",
                "avd_pct",
                "impressions",
                "average_view_duration_s",
                "estimated_minutes_watched",
                "subscribers_gained",
                "subscribers_lost",
                "start_date",
                "end_date",
                "first_60s_retention_pct",
            ):
                if analytics.get(key) is not None:
                    base[key] = analytics[key]
            # Prefer Analytics views when present (lifetime window may differ).
            if analytics.get("views") is not None and base.get("views") is None:
                base["views"] = analytics["views"]
            base["source"] = analytics.get("source") or "youtube_analytics"
            if analytics.get("note"):
                base["analytics_note"] = analytics["note"]
            base.pop("note", None)
            if analytics.get("ops_note"):
                base["ops_note"] = analytics["ops_note"]
                base["analytics_scope_ok"] = False
            else:
                base["analytics_scope_ok"] = True
            if analytics.get("ctr_unavailable"):
                base["ctr_unavailable"] = True
            if analytics.get("ctr_pending_reporting"):
                base["ctr_pending_reporting"] = True
            if analytics.get("ctr_source"):
                base["ctr_source"] = analytics["ctr_source"]
            if analytics.get("reports_available") is not None:
                base["reports_available"] = analytics["reports_available"]
            if analytics.get("reporting"):
                base["reporting"] = analytics["reporting"]
            if analytics.get("first_60s_source"):
                base["first_60s_source"] = analytics["first_60s_source"]
            if analytics.get("first_60s_unavailable"):
                base["first_60s_unavailable"] = True
            if analytics.get("first_60s_note"):
                base["first_60s_note"] = analytics["first_60s_note"]
        return base

    def _fetch_youtube_analytics_metrics(
        self,
        video_id: str,
        *,
        channel: str | None = None,
        duration_s: float | None = None,
    ) -> dict[str, Any] | None:
        """Pull CTR/AVD/impressions via YouTube Analytics API (per Brand Account)."""
        from src.services.publish_youtube import PublishModule
        from src.services.youtube_analytics import (
            fetch_video_analytics,
            is_analytics_scope_error,
            scope_error_ops_note,
            token_has_analytics_scope,
        )

        ch = self._normalize_channel(channel)
        channel_id = self._youtube_channel_id_for(ch)
        if not channel_id:
            return {
                "source": "youtube_analytics_skipped",
                "note": f"no youtube channel id for {ch}",
                "ctr_pct": None,
                "avd_pct": None,
            }

        pub = PublishModule(channel=ch)
        scopes = pub.token_scopes()
        has_scope = token_has_analytics_scope(scopes)
        lookback = int(self.smm_cfg.get("analytics_lookback_days") or 90)

        try:
            creds = pub._load_credentials()
            result = fetch_video_analytics(
                credentials=creds,
                channel_id=channel_id,
                video_id=video_id,
                lookback_days=lookback,
                duration_s=duration_s,
            )
            if not has_scope:
                # Unexpected success without declared scope — still mark ok.
                result["scope_declared"] = False
            return result
        except Exception as exc:  # noqa: BLE001
            if is_analytics_scope_error(exc) or not has_scope:
                note = scope_error_ops_note(ch, exc)
                logger.warning("smm analytics scope/API blocked ch=%s: %s", ch, exc)
                self._note_analytics_scope_block(ch, note)
                return {
                    "source": "youtube_analytics_scope_blocked",
                    "ctr_pct": None,
                    "avd_pct": None,
                    "ops_note": (
                        f"Analytics 403 for {ch} — re-auth with "
                        "yt-analytics.readonly (see "
                        "output/ops/smm_analytics_scope_needed.md)"
                    ),
                    "note": (
                        "Analytics 403/insufficient scopes — re-auth with "
                        "yt-analytics.readonly (see ops note / CLI)"
                    ),
                    "error": str(exc)[:400],
                }
            logger.info("smm analytics fetch failed ch=%s vid=%s: %s", ch, video_id, exc)
            return {
                "source": "youtube_analytics_error",
                "ctr_pct": None,
                "avd_pct": None,
                "note": f"analytics error: {exc}",
                "error": str(exc)[:400],
            }

    def _note_analytics_scope_block(self, channel: str, note: str) -> None:
        """Write once-per-day ops flag when Analytics scope is missing."""
        try:
            from src.agents.store import OPS_DIR

            OPS_DIR.mkdir(parents=True, exist_ok=True)
            path = OPS_DIR / "smm_analytics_scope_needed.md"
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            stamp = OPS_DIR / "smm_analytics_scope_stamp.txt"
            if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == today:
                # Still refresh body if channel-specific, but avoid ledger spam.
                pass
            else:
                stamp.write_text(today + "\n", encoding="utf-8")
                try:
                    self.ledger.write(
                        agent="smm",
                        problem="YouTube Analytics scope missing",
                        action="scorecard CTR/AVD blocked until re-auth",
                        severity="warn",
                        extra={"channel": channel},
                    )
                except Exception:  # noqa: BLE001
                    pass
            body = (
                f"# YouTube Analytics re-auth needed\n\n"
                f"- Day (UTC): `{today}`\n"
                f"- Channel: `{channel}`\n\n"
                f"{note}\n\n"
                "Both channels need `yt-analytics.readonly` for real CTR/AVD:\n"
                "- napstorian → `config/youtube_token.json`\n"
                "- napping_historian → `config/youtube_token_napping_historian.json`\n"
            )
            path.write_text(body, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.info("analytics scope ops note failed: %s", exc)

    # ------------------------------------------------------------------ daily scorecard + image reuse
    def _job_is_shorts(self, job: JobRecord | None) -> bool:
        if job is None:
            return False
        meta = dict(job.meta or {})
        if meta.get("kind") == "shorts" or meta.get("is_shorts"):
            return True
        return False

    def _shorts_funnel_slice(self, channel: str) -> dict[str, Any]:
        """Public Shorts metrics for the funnel scorecard (not longform bars)."""
        from src.agents.sheet_channels import shorts_tab_name
        from src.agents.title_queue import shorts_title_queue

        ch = self._normalize_channel(channel)
        videos: list[dict[str, Any]] = []
        try:
            tab = shorts_tab_name(ch)
            rows = shorts_title_queue(ch).list_rows(channel=tab)
        except Exception as exc:  # noqa: BLE001
            return {"videos": [], "n_public": 0, "error": str(exc)[:200]}
        for r in rows:
            vid = (r.short_video_id or r.video_id or "").strip()
            if not vid:
                continue
            if (r.status or "").lower() != "public" and not r.public_approved:
                continue
            metrics = self._fetch_or_stub_metrics(vid, privacy="public", channel=ch)
            videos.append(
                {
                    "video_id": vid,
                    "title": r.title,
                    "parent_title": r.parent_title,
                    "parent_video_id": r.parent_video_id,
                    "views": metrics.get("views"),
                    "ctr_pct": metrics.get("ctr_pct"),
                    "avd_pct": metrics.get("avd_pct"),
                    "impressions": metrics.get("impressions"),
                    "flag": "funnel",
                }
            )
        return {"videos": videos[:15], "n_public": len(videos)}

    def reuse_freeze_video_heavy(self) -> bool:
        return bool(self.smm_cfg.get("reuse_freeze_video_heavy", True))

    def maybe_write_daily_yt_scorecard(self, *, force: bool = False) -> dict[str, Any]:
        """Write once-per-UTC-day YT quality scorecard (both channels)."""
        if not bool(self.smm_cfg.get("daily_scorecard", True)) and not force:
            return {"skipped": True, "reason": "daily_scorecard_disabled"}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stamp = _smm_scorecard_stamp_path()
        if not force and stamp.exists():
            try:
                if stamp.read_text(encoding="utf-8").strip() == today:
                    return {"skipped": True, "reason": "already_wrote_today", "day": today}
            except OSError:
                pass
        payload = self.write_daily_yt_scorecard()
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(today + "\n", encoding="utf-8")
        except OSError as exc:
            logger.info("scorecard stamp failed: %s", exc)
        return payload

    def write_daily_yt_scorecard(self) -> dict[str, Any]:
        """Build markdown + jsonl scorecard from public jobs + Live snapshot."""
        from src.agents.store import OPS_DIR

        now = datetime.now(timezone.utc)
        ctr_min = float(self.smm_cfg.get("ctr_pct_min") or 4)
        avd_min = float(self.smm_cfg.get("avd_pct_min") or 40)
        channels = self._smm_live_channels()
        channel_blocks: dict[str, Any] = {}
        red_alerts: list[str] = []

        for ch in channels:
            rows: list[dict[str, Any]] = []
            for job in self.store.list_jobs():
                if self._job_channel(job) != ch:
                    continue
                if job.status != "public" or not job.video_id:
                    continue
                if self._job_is_shorts(job):
                    continue
                metrics = self._fetch_or_stub_metrics(
                    job.video_id, privacy="public", channel=ch
                )
                # Merge last algo insight metrics if present
                insight_m = self._latest_insight_metrics(job.video_id)
                for k in ("ctr_pct", "avd_pct", "first_60s_retention_pct"):
                    if metrics.get(k) is None and insight_m.get(k) is not None:
                        metrics[k] = insight_m[k]
                ctr = metrics.get("ctr_pct")
                avd = metrics.get("avd_pct")
                views = metrics.get("views")
                ctr_ok = None if ctr is None else float(ctr) >= ctr_min
                avd_ok = None if avd is None else float(avd) >= avd_min
                flag = "green"
                if ctr_ok is False or avd_ok is False:
                    flag = "red"
                    red_alerts.append(
                        f"{ch}: {job.title[:60]} CTR={ctr} AVD={avd} views={views}"
                    )
                elif ctr is None and avd is None:
                    flag = "amber"
                rows.append(
                    {
                        "job_id": job.id,
                        "video_id": job.video_id,
                        "title": job.title,
                        "views": views,
                        "likes": metrics.get("likes"),
                        "impressions": metrics.get("impressions"),
                        "ctr_pct": ctr,
                        "avd_pct": avd,
                        "first_60s_retention_pct": metrics.get("first_60s_retention_pct"),
                        "estimated_minutes_watched": metrics.get(
                            "estimated_minutes_watched"
                        ),
                        "flag": flag,
                        "metrics_source": metrics.get("source"),
                        "ops_note": metrics.get("ops_note"),
                        "analytics_note": metrics.get("analytics_note"),
                        "ctr_unavailable": bool(metrics.get("ctr_unavailable")),
                        "ctr_pending_reporting": bool(
                            metrics.get("ctr_pending_reporting")
                        ),
                        "ctr_source": metrics.get("ctr_source"),
                        "first_60s_note": metrics.get("first_60s_note"),
                        "first_60s_source": metrics.get("first_60s_source"),
                    }
                )
            rows.sort(key=lambda r: int(r.get("views") or 0), reverse=True)
            live_conc = None
            featured = None
            try:
                live_conc = self._own_live_concurrent_viewers(ch)
            except Exception:  # noqa: BLE001
                live_conc = None
            try:
                feat = self._load_featured_vods()
                featured = (feat.get("channels") or {}).get(ch) or feat.get(ch)
            except Exception:  # noqa: BLE001
                featured = None
            channel_blocks[ch] = {
                "videos": rows[:15],
                "top3": rows[:3],
                "live_concurrent": live_conc,
                "featured": featured,
                "n_public": len(rows),
                "n_red": sum(1 for r in rows if r.get("flag") == "red"),
                "shorts_funnel": self._shorts_funnel_slice(ch),
            }

        analytics_blocked = any(
            (r.get("metrics_source") or "").endswith("scope_blocked")
            or r.get("ops_note")
            for block in channel_blocks.values()
            for r in (block.get("videos") or [])
        )
        ctr_pending = any(
            r.get("ctr_pending_reporting") or (r.get("ctr_pct") is None and r.get("ctr_unavailable"))
            for block in channel_blocks.values()
            for r in (block.get("videos") or [])
        )
        n_with_ctr = sum(
            1
            for block in channel_blocks.values()
            for r in (block.get("videos") or [])
            if r.get("ctr_pct") is not None
        )
        n_with_avd = sum(
            1
            for block in channel_blocks.values()
            for r in (block.get("videos") or [])
            if r.get("avd_pct") is not None
        )
        note = (
            "CTR/AVD from YouTube Analytics when available; "
            "thumbnail CTR via Reporting reach CSVs when Analytics rejects "
            "impressions metrics. Soft packaging waits for real CTR (never invented). "
            "AVD fails → advisory pacing + retention dogs only (no auto rewrite). "
            "first-60s from audienceWatchRatio when rows exist; else skipped."
        )
        if ctr_pending and n_with_ctr == 0:
            note = (
                "CTR PENDING: Reporting channel_reach_basic_a1 job exists but daily "
                "CSV files not ready yet (~24–48h after job create) — scorecard keeps "
                "CTR/impressions null; AVD/views + retention dogs still active; "
                "soft packaging idle until CTR fills. " + note
            )
        elif n_with_ctr > 0:
            note = (
                f"CTR live on {n_with_ctr} video(s) — soft packaging can auto-apply "
                f"when CTR < {ctr_min}% and auto_apply_soft_packaging=true. " + note
            )
        if analytics_blocked:
            note = (
                "OPS: Analytics scope missing or API 403 — re-auth both channels "
                "with yt-analytics.readonly. See output/ops/smm_analytics_scope_needed.md. "
                + note
            )

        payload = {
            "at": now.isoformat(),
            "day_utc": now.strftime("%Y-%m-%d"),
            "bars": {
                "ctr_pct_min": ctr_min,
                "avd_pct_min": avd_min,
                "avd_policy": "advisory_only",
                "first_60s_policy": "advisory_when_available",
            },
            "channels": channel_blocks,
            "red_alerts": red_alerts,
            "reuse_freeze_video_heavy": self.reuse_freeze_video_heavy(),
            "analytics_scope_blocked": analytics_blocked,
            "ctr_pending_reporting": ctr_pending and n_with_ctr == 0,
            "n_with_ctr": n_with_ctr,
            "n_with_avd": n_with_avd,
            "note": note,
        }

        md_path = _smm_yt_scorecard_path()
        lines = [
            "# SMM daily YT scorecard",
            "",
            f"- At (UTC): `{payload['at']}`",
            f"- Bars: CTR ≥ {ctr_min}% (auto soft packaging) · "
            f"AVD ≥ {avd_min}% (advisory only) · first-60s ≥ 70% (advisory when available)",
            f"- Video-heavy reuse freeze: `{payload['reuse_freeze_video_heavy']}`",
            f"- Analytics scope blocked: `{analytics_blocked}`",
            f"- CTR pending Reporting CSVs: `{payload['ctr_pending_reporting']}`",
            f"- Videos with CTR / AVD: `{n_with_ctr}` / `{n_with_avd}`",
            f"- Shorts funnel: tracked separately (does not mix into longform CTR/AVD bars)",
            "",
            f"> {note}",
            "",
        ]
        for ch, block in channel_blocks.items():
            lines.append(f"## {ch}")
            lines.append(
                f"- Public tracked: {block['n_public']} · red: {block['n_red']} · "
                f"Live concurrent: {block.get('live_concurrent')}"
            )
            lines.append("")
            lines.append("| flag | views | impr | CTR | AVD | first60 | title |")
            lines.append("|---|---:|---:|---:|---:|---:|---|")
            for r in block["videos"][:10]:
                lines.append(
                    f"| {r['flag']} | {r.get('views')} | {r.get('impressions')} | "
                    f"{r.get('ctr_pct')} | {r.get('avd_pct')} | "
                    f"{r.get('first_60s_retention_pct')} | "
                    f"{(r.get('title') or '')[:70]} |"
                )
            shorts = block.get("shorts_funnel") or {}
            srows = shorts.get("videos") or []
            if srows:
                lines.append("")
                lines.append(
                    f"Shorts funnel (not in longform bars): {shorts.get('n_public', len(srows))} public"
                )
                lines.append("| views | CTR | title | parent |")
                lines.append("|---:|---:|---|---|")
                for r in srows[:8]:
                    lines.append(
                        f"| {r.get('views')} | {r.get('ctr_pct')} | "
                        f"{(r.get('title') or '')[:50]} | "
                        f"{(r.get('parent_title') or '')[:40]} |"
                    )
            pending_notes = [
                (r.get("analytics_note") or r.get("first_60s_note") or "")
                for r in block["videos"][:10]
                if r.get("ctr_pending_reporting")
                or r.get("analytics_note")
                or r.get("first_60s_note")
            ]
            if pending_notes:
                lines.append("")
                lines.append("Notes:")
                for n in pending_notes[:5]:
                    if n:
                        lines.append(f"- {n}")
            lines.append("")
        if red_alerts:
            lines.append("## Red alerts")
            lines.append(
                "_AVD/CTR red → retention dogs exclude from Live when alternatives exist. "
                "AVD does not auto-rewrite packaging._"
            )
            for a in red_alerts[:20]:
                lines.append(f"- {a}")
            lines.append("")
        if payload["ctr_pending_reporting"]:
            lines.append("## CTR pending (Reporting reach)")
            lines.append(
                "Jobs `channel_reach_basic_a1` are created; Google emits daily CSVs "
                "asynchronously (~24–48h). No manual download — next scorecard after "
                "files appear fills CTR/impressions. Soft packaging stays idle until then. "
                "See `output/ops/smm_analytics_scope_needed.md`.\n"
            )
        if analytics_blocked:
            lines.append("## Analytics re-auth")
            lines.append(
                "Tokens need `yt-analytics.readonly`. Re-auth each channel:\n\n"
                "```bash\n"
                ".venv/bin/python -m src.cli.youtube_auth --channel napstorian --print-url\n"
                ".venv/bin/python -m src.cli.youtube_auth --channel napstorian "
                '--complete "PASTE_REDIRECT_URL"\n'
                ".venv/bin/python -m src.cli.youtube_auth --channel napping_historian "
                "--print-url\n"
                ".venv/bin/python -m src.cli.youtube_auth --channel napping_historian "
                '--complete "PASTE_REDIRECT_URL"\n'
                "```\n"
            )
        lines.append(
            "_Daily cadence (not weekly). Sheet TitleQueue does **not** trigger this "
            "scorecard — cron `scan_and_act` + YouTube APIs do. "
            "Deep Studio retention graphs still useful 2–3×/week._\n"
        )
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("\n".join(lines), encoding="utf-8")

        jsonl = _smm_yt_scorecard_jsonl_path()
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")

        # Persist retention dogs for Live VOD exclusion (easy win).
        try:
            self._persist_retention_dogs(payload)
        except Exception as exc:  # noqa: BLE001
            logger.info("retention dogs persist failed: %s", exc)

        winner_locks: dict[str, Any] = {}
        try:
            from src.services.smm_pipeline_ab import maybe_lock_winner_from_scorecard

            goals = None
            try:
                from src.agents.ceo_smm import load_or_seed_goals

                goals = load_or_seed_goals()
            except Exception:  # noqa: BLE001
                goals = None
            winner_locks = maybe_lock_winner_from_scorecard(payload, goals=goals)
            if winner_locks.get("n_newly_locked"):
                self.ledger.write(
                    agent="smm",
                    problem="auto-lock scorecard winners",
                    action=(
                        f"locked {winner_locks.get('n_newly_locked')} video(s): "
                        f"{[x.get('video_id') for x in (winner_locks.get('newly_locked') or [])]}"
                    ),
                    severity="info",
                    extra={"winner_locks": winner_locks},
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("winner auto-lock failed: %s", exc)
            winner_locks = {"ok": False, "error": str(exc)[:300]}

        if red_alerts and bool(self.smm_cfg.get("retention_cliff_alert", True)):
            self._write_quality_alerts(
                job=None,
                video_id="daily_scorecard",
                title="Daily YT scorecard reds",
                proposals=[
                    {
                        "level": "soft",
                        "type": "daily_scorecard",
                        "auto_apply": False,
                        "change": a,
                    }
                    for a in red_alerts[:10]
                ],
            )

        self.ledger.write(
            agent="smm",
            problem="daily YT scorecard",
            action=f"wrote {md_path.name} reds={len(red_alerts)}",
            severity="warn" if red_alerts or analytics_blocked else "info",
            extra={
                "day": payload["day_utc"],
                "n_red": len(red_alerts),
                "analytics_scope_blocked": analytics_blocked,
                "n_newly_locked": (winner_locks or {}).get("n_newly_locked"),
            },
        )
        return {
            "ok": True,
            "path": str(md_path),
            "payload": payload,
            "winner_locks": winner_locks,
        }

    def _latest_insight_metrics(self, video_id: str) -> dict[str, Any]:
        """Best-effort pull CTR/AVD from stored algo insights."""
        try:
            insights = self.store.list_algo_insights(limit=80)
        except Exception:  # noqa: BLE001
            return {}
        for row in reversed(list(insights)):
            vid = getattr(row, "video_id", None)
            if str(vid or "") != str(video_id):
                continue
            metrics = getattr(row, "metrics", None) or {}
            if isinstance(metrics, dict) and any(
                metrics.get(k) is not None
                for k in ("ctr_pct", "avd_pct", "first_60s_retention_pct")
            ):
                return dict(metrics)
            vs = getattr(row, "vs_benchmark", None) or {}
            out: dict[str, Any] = {}
            if isinstance(vs, dict):
                for key, out_key in (
                    ("ctr_pct", "ctr_pct"),
                    ("avd_pct", "avd_pct"),
                    ("first_60s", "first_60s_retention_pct"),
                ):
                    cell = vs.get(key)
                    if isinstance(cell, dict) and cell.get("value") is not None:
                        out[out_key] = cell.get("value")
            if out:
                return out
        return {}

    def _persist_retention_dogs(self, scorecard_payload: dict[str, Any]) -> Path:
        """Write red-flag video_ids so Live VOD picker can exclude retention dogs."""
        from src.agents.store import OPS_DIR

        dogs: dict[str, list[dict[str, Any]]] = {}
        for ch, block in (scorecard_payload.get("channels") or {}).items():
            rows = []
            for r in block.get("videos") or []:
                if r.get("flag") != "red":
                    continue
                vid = str(r.get("video_id") or "").strip()
                if not vid:
                    continue
                rows.append(
                    {
                        "video_id": vid,
                        "title": r.get("title"),
                        "ctr_pct": r.get("ctr_pct"),
                        "avd_pct": r.get("avd_pct"),
                        "views": r.get("views"),
                    }
                )
            dogs[str(ch)] = rows
        path = OPS_DIR / "smm_retention_dogs.json"
        OPS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "at": scorecard_payload.get("at"),
                    "day_utc": scorecard_payload.get("day_utc"),
                    "channels": dogs,
                    "note": (
                        "Red CTR/AVD videos — Live VOD picker excludes matching "
                        "final.mp4 paths when alternatives exist."
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def _retention_dog_exclude_paths(
        self, channel: str | None = None
    ) -> dict[str, list[str]]:
        """Map channel → local final.mp4 paths for scorecard red (retention dog) videos."""
        from src.agents.store import OPS_DIR
        from src.streaming.vod_picker import _load_publish_index

        path = OPS_DIR / "smm_retention_dogs.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        want_ch = self._normalize_channel(channel) if channel else None
        dog_ids: set[str] = set()
        for ch, rows in (data.get("channels") or {}).items():
            if want_ch and self._normalize_channel(ch) != want_ch:
                continue
            for r in rows or []:
                vid = str((r or {}).get("video_id") or "").strip()
                if vid:
                    dog_ids.add(vid)
        if not dog_ids:
            return {}
        pub = _load_publish_index()
        out: dict[str, list[str]] = {}
        for final_path, meta in pub.items():
            vid = str((meta or {}).get("video_id") or "").strip()
            if vid not in dog_ids:
                continue
            ch = self._normalize_channel((meta or {}).get("channel") or want_ch)
            out.setdefault(ch, []).append(str(final_path))
        return out

    def unfreeze_gate_status(self) -> dict[str, Any]:
        """Phase-2 readiness: rolling publics + green daily scorecards (does not flip freeze)."""
        ctr_min = float(self.smm_cfg.get("ctr_pct_min") or 4)
        avd_min = float(self.smm_cfg.get("avd_pct_min") or 40)
        need_days = int(self.smm_cfg.get("unfreeze_green_days") or 14)
        need_videos = int(self.smm_cfg.get("unfreeze_rolling_videos") or 8)

        samples: list[dict[str, Any]] = []
        for job in self.store.list_jobs():
            if job.status != "public" or not job.video_id:
                continue
            insight = self._latest_insight_metrics(job.video_id)
            ctr = insight.get("ctr_pct")
            avd = insight.get("avd_pct")
            if ctr is None and avd is None:
                continue
            samples.append(
                {
                    "job_id": job.id,
                    "video_id": job.video_id,
                    "ctr_pct": ctr,
                    "avd_pct": avd,
                }
            )
        samples = samples[-need_videos:]
        ctrs = [float(s["ctr_pct"]) for s in samples if s.get("ctr_pct") is not None]
        avds = [float(s["avd_pct"]) for s in samples if s.get("avd_pct") is not None]
        med_ctr = float(median(ctrs)) if ctrs else None
        med_avd = float(median(avds)) if avds else None

        green_days = 0
        days_seen: list[str] = []
        jsonl = _smm_yt_scorecard_jsonl_path()
        if jsonl.is_file():
            try:
                lines = jsonl.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in reversed(lines):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                day = str(row.get("day_utc") or "")[:10]
                if not day or day in days_seen:
                    continue
                days_seen.append(day)
                reds = row.get("red_alerts") or []
                blocked = bool(row.get("analytics_scope_blocked"))
                if not reds and not blocked:
                    green_days += 1
                else:
                    break
                if len(days_seen) >= need_days + 5:
                    break

        metrics_ok = (
            med_ctr is not None
            and med_avd is not None
            and len(ctrs) >= max(3, need_videos // 2)
            and med_ctr >= ctr_min
            and med_avd >= avd_min
        )
        days_ok = green_days >= need_days
        ready = bool(metrics_ok and days_ok)
        return {
            "ready": ready,
            "reuse_freeze_video_heavy": self.reuse_freeze_video_heavy(),
            "bars": {"ctr_pct_min": ctr_min, "avd_pct_min": avd_min},
            "rolling": {
                "n": len(samples),
                "need": need_videos,
                "median_ctr_pct": med_ctr,
                "median_avd_pct": med_avd,
                "metrics_ok": metrics_ok,
            },
            "scorecard": {
                "green_streak_days": green_days,
                "need_days": need_days,
                "days_ok": days_ok,
            },
            "note": (
                "Human CTO sign-off still required before flipping "
                "smm.reuse_freeze_video_heavy=false."
            ),
        }

    def ensure_chapters_on_public(
        self,
        job: JobRecord,
        *,
        video_id: str | None = None,
    ) -> dict[str, Any]:
        """If description lacks Chapters timestamps, patch from youtube_meta/chapters.txt."""
        import re

        vid = (video_id or job.video_id or "").strip()
        if not vid:
            return {"ok": False, "skipped": True, "reason": "no_video_id"}
        meta = dict(job.meta or {})
        if meta.get("smm_chapters_ensured_at"):
            return {"ok": True, "skipped": True, "reason": "already_ensured"}

        job_dir = Path(str(job.job_dir or meta.get("job_dir") or ""))
        chapters_path = job_dir / "youtube_meta" / "chapters.txt"
        if not chapters_path.is_file():
            return {"ok": False, "skipped": True, "reason": "no_chapters_txt"}

        chapter_block = chapters_path.read_text(encoding="utf-8").strip()
        if not chapter_block:
            return {"ok": False, "skipped": True, "reason": "empty_chapters"}

        ch = self._job_channel(job)
        try:
            youtube = self._build_youtube(ch)
            items = (
                youtube.videos()
                .list(part="snippet", id=vid)
                .execute()
                .get("items")
                or []
            )
            if not items:
                return {"ok": False, "error": "video_not_found"}
            snip = dict(items[0].get("snippet") or {})
            desc = str(snip.get("description") or "")
            has_chapters = "chapters:" in desc.lower() and bool(
                re.search(r"(?m)^\d{1,2}:\d{2}(?::\d{2})?\s+\S+", desc)
            )
            if has_chapters:
                meta["smm_chapters_ensured_at"] = datetime.now(timezone.utc).isoformat()
                meta["smm_chapters_already_present"] = True
                self.store.update_job(job.id, meta=meta)
                return {"ok": True, "skipped": True, "reason": "already_in_description"}

            from src.services.publish_youtube import PublishModule

            block = chapter_block
            if not block.lower().startswith("chapters:"):
                block = "Chapters:\n" + block
            new_desc = desc.rstrip() + "\n\n" + block + "\n"
            pub = PublishModule(channel=ch)
            pub.update_packaging(vid, description=new_desc, channel=ch)
            meta["smm_chapters_ensured_at"] = datetime.now(timezone.utc).isoformat()
            meta["smm_chapters_patched"] = True
            self.store.update_job(job.id, meta=meta)
            return {"ok": True, "action": "chapters_patched", "video_id": vid}
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            meta["smm_chapters_error"] = err[:300]
            self.store.update_job(job.id, meta=meta)
            return {"ok": False, "error": err}

    def maybe_emit_image_reuse_packs(self, *, force: bool = False) -> dict[str, Any]:
        """Emit Pinterest/IG/FB stills packs for public jobs with images (side track)."""
        if not bool(self.smm_cfg.get("emit_image_reuse_packs", True)) and not force:
            return {"skipped": True, "reason": "emit_image_reuse_packs_disabled"}
        from src.content.image_reuse import build_image_reuse_pack, list_scene_images

        built: list[dict[str, Any]] = []
        # Prefer public jobs with a real video_id so packs deep-link to YT.
        jobs = list(self.store.list_jobs())
        jobs.sort(
            key=lambda j: (
                0 if (j.status == "public" and j.video_id) else 1,
                0 if j.video_id else 1,
                str(j.id),
            )
        )
        for job in jobs:
            if job.status not in {"public", "private", "scheduled"}:
                continue
            job_dir = Path(str(job.job_dir or ""))
            if not job_dir.is_dir():
                continue
            meta = dict(job.meta or {})
            if meta.get("image_reuse_packed_at") and not force:
                continue
            if not list_scene_images(job_dir, limit=3):
                continue
            try:
                manifest = build_image_reuse_pack(
                    job_dir,
                    video_id=job.video_id,
                    channel=self._job_channel(job),
                )
                meta["image_reuse_packed_at"] = datetime.now(timezone.utc).isoformat()
                meta["image_reuse_manifest"] = str(
                    job_dir / "derivatives" / "image_reuse" / "manifest.json"
                )
                self.store.update_job(job.id, meta=meta)
                built.append(
                    {
                        "job_id": job.id,
                        "video_id": job.video_id,
                        "n_images": manifest.get("n_images"),
                        "packs": list((manifest.get("packs") or {}).keys()),
                        "youtube_url": manifest.get("youtube_url"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("image reuse pack failed job=%s: %s", job.id, exc)
                built.append({"job_id": job.id, "ok": False, "error": str(exc)})
        return {
            "ok": True,
            "built": built,
            "n": len(built),
            "freeze_video_heavy": self.reuse_freeze_video_heavy(),
        }
