"""CEO SMM — autonomous dual-channel growth manager.

Full-trust defaults: act on metrics, heal SOP stages (incl. GPU), patch prompts/SOPs,
refresh competitors, email Rayyan what was done — never wait for approval.

Cron: ``python -m src.cli.ceo_smm_beat`` (hourly) + hooked from watchdog/sleep beats.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents.ledger import OpsLedger
from src.agents.store import OPS_DIR, OpsStore
from src.services.settings import CONFIG_DIR
from src.services.youtube_channel_auth import KNOWN_CHANNELS, youtube_token_path

logger = logging.getLogger(__name__)

CEO_GOALS_PATH = OPS_DIR / "ceo_channel_goals.json"
CEO_REACH_LAST = OPS_DIR / "ceo_reach_last.json"
CEO_BEAT_LAST = OPS_DIR / "ceo_smm_beat_last.json"
CEO_DIGEST_MD = OPS_DIR / "ceo_smm_digest.md"
CEO_DIGEST_JSON = OPS_DIR / "ceo_smm_digest.json"
CEO_PLAYBOOK = OPS_DIR / "ceo_zero_dollar_playbook.md"
CEO_ACTIONS_LOG = OPS_DIR / "ceo_actions.jsonl"
NEW_FORMAT_SOP = OPS_DIR / "NEW_FORMAT_EVERY_VIDEO_SOP.md"

# 15 videos/mo ≈ ~4 publics/week (15/4); cadence_days=2. Frequency is Rayyan-locked.
_FIXED_WEEKLY_PUBLICS = 4
_FIXED_VIDEOS_PER_MONTH = 15
_FIXED_CADENCE_DAYS = 2
_FREQUENCY_SOURCE = "rayyan_fixed_15_per_month"

DEFAULT_GOALS: dict[str, Any] = {
    "updated_at": None,
    "primary": "monetization_path",
    "trust_mode": "full_auto_email_only",
    "channels": {
        "napstorian": {
            "ctr_pct_min": 4.0,
            "avd_pct_min": 40.0,
            "first_60s_retention_pct_min": 70.0,
            "weekly_publics_target": _FIXED_WEEKLY_PUBLICS,
            "notes": "What-If history → watch hours + subs toward monetization",
        },
        "napping_historian": {
            "ctr_pct_min": 4.0,
            "avd_pct_min": 40.0,
            "first_60s_retention_pct_min": 70.0,
            "weekly_publics_target": _FIXED_WEEKLY_PUBLICS,
            "notes": "Sleep/documentary → AVD + session time toward monetization",
        },
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ceo_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    smm: dict[str, Any] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        smm = dict((raw or {}).get("smm") or {})
    except Exception:  # noqa: BLE001
        smm = {}
    ceo = dict(smm.get("ceo") or {})
    # Full-trust defaults — always True unless explicitly set false.
    for key in (
        "enabled",
        "auto_reach_scorecard",
        "auto_sop_heal",
        "auto_gpu",
        "auto_prompt_write",
        "auto_sop_write",
        "auto_competitors",
        "auto_email",
        "auto_critical_email",
        "act_on_instant_metrics",
    ):
        if key not in ceo:
            ceo[key] = True
        else:
            ceo[key] = bool(ceo[key])
    return ceo


def load_or_seed_goals() -> dict[str, Any]:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    if CEO_GOALS_PATH.is_file():
        try:
            data = json.loads(CEO_GOALS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("channels"):
                return data
        except Exception:  # noqa: BLE001
            pass
    seeded = dict(DEFAULT_GOALS)
    seeded["updated_at"] = _now_iso()
    CEO_GOALS_PATH.write_text(json.dumps(seeded, indent=2) + "\n", encoding="utf-8")
    return seeded


def _append_action(action: dict[str, Any]) -> None:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    row = {"at": _now_iso(), **action}
    with CEO_ACTIONS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def probe_reach_reports() -> dict[str, Any]:
    """Count Reporting reach CSVs per channel (creates job if missing)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from src.services.youtube_reporting import (
        build_youtube_reporting_client,
        ensure_reach_job,
        list_job_reports,
    )

    out: dict[str, Any] = {"at": _now_iso(), "channels": {}}
    for ch in KNOWN_CHANNELS:
        try:
            token = youtube_token_path(ch)
            creds = Credentials.from_authorized_user_file(str(token))
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            rep = build_youtube_reporting_client(creds)
            ensured = ensure_reach_job(rep)
            job = ensured.get("job") or {}
            jid = str(job.get("id") or "")
            reports = list_job_reports(rep, jid) if jid else []
            out["channels"][ch] = {
                "ok": True,
                "job_id": jid,
                "job_action": ensured.get("action"),
                "reports_available": len(reports),
                "job_create_time": job.get("createTime"),
            }
        except Exception as exc:  # noqa: BLE001
            out["channels"][ch] = {"ok": False, "error": str(exc)[:400]}
    return out


