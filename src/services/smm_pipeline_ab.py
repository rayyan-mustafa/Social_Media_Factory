"""Whole-pipeline A/B ledger, competitor-trained works/fails memory, coding snapshots.

Soft levers auto-revert; coding restores require Rayyan consent via digest/queue.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.store import OPS_DIR
from src.services.settings import CONFIG_DIR, ROOT

EXPERIMENTS = OPS_DIR / "smm_experiments.jsonl"
WORKS = OPS_DIR / "smm_works.jsonl"
FAILS = OPS_DIR / "smm_fails.jsonl"
PLAYBOOK = OPS_DIR / "smm_pipeline_playbook.md"
CODE_CHANGELOG = OPS_DIR / "smm_code_changelog.jsonl"
CODE_SNAPSHOTS = OPS_DIR / "smm_code_snapshots"
CONSENT_QUEUE = OPS_DIR / "smm_consent_queue.md"
LOCKED_VIDEOS = OPS_DIR / "smm_locked_videos.json"
LOCKED_WINNERS_JSONL = OPS_DIR / "smm_locked_winners.jsonl"

# Channel goals fallbacks (ceo_channel_goals.json overrides when passed in)
DEFAULT_CTR_MIN = 4.0
DEFAULT_AVD_MIN = 40.0
DEFAULT_FIRST60_MIN = 70.0
# Clear overperformance vs channel median (relative)
OVERPERF_MEDIAN_RATIO = 1.15

# Soft levers may auto-revert; coding requires consent
SOFT_LEVERS = frozenset(
    {
        "soft_packaging",
        "publish_hour",
        "thumbnail",
        "hook",
        "voice",
        "pinned_comment",
        "end_screen",
    }
)
CODING_LEVERS = frozenset({"coding", "prompt_patch", "sop_structural", "module_patch"})

PIPELINE_LEVER_ORDER = [
    "soft_packaging",
    "publish_hour",
    "hook",
    "thumbnail",
    "outline_pacing",
    "visual_mix",
    "voice",
    "length_structure",
    "coding",
]

# Full-video editing levers (runtime JSON overrides — no Rayyan consent)
EDITING_LEVER_ORDER = [
    "thumbnail_style",
    "hook_pattern",
    "compose_sfx_beat_interval_s",
    "sfx_event_cap",
    "ambient_bed_db",
    "overlay_cadence_s",
    "max_infographic_cards",
    "ken_burns_scale",
    "target_length_band",
    "motion_mode",
    "format_mode",
    "voice_mode",
    "score_mode",
]

EDITING_LEVER_TO_OVERRIDE_KEY = {
    "thumbnail_style": ("thumbnail_style",),
    "hook_pattern": ("hook_pattern", {"default": None}),
    "compose_sfx_beat_interval_s": ("compose", {"beat_interval_s": None}),
    "sfx_event_cap": ("compose", {"sfx_event_cap": None}),
    "ambient_bed_db": ("audio", {"ambient_bed_db": None}),
    "overlay_cadence_s": ("compose", {"overlay_cadence_s": None}),
    "max_infographic_cards": ("compose", {"max_infographic_cards": None}),
    "ken_burns_scale": ("compose", {"ken_burns_scale": None}),
    "target_length_band": ("pacing", {"target_length_band_min_s": None, "target_length_band_max_s": None}),
    "motion_mode": ("motion", {"motion_mode": None}),
    "format_mode": ("pacing", {"format_mode": None}),
    "voice_mode": ("audio", {"voice_mode": None}),
    "score_mode": ("audio", {"score_mode": None}),
}

CHANNEL_EDITING_OVERRIDES = CONFIG_DIR / "channel_editing_overrides.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_locked_videos() -> dict[str, Any]:
    """Return ``{video_id: lock_record}`` from ``smm_locked_videos.json``."""
    if not LOCKED_VIDEOS.is_file():
        return {}
    try:
        data = json.loads(LOCKED_VIDEOS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    videos = data.get("videos") if isinstance(data, dict) else None
    if isinstance(videos, dict):
        return {str(k): v for k, v in videos.items() if isinstance(v, dict)}
    return {}


def is_video_locked(video_id: str | None) -> bool:
    """True when video has a hard winner lock (do not thrash levers / open experiments)."""
    vid = (video_id or "").strip()
    if not vid:
        return False
    rec = load_locked_videos().get(vid)
    if not rec:
        return False
    return str(rec.get("status") or "locked") == "locked"


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _channel_goals(
    channel: str,
    goals: dict[str, Any] | None = None,
) -> dict[str, float]:
    ch = ((goals or {}).get("channels") or {}).get(channel) or {}
    return {
        "ctr_pct_min": float(ch.get("ctr_pct_min") or DEFAULT_CTR_MIN),
        "avd_pct_min": float(ch.get("avd_pct_min") or DEFAULT_AVD_MIN),
        "first_60s_retention_pct_min": float(
            ch.get("first_60s_retention_pct_min") or DEFAULT_FIRST60_MIN
        ),
    }


def detect_winner_lock_candidate(
    row: dict[str, Any],
    *,
    channel: str,
    goals: dict[str, Any] | None = None,
    channel_medians: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide hard lock vs provisional AVD-only note vs not a candidate.

    Hard lock prefers Reporting-ready CTR + first-60s (+ AVD when present).
    AVD-only overperformance → provisional soft note (not a hard lock).
    """
    g = _channel_goals(channel, goals)
    ctr = _num(row.get("ctr_pct"))
    avd = _num(row.get("avd_pct"))
    r60 = _num(row.get("first_60s_retention_pct"))
    med = channel_medians or {}
    med_ctr = _num(med.get("ctr_pct"))
    med_avd = _num(med.get("avd_pct"))
    med_r60 = _num(med.get("first_60s_retention_pct"))

    goals_hit = {
        "ctr": ctr is not None and ctr >= g["ctr_pct_min"],
        "first_60s": r60 is not None and r60 >= g["first_60s_retention_pct_min"],
        "avd": avd is not None and avd >= g["avd_pct_min"],
    }
    overperf = False
    if med_ctr is not None and ctr is not None and ctr >= med_ctr * OVERPERF_MEDIAN_RATIO:
        if med_r60 is not None and r60 is not None and r60 >= med_r60 * OVERPERF_MEDIAN_RATIO:
            overperf = True
        elif med_avd is not None and avd is not None and avd >= med_avd * OVERPERF_MEDIAN_RATIO:
            overperf = True

    # Hard lock: CTR + first-60s meeting goals; if AVD present it must not miss goal
    if goals_hit["ctr"] and goals_hit["first_60s"]:
        if avd is not None and not goals_hit["avd"]:
            return {
                "lock": False,
                "provisional": False,
                "reason": "ctr_first60_ok_but_avd_below_goal",
                "metrics": {"ctr_pct": ctr, "avd_pct": avd, "first_60s_retention_pct": r60},
                "goals": g,
            }
        reason = "goals_ctr_first60"
        if goals_hit["avd"]:
            reason = "goals_ctr_first60_avd"
        elif overperf:
            reason = "goals_ctr_first60_plus_median_overperf"
        return {
            "lock": True,
            "provisional": False,
            "reason": reason,
            "metrics": {"ctr_pct": ctr, "avd_pct": avd, "first_60s_retention_pct": r60},
            "goals": g,
            "overperformance_vs_median": overperf,
        }

    # Clear overperformance vs medians when CTR+first60 both present
    if (
        ctr is not None
        and r60 is not None
        and overperf
        and (avd is None or goals_hit["avd"] or (
            med_avd is not None and avd >= med_avd * OVERPERF_MEDIAN_RATIO
        ))
    ):
        return {
            "lock": True,
            "provisional": False,
            "reason": "median_overperformance",
            "metrics": {"ctr_pct": ctr, "avd_pct": avd, "first_60s_retention_pct": r60},
            "goals": g,
            "overperformance_vs_median": True,
        }

    # Soft provisional: AVD-only win while CTR/first60 still pending Reporting
    if goals_hit["avd"] and ctr is None and r60 is None:
        return {
            "lock": False,
            "provisional": True,
            "reason": "provisional_avd_only_awaiting_ctr_first60",
            "metrics": {"ctr_pct": ctr, "avd_pct": avd, "first_60s_retention_pct": r60},
            "goals": g,
        }

    return {
        "lock": False,
        "provisional": False,
        "reason": "not_a_winner_yet",
        "metrics": {"ctr_pct": ctr, "avd_pct": avd, "first_60s_retention_pct": r60},
        "goals": g,
    }


