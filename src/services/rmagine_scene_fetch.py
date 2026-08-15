"""Per-scene Wiki+Met → vision → place still; Flux only on reject/gap.

RMagine autonomous ladder for every scene slot (no hero-count product cap).
"""

from __future__ import annotations

import gc
import json
import logging
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from src.services.asset_fetcher import search_and_stage
from src.services.pd_clippings import fit_still
from src.services.query_distill import (
    PORTRAIT_REUSE_LIMIT,
    scene_search_bundle,
)
from src.services.vision_judge import validate_image_with_vision_llm

log = logging.getLogger(__name__)

# Soft ops bounds — NOT product caps on how many Wiki+Met PASSes we keep.
_FETCH_PER_PHASE = 4
_JUDGE_TOP = 3
_DEFAULT_WORKERS = 2  # keep low — openrouter/free rate-limits concurrent VL calls
_HARD_MAX_WORKERS = 2  # OOM + free-pool VL — never raise above 2 on 8GB VPS

_SOURCES = ["wikimedia", "met"]


def _resolve_workers(max_workers: int | None) -> int:
    env = os.getenv("RMAGINE_FETCH_WORKERS", "").strip()
    if env:
        try:
            max_workers = int(env)
        except ValueError:
            pass
    return max(1, min(int(max_workers or _DEFAULT_WORKERS), _HARD_MAX_WORKERS))


def _scene_still_exists(job_dir: Path, scene_idx: int) -> bool:
    return (job_dir / "images" / f"scene_{int(scene_idx):03d}.jpg").is_file()


def _load_script(job_dir: Path) -> dict[str, Any] | None:
    sp = job_dir / "script" / "script.json"
    if not sp.is_file():
        return None
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _scene_rows(script: dict[str, Any]) -> list[dict[str, Any]]:
    scenes = script.get("scenes") or []
    rows: list[dict[str, Any]] = []
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        try:
            idx = int(sc.get("index", i))
        except (TypeError, ValueError):
            idx = i
        rows.append(
            {
                "index": idx,
                "text": str(sc.get("text") or ""),
                "visual_prompt": str(sc.get("visual_prompt") or ""),
            }
        )
    return rows


def _place_still(
    src: Path,
    job_dir: Path,
    scene_idx: int,
    *,
    width: int = 1280,
    height: int = 720,
) -> Path | None:
    images = job_dir / "images"
    images.mkdir(parents=True, exist_ok=True)
    hero_dir = images / "fetched_heroes"
    hero_dir.mkdir(parents=True, exist_ok=True)
    fitted = hero_dir / f"hero_{int(scene_idx):03d}.jpg"
    try:
        fit_still(src, fitted, width=width, height=height)
    except Exception as exc:  # noqa: BLE001
        log.warning("rmagine fit failed scene %s: %s", scene_idx, exc)
        return None
    dest = images / f"scene_{int(scene_idx):03d}.jpg"
    if dest.is_file():
        backup = images / "flux_backup"
        backup.mkdir(parents=True, exist_ok=True)
        bak = backup / dest.name
        if not bak.exists():
            try:
                shutil.copyfile(dest, bak)
            except OSError:
                pass
    try:
        shutil.copyfile(fitted, dest)
    except OSError as exc:
        log.warning("rmagine write failed %s: %s", dest, exc)
        return None
    return dest