def maybe_force_scorecard_on_new_reach(
    *,
    smm: Any | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """If reach CSV count increased (or force), rewrite scorecard."""
    ceo = _ceo_cfg()
    if not ceo.get("auto_reach_scorecard", True) and not force:
        return {"skipped": True, "reason": "auto_reach_scorecard=false"}

    probe = probe_reach_reports()
    prev = _read_json(CEO_REACH_LAST)
    prev_ch = prev.get("channels") or {}
    triggered: list[str] = []
    for ch, info in (probe.get("channels") or {}).items():
        if not info.get("ok"):
            continue
        n = int(info.get("reports_available") or 0)
        old = int(((prev_ch.get(ch) or {}).get("reports_available")) or 0)
        if force or n > old:
            triggered.append(ch)

    result: dict[str, Any] = {
        "probe": probe,
        "triggered_channels": triggered,
        "scorecard": None,
    }
    _write_json(CEO_REACH_LAST, probe)

    if not triggered and not force:
        result["skipped"] = True
        result["reason"] = "no_new_reach_csvs"
        return result

    from src.agents.smm_agent import SocialMediaManager

    manager = smm or SocialMediaManager()
    sc = manager.maybe_write_daily_yt_scorecard(force=True)
    result["scorecard"] = {
        "ok": bool(sc.get("ok")),
        "path": sc.get("path"),
        "ctr_pending": (sc.get("payload") or {}).get("ctr_pending_reporting"),
        "n_with_ctr": (sc.get("payload") or {}).get("n_with_ctr"),
    }
    # Instant lock when Reporting CTR + first-60s land on a winner
    try:
        from src.services.smm_pipeline_ab import maybe_lock_winner_from_scorecard

        lock_out = maybe_lock_winner_from_scorecard(
            sc.get("payload") or {},
            goals=load_or_seed_goals(),
        )
        result["winner_locks"] = {
            "n_newly_locked": lock_out.get("n_newly_locked"),
            "n_provisionals": lock_out.get("n_provisionals"),
            "newly_locked": [
                (x.get("video_id") if isinstance(x, dict) else x)
                for x in (lock_out.get("newly_locked") or [])
            ],
        }
        if lock_out.get("n_newly_locked"):
            _append_action(
                {
                    "type": "auto_lock_winners",
                    "source": "reach_scorecard",
                    "newly_locked": result["winner_locks"]["newly_locked"],
                }
            )
    except Exception as exc:  # noqa: BLE001
        result["winner_locks_error"] = str(exc)[:300]
    _append_action(
        {
            "type": "force_scorecard_on_reach",
            "triggered_channels": triggered,
            "force": force,
            "n_with_ctr": result["scorecard"].get("n_with_ctr"),
        }
    )
    # Packaging pass when CTR may now exist
    try:
        scan = manager.scan_and_act()
        result["packaging_scan"] = {
            "n": len(scan) if isinstance(scan, list) else 0,
            "coalesce": bool(
                isinstance(scan, list)
                and scan
                and isinstance(scan[0], dict)
                and scan[0].get("reason") == "quota_coalesce"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        result["packaging_scan_error"] = str(exc)[:300]
    return result


def _resend_to_farm_phase(resend: str | None) -> str | None:
    r = (resend or "").strip().lower()
    if r in {"script", "tts", "idea", "outline"}:
        return "prep"
    if r in {"stills", "visuals", "compose", "edit", "package", "publish"}:
        return "visuals"
    return None


def heal_sop_resend_jobs(*, store: OpsStore | None = None) -> dict[str, Any]:
    """Auto-spawn farm phase for jobs stamped with smm_sop_resend_stage (full GPU trust)."""
    ceo = _ceo_cfg()
    if not ceo.get("auto_sop_heal", True):
        return {"skipped": True, "reason": "auto_sop_heal=false"}

    store = store or OpsStore()
    from src.agents.farm import spawn_farm_job

    actions: list[dict[str, Any]] = []
    for job in store.list_jobs():
        meta = dict(job.meta or {})
        resend = meta.get("smm_sop_resend_stage")
        if not resend:
            continue
        if meta.get("ceo_sop_heal_spawned_for") == resend:
            continue
        # Don't thrash public/live jobs mid-stream
        if (job.status or "").lower() in {"public", "scheduled", "done"}:
            continue
        phase = _resend_to_farm_phase(str(resend))
        if not phase:
            continue
        if not ceo.get("auto_gpu", True) and phase == "visuals":
            actions.append(
                {
                    "job_id": job.id,
                    "skipped": True,
                    "reason": "auto_gpu=false",
                    "resend": resend,
                }
            )
            continue
        # Surgical still: delete named scene if present so resume regenerates one file
        scene_hint = meta.get("smm_sop_bad_scene") or meta.get("sop_bad_scene")
        deleted = None
        if scene_hint and job.job_dir:
            deleted = _delete_bad_still(Path(job.job_dir), str(scene_hint))
        try:
            spawned = spawn_farm_job(
                job.id,
                store=store,
                resume=True,
                phase=phase,
                skip_capacity_gate=False,
            )
        except Exception as exc:  # noqa: BLE001
            actions.append(
                {
                    "job_id": job.id,
                    "ok": False,
                    "error": str(exc)[:300],
                    "resend": resend,
                    "phase": phase,
                }
            )
            continue
        meta["ceo_sop_heal_spawned_for"] = resend
        meta["ceo_sop_heal_at"] = _now_iso()
        meta["ceo_sop_heal_phase"] = phase
        if deleted:
            meta["ceo_sop_deleted_still"] = deleted
        store.update_job(job.id, meta=meta)
        row = {
            "job_id": job.id,
            "ok": bool(spawned.get("ok")),
            "resend": resend,
            "phase": phase,
            "spawned": spawned,
            "deleted_still": deleted,
        }
        actions.append(row)
        _append_action({"type": "sop_heal_spawn", **row})
    return {"ok": True, "n": len(actions), "actions": actions}


def _delete_bad_still(job_dir: Path, scene_hint: str) -> str | None:
    """Delete one scene still so visuals resume regenerates only that image."""
    m = re.search(r"(\d{1,4})", scene_hint)
    if not m:
        return None
    num = int(m.group(1))
    images = job_dir / "images"
    if not images.is_dir():
        return None
    for pattern in (f"scene_{num:03d}.jpg", f"scene_{num:03d}.png", f"scene_{num}.jpg"):
        p = images / pattern
        if p.is_file():
            try:
                p.unlink()
                return str(p)
            except OSError:
                return None
    # Also accept exact relative path under job_dir
    cand = job_dir / scene_hint
    if cand.is_file() and "images" in cand.parts:
        try:
            cand.unlink()
            return str(cand)
        except OSError:
            return None
    return None


def apply_prompt_patches_from_metrics(
    *,
    store: OpsStore | None = None,
) -> dict[str, Any]:
    """Auto-patch hook/outline prompts when retention/CTR patterns fail (full trust)."""
    ceo = _ceo_cfg()
    if not ceo.get("auto_prompt_write", True):
        return {"skipped": True, "reason": "auto_prompt_write=false"}

    store = store or OpsStore()
    goals = load_or_seed_goals()
    sc_path = OPS_DIR / "smm_yt_scorecard.md"
    # Prefer last scorecard jsonl line
    payload = _latest_scorecard_payload()
    patches: list[dict[str, Any]] = []
    channels = (payload.get("channels") or {}) if payload else {}
    for ch, block in channels.items():
        reds = [r for r in (block.get("videos") or []) if r.get("flag") == "red"]
        avd_fails = [
            r
            for r in reds
            if r.get("avd_pct") is not None
            and float(r["avd_pct"])
            < float(
                ((goals.get("channels") or {}).get(ch) or {}).get("avd_pct_min") or 40
            )
        ]
        ctr_fails = [
            r
            for r in reds
            if r.get("ctr_pct") is not None
            and float(r["ctr_pct"])
            < float(
                ((goals.get("channels") or {}).get(ch) or {}).get("ctr_pct_min") or 4
            )
        ]
        first60_fails = [
            r
            for r in (block.get("videos") or [])
            if r.get("first_60s_retention_pct") is not None
            and float(r["first_60s_retention_pct"])
            < float(
                ((goals.get("channels") or {}).get(ch) or {}).get(
                    "first_60s_retention_pct_min"
                )
                or 70
            )
        ]
        if first60_fails or avd_fails:
            p = _patch_hook_prompt(ch, reason="first60_or_avd_red")
            if p:
                patches.append(p)
        if avd_fails:
            p = _patch_outline_prompt(ch, reason="avd_red")
            if p:
                patches.append(p)
        if ctr_fails:
            p = _patch_thumbnail_prompt_note(ch, reason="ctr_red")
            if p:
                patches.append(p)
    for p in patches:
        _append_action({"type": "prompt_patch", **p})
    return {"ok": True, "n": len(patches), "patches": patches}


def _prompts_dir_for_channel(channel: str) -> Path:
    ch = (channel or "").strip()
    if ch == "napping_historian":
        return CONFIG_DIR / "prompts" / "napping_historian"
    return CONFIG_DIR / "prompts"


def _append_ceo_block(path: Path, marker: str, body: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return {"path": str(path), "skipped": True, "reason": "already_patched"}
    stamp = _now_iso()
    block = (
        f"\n\n# === CEO-SMM AUTO PATCH {marker} @ {stamp} ===\n"
        f"{body.rstrip()}\n"
        f"# === END CEO-SMM PATCH ===\n"
    )
    path.write_text(text.rstrip() + block, encoding="utf-8")
    return {"path": str(path), "ok": True, "marker": marker}


def _patch_hook_prompt(channel: str, *, reason: str) -> dict[str, Any] | None:
    path = _prompts_dir_for_channel(channel) / "hook_cold_open.txt"
    body = (
        f"CEO retention hardening ({reason}): Open with the title promise in the first "
        "breath. Name the divergence, the human cost, and one concrete sensory detail "
        "before any throat-clearing. Ban soft intros ('in this video', 'today we explore'). "
        "First 15s must feel like the middle of a crisis, not a lecture."
    )
    return _append_ceo_block(path, f"HOOK_{channel.upper()}", body)


def _patch_outline_prompt(channel: str, *, reason: str) -> dict[str, Any] | None:
    path = _prompts_dir_for_channel(channel) / "outline.txt"
    if not path.is_file():
        return None
    body = (
        f"CEO pacing hardening ({reason}): Force a pattern interrupt every ~90–120s "
        "(question, reversal, sealed-letter beat, or visual reset). Cut filler bridges. "
        "Each chapter must advance stakes; no repeated exposition. Prefer shorter mid-video "
        "plateaus — AVD floor is a channel goal."
    )
    return _append_ceo_block(path, f"OUTLINE_{channel.upper()}", body)


def _patch_thumbnail_prompt_note(channel: str, *, reason: str) -> dict[str, Any] | None:
    path = _prompts_dir_for_channel(channel) / "thumbnail_template.txt"
    if not path.is_file():
        return None
    body = (
        f"CEO CTR hardening ({reason}): One dominant face/object, high-contrast focal point, "
        "readable 3–5 word promise matching the title. Avoid cluttered collage. "
        "Emotion > decoration."
    )
    return _append_ceo_block(path, f"THUMB_{channel.upper()}", body)


def evolve_sop_from_competitors() -> dict[str, Any]:
    """Append $0 competitor-learned rules into NEW_FORMAT SOP (auto-write default on)."""
    ceo = _ceo_cfg()
    playbook = build_zero_dollar_playbook()
    if not ceo.get("auto_sop_write", True):
        return {
            "skipped": True,
            "reason": "auto_sop_write=false",
            "playbook_path": str(CEO_PLAYBOOK),
        }
    if not NEW_FORMAT_SOP.is_file():
        return {"ok": False, "error": "NEW_FORMAT_EVERY_VIDEO_SOP.md missing"}
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    marker = f"CEO_COMPETITOR_SOP_EVOLUTION_{day}"
    text = NEW_FORMAT_SOP.read_text(encoding="utf-8")
    if marker in text:
        return {"skipped": True, "reason": "already_evolved_today", "marker": marker}
    bullets = playbook.get("sop_bullets") or []
    if not bullets:
        return {"skipped": True, "reason": "no_bullets"}
    block = (
        f"\n\n## {marker}\n\n"
        f"_Auto-appended by CEO-SMM {_now_iso()} from competitor $0 scan. "
        f"Full trust mode — emailed in digest (no approval wait)._ \n\n"
        + "\n".join(f"- {b}" for b in bullets)
        + "\n"
    )
    NEW_FORMAT_SOP.write_text(text.rstrip() + block, encoding="utf-8")
    _append_action({"type": "sop_evolve", "n_bullets": len(bullets), "marker": marker})
    return {"ok": True, "n_bullets": len(bullets), "path": str(NEW_FORMAT_SOP), "marker": marker}


def build_zero_dollar_playbook() -> dict[str, Any]:
    """Synthesize $0 tactics from competitor configs + metrics artifacts."""
    lines = [
        "# CEO $0 competitor playbook",
        "",
        f"_Generated: {_now_iso()}_",
        "",
        "Tactics that cost $0 (prompt/SOP/packaging/cadence only — no paid tools).",
        "",
    ]
    sop_bullets: list[str] = []
    for ch in KNOWN_CHANNELS:
        path = CONFIG_DIR / (
            "competitors.json"
            if ch == "napstorian"
            else f"competitors_{ch}.json"
        )
        if ch == "napstorian":
            path = CONFIG_DIR / "competitors.json"
        if not path.is_file():
            lines.append(f"## {ch}\n- No competitors file at `{path.name}`\n")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        comps = data.get("competitors") or []
        lines.append(f"## {ch}")
        lines.append(f"- Tracked competitors: {len(comps)}")
        # Sort by recent median views when present
        ranked = sorted(
            [c for c in comps if isinstance(c, dict)],
            key=lambda c: float(c.get("median_recent_views") or c.get("avg_recent_views") or 0),
            reverse=True,
        )[:5]
        for c in ranked:
            label = c.get("label") or c.get("handle") or c.get("channel_id")
            med = c.get("median_recent_views") or c.get("avg_recent_views")
            freq = c.get("upload_freq_per_30d")
            lines.append(
                f"- **{label}**: median_recent_views={med}, "
                f"uploads/30d={freq}, active_7d={c.get('active_last_7_days')}"
            )
        lines.append("")
        # $0 tactics
        tactics = [
            "Mirror winning packaging pattern length (short punchy title promise) without copying titles.",
            "Match competitor upload-hour peaks in Asia/Karachi (CEO auto-writes preferred_hours).",
            "Keep volume locked at 15/mo (cadence_days=2) — competitor freq is informational only.",
            "Lead thumbnails with one emotional focal subject (competitor pattern).",
            "Put the counterfactual / mystery hook in cold open (title promise in first breath).",
            "Use chapters + pin CTA (already SOP) — competitors with high watch often structure longform.",
        ]
        lines.append("### $0 implement now")
        for t in tactics:
            lines.append(f"- {t}")
            sop_bullets.append(f"[{ch}] {t}")
        lines.append("")

    CEO_PLAYBOOK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(CEO_PLAYBOOK), "sop_bullets": sop_bullets[:12]}


def refresh_competitors_if_due(*, force: bool = False) -> dict[str, Any]:
    ceo = _ceo_cfg()
    if not ceo.get("auto_competitors", True):
        return {"skipped": True, "reason": "auto_competitors=false"}
    from src.agents.competitors_agent import CompetitorsAgent

    out: dict[str, Any] = {"channels": {}}
    for ch in KNOWN_CHANNELS:
        try:
            agent = CompetitorsAgent(channel=ch)
            refreshed = agent.refresh(force=force)
            # Always pull upload hours / freq for schedule learning (both channels).
            try:
                metrics = agent.refresh_metrics(force=True, dry_run=False)
            except Exception as mex:  # noqa: BLE001
                metrics = {"ok": False, "error": str(mex)[:300]}
            out["channels"][ch] = {"refresh": refreshed, "metrics": metrics}
        except Exception as exc:  # noqa: BLE001
            out["channels"][ch] = {"ok": False, "error": str(exc)[:300]}
    _append_action({"type": "competitors_refresh", "force": force})
    return out


def _competitor_path_for(channel: str) -> Path:
    from src.agents.competitors_agent import competitors_path

    return competitors_path(channel)


def _aggregate_competitor_schedule(channel: str) -> dict[str, Any]:
    """Aggregate competitor upload hours + pacing for one of our Brand channels."""
    path = _competitor_path_for(channel)
    if not path.is_file():
        return {"ok": False, "channel": channel, "reason": "no_competitors_file"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "channel": channel, "error": str(exc)[:200]}

    comps = [c for c in (data.get("competitors") or []) if isinstance(c, dict)]
    hour_weights: dict[int, float] = {}
    freqs: list[float] = []
    used = 0
    # Style-role multipliers so FoC aspirational / sleep-only peers do not
    # dominate preferred_hours vs History Calling–tier anchors.
    _ROLE_HOUR_MULT = {
        "primary_anchor": 3.0,
        "style_peer": 1.5,
        "optional_format": 0.5,
        "ambient_sleep": 0.35,
        "aspirational_reference": 0.08,
    }
    for c in comps:
        # Prefer eligible; fall back to any with hour signal
        hours = c.get("best_upload_hours_local") or []
        hist = c.get("upload_hour_histogram_local") or {}
        weight = float(
            c.get("median_recent_views")
            or c.get("avg_recent_views")
            or 1
        )
        role = str(c.get("style_role") or "style_peer").strip().lower()
        label_l = str(c.get("label") or "").strip().lower()
        if "fall of civilizations" in label_l:
            role = "aspirational_reference"
        if role in {"aspirational_reference", "ambient_sleep"}:
            # FoC / sleep-only peers stay on the list for craft cues but must not
            # drive preferred_hours or competitor-implied frequency.
            continue
        role_mult = float(_ROLE_HOUR_MULT.get(role, 1.0))
        weight = max(weight, 1.0) * role_mult
        if hist:
            for h_s, w in hist.items():
                try:
                    h = int(h_s)
                    hour_weights[h] = hour_weights.get(h, 0.0) + float(w) * weight
                except (TypeError, ValueError):
                    continue
            used += 1
        elif hours:
            for i, h in enumerate(hours[:3]):
                try:
                    hi = int(h)
                except (TypeError, ValueError):
                    continue
                hour_weights[hi] = hour_weights.get(hi, 0.0) + weight * (3 - i)
            used += 1
        freq = c.get("upload_freq_per_30d")
        try:
            f = float(freq)
        except (TypeError, ValueError):
            f = 0.0
        if f > 0:
            freqs.append(f)

    if not hour_weights and not freqs:
        return {
            "ok": False,
            "channel": channel,
            "reason": "no_hour_or_freq_signal",
            "competitors_n": len(comps),
        }

    best_hours = [
        h
        for h, _w in sorted(hour_weights.items(), key=lambda x: x[1], reverse=True)[:3]
    ]
    median_freq = sorted(freqs)[len(freqs) // 2] if freqs else 0.0
    # Competitor median freq is informational only. Our publish volume is locked
    # (15/mo, cadence_days=2) — apply_competitor_schedules never writes these.
    competitor_implied_cadence = None
    competitor_implied_monthly = None
    if median_freq > 0:
        competitor_implied_cadence = max(1, min(7, int(round(30.0 / median_freq))))
        competitor_implied_monthly = max(1, min(30, int(round(median_freq))))

    return {
        "ok": True,
        "channel": channel,
        "best_hours": best_hours,
        "hour_weights": {str(k): round(v, 1) for k, v in hour_weights.items()},
        "median_upload_freq_30d": median_freq,
        # Locked Rayyan targets (what we actually publish at):
        "cadence_days": _FIXED_CADENCE_DAYS,
        "videos_per_month_target": _FIXED_VIDEOS_PER_MONTH,
        "frequency_locked": True,
        "frequency_source": _FREQUENCY_SOURCE,
        # Competitor-implied (do not apply to schedule writers):
        "competitor_implied_cadence_days": competitor_implied_cadence,
        "competitor_implied_videos_per_month": competitor_implied_monthly,
        "competitors_used": used,
        "competitors_n": len(comps),
        "freqs_n": len(freqs),
    }


def apply_competitor_schedules_both_channels() -> dict[str, Any]:
    """Update preferred_hours ONLY for BOTH channels from competitor upload hours.

    Frequency is Rayyan-locked (15/mo, cadence_days=2): never writes
    ``cadence_days``, ``videos_per_month_target``, or ``weekly_publics_target``
    from competitor upload frequency.
    """
    ceo = _ceo_cfg()
    if not ceo.get("auto_competitors", True):
        return {"skipped": True, "reason": "auto_competitors=false"}

    from src.agents.schedule_agent import ScheduleAgent

    sched = ScheduleAgent()
    out: dict[str, Any] = {
        "ok": True,
        "channels": {},
        "frequency_locked": True,
        "frequency_source": _FREQUENCY_SOURCE,
        "videos_per_month_target": _FIXED_VIDEOS_PER_MONTH,
        "cadence_days": _FIXED_CADENCE_DAYS,
    }

    for ch in KNOWN_CHANNELS:
        agg = _aggregate_competitor_schedule(ch)
        channel_out: dict[str, Any] = {"aggregate": agg}
        if not agg.get("ok"):
            channel_out["skipped"] = True
            out["channels"][ch] = channel_out
            continue

        hours = list(agg.get("best_hours") or [])
        if hours:
            try:
                channel_out["hours"] = sched.write_channel_preferred_hours(
                    ch,
                    hours,
                    source="ceo_competitor_upload_hours",
                    soft=False,
                )
            except Exception as exc:  # noqa: BLE001
                channel_out["hours"] = {"ok": False, "error": str(exc)[:300]}

        # Frequency locked — never write cadence / monthly target from competitors.
        channel_out["cadence"] = {
            "ok": True,
            "applied": False,
            "reason": "frequency_locked",
            "cadence_days": _FIXED_CADENCE_DAYS,
            "videos_per_month_target": _FIXED_VIDEOS_PER_MONTH,
            "source": _FREQUENCY_SOURCE,
            "competitor_implied_cadence_days": agg.get("competitor_implied_cadence_days"),
            "competitor_implied_videos_per_month": agg.get(
                "competitor_implied_videos_per_month"
            ),
        }

        try:
            goals = load_or_seed_goals()
            ch_goals = dict((goals.get("channels") or {}).get(ch) or {})
            # Keep weekly target locked to 15/mo ≈ 4/week — do not derive from competitors.
            ch_goals["weekly_publics_target"] = _FIXED_WEEKLY_PUBLICS
            ch_goals["competitor_schedule"] = {
                "best_hours": hours,
                "cadence_days": _FIXED_CADENCE_DAYS,
                "videos_per_month_target": _FIXED_VIDEOS_PER_MONTH,
                "frequency_locked": True,
                "frequency_source": _FREQUENCY_SOURCE,
                "median_upload_freq_30d": agg.get("median_upload_freq_30d"),
                "median_upload_freq_30d_note": (
                    "Competitor median is informational only; does not drive our cadence."
                ),
                "competitor_implied_cadence_days": agg.get(
                    "competitor_implied_cadence_days"
                ),
                "competitor_implied_videos_per_month": agg.get(
                    "competitor_implied_videos_per_month"
                ),
                "updated_at": _now_iso(),
            }
            goals.setdefault("channels", {})[ch] = ch_goals
            goals["updated_at"] = _now_iso()
            _write_json(CEO_GOALS_PATH, goals)
            channel_out["goals_updated"] = True
        except Exception as exc:  # noqa: BLE001
            channel_out["goals_error"] = str(exc)[:200]

        _append_action(
            {
                "type": "competitor_schedule",
                "channel": ch,
                "hours": hours,
                "cadence_days": _FIXED_CADENCE_DAYS,
                "videos_per_month_target": _FIXED_VIDEOS_PER_MONTH,
                "frequency_locked": True,
                "median_freq_30d": agg.get("median_upload_freq_30d"),
            }
        )
        out["channels"][ch] = channel_out

    out["global_cadence"] = {
        "ok": True,
        "applied": False,
        "reason": "frequency_locked",
        "cadence_days": _FIXED_CADENCE_DAYS,
        "videos_per_month_target": _FIXED_VIDEOS_PER_MONTH,
        "source": _FREQUENCY_SOURCE,
    }

    return out


def _latest_scorecard_payload() -> dict[str, Any]:
    jsonl = OPS_DIR / "smm_yt_scorecard.jsonl"
    if not jsonl.is_file():
        return {}
    try:
        lines = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return {}
        data = json.loads(lines[-1])
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def build_ceo_digest(
    *,
    actions: list[dict[str, Any]] | None = None,
    goals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build projected-vs-actual digest from latest scorecard + goals."""
    goals = goals or load_or_seed_goals()
    payload = _latest_scorecard_payload()
    actions = actions or []
    lines = [
        "# CEO-SMM digest",
        "",
        f"- At (UTC): `{_now_iso()}`",
        f"- Trust mode: **full auto** — actions executed; this email reports what happened",
        f"- Primary goal: `{goals.get('primary')}`",
        "",
    ]
    channel_health: dict[str, str] = {}
    video_rows: list[dict[str, Any]] = []

    for ch, block in (payload.get("channels") or {}).items():
        g = (goals.get("channels") or {}).get(ch) or {}
        ctr_min = float(g.get("ctr_pct_min") or 4)
        avd_min = float(g.get("avd_pct_min") or 40)
        r60_min = float(g.get("first_60s_retention_pct_min") or 70)
        n_red = int(block.get("n_red") or 0)
        n_pub = int(block.get("n_public") or 0)
        health = "healthy"
        if n_pub == 0:
            health = "no_tracked_publics"
        elif n_red >= max(1, n_pub):
            health = "critical"
        elif n_red > 0:
            health = "at_risk"
        channel_health[ch] = health
        lines.append(f"## {ch} — `{health}`")
        lines.append(
            f"- Targets: CTR≥{ctr_min}% · AVD≥{avd_min}% · first-60s≥{r60_min}%"
        )
        lines.append(
            f"- Public tracked: {n_pub} · red: {n_red} · "
            f"Live concurrent: {block.get('live_concurrent')}"
        )
        lines.append("")
        lines.append(
            "| title | views | CTR (now→target) | AVD (now→target) | first60 | flag | projectile |"
        )
        lines.append("|---|---:|---|---|---|---|---|")
        for r in (block.get("videos") or [])[:12]:
            views = r.get("views")
            ctr = r.get("ctr_pct")
            avd = r.get("avd_pct")
            r60 = r.get("first_60s_retention_pct")
            projectile = _projectile(
                views=views,
                ctr=ctr,
                avd=avd,
                ctr_min=ctr_min,
                avd_min=avd_min,
            )
            video_rows.append(
                {
                    "channel": ch,
                    "title": r.get("title"),
                    "video_id": r.get("video_id"),
                    "views": views,
                    "ctr_pct": ctr,
                    "avd_pct": avd,
                    "first_60s_retention_pct": r60,
                    "targets": {
                        "ctr_pct_min": ctr_min,
                        "avd_pct_min": avd_min,
                        "first_60s_retention_pct_min": r60_min,
                    },
                    "flag": r.get("flag"),
                    "projectile": projectile,
                }
            )
            lines.append(
                f"| {(r.get('title') or '')[:48]} | {views} | "
                f"{ctr}→{ctr_min} | {avd}→{avd_min} | {r60} | "
                f"{r.get('flag')} | {projectile} |"
            )
        lines.append("")

    if payload.get("ctr_pending_reporting"):
        lines.append(
            "## CTR status\n"
            "Reporting reach CSVs still pending — packaging waits for real CTR; "
            "AVD/views actions already active.\n"
        )

    lines.append("## Actions taken this beat")
    if actions:
        for a in actions[:40]:
            lines.append(f"- `{a.get('type') or a}` {json.dumps({k: v for k, v in a.items() if k != 'type'}, default=str)[:180]}")
    else:
        lines.append("- (none new)")
    lines.append("")

    critical = [ch for ch, h in channel_health.items() if h == "critical"]
    if critical:
        lines.append("## CEO verdict")
        lines.append(
            f"**Channels at critical:** {', '.join(critical)}. "
            "CEO continues auto-healing (packaging/SOP/prompts). "
            "If trajectory stays red after maturity + retries, treat as format risk — "
            "consider pausing invent volume and doubling cold-open/SOP experiments."
        )
        lines.append("")

    playbook = CEO_PLAYBOOK if CEO_PLAYBOOK.is_file() else None
    if playbook:
        lines.append(f"## $0 playbook\nSee `{playbook.name}` (competitor-derived).\n")

    try:
        from src.services.smm_pipeline_ab import digest_training_block

        lines.append(digest_training_block())
    except Exception:  # noqa: BLE001
        pass

    body = "\n".join(lines) + "\n"
    CEO_DIGEST_MD.write_text(body, encoding="utf-8")
    digest = {
        "at": _now_iso(),
        "channel_health": channel_health,
        "videos": video_rows,
        "actions": actions,
        "ctr_pending_reporting": payload.get("ctr_pending_reporting"),
        "critical_channels": critical,
        "body_path": str(CEO_DIGEST_MD),
    }
    _write_json(CEO_DIGEST_JSON, digest)
    return digest


def _projectile(
    *,
    views: Any,
    ctr: Any,
    avd: Any,
    ctr_min: float,
    avd_min: float,
) -> str:
    try:
        v = int(views) if views is not None else None
    except (TypeError, ValueError):
        v = None
    ctr_ok = ctr is None or float(ctr) >= ctr_min
    avd_ok = avd is None or float(avd) >= avd_min
    if ctr is None and avd is None:
        return "awaiting_analytics"
    if not ctr_ok and not avd_ok:
        return "critical_behind"
    if not ctr_ok or not avd_ok:
        return "behind_bars"
    if v is not None and v < 50:
        return "on_track_early"
    return "on_track"


def maybe_email_digest(
    digest: dict[str, Any],
    *,
    ledger: OpsLedger | None = None,
    force: bool = False,
) -> dict[str, Any]:
    ceo = _ceo_cfg()
    ledger = ledger or OpsLedger()
    body = CEO_DIGEST_MD.read_text(encoding="utf-8") if CEO_DIGEST_MD.is_file() else ""
    critical = list(digest.get("critical_channels") or [])

    # Critical always emails when configured
    if critical and ceo.get("auto_critical_email", True):
        subj = f"[CEO-SMM CRITICAL] channels={','.join(critical)}"
        return {
            "critical": ledger.alert_now(subject=subj, body=body or subj),
            "digest": None,
        }

    if not ceo.get("auto_email", True) and not force:
        return {"skipped": True, "reason": "auto_email=false"}

    # Coalesce CEO digests to ~6h unless force
    last = _read_json(CEO_BEAT_LAST)
    last_email = last.get("last_email_at")
    if last_email and not force:
        try:
            prev = datetime.fromisoformat(str(last_email).replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - prev).total_seconds() / 3600.0
            if age_h < 6.0 and not digest.get("actions"):
                return {"skipped": True, "reason": "email_coalesce_6h"}
        except Exception:  # noqa: BLE001
            pass

    subject = "[CEO-SMM] channel digest — actions + projected vs actual"
    if not ledger.smtp_configured():
        return {
            "ok": False,
            "sent_email": False,
            "detail": "SMTP not configured — wrote ceo_smm_digest.md only",
            "path": str(CEO_DIGEST_MD),
        }
    sent, detail = ledger._send_smtp(subject, body)
    return {"ok": sent, "sent_email": sent, "detail": detail, "subject": subject}


def run_ceo_beat(
    *,
    force_scorecard: bool = False,
    force_email: bool = False,
    force_competitors: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One CEO tick: reach→scorecard, SOP heal, competitors, prompts/SOP, digest email."""
    ceo = _ceo_cfg()
    out: dict[str, Any] = {
        "at": _now_iso(),
        "trust_mode": "full_auto_email_only",
        "ceo_cfg": {k: ceo.get(k) for k in sorted(ceo)},
        "steps": {},
    }
    if not ceo.get("enabled", True):
        out["skipped"] = True
        out["reason"] = "ceo.enabled=false"
        return out
    if dry_run:
        out["dry_run"] = True

    goals = load_or_seed_goals()
    out["goals_path"] = str(CEO_GOALS_PATH)
    actions_summary: list[dict[str, Any]] = []

    # 1) Reach CSV watcher → force scorecard + packaging
    if not dry_run:
        reach = maybe_force_scorecard_on_new_reach(force=force_scorecard)
        out["steps"]["reach_scorecard"] = reach
        if reach.get("triggered_channels") or force_scorecard:
            actions_summary.append(
                {
                    "type": "reach_scorecard",
                    "triggered": reach.get("triggered_channels"),
                    "n_with_ctr": (reach.get("scorecard") or {}).get("n_with_ctr"),
                }
            )
    else:
        out["steps"]["reach_scorecard"] = {"skipped": True, "reason": "dry_run"}

    # 2) Instant metrics: ensure scorecard exists at least daily via SMM; act via scan
    if not dry_run and ceo.get("act_on_instant_metrics", True):
        try:
            from src.agents.smm_agent import SocialMediaManager

            smm = SocialMediaManager()
            # Always refresh scorecard on CEO hourly if missing today
            sc = smm.maybe_write_daily_yt_scorecard(force=force_scorecard)
            out["steps"]["scorecard"] = {
                "ok": sc.get("ok"),
                "path": sc.get("path"),
                "ctr_pending": (sc.get("payload") or {}).get("ctr_pending_reporting"),
            }
            try:
                from src.services.smm_pipeline_ab import maybe_lock_winner_from_scorecard

                lock_out = maybe_lock_winner_from_scorecard(
                    sc.get("payload") or {},
                    goals=goals,
                )
                out["steps"]["winner_locks"] = {
                    "n_newly_locked": lock_out.get("n_newly_locked"),
                    "n_provisionals": lock_out.get("n_provisionals"),
                    "newly_locked": [
                        (x.get("video_id") if isinstance(x, dict) else x)
                        for x in (lock_out.get("newly_locked") or [])
                    ],
                }
                if lock_out.get("n_newly_locked"):
                    actions_summary.append(
                        {
                            "type": "auto_lock_winners",
                            "newly_locked": out["steps"]["winner_locks"]["newly_locked"],
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                out["steps"]["winner_locks"] = {"error": str(exc)[:300]}
            scan = smm.scan_and_act()
            out["steps"]["smm_scan"] = {
                "n": len(scan) if isinstance(scan, list) else 0,
            }
            actions_summary.append({"type": "smm_scan_instant_metrics"})
        except Exception as exc:  # noqa: BLE001
            out["steps"]["smm_scan"] = {"error": str(exc)[:300]}

    # 3) SOP heal (GPU allowed)
    if not dry_run:
        heal = heal_sop_resend_jobs()
        out["steps"]["sop_heal"] = heal
        for a in heal.get("actions") or []:
            actions_summary.append({"type": "sop_heal", **{k: a.get(k) for k in ("job_id", "phase", "resend", "ok")}})
    else:
        out["steps"]["sop_heal"] = {"skipped": True, "reason": "dry_run"}

    # 4) Competitors + metrics + schedule hours (frequency locked) + $0 playbook + SOP evolve
    if not dry_run:
        out["steps"]["competitors"] = refresh_competitors_if_due(force=force_competitors)
        out["steps"]["competitor_schedule"] = apply_competitor_schedules_both_channels()
        for ch, payload in (out["steps"]["competitor_schedule"].get("channels") or {}).items():
            if payload.get("skipped"):
                continue
            actions_summary.append(
                {
                    "type": "competitor_schedule",
                    "channel": ch,
                    "hours": (payload.get("hours") or {}).get("preferred_hours"),
                    "cadence": (payload.get("cadence") or {}).get("cadence_days"),
                    "frequency_locked": True,
                }
            )
        out["steps"]["playbook"] = build_zero_dollar_playbook()
        out["steps"]["sop_evolve"] = evolve_sop_from_competitors()
        if out["steps"]["sop_evolve"].get("ok"):
            actions_summary.append({"type": "sop_evolve", **out["steps"]["sop_evolve"]})
    else:
        out["steps"]["playbook"] = build_zero_dollar_playbook()
        out["steps"]["competitor_schedule"] = {
            "dry_run": True,
            "preview": {
                ch: _aggregate_competitor_schedule(ch) for ch in KNOWN_CHANNELS
            },
        }

    # 5) Prompt patches from metrics
    if not dry_run:
        # Snapshot prompt files before patches (consent restore trail)
        try:
            from src.services.smm_pipeline_ab import snapshot_coding

            prompt_paths: list[Path] = []
            for ch in KNOWN_CHANNELS:
                base = (
                    CONFIG_DIR / "prompts" / "napping_historian"
                    if ch == "napping_historian"
                    else CONFIG_DIR / "prompts"
                )
                for name in ("hook_cold_open.txt", "outline.txt", "thumbnail_template.txt"):
                    p = base / name
                    if p.is_file():
                        prompt_paths.append(p)
            if prompt_paths:
                out["steps"]["code_snapshot_pre_prompts"] = snapshot_coding(
                    prompt_paths,
                    reason="pre_prompt_patches_ceo_beat",
                )
        except Exception as exc:  # noqa: BLE001
            out["steps"]["code_snapshot_pre_prompts"] = {"error": str(exc)[:200]}

        out["steps"]["prompt_patches"] = apply_prompt_patches_from_metrics()
        for p in out["steps"]["prompt_patches"].get("patches") or []:
            actions_summary.append({"type": "prompt_patch", "path": p.get("path")})
    else:
        out["steps"]["prompt_patches"] = {"skipped": True, "reason": "dry_run"}

    # 5b) SMM self-train + playbook from competitor cues (both channels)
    try:
        from src.services import smm_pipeline_ab as _ab

        if not dry_run:
            out["steps"]["smm_train"] = _ab.run_training_tick()
            actions_summary.append(
                {
                    "type": "smm_train",
                    "n_cues_napstorian": out["steps"]["smm_train"].get("n_cues_napstorian"),
                    "n_cues_historian": out["steps"]["smm_train"].get("n_cues_historian"),
                }
            )
            restores = []
            for row in _ab._read_jsonl(_ab.CODE_CHANGELOG):
                if row.get("event") == "proposed_restore" or row.get("status") == "proposed_restore":
                    sid = row.get("id")
                    if sid:
                        r = _ab.apply_coding_restore_if_consented(str(sid))
                        if r.get("ok"):
                            restores.append(sid)
            out["steps"]["coding_restores"] = {"applied": restores}
        else:
            out["steps"]["smm_train"] = _ab.run_training_tick()
    except Exception as exc:  # noqa: BLE001
        out["steps"]["smm_train"] = {"error": str(exc)[:300]}

    # 5c) Premium editor — competitor style intel + editing lever directives
    try:
        from src.services.premium_editor_smm import run_premium_editor_beat

        sc_payload = None
        sc_path = OPS_DIR / "smm_yt_scorecard.json"
        if sc_path.is_file():
            try:
                sc_payload = json.loads(sc_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                sc_payload = None
        pe = run_premium_editor_beat(
            goals=goals,
            scorecard=sc_payload,
            dry_run=dry_run,
            fetch_durations=not dry_run,
        )
        out["steps"]["premium_editor"] = pe
        for ch, payload in (pe.get("channels") or {}).items():
            if payload.get("chosen_lever"):
                actions_summary.append(
                    {
                        "type": "premium_editor_lever",
                        "channel": ch,
                        "lever": payload.get("chosen_lever"),
                        "underperforming": payload.get("underperforming"),
                    }
                )
        # Thumb redesign runs inside premium_editor (after competitor intel).
        tr = pe.get("thumb_redesign") or {}
        out["steps"]["thumb_redesign"] = tr
        for ch, payload in (tr.get("channels") or {}).items():
            if not isinstance(payload, dict) or payload.get("skipped"):
                continue
            actions_summary.append(
                {
                    "type": "thumb_redesign",
                    "channel": ch,
                    "map_vs_face": (payload.get("after") or {}).get("map_vs_face"),
                    "mood": (payload.get("after") or {}).get("mood"),
                    "n_refs": payload.get("n_refs"),
                    "n_analyzed": payload.get("n_analyzed"),
                }
            )
            if payload.get("network_escalation"):
                out["thumb_redesign_escalation"] = payload["network_escalation"]
    except Exception as exc:  # noqa: BLE001
        out["steps"]["premium_editor"] = {"error": str(exc)[:300]}

    # 6) Digest + email (report what was done — no approval wait)
    digest = build_ceo_digest(actions=actions_summary, goals=goals)
    out["steps"]["digest"] = {
        "path": digest.get("body_path"),
        "critical_channels": digest.get("critical_channels"),
        "channel_health": digest.get("channel_health"),
    }
    if not dry_run:
        mail = maybe_email_digest(digest, force=force_email)
        out["steps"]["email"] = mail
        if mail.get("sent_email"):
            stamp = _read_json(CEO_BEAT_LAST)
            stamp["last_email_at"] = _now_iso()
            _write_json(CEO_BEAT_LAST, stamp)
    else:
        out["steps"]["email"] = {"skipped": True, "reason": "dry_run"}

    # 7) EOD digest (once/day after 23:00 Asia/Karachi) — finance + SMM + Live + RAM
    try:
        from src.agents.eod_digest import maybe_send_eod_digest

        out["steps"]["eod"] = maybe_send_eod_digest(force=False, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        out["steps"]["eod"] = {"ok": False, "error": str(exc)[:300]}

    out["actions"] = actions_summary
    _write_json(CEO_BEAT_LAST, {**_read_json(CEO_BEAT_LAST), "last_beat": out})
    try:
        OpsLedger().write(
            agent="ceo_smm",
            problem="CEO beat complete",
            action=f"steps={list(out['steps'].keys())} actions={len(actions_summary)}",
            severity="warn" if digest.get("critical_channels") else "info",
            extra={"critical": digest.get("critical_channels"), "health": digest.get("channel_health")},
        )
    except Exception:  # noqa: BLE001
        pass
    return out