def _close_open_experiments_for_video(video_id: str, *, note: str) -> list[str]:
    closed: list[str] = []
    for row in _read_jsonl(EXPERIMENTS):
        if row.get("video_id_or_job") != video_id:
            continue
        if row.get("outcome") not in (None, "open", "running"):
            continue
        closed_row = {
            **row,
            "outcome": "locked_winner",
            "evaluated_at": _now(),
            "lock_note": note,
        }
        _append_jsonl(EXPERIMENTS, closed_row)
        closed.append(str(row.get("id") or ""))
    return [c for c in closed if c]


def lock_video_winner(
    *,
    video_id: str,
    channel: str,
    metrics: dict[str, Any],
    reason: str,
    title: str | None = None,
    packaging_snapshot: dict[str, Any] | None = None,
    format_cue: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Hard-lock a winner: persist index + jsonl, promote works, refresh playbook."""
    vid = (video_id or "").strip()
    if not vid:
        return {"ok": False, "reason": "missing_video_id"}
    if is_video_locked(vid):
        existing = load_locked_videos().get(vid) or {}
        return {"ok": True, "already_locked": True, "lock": existing}

    closed = _close_open_experiments_for_video(vid, note=f"winner_lock:{reason}")
    lock_rec = {
        "video_id": vid,
        "channel": channel,
        "status": "locked",
        "reason": reason,
        "metrics": metrics,
        "title": title,
        "job_id": job_id,
        "format_cue": format_cue or "new_format_sop",
        "packaging_snapshot": packaging_snapshot or {},
        "locked_at": _now(),
        "closed_experiments": closed,
    }
    # Persist index
    index: dict[str, Any] = {"updated_at": _now(), "videos": {}}
    if LOCKED_VIDEOS.is_file():
        try:
            prev = json.loads(LOCKED_VIDEOS.read_text(encoding="utf-8"))
            if isinstance(prev, dict) and isinstance(prev.get("videos"), dict):
                index["videos"] = dict(prev["videos"])
        except (OSError, json.JSONDecodeError):
            pass
    index["videos"][vid] = lock_rec
    index["updated_at"] = _now()
    LOCKED_VIDEOS.parent.mkdir(parents=True, exist_ok=True)
    LOCKED_VIDEOS.write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _append_jsonl(LOCKED_WINNERS_JSONL, {**lock_rec, "event": "locked"})

    works_row = {
        "id": f"lock_{vid}",
        "channel": channel,
        "video_id_or_job": vid,
        "lever": "packaging+format",
        "outcome": "works",
        "competitor_source": None,
        "before_metrics": {},
        "after_metrics": metrics,
        "ts": _now(),
        "locked": True,
        "format_cue": lock_rec["format_cue"],
        "reason": reason,
        "title": title,
        "packaging_snapshot": packaging_snapshot or {},
        "note": "auto-locked winner — do not thrash levers on this video",
    }
    _append_jsonl(WORKS, works_row)
    # Retention + packaging win → also stamp new-format SOP cue in works
    if _num(metrics.get("avd_pct")) is not None or _num(
        metrics.get("first_60s_retention_pct")
    ) is not None:
        _append_jsonl(
            WORKS,
            {
                **works_row,
                "id": f"lock_fmt_{vid}",
                "lever": "new_format_sop",
                "note": "retention+packaging win — promote new-format SOP cues",
            },
        )
    refresh_playbook()
    return {"ok": True, "newly_locked": True, "lock": lock_rec, "works": works_row}


def maybe_lock_winner_from_scorecard(
    payload: dict[str, Any] | None,
    *,
    goals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scan scorecard payload; hard-lock new winners; soft-note AVD-only provisionals."""
    payload = payload or {}
    newly_locked: list[dict[str, Any]] = []
    provisionals: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    # Optional channel medians from scorecard aggregates
    for ch, block in (payload.get("channels") or {}).items():
        rows = list(block.get("videos") or [])
        # Channel medians from rows that have each metric
        def _median(vals: list[float]) -> float | None:
            if not vals:
                return None
            vals = sorted(vals)
            mid = len(vals) // 2
            if len(vals) % 2:
                return vals[mid]
            return (vals[mid - 1] + vals[mid]) / 2.0

        medians = {
            "ctr_pct": _median(
                [float(r["ctr_pct"]) for r in rows if r.get("ctr_pct") is not None]
            ),
            "avd_pct": _median(
                [float(r["avd_pct"]) for r in rows if r.get("avd_pct") is not None]
            ),
            "first_60s_retention_pct": _median(
                [
                    float(r["first_60s_retention_pct"])
                    for r in rows
                    if r.get("first_60s_retention_pct") is not None
                ]
            ),
        }
        for r in rows:
            vid = str(r.get("video_id") or "").strip()
            if not vid:
                continue
            if is_video_locked(vid):
                skipped.append({"video_id": vid, "reason": "already_locked"})
                continue
            decision = detect_winner_lock_candidate(
                r, channel=str(ch), goals=goals, channel_medians=medians
            )
            if decision.get("lock"):
                packaging = {
                    "title": r.get("title"),
                    "flag": r.get("flag"),
                    "metrics_source": r.get("metrics_source") or r.get("ctr_source"),
                }
                fmt = "new_format_sop"
                if str(ch) == "napstorian":
                    fmt = "napstorian_new_format_10m_sop"
                out = lock_video_winner(
                    video_id=vid,
                    channel=str(ch),
                    metrics=decision.get("metrics") or {},
                    reason=str(decision.get("reason") or "winner"),
                    title=r.get("title"),
                    packaging_snapshot=packaging,
                    format_cue=fmt,
                    job_id=r.get("job_id"),
                )
                if out.get("newly_locked"):
                    newly_locked.append(out.get("lock") or {"video_id": vid})
            elif decision.get("provisional"):
                # Avoid spam: one provisional note per video until hard-locked
                already_prov = any(
                    r.get("video_id") == vid
                    and (
                        r.get("event") == "provisional_avd_only"
                        or r.get("status") == "provisional"
                    )
                    for r in _read_jsonl(LOCKED_WINNERS_JSONL)
                )
                if already_prov:
                    skipped.append({"video_id": vid, "reason": "provisional_already_noted"})
                    continue
                note = {
                    "video_id": vid,
                    "channel": ch,
                    "status": "provisional",
                    "reason": decision.get("reason"),
                    "metrics": decision.get("metrics"),
                    "title": r.get("title"),
                    "ts": _now(),
                    "event": "provisional_avd_only",
                }
                _append_jsonl(LOCKED_WINNERS_JSONL, note)
                provisionals.append(note)
            else:
                skipped.append(
                    {
                        "video_id": vid,
                        "reason": decision.get("reason"),
                    }
                )

    return {
        "ok": True,
        "newly_locked": newly_locked,
        "n_newly_locked": len(newly_locked),
        "provisionals": provisionals,
        "n_provisionals": len(provisionals),
        "n_skipped": len(skipped),
    }


def open_experiment(
    *,
    channel: str,
    video_id_or_job: str,
    lever: str,
    baseline_metrics: dict[str, Any],
    baseline_snapshot: dict[str, Any] | None = None,
    competitor_source: str | None = None,
    coding_snapshot_id: str | None = None,
    stage: str = "publish",
) -> dict[str, Any]:
    """Start one experiment. Refuses if video locked or another open experiment exists."""
    if is_video_locked(video_id_or_job):
        return {
            "ok": False,
            "reason": "video_locked_winner",
            "video_id_or_job": video_id_or_job,
        }
    for row in _read_jsonl(EXPERIMENTS):
        if (
            row.get("video_id_or_job") == video_id_or_job
            and row.get("outcome") in (None, "open", "running")
        ):
            return {
                "ok": False,
                "reason": "experiment_already_open",
                "existing": row.get("id"),
            }
    exp = {
        "id": str(uuid.uuid4())[:12],
        "channel": channel,
        "video_id_or_job": video_id_or_job,
        "stage": stage,
        "lever": lever,
        "competitor_source": competitor_source,
        "before_metrics": baseline_metrics,
        "after_metrics": None,
        "outcome": "open",
        "baseline_snapshot": baseline_snapshot or {},
        "coding_snapshot_id": coding_snapshot_id,
        "applied_at": _now(),
        "evaluated_at": None,
        "is_coding": lever in CODING_LEVERS or lever.startswith("coding"),
    }
    _append_jsonl(EXPERIMENTS, exp)
    return {"ok": True, "experiment": exp}


def _better(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    primary: str = "ctr",
) -> bool:
    """Improved if primary rises and secondary does not crater >15% relative."""
    b = before.get(primary)
    a = after.get(primary)
    if b is None or a is None:
        # fallback views / avd
        for key in ("views_velocity", "avd_pct", "first_60s"):
            if before.get(key) is not None and after.get(key) is not None:
                primary = key
                b = before[key]
                a = after[key]
                break
        else:
            return False
    try:
        b_f, a_f = float(b), float(a)
    except (TypeError, ValueError):
        return False
    if a_f <= b_f:
        return False
    # secondary guard
    for sec in ("avd_pct", "first_60s"):
        if sec == primary:
            continue
        sb, sa = before.get(sec), after.get(sec)
        if sb is None or sa is None:
            continue
        try:
            sb_f, sa_f = float(sb), float(sa)
        except (TypeError, ValueError):
            continue
        if sb_f > 0 and sa_f < sb_f * 0.85:
            return False
    return True


def evaluate_experiment(
    experiment_id: str,
    after_metrics: dict[str, Any],
) -> dict[str, Any]:
    rows = _read_jsonl(EXPERIMENTS)
    target = None
    for row in rows:
        if row.get("id") == experiment_id:
            target = row
            break
    if not target:
        return {"ok": False, "reason": "not_found"}

    before = target.get("before_metrics") or {}
    improved = _better(before, after_metrics)
    outcome = "works" if improved else "fails"
    target["after_metrics"] = after_metrics
    target["outcome"] = outcome
    target["evaluated_at"] = _now()
    _append_jsonl(EXPERIMENTS, target)

    mem = {
        "id": target["id"],
        "channel": target.get("channel"),
        "lever": target.get("lever"),
        "competitor_source": target.get("competitor_source"),
        "before_metrics": before,
        "after_metrics": after_metrics,
        "outcome": outcome,
        "ts": _now(),
        "video_id_or_job": target.get("video_id_or_job"),
        "coding_snapshot_id": target.get("coding_snapshot_id"),
    }
    if outcome == "works":
        _append_jsonl(WORKS, mem)
    else:
        _append_jsonl(FAILS, mem)
        if target.get("runtime_override") and target.get("lever"):
            revert_editing_lever(str(target.get("channel") or ""), str(target.get("lever")))
        if target.get("is_coding") and target.get("coding_snapshot_id"):
            propose_coding_restore(
                target["coding_snapshot_id"],
                reason=f"experiment {experiment_id} failed metrics",
                experiment_id=experiment_id,
            )

    refresh_playbook()
    return {
        "ok": True,
        "outcome": outcome,
        "auto_revert_soft": (not improved) and not target.get("is_coding"),
        "propose_restore": (not improved) and bool(target.get("is_coding")),
        "experiment": target,
    }


def snapshot_coding(
    paths: list[str | Path],
    *,
    reason: str,
    competitor_cue: str | None = None,
    linked_experiment_id: str | None = None,
) -> dict[str, Any]:
    """Copy before-blobs for consent-based restore later."""
    sid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + str(uuid.uuid4())[:8]
    dest = CODE_SNAPSHOTS / sid
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for p in paths:
        src = Path(p)
        if not src.is_file():
            continue
        # Keep relative path under snapshot
        try:
            rel = src.resolve().relative_to(ROOT.resolve())
        except ValueError:
            rel = Path(src.name)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        saved.append(str(rel))
    row = {
        "id": sid,
        "paths": saved,
        "reason": reason,
        "competitor_cue": competitor_cue,
        "linked_experiment_id": linked_experiment_id,
        "created_at": _now(),
        "status": "active",
    }
    _append_jsonl(CODE_CHANGELOG, row)
    return row


def propose_coding_restore(
    snapshot_id: str,
    *,
    reason: str,
    experiment_id: str | None = None,
) -> dict[str, Any]:
    row = {
        "id": snapshot_id,
        "event": "proposed_restore",
        "reason": reason,
        "experiment_id": experiment_id,
        "ts": _now(),
        "status": "proposed_restore",
    }
    _append_jsonl(CODE_CHANGELOG, row)
    CONSENT_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with CONSENT_QUEUE.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\n## Coding restore proposed — `{snapshot_id}`\n"
            f"- Reason: {reason}\n"
            f"- Experiment: {experiment_id}\n"
            f"- Accept: `python -m src.cli.smm_consent accept_restore --id {snapshot_id}`\n"
            f"- Or write flag: `output/ops/smm_consent_restore_{snapshot_id}.flag`\n"
        )
    return row


