"""New-format every-video SOP compliance check for SMM.

Inspects job meta / edit_manifest / publish_manifest / youtube_meta against
``output/ops/NEW_FORMAT_EVERY_VIDEO_SOP.md``. Soft alerts by default
(``smm.enforce_new_format_sop``); does not block Live mid-stream.

Per-stage gates (``smm.sop_stage_gates``): before advancing pipeline stages,
evaluate the *completed* stage against its SOP map. Soft audit by default
(``sop_stage_gates_hard=false``); hard-block only when that flag is on
(compose→package/publish is the primary wired hard path).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.gate_r_env import gate_r_enforce_enabled
from src.services.settings import CONFIG_DIR, ROOT

logger = logging.getLogger(__name__)

SOP_ALERT_TYPE = "sop_compliance"
SOP_STAGE_GATE_TYPE = "sop_stage_gate"


class SopStageGateError(RuntimeError):
    """Raised when a hard SOP stage gate blocks pipeline advance."""

    def __init__(self, message: str, *, digest: dict[str, Any] | None = None):
        super().__init__(message)
        self.digest = digest or {}


# Canonical SOP stages (logical) in pipeline order.
SOP_STAGE_ORDER: tuple[str, ...] = (
    "idea",
    "script",
    "tts",
    "stills",
    "compose",
    "package",
    "publish",
    "live",
)

# Farm / Pipeline.on_stage names → SOP stage.
PIPELINE_TO_SOP_STAGE: dict[str, str] = {
    "scripting": "script",
    "tts": "tts",
    "visuals": "stills",
    "edit": "compose",
    "package": "package",
    "publish": "publish",
    "private": "publish",
    "public": "live",
    "awaiting_gpu": "tts",  # Phase A park — script+tts done
}

# When *entering* a pipeline stage, verify this completed SOP stage first.
ENTERING_REQUIRES_COMPLETED: dict[str, str] = {
    "tts": "script",
    "visuals": "tts",
    "edit": "stills",
    "publish": "compose",  # compose MUST pass before package/publish advance
    "package": "compose",
}

# SOP checklist items (# / id) owned by each stage (for docs + compliance table).
STAGE_SOP_ITEMS: dict[str, dict[str, Any]] = {
    "idea": {
        "must": ["14_selected_pack", "12_dual_channel_tone", "11_evergreen_bias"],
        "should": [],
        "pipeline": None,
        "resend": "idea",
        "notes": "Harvest / outline / hook selection before script",
    },
    "script": {
        "must": ["1_hook_cold_open", "12_dual_channel_tone", "14_selected_pack"],
        "should": [],
        "pipeline": "scripting",
        "resend": "script",
        "notes": "script.json + channel cold-open rules",
    },
    "tts": {
        "must": ["4_kokoro"],
        "should": [],
        "pipeline": "tts",
        "resend": "tts",
        "notes": "voice_manifest + Kokoro WAVs",
    },
    "stills": {
        "must": ["3_flux_stills"],
        "should": ["S1_pd_clippings"],
        "pipeline": "visuals",
        "resend": "stills",
        "notes": "Wiki+Met PD/OA first → vision_judge; PASS heroes; REJECT/gaps → Flux (both channels)",
    },
    "compose": {
        "must": [
            "5_ken_burns",
            "6_infographic_overlays",
            "7_premium_overlays_v1",
            "8_date_overlay",
            "9_loudness",
            "14_selected_pack",
            "15_cinematic_sfx",
            "16_compliance_table",
        ],
        "should": ["S1_pd_clippings"],
        "pipeline": "edit",
        "resend": "compose",
        "notes": "final.mp4 + edit_manifest burn evidence",
    },
    "package": {
        "must": ["10_soft_packaging_chapters_pin"],
        "should": [],
        "pipeline": "package",
        "resend": "package",
        "notes": "youtube_meta title/desc/tags/chapters (+ thumb)",
    },
    "publish": {
        "must": ["2_gate_r", "10_soft_packaging_chapters_pin", "14_selected_pack"],
        "should": [],
        "pipeline": "publish",
        "resend": "publish",
        "notes": "Gate R (advisory) / Gate B / private upload evidence",
    },
    "live": {
        "must": ["10_soft_packaging_chapters_pin", "11_evergreen_bias", "16_compliance_table"],
        "should": [],
        "pipeline": "public",
        "resend": "live",
        "notes": "Public pin+chapters; never hard-block Live encode mid-stream",
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agents_cfg() -> dict[str, Any]:
    path = CONFIG_DIR / "agents_settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _normalize_channel(channel: str | None) -> str:
    from src.services.youtube_channel_auth import normalize_youtube_channel

    return normalize_youtube_channel(channel)


def _is_historian(channel: str | None) -> bool:
    ch = _normalize_channel(channel)
    return ch in {"napping_historian", "historian"}


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _job_dir(job: Any) -> Path | None:
    raw = getattr(job, "job_dir", None)
    if not raw and isinstance(job, dict):
        raw = job.get("job_dir")
    if not raw:
        meta = _job_meta(job)
        raw = meta.get("job_dir")
    if not raw:
        return None
    p = Path(str(raw))
    return p if p.is_dir() else p


def _job_meta(job: Any) -> dict[str, Any]:
    if job is None:
        return {}
    meta = getattr(job, "meta", None)
    if meta is None and isinstance(job, dict):
        meta = job.get("meta")
    return dict(meta or {})


def _job_channel(job: Any) -> str:
    meta = _job_meta(job)
    return _normalize_channel(
        meta.get("channel") or meta.get("sheet_tab") or meta.get("youtube_channel")
    )


def _job_status(job: Any) -> str:
    if job is None:
        return ""
    status = getattr(job, "status", None)
    if status is None and isinstance(job, dict):
        status = job.get("status")
    return str(status or "").lower()


def _edit_manifest(job_dir: Path | None) -> dict[str, Any] | None:
    if job_dir is None:
        return None
    return _load_json(job_dir / "video" / "edit_manifest.json")


def _edit_meta(job_dir: Path | None) -> dict[str, Any]:
    data = _edit_manifest(job_dir)
    if not data:
        return {}
    meta = data.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _publish_manifest(job_dir: Path | None) -> dict[str, Any] | None:
    if job_dir is None:
        return None
    return _load_json(job_dir / "publish_manifest.json")


def _final_path(job_dir: Path | None) -> Path | None:
    if job_dir is None:
        return None
    p = job_dir / "video" / "final.mp4"
    return p if p.is_file() else None


def _has_composed_final(job_dir: Path | None) -> bool:
    if job_dir is None:
        return False
    if _final_path(job_dir) is not None:
        return True
    em = _edit_manifest(job_dir)
    return bool(em and (em.get("scenes") or em.get("final_path") or em.get("meta")))


def _infographic_count(
    edit_meta: dict[str, Any],
    job_meta: dict[str, Any],
    *,
    burned_only: bool = False,
) -> int:
    sources = (edit_meta,) if burned_only else (edit_meta, job_meta)
    for src in sources:
        ovs = src.get("infographic_overlays")
        if isinstance(ovs, list) and ovs:
            return len(ovs)
        if isinstance(ovs, dict) and ovs:
            return len(ovs)
    return 0


def _overlays_planned(edit_meta: dict[str, Any], job_meta: dict[str, Any]) -> bool:
    for src in (edit_meta, job_meta):
        if src.get("compose_infographic_overlays") or src.get(
            "compose_infographic_overlays_enabled"
        ):
            return True
        if src.get("premium_overlays_v1") or src.get("compose_premium_overlays"):
            return True
        checklist = src.get("sop_checklist") or {}
        must = checklist.get("must") if isinstance(checklist, dict) else {}
        if isinstance(must, dict) and (
            must.get("infographic_overlays") or must.get("premium_overlays_v1")
        ):
            return True
    return False


def _premium_present(
    edit_meta: dict[str, Any],
    job_meta: dict[str, Any],
    *,
    burned_only: bool = False,
) -> bool:
    sources = (edit_meta,) if burned_only else (edit_meta, job_meta)
    for src in sources:
        if src.get("premium_overlays_v1"):
            return True
        plan = src.get("premium_plan")
        if isinstance(plan, dict) and plan:
            return True
    return False


def _hook_cold_open_ok(channel: str, job_meta: dict[str, Any]) -> tuple[bool, str]:
    checklist = job_meta.get("sop_checklist") or {}
    must = checklist.get("must") if isinstance(checklist, dict) else {}
    if isinstance(must, dict) and must.get("hook_cold_open"):
        # Stamp alone is not enough — prefer prompt file evidence.
        pass
    if job_meta.get("hook_cold_open") or job_meta.get("selected_pack_hook_cold_open"):
        return True, "job_meta"

    hist = _is_historian(channel)
    if hist:
        paths = [
            CONFIG_DIR / "prompts" / "napping_historian" / "hook_cold_open.txt",
            ROOT / "config" / "prompts" / "napping_historian" / "hook_cold_open.txt",
        ]
    else:
        paths = [
            CONFIG_DIR / "prompts" / "hook_cold_open.txt",
            ROOT / "config" / "prompts" / "hook_cold_open.txt",
        ]
    for p in paths:
        if p.is_file() and p.stat().st_size > 0:
            return True, str(p)
    return False, "missing_hook_cold_open_prompt"


_TITLE_PROMISE_STOPWORDS = frozenset(
    {
        "what",
        "when",
        "where",
        "which",
        "that",
        "this",
        "with",
        "from",
        "into",
        "about",
        "would",
        "could",
        "should",
        "their",
        "there",
        "these",
        "those",
        "have",
        "been",
        "were",
        "will",
        "your",
        "they",
        "them",
        "than",
        "then",
        "history",
        "documentary",
        "video",
        "episode",
        "tonight",
        "night",
        "secret",
        "still",
        "haunts",
        "became",
        "become",
        "ruled",
        "never",
        "after",
        "before",
        "under",
        "over",
        "into",
        "upon",
    }
)


def _title_promise_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z]{4,}", (title or "").lower())
    return {w for w in words if w not in _TITLE_PROMISE_STOPWORDS}


def _cold_open_spoken_corpus(job_dir: Path | None) -> tuple[str, str, dict[str, Any]]:
    """Return (title, cold_open_text, meta) from script.json / narration if present."""
    meta: dict[str, Any] = {"source": None, "scene_count": 0}
    if job_dir is None:
        return "", "", meta
    script_path = job_dir / "script" / "script.json"
    narr_path = job_dir / "script" / "narration.txt"
    title = ""
    cold = ""
    if script_path.is_file():
        raw = _load_json(script_path) or {}
        title = str(raw.get("title") or raw.get("topic") or "").strip()
        hook = str(raw.get("hook") or "").strip()
        scenes = raw.get("scenes") or []
        ch1: list[str] = []
        for sc in scenes:
            if not isinstance(sc, dict):
                continue
            cid = sc.get("chapter_id")
            text = str(sc.get("text") or sc.get("narration") or "").strip()
            if not text:
                continue
            if cid is None or int(cid or 0) == 1:
                ch1.append(text)
            if len(ch1) >= 10:
                break
        # If chapter_id missing, use first ~10 scenes as cold open.
        if not ch1:
            for sc in scenes[:10]:
                if not isinstance(sc, dict):
                    continue
                text = str(sc.get("text") or sc.get("narration") or "").strip()
                if text:
                    ch1.append(text)
        cold = " ".join([hook] + ch1).strip()
        meta["source"] = "script.json"
        meta["scene_count"] = len(ch1)
        meta["hook_len"] = len(hook)
        return title, cold, meta
    if narr_path.is_file():
        lines = [
            ln.strip()
            for ln in narr_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        cold = " ".join(lines[:12])
        meta["source"] = "narration.txt"
        meta["scene_count"] = min(12, len(lines))
        # Prefer youtube_meta title when narration-only.
        yt_title = job_dir / "youtube_meta" / "title.txt"
        if yt_title.is_file():
            title = yt_title.read_text(encoding="utf-8").strip().splitlines()[0]
        return title, cold, meta
    return "", "", meta


def _title_promise_in_cold_open(
    job_dir: Path | None,
    *,
    title_override: str = "",
) -> dict[str, Any]:
    """Soft audit: title content tokens should appear in cold-open speech.

    Advisory only — never hard-fails Gate R / SOP advance.
    """
    title, cold, src_meta = _cold_open_spoken_corpus(job_dir)
    if title_override:
        title = title_override
    tokens = _title_promise_tokens(title)
    out: dict[str, Any] = {
        "ok": None,
        "title": title[:160],
        "tokens": sorted(tokens),
        "matched": [],
        "missing": [],
        **src_meta,
    }
    if not title or not tokens:
        out["ok"] = None
        out["evidence"] = "no_title_tokens"
        return out
    if not cold:
        out["ok"] = False
        out["missing"] = sorted(tokens)
        out["evidence"] = "no_cold_open_text"
        return out
    cold_l = cold.lower()
    matched = sorted(t for t in tokens if t in cold_l)
    missing = sorted(t for t in tokens if t not in cold_l)
    # Soft pass: ≥2 title tokens OR ≥40% of tokens present.
    need = max(2, int(round(len(tokens) * 0.4)))
    ok = len(matched) >= min(need, len(tokens))
    out["ok"] = ok
    out["matched"] = matched
    out["missing"] = missing
    out["need"] = need
    out["evidence"] = (
        f"matched={len(matched)}/{len(tokens)} need>={min(need, len(tokens))}"
    )
    return out


def _packaging_path(job_dir: Path | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "youtube_meta_dir": False,
        "title": False,
        "description": False,
        "chapters_txt": False,
        "tags": False,
    }
    if job_dir is None:
        return out
    meta_dir = job_dir / "youtube_meta"
    out["youtube_meta_dir"] = meta_dir.is_dir()
    if not out["youtube_meta_dir"]:
        return out
    out["title"] = (meta_dir / "title.txt").is_file() or (
        meta_dir / "packaging.json"
    ).is_file()
    out["description"] = (meta_dir / "description.txt").is_file() or (
        meta_dir / "packaging.json"
    ).is_file()
    out["chapters_txt"] = (meta_dir / "chapters.txt").is_file()
    out["tags"] = (meta_dir / "tags.txt").is_file() or (
        meta_dir / "packaging.json"
    ).is_file()
    # packaging.json bundle
    pack = _load_json(meta_dir / "packaging.json")
    if pack:
        out["title"] = out["title"] or bool(pack.get("title"))
        out["description"] = out["description"] or bool(pack.get("description"))
        out["tags"] = out["tags"] or bool(pack.get("tags"))
    return out


def _pd_clippings_evidence(
    job_dir: Path | None, job_meta: dict[str, Any]
) -> dict[str, Any]:
    """Detect PD/open hero inserts (stills or clips).

    Real layout uses ``pd_clippings.json`` + ``images/pd_heroes/`` (curated fetch).
    Older probes looked only for ``images/pd`` dirs and missed true positives.
    """
    evidence: dict[str, Any] = {
        "present": False,
        "count": 0,
        "sources": [],
    }
    count = 0
    try:
        raw = job_meta.get("pd_clip_count")
        if raw is not None:
            count = max(count, int(raw))
    except (TypeError, ValueError):
        pass
    if job_meta.get("pd_clippings") or job_meta.get("pd_heroes"):
        evidence["sources"].append("job_meta")
    if job_dir is None:
        evidence["present"] = bool(evidence["sources"] or count > 0)
        evidence["count"] = count
        return evidence

    # Canonical curated-fetch artifact
    pd_json = job_dir / "pd_clippings.json"
    if pd_json.is_file():
        evidence["sources"].append("pd_clippings.json")
        try:
            payload = json.loads(pd_json.read_text(encoding="utf-8"))
            heroes = payload.get("heroes") if isinstance(payload, dict) else None
            if isinstance(heroes, list) and heroes:
                count = max(count, len(heroes))
            if isinstance(payload, dict):
                try:
                    count = max(count, int(payload.get("pd_clip_count") or 0))
                except (TypeError, ValueError):
                    pass
                try:
                    count = max(count, int(payload.get("pd_motion_count") or 0))
                except (TypeError, ValueError):
                    pass
                if payload.get("pd_motion_clips") or payload.get("pd_motion"):
                    evidence["sources"].append("pd_clippings.json:motion")
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            pass

    for rel in (
        "images/pd_heroes",
        "images/pd_heroes/fitted",
        "images/pd_motion",
        "images/pd_motion/fitted",
        "clips/pd",
        "assets/pd",
        "stills/pd",
        "images/pd",
        "pd_clippings",  # dir (legacy)
        "sidecars/pd_sources.json",
        "video/pd_inserts",
        "clips/pd",
    ):
        p = job_dir / rel
        if p.exists():
            evidence["sources"].append(rel)
            if p.is_dir():
                n_files = sum(
                    1
                    for f in p.rglob("*")
                    if f.is_file()
                    and f.suffix.lower()
                    in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mov"}
                )
                count = max(count, n_files)

    evidence["count"] = int(count)
    evidence["present"] = bool(evidence["sources"]) or count > 0
    return evidence


def _pd_clippings_present(job_dir: Path | None, job_meta: dict[str, Any]) -> bool:
    return bool(_pd_clippings_evidence(job_dir, job_meta).get("present"))


def _sfx_applied(
    edit_meta: dict[str, Any],
    job_meta: dict[str, Any],
    *,
    burned_only: bool = False,
) -> bool | None:
    """Return True/False when evidence exists; None if SFX not yet stamped."""
    sources = (edit_meta,) if burned_only else (edit_meta, job_meta)
    for src in sources:
        if "compose_sfx" in src:
            return bool(src.get("compose_sfx"))
        sfx_meta = src.get("compose_sfx_meta")
        if isinstance(sfx_meta, dict) and "compose_sfx" in sfx_meta:
            return bool(sfx_meta.get("compose_sfx"))
    return None


# Mandatory top-level meta keys for every Brand channel farm job (new-format SOP).
NEW_FORMAT_MUST_META_KEYS: tuple[str, ...] = (
    "selected_pack",
    "new_format",
    "premium_overlays_v1",
    "compose_infographic_overlays",
    "hook_cold_open",
    "hook_generator",
    "asset_fetcher",
    "gate_r_advisory",
)


def is_new_format_stamped(meta: dict[str, Any] | None) -> bool:
    """True when job meta carries the current every-video new-format stamps."""
    m = dict(meta or {})
    if not (m.get("selected_pack") and m.get("new_format")):
        return False
    must = (m.get("sop_checklist") or {}).get("must") or {}
    if not isinstance(must, dict) or not must:
        return False
    for key in NEW_FORMAT_MUST_META_KEYS:
        if not m.get(key) and not must.get(key):
            # gate_r_advisory may live only under must / top-level
            if key == "gate_r_advisory" and (
                m.get("gate_r_advisory") is True or must.get("gate_r_advisory") is True
            ):
                continue
            return False
    return True


def missing_new_format_stamp_keys(meta: dict[str, Any] | None) -> list[str]:
    """Return missing mandatory new-format stamp keys (empty = complete)."""
    m = dict(meta or {})
    must = (m.get("sop_checklist") or {}).get("must") or {}
    if not isinstance(must, dict):
        must = {}
    missing: list[str] = []
    for key in NEW_FORMAT_MUST_META_KEYS:
        if m.get(key) or must.get(key):
            continue
        missing.append(key)
    if not m.get("sop_checklist"):
        missing.append("sop_checklist")
    return missing


def stamp_new_format_sop_checklist(
    meta: dict[str, Any] | None,
    *,
    channel: str | None = None,
    stage: str = "farm_start",
) -> dict[str, Any]:
    """Stamp selected-pack / SOP checklist into job meta for EVERY Brand channel.

    Applies to all ``KNOWN_CHANNELS`` (napstorian + napping_historian). Channel
    split is tone/SFX/PD-motion only — both are ``new_format`` / ``selected_pack``.
    """
    out = dict(meta or {})
    ch = _normalize_channel(
        channel or out.get("channel") or out.get("sheet_tab") or out.get("youtube_channel")
    )
    now = _now_iso()
    historian = _is_historian(ch)

    # Resolve expected SFX without failing if settings import is awkward in tests.
    sfx_expected = False
    try:
        from src.services.compose_sfx import resolve_compose_sfx

        sfx_expected = bool(resolve_compose_sfx(ch))
    except Exception:  # noqa: BLE001
        sfx_expected = not historian

    # Napstorian: denser PD motion (10–15); historian: soft/slow pans (4–6).
    pd_motion_expected = True

    prompts_dir: str | None = None
    try:
        from src.agents.sheet_channels import channel_prompts_dir

        prompts_dir = channel_prompts_dir(ch)
    except Exception:  # noqa: BLE001
        prompts_dir = None
    if not prompts_dir and historian:
        prompts_dir = "config/prompts/napping_historian"
    elif not prompts_dir and ch == "napstorian":
        prompts_dir = "config/prompts"

    out["selected_pack"] = True
    out["new_format"] = True
    out["compose_infographic_overlays"] = True
    out["premium_overlays_v1"] = True
    out["compose_premium_overlays"] = True
    out.setdefault("new_format_at", now)
    out["compose_sfx_expected"] = sfx_expected
    out["hook_cold_open"] = True
    # Defaults match pipeline env: HOOK_GENERATOR=1 / ASSET_FETCHER=1 unless off.
    out["hook_generator"] = True
    out["asset_fetcher"] = True
    out["gate_r_advisory"] = True
    out["pd_motion_expected"] = pd_motion_expected
    out["channel_tone"] = "calm_sleep" if historian else "punchy_what_if"
    if ch:
        out["channel"] = ch
        out["prompts_channel"] = ch
    if prompts_dir:
        out["prompts_dir"] = prompts_dir

    out["sop_checklist"] = {
        "stamped_at": now,
        "stage": stage,
        "channel": ch,
        "doc": "output/ops/NEW_FORMAT_EVERY_VIDEO_SOP.md",
        "must": {
            "selected_pack": True,
            "new_format": True,
            "infographic_overlays": True,
            "premium_overlays_v1": True,
            "hook_cold_open": True,
            "hook_generator": True,
            "asset_fetcher": True,
            "gate_r_enforce": False,  # advisory/soft — GATE_R_ENFORCE=false
            "gate_r_advisory": True,
            "soft_packaging": True,
            "chapters_pin": True,
            "compose_sfx": sfx_expected,
            "historian_sfx_off": historian,
            "channel_prompts": bool(ch),
            "pd_motion": pd_motion_expected,
        },
        "should": {
            "pd_clippings": True,
        },
    }
    return out


def check_new_format_sop(
    job: Any,
    *,
    smm_cfg: dict[str, Any] | None = None,
    agents_cfg: dict[str, Any] | None = None,
    for_public: bool | None = None,
) -> dict[str, Any]:
    """Inspect a farm job against the every-video new-format SOP.

    Returns a digest with ``ok`` (no hard failures), ``failures``, ``warnings``,
    and proposal rows suitable for ``smm_quality_alerts.md`` (type
    ``sop_compliance``). Soft by default — caller decides whether to block.
    """
    cfg = agents_cfg if agents_cfg is not None else _agents_cfg()
    smm = dict(smm_cfg if smm_cfg is not None else (cfg.get("smm") or {}))
    pack = dict(smm.get("selected_pack") or {})
    selected_default = bool(smm.get("selected_pack_default", True))

    job_meta = _job_meta(job)
    channel = _job_channel(job)
    job_dir = _job_dir(job)
    edit_meta = _edit_meta(job_dir)
    pub = _publish_manifest(job_dir) or {}
    pub_meta = dict(pub.get("meta") or {}) if isinstance(pub.get("meta"), dict) else {}
    composed = _has_composed_final(job_dir)
    status = _job_status(job)
    if for_public is None:
        for_public = status == "public"

    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks: dict[str, Any] = {
        "channel": channel,
        "job_dir": str(job_dir) if job_dir else None,
        "composed_final": composed,
        "for_public": bool(for_public),
        "selected_pack_default": selected_default,
    }

    def _fail(code: str, msg: str, **extra: Any) -> None:
        failures.append({"code": code, "message": msg, "level": "hard", **extra})

    def _warn(code: str, msg: str, **extra: Any) -> None:
        warnings.append({"code": code, "message": msg, "level": "should", **extra})

    # --- stamps / selected pack (every Brand channel) ---
    has_pack = bool(
        job_meta.get("selected_pack")
        or job_meta.get("new_format")
        or edit_meta.get("selected_pack")
        or edit_meta.get("new_format")
        or (job_meta.get("sop_checklist") or {}).get("must")
    )
    full_stamp = is_new_format_stamped(job_meta)
    missing_stamp_keys = missing_new_format_stamp_keys(job_meta)
    checks["selected_pack_or_new_format"] = has_pack
    checks["full_new_format_stamp"] = full_stamp
    checks["missing_stamp_keys"] = missing_stamp_keys
    if selected_default and not has_pack:
        _fail(
            "missing_new_format_stamp",
            "expected selected_pack / new_format stamp (or sop_checklist) on job/meta",
        )
    elif selected_default and has_pack and missing_stamp_keys:
        # Older jobs stamped before hook_generator/asset_fetcher/gate_r_advisory —
        # soft warn; farm spawn / execute re-stamp refreshes them.
        _warn(
            "stale_new_format_stamp",
            "new-format stamp incomplete for latest SOP; missing="
            + ",".join(missing_stamp_keys),
        )
    if selected_default and channel and not job_meta.get("prompts_channel"):
        _warn(
            "missing_prompts_channel",
            "prompts_channel unset — channel twin prompts path not stamped",
        )

    # --- overlays (required when composed, or planned before compose) ---
    expect_info = bool(pack.get("infographic_overlays", True)) or selected_default
    expect_premium = bool(pack.get("premium_overlays_v1", True)) or selected_default
    # Composed finals: require burned evidence in edit_manifest (stamps alone ≠ burn).
    info_n = _infographic_count(edit_meta, job_meta, burned_only=composed)
    planned = _overlays_planned(edit_meta, job_meta)
    premium_ok = _premium_present(edit_meta, job_meta, burned_only=composed)
    checks["infographic_count"] = info_n
    checks["overlays_planned"] = planned
    checks["premium_overlays_v1"] = premium_ok

    if expect_info:
        if composed and info_n < 1 and not premium_ok:
            _fail(
                "missing_infographic_overlays",
                "composed final lacks infographic_overlays (expect Netflix-dense plates)",
                count=info_n,
            )
        elif not composed and not planned:
            _warn(
                "overlays_not_planned",
                "infographic/premium overlays not stamped/planned yet (pre-compose)",
            )

    if expect_premium and composed and not premium_ok:
        # Live-test heads may predate premium burn — hard fail for new-format expectation.
        _fail(
            "missing_premium_overlays_v1",
            "composed final missing premium_overlays_v1 evidence (recompose for new format)",
        )

    # --- hook cold open ---
    if bool(pack.get("hook_cold_open", True)) or selected_default:
        hook_ok, hook_ev = _hook_cold_open_ok(channel, job_meta)
        checks["hook_cold_open"] = {"ok": hook_ok, "evidence": hook_ev}
        if not hook_ok:
            _fail("missing_hook_cold_open", f"cold-open hook path missing: {hook_ev}")

    # --- soft title-promise audit (advisory; never hard-hold) ---
    title_prom = _title_promise_in_cold_open(job_dir)
    checks["title_promise_cold_open"] = title_prom
    if title_prom.get("ok") is False:
        _warn(
            "title_promise_weak_in_cold_open",
            "cold open may not fulfill title promise "
            f"({title_prom.get('evidence')}; missing={title_prom.get('missing')[:8]})",
        )
    gate_env = str(pack.get("gate_r_enforce_env") or "GATE_R_ENFORCE")
    # Prefer live .env over stale process env (long-lived farm PIDs).
    gate_env_on = gate_r_enforce_enabled(env_name=gate_env)
    gate_r_ok = pub.get("gate_r_ok")
    if gate_r_ok is None:
        gate_r_ok = pub_meta.get("gate_r_ok")
    if gate_r_ok is None:
        gate_r_ok = job_meta.get("gate_r_ok")
    checks["gate_r"] = {
        "env": gate_env,
        "env_on": gate_env_on,
        "ok": gate_r_ok,
    }
    if pub or job_meta.get("gate_r_ok") is not None:
        if gate_r_ok is False:
            # Hard fail only while GATE_R_ENFORCE is on; otherwise advisory.
            if gate_env_on:
                _fail(
                    "gate_r_failed",
                    "publish_manifest/job meta reports gate_r_ok=false",
                )
            else:
                _warn(
                    "gate_r_failed_advisory",
                    "gate_r_ok=false but GATE_R_ENFORCE off — soft only",
                )
        elif gate_r_ok is None and for_public:
            _warn("gate_r_meta_missing", "no Gate R result on public job")
    elif for_public and not gate_env_on:
        _warn(
            "gate_r_env_off",
            f"{gate_env} is not truthy — Gate R is advisory/soft (does not hold)",
        )

    # --- packaging / chapters / pin (public path) ---
    pack_path = _packaging_path(job_dir)
    checks["packaging"] = pack_path
    pin_id = job_meta.get("smm_pin_comment_id")
    chapters_at = job_meta.get("smm_chapters_ensured_at")
    checks["pin"] = {
        "comment_id": pin_id,
        "needs_reauth": bool(job_meta.get("smm_pin_needs_reauth")),
        "error": job_meta.get("smm_pin_error"),
    }
    checks["chapters"] = {
        "ensured_at": chapters_at,
        "chapters_txt": pack_path.get("chapters_txt"),
        "error": job_meta.get("smm_chapters_error"),
    }
    if for_public:
        if not (pack_path.get("title") and pack_path.get("description")):
            _fail(
                "missing_packaging",
                "public job missing youtube_meta title/description packaging",
            )
        if not (pack_path.get("chapters_txt") or chapters_at):
            _fail(
                "missing_chapters_path",
                "public job missing chapters.txt and smm_chapters_ensured_at",
            )
        if not pin_id and not job_meta.get("smm_pin_needs_reauth"):
            # Pin may still be in flight — warn loudly; hard if error stuck.
            if job_meta.get("smm_pin_error"):
                _fail(
                    "pin_failed",
                    f"pin comment failed: {str(job_meta.get('smm_pin_error'))[:160]}",
                )
            else:
                _warn(
                    "pin_pending",
                    "public job has no smm_pin_comment_id yet",
                )

    # --- SFX channel split ---
    sfx_expected = job_meta.get("compose_sfx_expected")
    if sfx_expected is None:
        try:
            from src.services.compose_sfx import resolve_compose_sfx

            sfx_expected = bool(resolve_compose_sfx(channel))
        except Exception:  # noqa: BLE001
            sfx_expected = not _is_historian(channel)
    sfx_applied = _sfx_applied(edit_meta, job_meta, burned_only=composed)
    checks["compose_sfx"] = {
        "expected": bool(sfx_expected),
        "applied": sfx_applied,
        "historian": _is_historian(channel),
    }
    if _is_historian(channel):
        if sfx_applied is True:
            _fail(
                "historian_sfx_on",
                "napping_historian must keep cinematic SFX off (compose_sfx=true in manifest)",
            )
    elif composed and sfx_expected:
        if sfx_applied is False:
            _fail(
                "napstorian_sfx_missing",
                "napstorian composed final missing compose_sfx (cinematic SFX MUST)",
            )
        elif sfx_applied is None:
            # Flag landed in settings but not yet stamped on older finals.
            _warn(
                "napstorian_sfx_unspecified",
                "napstorian final has no compose_sfx stamp yet — recompose to burn SFX",
            )

    # --- PD clippings = SHOULD only ---
    pd_ev = _pd_clippings_evidence(job_dir, job_meta)
    pd_ok = bool(pd_ev.get("present"))
    checks["pd_clippings"] = pd_ok
    checks["pd_clippings_evidence"] = pd_ev
    if composed and not pd_ok:
        _warn(
            "pd_clippings_missing",
            "no PD/open hero clippings noted (Wiki+Met every scene; vision REJECT/gaps → Flux)",
        )

    ok = len(failures) == 0
    proposals = _sop_proposals(failures, warnings)
    return {
        "ok": ok,
        "soft": True,  # caller may flip via config; check itself never blocks
        "channel": channel,
        "failures": failures,
        "warnings": warnings,
        "checks": checks,
        "proposals": proposals,
        "fingerprint": _fingerprint(failures, warnings),
        "n_failures": len(failures),
        "n_warnings": len(warnings),
    }


def _fingerprint(
    failures: list[dict[str, Any]], warnings: list[dict[str, Any]]
) -> str:
    codes = [f"F:{f.get('code')}" for f in failures] + [
        f"W:{w.get('code')}" for w in warnings
    ]
    return "|".join(sorted(codes)) if codes else "clean"


def _sop_proposals(
    failures: list[dict[str, Any]], warnings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    props: list[dict[str, Any]] = []
    for item in failures:
        props.append(
            {
                "level": "high",
                "type": SOP_ALERT_TYPE,
                "change": f"[MUST] {item.get('code')}: {item.get('message')}",
                "code": item.get("code"),
                "auto_apply": False,
                "severity": "hard",
            }
        )
    for item in warnings:
        props.append(
            {
                "level": "medium",
                "type": SOP_ALERT_TYPE,
                "change": f"[SHOULD] {item.get('code')}: {item.get('message')}",
                "code": item.get("code"),
                "auto_apply": False,
                "severity": "should",
            }
        )
    return props


def sop_block_publish_enabled(smm_cfg: dict[str, Any] | None = None) -> bool:
    smm = smm_cfg if smm_cfg is not None else dict((_agents_cfg().get("smm") or {}))
    return bool(smm.get("sop_block_publish", False))


def enforce_new_format_sop_enabled(smm_cfg: dict[str, Any] | None = None) -> bool:
    smm = smm_cfg if smm_cfg is not None else dict((_agents_cfg().get("smm") or {}))
    return bool(smm.get("enforce_new_format_sop", True))


def sop_stage_gates_enabled(smm_cfg: dict[str, Any] | None = None) -> bool:
    """When true, run per-stage SOP audits + write remediation meta (default on)."""
    smm = smm_cfg if smm_cfg is not None else dict((_agents_cfg().get("smm") or {}))
    return bool(smm.get("sop_stage_gates", True))


def sop_stage_gates_hard_enabled(smm_cfg: dict[str, Any] | None = None) -> bool:
    """When true, hard-block stage advance on MUST failures (default off — safe for DISARMED farm)."""
    smm = smm_cfg if smm_cfg is not None else dict((_agents_cfg().get("smm") or {}))
    return bool(smm.get("sop_stage_gates_hard", False))


def normalize_sop_stage(stage: str | None) -> str:
    raw = str(stage or "").strip().lower()
    if raw in STAGE_SOP_ITEMS:
        return raw
    return PIPELINE_TO_SOP_STAGE.get(raw, raw)


def _script_present(job_dir: Path | None) -> bool:
    if job_dir is None:
        return False
    return (job_dir / "script" / "script.json").is_file()


def _voice_present(job_dir: Path | None) -> bool:
    if job_dir is None:
        return False
    return (job_dir / "audio" / "voice_manifest.json").is_file()


def _visual_present(job_dir: Path | None) -> bool:
    if job_dir is None:
        return False
    return (job_dir / "images" / "visual_manifest.json").is_file()


def _visual_backend(job_dir: Path | None) -> str | None:
    if job_dir is None:
        return None
    data = _load_json(job_dir / "images" / "visual_manifest.json") or {}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    backend = data.get("backend") or meta.get("backend") or meta.get("image_backend")
    return str(backend) if backend else None


def _edit_density(edit_meta: dict[str, Any]) -> dict[str, Any]:
    """Rough Netflix density signals from edit_manifest meta."""
    ovs = edit_meta.get("infographic_overlays") or []
    n = len(ovs) if isinstance(ovs, list) else (len(ovs) if isinstance(ovs, dict) else 0)
    duration = edit_meta.get("duration_s") or edit_meta.get("total_duration_s")
    try:
        duration_f = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_f = None
    interval = None
    if duration_f and n > 0:
        interval = duration_f / max(n, 1)
    return {
        "infographic_count": n,
        "duration_s": duration_f,
        "approx_interval_s": interval,
        "date_overlay": bool(edit_meta.get("compose_date_overlay_enabled")),
        "loudness": bool(edit_meta.get("loudness_normalize")),
        "ken_burns": edit_meta.get("ken_burns") is not False,
    }


def check_stage_sop(
    job: Any,
    stage: str,
    *,
    smm_cfg: dict[str, Any] | None = None,
    agents_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate SOP points for a *completed* stage. Pass → advance; fail → resend that stage only.

    Does not block Live encode mid-stream even when hard flags are on — callers
    must skip hard-raise for ``live``.
    """
    cfg = agents_cfg if agents_cfg is not None else _agents_cfg()
    smm = dict(smm_cfg if smm_cfg is not None else (cfg.get("smm") or {}))
    stage_n = normalize_sop_stage(stage)
    stage_info = STAGE_SOP_ITEMS.get(stage_n) or {
        "must": [],
        "should": [],
        "resend": stage_n,
        "notes": "unknown stage",
    }

    job_meta = _job_meta(job)
    channel = _job_channel(job)
    job_dir = _job_dir(job)
    edit_meta = _edit_meta(job_dir)
    pub = _publish_manifest(job_dir) or {}
    pub_meta = dict(pub.get("meta") or {}) if isinstance(pub.get("meta"), dict) else {}
    composed = _has_composed_final(job_dir)
    pack_path = _packaging_path(job_dir)
    density = _edit_density(edit_meta) if edit_meta else {}

    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks: dict[str, Any] = {
        "stage": stage_n,
        "channel": channel,
        "job_dir": str(job_dir) if job_dir else None,
    }

    def _fail(code: str, msg: str, **extra: Any) -> None:
        failures.append({"code": code, "message": msg, "level": "hard", **extra})

    def _warn(code: str, msg: str, **extra: Any) -> None:
        warnings.append({"code": code, "message": msg, "level": "should", **extra})

    # --- stage-specific MUST / SHOULD ---
    if stage_n == "idea":
        has_pack = bool(
            job_meta.get("selected_pack")
            or job_meta.get("new_format")
            or (job_meta.get("sop_checklist") or {}).get("must")
        )
        full_stamp = is_new_format_stamped(job_meta)
        checks["selected_pack"] = has_pack
        checks["full_new_format_stamp"] = full_stamp
        if bool(smm.get("selected_pack_default", True)) and not has_pack:
            _fail(
                "missing_new_format_stamp",
                "idea/farm start missing selected_pack / new_format / sop_checklist",
            )
        elif bool(smm.get("selected_pack_default", True)) and not full_stamp:
            missing = missing_new_format_stamp_keys(job_meta)
            _fail(
                "incomplete_new_format_stamp",
                "idea/enqueue missing latest SOP stamps: " + ",".join(missing),
            )

    elif stage_n == "script":
        script_ok = _script_present(job_dir)
        checks["script_json"] = script_ok
        if not script_ok:
            _fail("missing_script", "script/script.json missing after scripting stage")
        hook_ok, hook_ev = _hook_cold_open_ok(channel, job_meta)
        checks["hook_cold_open"] = {"ok": hook_ok, "evidence": hook_ev}
        if not hook_ok:
            _fail("missing_hook_cold_open", f"cold-open hook path missing: {hook_ev}")
        title_prom = _title_promise_in_cold_open(job_dir)
        checks["title_promise_cold_open"] = title_prom
        if title_prom.get("ok") is False:
            _warn(
                "title_promise_weak_in_cold_open",
                "cold open may not fulfill title promise "
                f"({title_prom.get('evidence')}; missing={title_prom.get('missing')[:8]})",
            )
        has_pack = bool(
            job_meta.get("selected_pack")
            or job_meta.get("new_format")
            or (job_meta.get("sop_checklist") or {}).get("must")
        )
        if bool(smm.get("selected_pack_default", True)) and not has_pack:
            _fail(
                "missing_new_format_stamp",
                "script stage missing selected_pack / new_format stamp",
            )
        elif bool(smm.get("selected_pack_default", True)) and not is_new_format_stamped(
            job_meta
        ):
            _warn(
                "stale_new_format_stamp",
                "script stage stamp incomplete: "
                + ",".join(missing_new_format_stamp_keys(job_meta)),
            )

    elif stage_n == "tts":
        voice_ok = _voice_present(job_dir)
        checks["voice_manifest"] = voice_ok
        if not voice_ok:
            _fail("missing_voice_manifest", "audio/voice_manifest.json missing after TTS")

    elif stage_n == "stills":
        vis_ok = _visual_present(job_dir)
        checks["visual_manifest"] = vis_ok
        if not vis_ok:
            _fail(
                "missing_visual_manifest",
                "images/visual_manifest.json missing after stills stage",
            )
        backend = _visual_backend(job_dir)
        checks["visual_backend"] = backend
        if backend and str(backend).lower() in {"mock", "placeholder"}:
            _warn(
                "flux_backend_mock",
                f"stills backend={backend} (Flux cinematic expected for production)",
            )
        pd_ev = _pd_clippings_evidence(job_dir, job_meta)
        pd_ok = bool(pd_ev.get("present"))
        checks["pd_clippings"] = pd_ok
        checks["pd_clippings_evidence"] = pd_ev
        if not pd_ok:
            _warn(
                "pd_clippings_missing",
                "no PD/open hero clippings (Wiki+Met every scene; vision REJECT/gaps → Flux)",
            )

    elif stage_n == "compose":
        if not composed:
            _fail("missing_composed_final", "compose stage lacks final.mp4 / edit_manifest")
        else:
            # Reuse full compose-relevant slice of check_new_format_sop
            full = check_new_format_sop(
                job, smm_cfg=smm, agents_cfg=cfg, for_public=False
            )
            # Keep only compose-scoped failure codes
            compose_codes = {
                "missing_new_format_stamp",
                "missing_infographic_overlays",
                "missing_premium_overlays_v1",
                "historian_sfx_on",
                "napstorian_sfx_missing",
            }
            for f in full.get("failures") or []:
                if f.get("code") in compose_codes:
                    failures.append(dict(f))
            for w in full.get("warnings") or []:
                code = str(w.get("code") or "")
                if code in {
                    "pd_clippings_missing",
                    "napstorian_sfx_unspecified",
                    "overlays_not_planned",
                }:
                    warnings.append(dict(w))
            checks.update(full.get("checks") or {})
            checks["density"] = density
            # Soft density: Netflix ~60–90s plates when duration known
            interval = density.get("approx_interval_s")
            n_info = int(density.get("infographic_count") or 0)
            dur = density.get("duration_s")
            if dur and float(dur) >= 180 and n_info > 0 and interval and interval > 120:
                _warn(
                    "infographic_density_sparse",
                    f"~{interval:.0f}s between plates (target ~60–90s Netflix density)",
                    count=n_info,
                    duration_s=dur,
                )
            if not density.get("date_overlay"):
                _warn(
                    "date_overlay_unspecified",
                    "compose_date_overlay_enabled not stamped on edit_manifest",
                )
            if not density.get("loudness"):
                _warn(
                    "loudness_unspecified",
                    "loudness_normalize not stamped on edit_manifest",
                )

    elif stage_n == "package":
        checks["packaging"] = pack_path
        if not (pack_path.get("title") and pack_path.get("description")):
            _fail(
                "missing_packaging",
                "youtube_meta title/description missing after package stage",
            )
        if not pack_path.get("chapters_txt"):
            _warn(
                "missing_chapters_txt",
                "chapters.txt missing (needed for public pin/chapters path)",
            )
        if not pack_path.get("tags"):
            _warn("missing_tags", "tags.txt / packaging.json tags missing")

    elif stage_n == "publish":
        checks["publish_manifest"] = bool(pub)
        if not pub and not job_meta.get("publish_dry_run"):
            # Dry-run / no-upload jobs may skip — warn only unless for_public path
            status = _job_status(job)
            if status in {"private", "public"}:
                _fail("missing_publish_manifest", "publish_manifest.json missing")
            else:
                _warn(
                    "publish_manifest_absent",
                    "no publish_manifest yet (dry-run / no-publish OK)",
                )
        gate_env = str(
            (smm.get("selected_pack") or {}).get("gate_r_enforce_env") or "GATE_R_ENFORCE"
        )
        gate_env_on = gate_r_enforce_enabled(env_name=gate_env)
        gate_r_ok = pub.get("gate_r_ok")
        if gate_r_ok is None:
            gate_r_ok = pub_meta.get("gate_r_ok")
        if gate_r_ok is None:
            gate_r_ok = job_meta.get("gate_r_ok")
        checks["gate_r"] = {"env_on": gate_env_on, "ok": gate_r_ok}
        if gate_r_ok is False:
            if gate_env_on:
                _fail("gate_r_failed", "gate_r_ok=false — do not advance to public")
            else:
                _warn(
                    "gate_r_failed_advisory",
                    "gate_r_ok=false but GATE_R_ENFORCE off — soft only",
                )
        elif gate_env_on and gate_r_ok is None and pub:
            _warn("gate_r_meta_missing", "Gate R env on but no gate_r_ok stamp")

    elif stage_n == "live":
        # Soft-only: pin/chapters; never implies hard Live encode stop
        pin_id = job_meta.get("smm_pin_comment_id")
        chapters_at = job_meta.get("smm_chapters_ensured_at")
        checks["pin"] = pin_id
        checks["chapters_ensured_at"] = chapters_at
        if _job_status(job) == "public":
            if not (pack_path.get("chapters_txt") or chapters_at):
                _fail(
                    "missing_chapters_path",
                    "public live missing chapters.txt / smm_chapters_ensured_at",
                )
            if not pin_id and not job_meta.get("smm_pin_needs_reauth"):
                if job_meta.get("smm_pin_error"):
                    _fail(
                        "pin_failed",
                        f"pin comment failed: {str(job_meta.get('smm_pin_error'))[:160]}",
                    )
                else:
                    _warn("pin_pending", "public job has no smm_pin_comment_id yet")

    else:
        _warn("unknown_sop_stage", f"no STAGE_SOP_ITEMS map for stage={stage_n!r}")

    ok = len(failures) == 0
    resend = str(stage_info.get("resend") or stage_n)
    remediation = {
        "action": "advance" if ok else "resend_stage",
        "resend_stage": None if ok else resend,
        "pipeline_stage": stage_info.get("pipeline"),
        "message": (
            f"SOP stage {stage_n} OK — continue"
            if ok
            else f"SOP stage {stage_n} FAIL — resend only `{resend}` (not full pipeline)"
        ),
        "must_items": list(stage_info.get("must") or []),
        "should_items": list(stage_info.get("should") or []),
    }
    proposals = _sop_proposals(failures, warnings)
    for p in proposals:
        p["type"] = SOP_STAGE_GATE_TYPE
        p["stage"] = stage_n
        p["resend_stage"] = remediation["resend_stage"]

    return {
        "ok": ok,
        "stage": stage_n,
        "soft": not sop_stage_gates_hard_enabled(smm),
        "channel": channel,
        "failures": failures,
        "warnings": warnings,
        "checks": checks,
        "remediation": remediation,
        "proposals": proposals,
        "fingerprint": _fingerprint(failures, warnings),
        "n_failures": len(failures),
        "n_warnings": len(warnings),
        "never_block_live_encode": stage_n == "live",
    }


