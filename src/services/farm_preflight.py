"""Farm resource preflight — fail BEFORE grinding when tools are unavailable.

Agents must call this (via spawn_farm_job) so we never burn TTS / RunPod / time
when vision, Met/Wiki, or GPU stills gates are down.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _truthy(name: str, default: str = "1") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }


def probe_met_ready(*, timeout: float = 12.0) -> dict[str, Any]:
    """Met Collection API readiness.

    Imperva/Incapsula in front of collectionapi returns HTML 403 when the
    request has no descriptive User-Agent (same class of bot gate as Wiki).
    """
    from src.services.asset_fetcher import USER_AGENT

    try:
        with httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            r = client.get(
                "https://collectionapi.metmuseum.org/public/collection/v1/search",
                params={"q": "roman", "hasImages": "true"},
            )
            if r.status_code == 403:
                body = (r.text or "")[:160]
                return {
                    "ok": False,
                    "error": (
                        "met HTTP 403 (Incapsula / missing-or-blocked User-Agent)"
                        f": {body!r}"
                    ),
                }
            if r.status_code >= 400:
                return {"ok": False, "error": f"met HTTP {r.status_code}"}
            data = r.json()
            n = int(data.get("total") or 0)
            return {"ok": n > 0, "total": n, "user_agent": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def probe_wikimedia_ready(*, timeout: float = 12.0) -> dict[str, Any]:
    from src.services.asset_fetcher import USER_AGENT

    try:
        with httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            r = client.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": "Cannae",
                    "srnamespace": 6,
                    "srlimit": 1,
                    "format": "json",
                },
            )
            if r.status_code == 403:
                return {
                    "ok": False,
                    "error": "wikimedia 403 (User-Agent / robot policy)",
                }
            if r.status_code >= 400:
                return {"ok": False, "error": f"wikimedia HTTP {r.status_code}"}
            hits = (
                ((r.json().get("query") or {}).get("search"))
                if isinstance(r.json(), dict)
                else None
            )
            return {"ok": bool(hits), "n": len(hits or [])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def farm_resource_preflight(
    *,
    farm_phase: str = "full",
    require_vision: bool | None = None,
    require_archival: bool | None = None,
    require_runpod_smoke: bool | None = None,
) -> dict[str, Any]:
    """Return ``{ok, blockers, checks}``. ``ok=False`` → refuse spawn.

    - ``prep``: skip vision/archival/GPU (script+TTS only).
    - ``full`` / ``visuals``: vision + archival required when ASSET_FETCHER on;
      RunPod smoke gate when IMAGE_BACKEND=runpod_pod.
    """
    phase = (farm_phase or "full").strip().lower()
    checks: dict[str, Any] = {"farm_phase": phase}
    blockers: list[str] = []

    if phase == "prep":
        return {
            "ok": True,
            "skipped": True,
            "reason": "prep phase — vision/GPU preflight deferred to visuals",
            "checks": checks,
            "blockers": [],
        }

    if require_vision is None:
        require_vision = _truthy("ASSET_FETCHER", "1") and _truthy(
            "FARM_REQUIRE_VISION", "1"
        )
    if require_archival is None:
        require_archival = _truthy("ASSET_FETCHER", "1")
    if require_runpod_smoke is None:
        backend = (
            os.environ.get("IMAGE_BACKEND")
            or os.environ.get("VISUALS_BACKEND")
            or ""
        ).strip().lower()
        require_runpod_smoke = backend in {"runpod_pod", "pod"}

    if require_vision:
        from src.services.vision_judge import VISION_MODEL_LOCKED, probe_vision_ready

        vision = probe_vision_ready()
        checks["vision"] = vision
        if not vision.get("ok"):
            blockers.append(
                f"vision judge not ready (model={VISION_MODEL_LOCKED}): "
                f"{vision.get('error') or vision}"
            )

    if require_archival:
        met = probe_met_ready()
        wiki = probe_wikimedia_ready()
        checks["met"] = met
        checks["wikimedia"] = wiki
        # Need at least one archival source; Met alone is enough to proceed.
        if not met.get("ok") and not wiki.get("ok"):
            blockers.append(
                "both Met and Wikimedia archival probes failed — "
                f"met={met.get('error')}; wiki={wiki.get('error')}"
            )
        elif not wiki.get("ok"):
            checks["wikimedia_warn"] = wiki.get("error")
            log.warning("wikimedia preflight warn: %s", wiki.get("error"))

    if require_runpod_smoke:
        try:
            from src.runpod.guards import farm_may_use_runpod_pod_stills

            ok_smoke, smoke_msg = farm_may_use_runpod_pod_stills()
            checks["runpod_smoke"] = {"ok": ok_smoke, "message": smoke_msg}
            if not ok_smoke:
                blockers.append(f"runpod stills smoke gate: {smoke_msg}")
        except Exception as exc:  # noqa: BLE001
            checks["runpod_smoke"] = {"ok": False, "error": str(exc)}
            blockers.append(f"runpod smoke check failed: {exc}")

    ok = not blockers
    out = {"ok": ok, "blockers": blockers, "checks": checks}
    if not ok:
        log.error("farm_resource_preflight BLOCKED: %s", blockers)
    return out