def apply_coding_restore_if_consented(snapshot_id: str, *, force: bool = False) -> dict[str, Any]:
    flag = OPS_DIR / f"smm_consent_restore_{snapshot_id}.flag"
    if not force and not flag.is_file():
        return {"ok": False, "reason": "consent_flag_missing", "flag": str(flag)}
    snap_dir = CODE_SNAPSHOTS / snapshot_id
    if not snap_dir.is_dir():
        return {"ok": False, "reason": "snapshot_missing"}
    restored: list[str] = []
    for src in snap_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(snap_dir)
        dest = ROOT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        restored.append(str(rel))
    _append_jsonl(
        CODE_CHANGELOG,
        {
            "id": snapshot_id,
            "event": "restored_with_consent",
            "paths": restored,
            "ts": _now(),
            "status": "restored_with_consent",
        },
    )
    _append_jsonl(
        WORKS,
        {
            "lever": "coding_restore",
            "coding_snapshot_id": snapshot_id,
            "outcome": "works",
            "note": "prior revision restored with Rayyan consent",
            "ts": _now(),
        },
    )
    try:
        flag.unlink(missing_ok=True)
    except OSError:
        pass
    refresh_playbook()
    return {"ok": True, "restored": restored}


def _title_thumb_style_cues(title: str) -> dict[str, Any]:
    t = (title or "").lower()
    return {
        "map_vs_face": "map" if any(k in t for k in ("map", "border", "empire", "territory")) else "face",
        "mood": "calm" if any(k in t for k in ("sleep", "calm", "relax", "bedtime")) else "dramatic",
        "word_count": len(t.split()),
    }