def gate_stage_advance(
    job: Any,
    *,
    completed_stage: str,
    next_stage: str | None = None,
    smm_cfg: dict[str, Any] | None = None,
    agents_cfg: dict[str, Any] | None = None,
    hard: bool | None = None,
    write_compliance: bool = True,
) -> dict[str, Any]:
    """Gate advancing to ``next_stage`` by verifying ``completed_stage`` product.

    Soft by default (``sop_stage_gates_hard=false``): always returns digest +
    stamps meta/compliance. Hard mode raises ``SopStageGateError`` on MUST fail
    (except ``live`` — never hard-blocks Live encode).
    """
    cfg = agents_cfg if agents_cfg is not None else _agents_cfg()
    smm = dict(smm_cfg if smm_cfg is not None else (cfg.get("smm") or {}))
    if not sop_stage_gates_enabled(smm) and not enforce_new_format_sop_enabled(smm):
        return {"skipped": True, "reason": "sop_stage_gates=false"}

    digest = check_stage_sop(
        job, completed_stage, smm_cfg=smm, agents_cfg=cfg
    )
    digest["next_stage"] = normalize_sop_stage(next_stage) if next_stage else None
    hard_on = sop_stage_gates_hard_enabled(smm) if hard is None else bool(hard)
    # Compose→package/publish is the primary hard path when flag on.
    digest["hard"] = hard_on
    digest["blocked"] = bool(
        hard_on
        and not digest.get("ok")
        and not digest.get("never_block_live_encode")
    )

    if write_compliance:
        try:
            path = write_sop_compliance_md(
                job,
                stage_digest=digest,
                agents_cfg=cfg,
                smm_cfg=smm,
            )
            digest["compliance_path"] = str(path) if path else None
        except Exception as exc:  # noqa: BLE001
            logger.info("sop compliance write failed: %s", exc)
            digest["compliance_write_error"] = str(exc)

    if digest.get("blocked"):
        rem = digest.get("remediation") or {}
        raise SopStageGateError(
            rem.get("message")
            or f"SOP stage gate blocked ({completed_stage})",
            digest=digest,
        )
    return digest