def _try_phases_for_scene(
    *,
    scene: dict[str, Any],
    stage_root: Path,
    force_symbolic: bool,
    settings: Any | None,
) -> dict[str, Any]:
    """Wiki+Met waterfall across distilled phases; return PASS placement or reject."""
    idx = int(scene["index"])
    bundle = scene_search_bundle(
        text=scene.get("text") or "",
        visual_prompt=scene.get("visual_prompt") or "",
        force_symbolic_broll=force_symbolic,
    )
    figure = bundle.get("historical_figure")
    scene_text = (
        f"{scene.get('visual_prompt') or ''} {scene.get('text') or ''}".strip()
        or "historical documentary still"
    )
    judged_all: list[dict[str, Any]] = []
    scene_stage = stage_root / f"scene_{idx:03d}"

    for phase_i, query in enumerate(bundle["phase_list"]):
        q = str(query or "").strip()
        if not q:
            continue
        phase_dir = scene_stage / f"phase{phase_i + 1}"
        try:
            summary = search_and_stage(
                q[:120],
                "image",
                phase_dir,
                max_results=_FETCH_PER_PHASE,
                sources=list(_SOURCES),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("scene %s phase %s fetch failed: %s", idx, phase_i, exc)
            continue
        approved = list(summary.get("approved") or [])[:_JUDGE_TOP]
        for asset in approved:
            ref = str(asset.get("local_path") or asset.get("source_url") or "")
            if not ref:
                continue
            vision = validate_image_with_vision_llm(
                ref, scene_text, figure, settings=settings
            )
            # No provisional PASS — vision down/unavailable → Flux fill only.
            row = {
                **asset,
                "vision": vision,
                "phase": phase_i + 1,
                "query": q,
                "scene_index": idx,
                "historical_figure": figure,
            }
            judged_all.append(row)
            if vision.get("status") == "PASS":
                return {
                    "status": "PASS",
                    "scene_index": idx,
                    "asset": row,
                    "historical_figure": figure,
                    "force_symbolic": force_symbolic,
                    "judged": judged_all,
                    "phases_tried": phase_i + 1,
                }
    return {
        "status": "REJECT_OR_EMPTY",
        "scene_index": idx,
        "asset": None,
        "historical_figure": figure,
        "force_symbolic": force_symbolic,
        "judged": judged_all,
        "phases_tried": len(bundle["phase_list"]),
        "flux_fill": True,
    }


def fetch_archival_for_all_scenes(
    job_dir: Path | str,
    *,
    settings: Any | None = None,
    max_workers: int = _DEFAULT_WORKERS,
    width: int = 1280,
    height: int = 720,
    skip_if_stamped: bool = True,
) -> dict[str, Any]:
    """Attempt Wiki+Met for EVERY script scene; Flux fills only empty slots later.

    No product cap on how many PASS archival stills may be placed.
    """
    job_dir = Path(job_dir)
    stage_root = job_dir / "assets" / "fetched"
    stage_root.mkdir(parents=True, exist_ok=True)
    stamp = stage_root / "vision_judge.json"

    if skip_if_stamped and stamp.is_file():
        try:
            prev = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev = {}
        if isinstance(prev, dict) and prev.get("policy") == (
            "per_scene_wiki_met_primary_flux_on_reject"
        ):
            log.info(
                "rmagine per-scene vision stamp present — skip re-fetch "
                "(mid-run resume); Flux fills remaining gaps"
            )
            return prev

    script = _load_script(job_dir)
    if not script:
        return {
            "ok": False,
            "reason": "no_script",
            "policy": "per_scene_wiki_met_primary_flux_on_reject",
            "n_scenes": 0,
            "n_pass": 0,
            "n_flux_gaps": 0,
        }

    scenes = _scene_rows(script)
    if not scenes:
        return {
            "ok": False,
            "reason": "no_scenes",
            "policy": "per_scene_wiki_met_primary_flux_on_reject",
            "n_scenes": 0,
            "n_pass": 0,
            "n_flux_gaps": 0,
        }

    figure_counts: dict[str, int] = {}
    lock = threading.Lock()
    results: list[dict[str, Any]] = []
    workers = _resolve_workers(max_workers)
    progress_path = stage_root / "rmagine_progress.jsonl"

    def _append_progress(row: dict[str, Any]) -> None:
        try:
            with progress_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _worker(sc: dict[str, Any]) -> dict[str, Any]:
        idx = int(sc["index"])
        # Resume-safe: keep already-placed stills (OOM mid-run left orphans).
        if _scene_still_exists(job_dir, idx):
            fig = None
            try:
                from src.services.query_distill import extract_historical_figure

                fig = extract_historical_figure(
                    f"{sc.get('visual_prompt') or ''} {sc.get('text') or ''}"
                )
            except Exception:  # noqa: BLE001
                fig = None
            outcome = {
                "status": "PASS",
                "scene_index": idx,
                "asset": None,
                "historical_figure": fig,
                "force_symbolic": False,
                "judged": [],
                "phases_tried": 0,
                "placed": True,
                "placed_path": str(job_dir / "images" / f"scene_{idx:03d}.jpg"),
                "skipped_existing": True,
                "flux_fill": False,
            }
            _append_progress(
                {"scene_index": idx, "status": "PASS", "skipped_existing": True}
            )
            return outcome

        figure_guess = None
        force = False
        blob = f"{sc.get('visual_prompt') or ''} {sc.get('text') or ''}"
        from src.services.query_distill import extract_historical_figure

        figure_guess = extract_historical_figure(blob)
        with lock:
            if figure_guess:
                key = figure_guess.lower()
                used = figure_counts.get(key, 0)
                if used >= PORTRAIT_REUSE_LIMIT:
                    force = True
        try:
            outcome = _try_phases_for_scene(
                scene=sc,
                stage_root=stage_root,
                force_symbolic=force,
                settings=settings,
            )
        finally:
            gc.collect()
        if outcome.get("status") == "PASS" and outcome.get("asset"):
            src = Path(str((outcome["asset"] or {}).get("local_path") or ""))
            placed = None
            if src.is_file():
                placed = _place_still(
                    src, job_dir, int(sc["index"]), width=width, height=height
                )
            if placed is not None:
                fig = outcome.get("historical_figure")
                with lock:
                    if fig and not force:
                        figure_counts[str(fig).lower()] = (
                            figure_counts.get(str(fig).lower(), 0) + 1
                        )
                outcome["placed_path"] = str(placed)
                outcome["placed"] = True
            else:
                outcome["status"] = "REJECT_OR_EMPTY"
                outcome["placed"] = False
                outcome["flux_fill"] = True
        else:
            outcome["placed"] = False
            outcome["flux_fill"] = True
        _append_progress(
            {
                "scene_index": idx,
                "status": outcome.get("status"),
                "placed": outcome.get("placed"),
                "flux_fill": outcome.get("flux_fill"),
            }
        )
        gc.collect()
        return outcome

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_worker, sc): sc for sc in scenes}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                sc = futs[fut]
                log.warning("rmagine scene %s failed: %s", sc.get("index"), exc)
                results.append(
                    {
                        "status": "REJECT_OR_EMPTY",
                        "scene_index": sc.get("index"),
                        "placed": False,
                        "flux_fill": True,
                        "error": str(exc)[:200],
                    }
                )

    results.sort(key=lambda r: int(r.get("scene_index") or 0))
    n_pass = sum(1 for r in results if r.get("status") == "PASS" and r.get("placed"))
    n_gap = sum(1 for r in results if r.get("flux_fill"))
    channel_hint = None
    try:
        meta = script.get("meta") if isinstance(script.get("meta"), dict) else {}
        channel_hint = (
            (meta or {}).get("channel")
            or script.get("channel")
            or None
        )
    except Exception:  # noqa: BLE001
        channel_hint = None
    # Shared ladder for every Brand Account — not limited to the original two.
    try:
        from src.agents.channel_empire import all_known_channel_names

        channel_scope = list(all_known_channel_names())
    except Exception:  # noqa: BLE001
        channel_scope = ["napstorian", "napping_historian"]
    if channel_hint and str(channel_hint) not in channel_scope:
        channel_scope.append(str(channel_hint))
    report = {
        "ok": True,
        "policy": "per_scene_wiki_met_primary_flux_on_reject",
        "policy_sop": (
            "All stills attempt Wiki+Met per scene; Flux = reject/no-candidate only; "
            "no product hero cap"
        ),
        "channels": channel_scope,
        "job_channel": channel_hint,
        "n_scenes": len(scenes),
        "n_pass": n_pass,
        "n_flux_gaps": n_gap,
        "n_rejected_or_empty": n_gap,
        "archival_pass_rate": (round(n_pass / len(scenes), 4) if scenes else 0.0),
        "ai_fill_gaps": True,
        "flux_on_vision_reject": True,
        "reuse_weak_pd": False,
        "no_hero_product_cap": True,
        "portrait_reuse_limit": PORTRAIT_REUSE_LIMIT,
        "max_workers": workers,
        "sources": list(_SOURCES),
        "figure_portrait_counts": dict(figure_counts),
        "scenes": [
            {
                "scene_index": r.get("scene_index"),
                "status": r.get("status"),
                "placed": r.get("placed"),
                "flux_fill": r.get("flux_fill"),
                "historical_figure": r.get("historical_figure"),
                "force_symbolic": r.get("force_symbolic"),
                "placed_path": r.get("placed_path"),
                "phases_tried": r.get("phases_tried"),
                "source_url": (r.get("asset") or {}).get("source_url"),
                "vision": (r.get("asset") or {}).get("vision"),
            }
            for r in results
        ],
    }
    stamp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Soft SOP stamp merge
    pd_stamp = job_dir / "pd_clippings.json"
    try:
        existing: dict[str, Any] = {}
        if pd_stamp.is_file():
            existing = json.loads(pd_stamp.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        existing["ok"] = True
        existing["fetched_heroes"] = True
        existing["fetched_hero_count"] = n_pass
        existing["rmagine_per_scene"] = True
        existing["flux_on_vision_reject"] = True
        pd_stamp.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        log.warning("pd_clippings rmagine merge failed: %s", exc)

    (job_dir / "fetched_heroes.json").write_text(
        json.dumps(
            {
                "ok": True,
                "fetched_hero_count": n_pass,
                "n_flux_gaps": n_gap,
                "policy": report["policy"],
                "no_hero_product_cap": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return report