def distill_competitor_cues(channel: str) -> list[dict[str, Any]]:
    """Primary training source: competitor configs + metrics + style intel."""
    if channel == "napping_historian":
        path = CONFIG_DIR / "competitors_napping_historian.json"
        metrics = OPS_DIR / "competitors_metrics_last_napping_historian.json"
        insp_path = OPS_DIR / "last_inspiration_napping_historian.json"
    else:
        path = CONFIG_DIR / "competitors.json"
        metrics = OPS_DIR / "competitors_metrics_last.json"
        insp_path = OPS_DIR / "last_inspiration.json"
    cues: list[dict[str, Any]] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        comps = [c for c in (data.get("competitors") or []) if isinstance(c, dict)]
        # Prefer History Calling–tier anchors; de-prioritize aspirational FoC.
        role_rank = {
            "primary_anchor": 0,
            "style_peer": 1,
            "optional_format": 2,
            "ambient_sleep": 3,
            "aspirational_reference": 9,
        }
        comps.sort(
            key=lambda c: (
                role_rank.get(str(c.get("style_role") or "style_peer"), 5),
                int(c.get("style_priority") or 50),
            )
        )
        hours: list[int] = []
        for c in comps:
            for h in c.get("best_upload_hours_local") or []:
                try:
                    hours.append(int(h))
                except (TypeError, ValueError):
                    pass
            label = c.get("label") or c.get("handle") or c.get("channel_id")
            cues.append(
                {
                    "type": "competitor",
                    "label": label,
                    "channel": channel,
                    "style_role": c.get("style_role") or "style_peer",
                    "style_priority": c.get("style_priority"),
                    "hint": "focal_thumb_subject",
                }
            )
        if hours:
            from collections import Counter

            top = [h for h, _ in Counter(hours).most_common(3)]
            cues.append(
                {
                    "type": "publish_hour",
                    "channel": channel,
                    "hours": top,
                    "source": str(path.name),
                }
            )
        brief = data.get("style_brief")
        if brief:
            cues.append(
                {
                    "type": "style_brief",
                    "channel": channel,
                    "brief": str(brief)[:400],
                    "hint": "editing_style_anchor",
                }
            )
    if metrics.is_file():
        cues.append({"type": "metrics_refresh", "path": str(metrics), "channel": channel})

    # Style intel cluster (post–competitor-list refresh) — prefer over raw FoC views.
    intel_path = OPS_DIR / "competitor_style_intel.json"
    if intel_path.is_file():
        try:
            intel = json.loads(intel_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            intel = {}
        ch_intel = ((intel.get("channels") or {}).get(channel)) or {}
        cluster = ch_intel.get("cluster") or {}
        if cluster:
            cues.append(
                {
                    "type": "style_cluster",
                    "channel": channel,
                    "dominant_form": cluster.get("dominant_form"),
                    "mystery_ratio": cluster.get("mystery_ratio"),
                    "style_anchors": ch_intel.get("style_anchors"),
                    "top_titles": (cluster.get("top_titles") or [])[:3],
                    "hint": "history_calling_mystery_doc"
                    if channel == "napping_historian"
                    else "competitor_form",
                }
            )

    # Top performer title → thumb style cues (map vs face, mood, word count)
    if insp_path.is_file():
        try:
            insp = json.loads(insp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            insp = {}
        top_vids = [
            it
            for it in (insp.get("items") or [])
            if isinstance(it, dict) and it.get("kind") == "competitor_video"
        ]
        top_vids.sort(key=lambda x: float(x.get("video_views") or 0), reverse=True)
        for it in top_vids[:5]:
            title = str(it.get("blocked_title") or it.get("title") or "")
            if not title:
                continue
            style = _title_thumb_style_cues(title)
            cues.append(
                {
                    "type": "thumb_style",
                    "channel": channel,
                    "title_sample": title[:80],
                    "map_vs_face": style["map_vs_face"],
                    "mood": style["mood"],
                    "word_count": style["word_count"],
                    "hint": "thumbnail_style_preset",
                }
            )
        if top_vids:
            cues.append(
                {
                    "type": "competitor_cluster",
                    "channel": channel,
                    "n_videos": len(top_vids),
                    "source": insp_path.name,
                }
            )

    intel_path = OPS_DIR / "competitor_style_intel.json"
    if intel_path.is_file():
        try:
            intel = json.loads(intel_path.read_text(encoding="utf-8"))
            cluster = ((intel.get("channels") or {}).get(channel) or {}).get("cluster") or {}
            if cluster.get("hints"):
                cues.append(
                    {
                        "type": "style_intel",
                        "channel": channel,
                        "dominant_form": cluster.get("dominant_form"),
                        "hints": cluster.get("hints"),
                    }
                )
        except json.JSONDecodeError:
            pass
    return cues


def rank_pipeline_levers(
    channel: str,
    *,
    failed_levers: set[str] | None = None,
    underperforming: bool = False,
    cluster_hints: dict[str, Any] | None = None,
) -> list[str]:
    failed = failed_levers or set()
    works = _read_jsonl(WORKS)
    # Promote levers that worked for this channel
    boost = {
        str(w.get("lever"))
        for w in works
        if w.get("channel") == channel and w.get("outcome") == "works"
    }
    ordered = []
    for lever in PIPELINE_LEVER_ORDER:
        if lever in failed:
            continue
        if lever in boost:
            ordered.insert(0, lever)
        else:
            ordered.append(lever)
    # When underperforming or competitor cluster shifted, prepend editing levers
    if underperforming or cluster_hints:
        editing = rank_editing_levers(
            channel,
            failed_levers=failed,
            underperforming=underperforming,
            cluster_hints=cluster_hints or {},
        )
        ordered = editing[:1] + ordered
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for x in ordered:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def rank_editing_levers(
    channel: str,
    *,
    failed_levers: set[str] | None = None,
    underperforming: bool = False,
    cluster_hints: dict[str, Any] | None = None,
    scorecard_reds: list[str] | None = None,
) -> list[str]:
    """Rank full-video editing levers — one applied per CEO beat when triggered."""
    failed = failed_levers or set()
    works = _read_jsonl(WORKS)
    boost = {
        str(w.get("lever"))
        for w in works
        if w.get("channel") == channel and w.get("outcome") == "works"
    }
    hints = cluster_hints or {}
    priority: list[str] = []

    # Map scorecard reds → levers first
    red_map = {
        "ctr_pct": "thumbnail_style",
        "first_60s_retention_pct": "hook_pattern",
        "avd_pct": "overlay_cadence_s",
    }
    for red in scorecard_reds or []:
        lev = red_map.get(red)
        if lev and lev not in priority:
            priority.append(lev)

    if hints.get("thumbnail_style"):
        if "thumbnail_style" not in priority:
            priority.insert(0, "thumbnail_style")
    if hints.get("hook_pattern"):
        if "hook_pattern" not in priority:
            priority.append("hook_pattern")
    if hints.get("compose"):
        for lev in ("compose_sfx_beat_interval_s", "overlay_cadence_s", "max_infographic_cards"):
            if lev not in priority:
                priority.append(lev)

    ordered: list[str] = []
    for lev in priority:
        if lev in EDITING_LEVER_ORDER and lev not in failed:
            ordered.append(lev)
    for lever in EDITING_LEVER_ORDER:
        if lever in failed or lever in ordered:
            continue
        if lever in boost:
            ordered.insert(0, lever)
        else:
            ordered.append(lever)
    if not underperforming and not hints and not scorecard_reds:
        return []
    return ordered


def editing_lever_default_value(channel: str, lever: str | None) -> Any:
    """Default value from merged profile + cluster hints."""
    if not lever:
        return None
    from src.services.editing_overrides import get_merged_channel_editing

    merged = get_merged_channel_editing(channel)
    if lever == "thumbnail_style":
        return dict(merged.get("thumbnail_style") or {})
    if lever == "hook_pattern":
        hp = merged.get("hook_pattern") or {}
        return hp.get("default") or (hp.get("preferred") or ["other"])[0]
    if lever == "compose_sfx_beat_interval_s":
        return (merged.get("compose") or {}).get("beat_interval_s")
    if lever == "sfx_event_cap":
        return (merged.get("compose") or {}).get("sfx_event_cap")
    if lever == "ambient_bed_db":
        return (merged.get("audio") or {}).get("ambient_bed_db")
    if lever == "overlay_cadence_s":
        return (merged.get("compose") or {}).get("overlay_cadence_s")
    if lever == "max_infographic_cards":
        return (merged.get("compose") or {}).get("max_infographic_cards")
    if lever == "ken_burns_scale":
        return (merged.get("compose") or {}).get("ken_burns_scale")
    if lever == "target_length_band":
        pacing = merged.get("pacing") or {}
        return {
            "min_s": pacing.get("target_length_band_min_s"),
            "max_s": pacing.get("target_length_band_max_s"),
        }
    if lever == "motion_mode":
        return (merged.get("motion") or {}).get("motion_mode") or "ken_burns"
    if lever == "format_mode":
        return (merged.get("pacing") or {}).get("format_mode") or "standard"
    if lever == "voice_mode":
        return (merged.get("audio") or {}).get("voice_mode") or "tts_single"
    if lever == "score_mode":
        return (merged.get("audio") or {}).get("score_mode") or "none"
    return None


def _lever_to_override_patch(lever: str, value: Any) -> dict[str, Any]:
    if lever == "thumbnail_style" and isinstance(value, dict):
        return {"thumbnail_style": value}
    if lever == "hook_pattern":
        return {"hook_pattern": {"default": value}}
    if lever == "compose_sfx_beat_interval_s":
        return {"compose": {"beat_interval_s": float(value)}}
    if lever == "sfx_event_cap":
        return {"compose": {"sfx_event_cap": int(value)}}
    if lever == "ambient_bed_db":
        return {"audio": {"ambient_bed_db": float(value)}}
    if lever == "overlay_cadence_s":
        return {"compose": {"overlay_cadence_s": float(value)}}
    if lever == "max_infographic_cards":
        return {"compose": {"max_infographic_cards": int(value)}}
    if lever == "ken_burns_scale":
        return {"compose": {"ken_burns_scale": float(value)}}
    if lever == "target_length_band" and isinstance(value, dict):
        return {
            "pacing": {
                "target_length_band_min_s": value.get("min_s"),
                "target_length_band_max_s": value.get("max_s"),
            }
        }
    if lever == "motion_mode":
        return {"motion": {"motion_mode": str(value)}}
    if lever == "format_mode":
        patch: dict[str, Any] = {"pacing": {"format_mode": str(value)}}
        if str(value).strip().lower() in {"foc_epic", "foc"}:
            patch["pacing"]["target_length_band_min_s"] = 10800
            patch["pacing"]["target_length_band_max_s"] = 14400
        return patch
    if lever == "voice_mode":
        return {"audio": {"voice_mode": str(value)}}
    if lever == "score_mode":
        return {"audio": {"score_mode": str(value)}}
    return {}


def apply_editing_lever(channel: str, lever: str, value: Any) -> dict[str, Any]:
    """Write runtime override to channel_editing_overrides.json (auto — no consent)."""
    from src.services.editing_overrides import (
        get_channel_runtime_overrides,
        write_channel_overrides,
    )

    patch = _lever_to_override_patch(lever, value)
    if not patch:
        return {"ok": False, "reason": "unknown_lever", "lever": lever}
    before = get_channel_runtime_overrides(channel)
    block = write_channel_overrides(
        channel,
        overrides_patch=patch,
        experiment={
            "lever": lever,
            "value": value,
            "applied_at": _now(),
            "baseline_overrides": before,
        },
    )
    _append_jsonl(
        EXPERIMENTS,
        {
            "id": f"edit_{channel}_{lever}_{datetime.now(timezone.utc).strftime('%H%M%S')}",
            "channel": channel,
            "video_id_or_job": f"channel:{channel}",
            "stage": "editing",
            "lever": lever,
            "competitor_source": "competitor_style_intel",
            "before_metrics": {},
            "after_metrics": None,
            "outcome": "running",
            "baseline_snapshot": before,
            "applied_at": _now(),
            "is_coding": False,
            "runtime_override": True,
        },
    )
    return {"ok": True, "lever": lever, "value": value, "patch": patch, "block": block}


def revert_editing_lever(channel: str, lever: str) -> dict[str, Any]:
    """Revert one lever from active experiment baseline."""
    from src.services.editing_overrides import load_channel_overrides_file, write_channel_overrides

    data = load_channel_overrides_file()
    block = dict((data.get("channels") or {}).get(channel) or {})
    exp = block.get("active_experiment") or {}
    if exp.get("lever") != lever:
        return {"ok": False, "reason": "no_matching_experiment"}
    baseline = dict(exp.get("baseline_overrides") or {})
    write_channel_overrides(
        channel,
        replace_overrides=baseline,
        experiment=None,
    )
    # Clear experiment on block
    data = load_channel_overrides_file()
    ch_block = dict((data.get("channels") or {}).get(channel) or {})
    ch_block.pop("active_experiment", None)
    data.setdefault("channels", {})[channel] = ch_block
    CHANNEL_EDITING_OVERRIDES.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _append_jsonl(
        FAILS,
        {
            "channel": channel,
            "lever": lever,
            "outcome": "fails",
            "note": "editing lever reverted after failed evaluation",
            "ts": _now(),
        },
    )
    refresh_playbook()
    return {"ok": True, "reverted": lever, "baseline": baseline}


def refresh_playbook() -> Path:
    works = _read_jsonl(WORKS)[-50:]
    fails = _read_jsonl(FAILS)[-50:]
    lines = [
        "# SMM pipeline playbook (self-trained)",
        f"_Updated {_now()}_",
        "",
        "Primary idea source: competitor data (both channels). Labels: our scorecards.",
        "",
        "## Works (keep / promote)",
        "",
    ]
    if not works:
        lines.append("- (none yet)")
    for w in works[-20:]:
        lines.append(
            f"- [{w.get('channel')}] `{w.get('lever')}` — {w.get('competitor_source') or ''} "
            f"before={w.get('before_metrics')} after={w.get('after_metrics')}"
        )
    lines += ["", "## Fails (cool-down)", ""]
    if not fails:
        lines.append("- (none yet)")
    for f in fails[-20:]:
        lines.append(
            f"- [{f.get('channel')}] `{f.get('lever')}` — do not retry soon"
        )
    lines += [
        "",
        "## Rules",
        "- One live experiment per video",
        "- Soft levers auto-revert on fail; coding restore needs Rayyan consent",
        "- Locked winners: do not thrash levers or open new experiments",
        "- No 5-channel identical-content mirrors",
        "",
    ]
    PLAYBOOK.parent.mkdir(parents=True, exist_ok=True)
    PLAYBOOK.write_text("\n".join(lines), encoding="utf-8")
    return PLAYBOOK


def digest_training_block() -> str:
    works = _read_jsonl(WORKS)[-5:]
    fails = _read_jsonl(FAILS)[-5:]
    pending = [
        r
        for r in _read_jsonl(CODE_CHANGELOG)
        if r.get("status") == "proposed_restore" or r.get("event") == "proposed_restore"
    ][-5:]
    locked_recent = [
        r
        for r in _read_jsonl(LOCKED_WINNERS_JSONL)
        if r.get("event") == "locked" or r.get("status") == "locked"
    ][-5:]
    provisional_recent = [
        r
        for r in _read_jsonl(LOCKED_WINNERS_JSONL)
        if r.get("event") == "provisional_avd_only" or r.get("status") == "provisional"
    ][-3:]
    lines = ["### SMM self-train / A/B", ""]
    lines.append("**Newly locked winners (do not thrash):**")
    if locked_recent:
        for w in locked_recent:
            lines.append(
                f"- `{w.get('video_id')}` [{w.get('channel')}] "
                f"{w.get('reason')} metrics={w.get('metrics')}"
            )
    else:
        lines.append("- (none new)")
    lines.append("")
    if provisional_recent:
        lines.append("**Provisional AVD-only (await CTR + first-60s):**")
        for p in provisional_recent:
            lines.append(
                f"- `{p.get('video_id')}` [{p.get('channel')}] "
                f"AVD={((p.get('metrics') or {}).get('avd_pct'))}"
            )
        lines.append("")
    lines.append("**Works:**")
    if works:
        for w in works:
            lock_tag = " 🔒" if w.get("locked") else ""
            lines.append(f"- `{w.get('lever')}` [{w.get('channel')}]{lock_tag}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("**Fails:**")
    if fails:
        for f in fails:
            lines.append(f"- `{f.get('lever')}` [{f.get('channel')}]")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("**Pending coding restores (consent):**")
    if pending:
        for p in pending:
            lines.append(f"- `{p.get('id')}` — {p.get('reason')}")
    else:
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


def run_training_tick() -> dict[str, Any]:
    """Refresh cues + playbook for both Brand channels."""
    cues = {
        "napstorian": distill_competitor_cues("napstorian"),
        "napping_historian": distill_competitor_cues("napping_historian"),
    }
    path = refresh_playbook()
    return {
        "ok": True,
        "playbook": str(path),
        "n_cues_napstorian": len(cues["napstorian"]),
        "n_cues_historian": len(cues["napping_historian"]),
        "digest": digest_training_block(),
    }