def maybe_gate_pipeline_stage(
    job: Any,
    *,
    entering_stage: str,
    smm_cfg: dict[str, Any] | None = None,
    agents_cfg: dict[str, Any] | None = None,
    hard: bool | None = None,
) -> dict[str, Any]:
    """When pipeline enters ``entering_stage``, verify the prerequisite completed stage."""
    smm = smm_cfg if smm_cfg is not None else dict((_agents_cfg().get("smm") or {}))
    if not sop_stage_gates_enabled(smm):
        return {"skipped": True, "reason": "sop_stage_gates=false"}

    entering = str(entering_stage or "").strip().lower()
    required = ENTERING_REQUIRES_COMPLETED.get(entering)
    if not required:
        return {
            "skipped": True,
            "reason": f"no gate for entering={entering}",
            "entering_stage": entering,
        }
    return gate_stage_advance(
        job,
        completed_stage=required,
        next_stage=normalize_sop_stage(entering),
        smm_cfg=smm,
        agents_cfg=agents_cfg,
        hard=hard,
    )


def audit_stage_complete(
    job: Any,
    *,
    completed_stage: str,
    smm_cfg: dict[str, Any] | None = None,
    agents_cfg: dict[str, Any] | None = None,
    hard: bool | None = None,
) -> dict[str, Any]:
    """End-of-stage audit: verify product, write compliance, return remediation."""
    smm = smm_cfg if smm_cfg is not None else dict((_agents_cfg().get("smm") or {}))
    if not sop_stage_gates_enabled(smm) and not enforce_new_format_sop_enabled(smm):
        return {"skipped": True, "reason": "sop_stage_gates=false"}
    return gate_stage_advance(
        job,
        completed_stage=completed_stage,
        next_stage=None,
        smm_cfg=smm,
        agents_cfg=agents_cfg,
        hard=hard if hard is not None else False,  # audit path soft unless caller forces
        write_compliance=True,
    )


def job_from_dir(
    job_dir: Path | str,
    *,
    channel: str | None = None,
    meta: dict[str, Any] | None = None,
    status: str = "",
    job_id: str | None = None,
) -> dict[str, Any]:
    """Build a minimal job dict for pipeline hooks (no OpsStore required)."""
    jd = Path(job_dir)
    m = dict(meta or {})
    if channel:
        m.setdefault("channel", channel)
    m.setdefault("job_dir", str(jd))
    return {
        "id": job_id or jd.name,
        "job_dir": str(jd),
        "status": status,
        "meta": m,
        "title": m.get("title") or jd.name,
    }


def _used_label(ok: bool | None, *, partial: bool = False) -> str:
    if partial:
        return "partial"
    if ok is True:
        return "yes"
    if ok is False:
        return "no"
    return "partial"


def build_sop_compliance_rows(
    job: Any,
    *,
    stage_digest: dict[str, Any] | None = None,
    smm_cfg: dict[str, Any] | None = None,
    agents_cfg: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build Used?/Reasoning rows for MUST #16 compliance table."""
    full = check_new_format_sop(
        job,
        smm_cfg=smm_cfg,
        agents_cfg=agents_cfg,
        for_public=_job_status(job) == "public",
    )
    checks = dict(full.get("checks") or {})
    fail_codes = {f.get("code") for f in (full.get("failures") or [])}
    warn_codes = {w.get("code") for w in (full.get("warnings") or [])}
    job_meta = _job_meta(job)
    job_dir = _job_dir(job)
    edit_meta = _edit_meta(job_dir)
    channel = _job_channel(job)
    density = _edit_density(edit_meta) if edit_meta else {}
    pack = checks.get("packaging") or _packaging_path(job_dir)
    composed = bool(checks.get("composed_final"))

    def row(item: str, used: str, reasoning: str) -> dict[str, str]:
        return {"item": item, "used": used, "reasoning": reasoning}

    rows: list[dict[str, str]] = []
    hook = checks.get("hook_cold_open") or {}
    rows.append(
        row(
            "Cold-open hook rules",
            _used_label(bool(hook.get("ok")) if hook else None),
            f"evidence={hook.get('evidence')}" if hook else "not checked",
        )
    )
    tp = checks.get("title_promise_cold_open")
    if not isinstance(tp, dict):
        tp = _title_promise_in_cold_open(job_dir)
    tp_ok = tp.get("ok")
    rows.append(
        row(
            "Title promise in cold open (soft)",
            (
                "yes"
                if tp_ok is True
                else ("no" if tp_ok is False else "partial")
            ),
            (
                f"{tp.get('evidence')}; matched={tp.get('matched')}; "
                f"missing={tp.get('missing')}"
                if tp
                else "not checked"
            ),
        )
    )
    gate = checks.get("gate_r") or {}
    g_ok = gate.get("ok")
    rows.append(
        row(
            "Gate R (advisory unless GATE_R_ENFORCE)",
            (
                "yes"
                if g_ok is True
                else ("no" if g_ok is False else ("off" if not gate.get("env_on") else "partial"))
            ),
            f"env_on={gate.get('env_on')} ok={g_ok} (soft when env off)",
        )
    )
    backend = _visual_backend(job_dir)
    rows.append(
        row(
            "Flux cinematic stills",
            "yes"
            if backend and "flux" in str(backend).lower()
            else ("partial" if _visual_present(job_dir) else "no"),
            f"backend={backend or 'unknown'}; visual_manifest={_visual_present(job_dir)}",
        )
    )
    rows.append(
        row(
            "Kokoro narration",
            _used_label(_voice_present(job_dir)),
            "voice_manifest present" if _voice_present(job_dir) else "missing voice_manifest",
        )
    )
    rows.append(
        row(
            "Ken Burns on stills",
            "yes" if composed else "no",
            "assumed on compose path when final exists"
            if composed
            else "no composed final yet",
        )
    )
    info_n = int(checks.get("infographic_count") or density.get("infographic_count") or 0)
    rows.append(
        row(
            "Infographic overlays throughout",
            "yes" if info_n >= 1 else ("no" if composed else "partial"),
            f"count={info_n}; density_interval={density.get('approx_interval_s')}",
        )
    )
    prem = bool(checks.get("premium_overlays_v1"))
    rows.append(
        row(
            "Premium overlays v1",
            "yes" if prem else ("no" if composed else "partial"),
            "edit_manifest premium_overlays_v1 / premium_plan",
        )
    )
    rows.append(
        row(
            "Date overlay on long holds",
            "yes" if density.get("date_overlay") else "partial",
            f"compose_date_overlay_enabled={density.get('date_overlay')}",
        )
    )
    rows.append(
        row(
            "Loudness normalize",
            "yes" if density.get("loudness") else "partial",
            f"loudness_normalize={density.get('loudness')}",
        )
    )
    pack_ok = bool(pack.get("title") and pack.get("description"))
    pin_ok = bool(job_meta.get("smm_pin_comment_id"))
    ch_ok = bool(pack.get("chapters_txt") or job_meta.get("smm_chapters_ensured_at"))
    rows.append(
        row(
            "Soft packaging + chapters + pin",
            "yes" if (pack_ok and ch_ok and (pin_ok or _job_status(job) != "public")) else (
                "partial" if pack_ok or ch_ok else "no"
            ),
            f"title/desc={pack_ok} chapters={ch_ok} pin={pin_ok}",
        )
    )
    rows.append(
        row(
            "Winner / evergreen bias",
            "partial",
            "harvest/SMM path — not always stamped per job",
        )
    )
    rows.append(
        row(
            "Dual-channel tone",
            "yes" if channel else "partial",
            f"channel={channel or 'unset'}",
        )
    )
    rows.append(
        row(
            "Image stills reuse packs",
            "partial",
            "side track — only when SMM emit_image_reuse_packs runs",
        )
    )
    has_pack = bool(
        job_meta.get("selected_pack")
        or job_meta.get("new_format")
        or edit_meta.get("selected_pack")
        or edit_meta.get("new_format")
    )
    rows.append(
        row(
            "`selected_pack` / `new_format` stamps",
            _used_label(has_pack),
            "job/edit meta stamps",
        )
    )
    sfx = checks.get("compose_sfx") or {}
    if _is_historian(channel):
        sfx_used = "yes" if sfx.get("applied") is not True else "no"
        sfx_reason = "historian SFX must stay off"
    else:
        sfx_used = (
            "yes"
            if sfx.get("applied") is True
            else ("no" if sfx.get("applied") is False else "partial")
        )
        sfx_reason = f"expected={sfx.get('expected')} applied={sfx.get('applied')}"
    rows.append(row("Cinematic SFX throughout (napstorian)", sfx_used, sfx_reason))
    rows.append(
        row(
            "Post-produce compliance table",
            "yes",
            "this file",
        )
    )
    pd_ev = checks.get("pd_clippings_evidence")
    if not isinstance(pd_ev, dict):
        pd_ev = _pd_clippings_evidence(job_dir, job_meta)
    pd = checks.get("pd_clippings")
    if pd is None:
        pd = bool(pd_ev.get("present"))
    if pd:
        pd_reason = (
            f"count={pd_ev.get('count') or '?'}; "
            f"sources={','.join(pd_ev.get('sources') or []) or 'meta'}"
        )
    else:
        pd_reason = (
            "Wiki+Met every scene (no hero product cap); vision REJECT/gaps → Flux "
            "(no pd_clippings.json / images/pd_heroes)"
        )
    rows.append(
        row(
            "PD / open clippings (SHOULD)",
            "yes" if pd else "no",
            pd_reason,
        )
    )

    if stage_digest:
        rem = stage_digest.get("remediation") or {}
        rows.append(
            row(
                f"Stage gate ({stage_digest.get('stage')})",
                "yes" if stage_digest.get("ok") else "no",
                str(rem.get("message") or stage_digest.get("fingerprint") or ""),
            )
        )

    # Surface outstanding machine fail codes briefly
    if fail_codes:
        rows.append(
            row(
                "Machine SOP failures",
                "no",
                ", ".join(sorted(str(c) for c in fail_codes if c)),
            )
        )
    if warn_codes:
        rows.append(
            row(
                "Machine SOP warnings",
                "partial",
                ", ".join(sorted(str(c) for c in warn_codes if c)),
            )
        )
    return rows


def write_sop_compliance_md(
    job: Any,
    *,
    stage_digest: dict[str, Any] | None = None,
    agents_cfg: dict[str, Any] | None = None,
    smm_cfg: dict[str, Any] | None = None,
    path: Path | None = None,
    force: bool = False,
) -> Path | None:
    """Write/update job-local ``ops/SOP_COMPLIANCE.md`` (MUST #16).

    If a human-authored table already exists (contains ``Produced:`` / rich
    reasoning header) and ``force`` is false, append a stage-gate footer instead
    of clobbering the hand table.
    """
    job_dir = _job_dir(job)
    if job_dir is None:
        return None
    ops = job_dir / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    out = path or (ops / "SOP_COMPLIANCE.md")

    # Preserve hand-written compliance tables from Live/compose tests.
    if out.is_file() and not force:
        existing = out.read_text(encoding="utf-8")
        if "**Produced:**" in existing or "Density evidence" in existing:
            if stage_digest:
                rem = stage_digest.get("remediation") or {}
                footer = (
                    "\n\n---\n\n"
                    f"### Machine stage gate ({_now_iso()})\n\n"
                    f"- stage: `{stage_digest.get('stage')}`\n"
                    f"- result: {'PASS' if stage_digest.get('ok') else 'FAIL'}\n"
                    f"- message: {rem.get('message')}\n"
                    f"- resend_stage: `{rem.get('resend_stage')}`\n"
                    f"- fingerprint: `{stage_digest.get('fingerprint')}`\n"
                )
                if "### Machine stage gate" not in existing:
                    out.write_text(existing.rstrip() + footer, encoding="utf-8")
                else:
                    # Replace last machine footer block loosely
                    head, _, _ = existing.partition("\n### Machine stage gate")
                    out.write_text(head.rstrip() + footer, encoding="utf-8")
            return out

    rows = build_sop_compliance_rows(
        job,
        stage_digest=stage_digest,
        smm_cfg=smm_cfg,
        agents_cfg=agents_cfg,
    )
    job_meta = _job_meta(job)
    channel = _job_channel(job)
    job_id = getattr(job, "id", None) or (
        job.get("id") if isinstance(job, dict) else None
    ) or job_dir.name
    title = getattr(job, "title", None) or (
        job.get("title") if isinstance(job, dict) else None
    ) or job_meta.get("title") or job_dir.name

    stage_line = ""
    if stage_digest:
        rem = stage_digest.get("remediation") or {}
        stage_line = (
            f"**Last stage gate:** `{stage_digest.get('stage')}` → "
            f"{'PASS' if stage_digest.get('ok') else 'FAIL'} — "
            f"{rem.get('message')}\n"
        )

    lines = [
        f"# SOP compliance — {job_id}",
        "",
        f"**Job:** `{job_id}`  ",
        f"**Title:** {title}  ",
        f"**Channel:** `{channel or 'unknown'}`  ",
        f"**Updated:** {_now_iso()}  ",
        f"**Doc:** `output/ops/NEW_FORMAT_EVERY_VIDEO_SOP.md`",
        "",
        stage_line.rstrip(),
        "",
        "| SOP item | Used? | Reasoning |",
        "|----------|-------|-----------|",
    ]
    for r in rows:
        item = str(r["item"]).replace("|", "\\|")
        used = str(r["used"]).replace("|", "\\|")
        reason = str(r["reasoning"]).replace("|", "\\|")
        lines.append(f"| {item} | {used} | {reason} |")

    lines.extend(
        [
            "",
            "## Stage map (gated)",
            "",
            "| Stage | Pipeline | Resend on fail | MUST focus |",
            "|-------|----------|----------------|------------|",
        ]
    )
    for st in SOP_STAGE_ORDER:
        info = STAGE_SOP_ITEMS[st]
        must = ", ".join(info.get("must") or []) or "—"
        lines.append(
            f"| `{st}` | `{info.get('pipeline')}` | `{info.get('resend')}` | {must} |"
        )

    lines.extend(
        [
            "",
            "## Remediation rule",
            "",
            "If a stage gate **fails**: resend **only that stage** "
            "(`remediation.resend_stage`) — do not restart the whole pipeline.",
            "Hard block requires `smm.sop_stage_gates_hard=true` "
            "(default soft audit). Live encode is never mid-stream blocked.",
            "",
        ]
    )
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def stamp_stage_gate_meta(
    meta: dict[str, Any] | None,
    digest: dict[str, Any],
) -> dict[str, Any]:
    """Stamp last stage-gate result onto job meta for farm/SMM / CEO heal."""
    out = dict(meta or {})
    if digest.get("skipped"):
        return out
    out["smm_sop_stage_checked_at"] = _now_iso()
    out["smm_sop_stage"] = digest.get("stage")
    out["smm_sop_stage_ok"] = bool(digest.get("ok"))
    out["smm_sop_stage_fp"] = digest.get("fingerprint")
    rem = digest.get("remediation") or {}
    if rem.get("resend_stage"):
        out["smm_sop_resend_stage"] = rem["resend_stage"]
        # Clear prior heal stamp so CEO can re-spawn for a new failure.
        out.pop("ceo_sop_heal_spawned_for", None)
    else:
        out.pop("smm_sop_resend_stage", None)
    # Surgical still hint: first scene_NNN mentioned in warnings/items
    blob = json.dumps(
        {
            "warnings": digest.get("warnings"),
            "items": digest.get("items"),
            "remediation": rem,
        },
        default=str,
    )
    m = re.search(r"scene[_\s-]?(\d{1,4})", blob, flags=re.I)
    if m:
        out["smm_sop_bad_scene"] = f"scene_{int(m.group(1)):03d}"
    out["smm_sop_stage_blocked"] = bool(digest.get("blocked"))
    if digest.get("compliance_path"):
        out["sop_compliance_path"] = digest["compliance_path"]
    history = list(out.get("smm_sop_stage_history") or [])
    history.append(
        {
            "at": out["smm_sop_stage_checked_at"],
            "stage": digest.get("stage"),
            "ok": bool(digest.get("ok")),
            "fp": digest.get("fingerprint"),
            "resend": rem.get("resend_stage"),
            "next": digest.get("next_stage"),
            "bad_scene": out.get("smm_sop_bad_scene"),
        }
    )
    out["smm_sop_stage_history"] = history[-20:]
    return out
