"""VisualModule — one still per scene (+ optional Plan C sparse AI video clips).

Plan C Module 3:
- scenes[].visual_prompt → scene_{index:03d}.jpg @ 1280×720
- no brightness/luma QA gate — accept generated stills after normalize
- hard provider errors stay finite (IMAGE_MAX_RETRIES_PER_SCENE)
- optional ENABLE_AI_VIDEO_CLIPS: pick up to 5 scenes → scene_{i}_clip.mp4

Stills backends: mock | runpod / runpod_serverless | runpod_pod | api
Intended production: runpod_pod (ephemeral RTX A5000 Community + Comfy Flux).
Cheapest sparse video default: RunPod Public Pruna `p-video` (~$0.02/s @ 720p).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from src.domain.models import Scene, SceneImage, ScriptResult, VisualResult, VisualValidation
from src.services.character_bible import (
    CharacterTracker,
    load_character_bible,
    match_characters,
    resolve_ref_paths,
)
from src.services.settings import CONFIG_DIR, ROOT, get_settings, load_style_hint
from src.services.visual_guardrails import apply_guardrails, load_guardrails


class VisualModuleError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ImageDims:
    width: int
    height: int


def pick_video_scene_indices(scene_count: int, max_clips: int) -> list[int]:
    """Evenly spaced sparse picks: hook … mid … closer (Plan C §7.1)."""
    if scene_count <= 0 or max_clips <= 0:
        return []
    k = min(max_clips, scene_count)
    if k == 1:
        return [0]
    # positions 0 .. n-1 inclusive
    idxs = sorted({round(i * (scene_count - 1) / (k - 1)) for i in range(k)})
    return [int(i) for i in idxs]


class VisualModule:
    def __init__(self):
        # Clear cache so .env edits apply without restarting long-lived shells
        get_settings.cache_clear()
        self.s = get_settings()
        self.dims = _ImageDims(width=self.s.image_width, height=self.s.image_height)
        self.guardrails = load_guardrails()
        self.style_hint = self.guardrails.style_lock or load_style_hint()
        bible_path = Path(self.s.character_bible_path)
        if not bible_path.is_absolute():
            bible_path = ROOT / bible_path
        self.bible_style_id, self.bible, self.bible_refs = load_character_bible(bible_path)
        self.bible_path = str(bible_path)
        self.tracker = CharacterTracker(
            style_id=self.bible_style_id,
            bible_path=self.bible_path,
        )
        self._pod_session = None  # EphemeralStillsSession when IMAGE_BACKEND=runpod_pod

    def effective_backend(self) -> str:
        """Resolve VISUALS_BACKEND alias or IMAGE_BACKEND."""
        alias = (getattr(self.s, "visuals_backend", None) or "").strip().lower()
        raw = (alias or self.s.image_backend or "mock").strip().lower()
        if raw in {"runpod_serverless", "serverless"}:
            return "runpod"
        if raw in {"runpod_pod", "pod"}:
            return "runpod_pod"
        return raw

    def synthesize_script(
        self,
        script_path: Path | str,
        *,
        out_dir: Path | str | None = None,
        resume: bool = True,
        limit_scenes: int | None = None,
    ) -> VisualResult:
        script_path = Path(script_path)
        if not script_path.exists():
            raise VisualModuleError(f"Script JSON not found: {script_path}")

        raw = json.loads(script_path.read_text(encoding="utf-8"))
        script = ScriptResult.model_validate(raw)
        if not script.scenes:
            raise VisualModuleError("Script has zero scenes")

        scenes_in = list(script.scenes)
        if limit_scenes is not None:
            scenes_in = scenes_in[: max(0, int(limit_scenes))]

        # FoC / long-epic still reuse: fewer unique Flux gens, copy across scenes.
        still_slot_of: list[int] = []
        unique_gen_indices: set[int] = set()
        try:
            from src.services.still_budget import (
                resolve_max_unique_stills,
                still_reuse_map,
                unique_scene_indices,
            )

            prof_name = ""
            if isinstance(script.meta, dict):
                prof_name = str(
                    script.meta.get("retention_profile")
                    or script.meta.get("format_mode")
                    or ""
                )
            try:
                from src.services.retention_profile import get_profile

                prof = get_profile(prof_name or None)
            except Exception:  # noqa: BLE001
                prof = {}
            cap = resolve_max_unique_stills(
                profile=prof,
                format_mode=prof_name or prof.get("format_mode"),
                scene_count=len(scenes_in),
            )
            still_slot_of = still_reuse_map(len(scenes_in), max_unique=cap)
            unique_gen_indices = set(unique_scene_indices(len(scenes_in), max_unique=cap))
        except Exception:  # noqa: BLE001
            still_slot_of = list(range(len(scenes_in)))
            unique_gen_indices = set(range(len(scenes_in)))

        if out_dir is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in (script.title or "job")
            )[:60]
            out_dir = ROOT / "output" / "images" / f"{stamp}_{safe}"
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        video_idxs = set()
        if self.s.enable_ai_video_clips:
            video_idxs = set(
                pick_video_scene_indices(
                    len(scenes_in), int(self.s.max_ai_video_clips_per_job)
                )
            )

        max_retries = int(self.s.image_max_retries_per_scene)
        scenes_out: list[SceneImage] = []
        warnings: list[str] = []
        errors: list[str] = []
        backend = self.effective_backend()
        pod_id: str | None = None

        def _run_scenes() -> None:
            nonlocal pod_id
            if self._pod_session is not None:
                pod_id = getattr(self._pod_session, "pod_id", None)
            slot_to_path: dict[int, Path] = {}
            for pos, scene in enumerate(scenes_in):
                img_path = out_dir / f"scene_{scene.index:03d}.jpg"
                want_video = scene.index in video_idxs
                slot = still_slot_of[pos] if pos < len(still_slot_of) else pos
                # Reuse a prior unique still when over budget (Ken Burns holds stretch runtime).
                if (
                    pos not in unique_gen_indices
                    and slot in slot_to_path
                    and slot_to_path[slot].is_file()
                ):
                    src = slot_to_path[slot]
                    if resume and img_path.exists() and img_path.stat().st_size > 1000:
                        pass
                    else:
                        shutil.copy2(src, img_path)
                    from src.domain.models import SceneImage

                    si = SceneImage(
                        index=scene.index,
                        visual_prompt=scene.visual_prompt or "",
                        path=str(img_path.resolve()),
                        width=self.dims.width,
                        height=self.dims.height,
                        backend=backend,
                        placeholder=False,
                        skipped=True,
                        characters=[],
                        video_path=None,
                    )
                    # Mark reuse in path meta via warnings
                    warnings.append(
                        f"scene {scene.index}: reused still from slot {slot}"
                    )
                    scenes_out.append(si)
                    continue
                varied = self._ensure_visual_variety(scene, scenes_out)
                try:
                    si = self._synthesize_scene(
                        img_path,
                        scene=varied,
                        resume=resume,
                        max_retries=max_retries,
                        is_video_scene=want_video,
                    )
                    self.tracker.record(varied.index, si.characters)
                    if want_video and self.s.enable_ai_video_clips:
                        clip_path = out_dir / f"scene_{varied.index:03d}_clip.mp4"
                        try:
                            self._maybe_generate_video_clip(
                                si, clip_path=clip_path, resume=resume
                            )
                        except Exception as exc:  # noqa: BLE001
                            warnings.append(
                                f"scene {varied.index}: video clip failed ({exc}); still kept"
                            )
                    scenes_out.append(si)
                    slot_to_path[slot] = Path(si.path)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"scene {varied.index}: {exc}")
                    raise VisualModuleError(
                        f"scene {varied.index} failed: {exc}"
                    ) from exc

        # Wiki+Met (VPS) may have already placed scene_XXX.jpg — only boot a
        # RunPod Flux session when at least one still is still missing.
        need_flux = any(
            not self._image_ready(out_dir / f"scene_{sc.index:03d}.jpg")
            for sc in scenes_in
        )

        if backend == "runpod_pod" and need_flux:
            from src.runpod.capacity import CapacityDeferError
            from src.services.visuals_runpod_pod import EphemeralStillsSession

            try:
                with EphemeralStillsSession() as sess:
                    self._pod_session = sess
                    try:
                        _run_scenes()
                    finally:
                        self._pod_session = None
                    capacity_meta = (
                        sess.capacity_result.to_dict()
                        if sess.capacity_result is not None
                        else None
                    )
            except CapacityDeferError as exc:
                raise VisualModuleError(
                    f"capacity pre-flight deferred stills — HOLD_CAPACITY "
                    f"awaiting GREEN (no create retries): {exc}"
                ) from exc
        else:
            capacity_meta = None
            if backend == "runpod_pod" and not need_flux:
                warnings.append(
                    "runpod_pod skipped — all stills already on disk "
                    "(Wiki+Met / prior resume); no Flux gaps"
                )
            _run_scenes()

        image_bytes = [
            os.path.getsize(si.path) for si in scenes_out if Path(si.path).exists()
        ]
        median_bytes = sorted(image_bytes)[len(image_bytes) // 2] if image_bytes else None
        video_clip_count = sum(1 for si in scenes_out if si.video_path)

        validation = VisualValidation(
            ok=not errors and len(scenes_out) == len(scenes_in),
            scene_count=len(scenes_in),
            image_count=len(scenes_out),
            width=self.dims.width,
            height=self.dims.height,
            expected_count=len(scenes_in),
            median_image_bytes=median_bytes,
            video_scene_count=len(video_idxs),
            video_clip_count=video_clip_count,
            warnings=warnings,
            errors=errors,
        )

        result = VisualResult(
            script_path=str(script_path.resolve()),
            title=script.title,
            topic=script.topic,
            out_dir=str(out_dir.resolve()),
            backend=backend,
            scenes=scenes_out,
            validation=validation,
            meta={
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "backend": backend,
                "runpod_worker_type": self.s.runpod_worker_type,
                "runpod_endpoint_id": self.s.runpod_endpoint_id,
                "runpod_stills_gpu": getattr(self.s, "runpod_stills_gpu_type_id", None),
                "runpod_stills_template_id": getattr(
                    self.s, "runpod_stills_template_id", None
                ),
                "ephemeral_pod_id": pod_id,
                "capacity_benchmark": capacity_meta,
                "dims": {"w": self.dims.width, "h": self.dims.height},
                "max_retries_per_scene": max_retries,
                "enable_ai_video_clips": self.s.enable_ai_video_clips,
                "video_scene_indices": sorted(video_idxs),
                "video_endpoint_id": self.s.video_endpoint_id,
                "style_hint": self.style_hint,
                "style_id": self.bible_style_id,
                "character_bible": self.bible_path,
                "character_totals": dict(self.tracker.totals),
                "reference_images": resolve_ref_paths(self.bible_refs),
                "guardrails_negative": self.guardrails.negative[:500],
                "guardrails_append": self.guardrails.append_rules[:300],
                "guardrails_face_boost": self.guardrails.face_boost[:200],
                "guardrails_hand_boost": self.guardrails.hand_boost[:200],
                "max_unique_stills": len(unique_gen_indices),
                "still_reuse_enabled": len(unique_gen_indices) < len(scenes_in),
            },
        )
        (out_dir / "visual_manifest.json").write_text(
            json.dumps(result.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.tracker.save(out_dir / "character_usage.json")
        return result

    def _synthesize_scene(
        self,
        img_path: Path,
        *,
        scene: Scene,
        resume: bool,
        max_retries: int,
        is_video_scene: bool,
    ) -> SceneImage:
        prompt_raw = (scene.visual_prompt or "").strip()
        if not prompt_raw:
            raise VisualModuleError("empty visual_prompt")

        matched = match_characters(prompt_raw, scene.text or "", bible=self.bible)
        # Inject locked character appearance BEFORE style/guardrail append
        prompt_for_guard = prompt_raw
        if matched.inject:
            prompt_for_guard = f"{matched.inject}. Scene: {prompt_raw}"

        guarded = apply_guardrails(prompt_for_guard, bundle=self.guardrails)
        prompt = guarded.positive
        negative = guarded.negative

        backend = self.effective_backend()
        placeholder = backend == "mock"

        if resume and self._image_ready(img_path):
            w, h = self._read_dims(img_path)
            return SceneImage(
                index=scene.index,
                visual_prompt=prompt,
                path=str(img_path.resolve()),
                width=w,
                height=h,
                retries_used=0,
                skipped=True,
                backend=backend,
                placeholder=placeholder,
                characters=matched.ids,
                is_video_scene=is_video_scene,
            )

        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                if img_path.exists():
                    img_path.unlink(missing_ok=True)
                seed = scene.index * 9973 + attempt * 7919 + int(matched.seed_offset)
                self._generate_image_to_path(
                    img_path,
                    prompt=prompt,
                    negative=negative,
                    seed=seed,
                )
                self._normalize_to_target_jpeg(img_path)
                if not self._image_ready(img_path):
                    raise VisualModuleError(f"image missing/unreadable after generate: {img_path}")
                w, h = self._read_dims(img_path)
                return SceneImage(
                    index=scene.index,
                    visual_prompt=prompt,
                    path=str(img_path.resolve()),
                    width=w,
                    height=h,
                    retries_used=attempt,
                    skipped=False,
                    backend=backend,
                    placeholder=placeholder,
                    characters=matched.ids,
                    is_video_scene=is_video_scene,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning(
                    "scene %s generate error %s/%s — retry: %s",
                    scene.index,
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                err_l = str(exc).lower()
                if any(
                    m in err_l
                    for m in (
                        "http 502",
                        "http 503",
                        "http 504",
                        "waiting for service",
                        "comfy not ready",
                    )
                ):
                    import time as _time

                    _time.sleep(min(60.0, 4.0 * (attempt + 1)))
        raise VisualModuleError(f"failed after {max_retries + 1} attempts: {last_err}")

    def _ensure_visual_variety(
        self, scene: Scene, prior: list[SceneImage]
    ) -> Scene:
        """Reject identical visual_prompt hash on consecutive scenes."""
        if not prior:
            return scene
        prev_prompt = (prior[-1].visual_prompt or "").strip()
        cur = (scene.visual_prompt or "").strip()
        if not prev_prompt or not cur:
            return scene
        if hashlib.sha256(prev_prompt.encode()).hexdigest() == hashlib.sha256(
            cur.encode()
        ).hexdigest():
            varied = scene.model_copy(
                update={
                    "visual_prompt": cur
                    + ", alternate angle, varied composition, distinct focal point"
                }
            )
            return varied
        return scene

    def _generate_image_to_path(
        self, img_path: Path, *, prompt: str, negative: str = "", seed: int
    ) -> None:
        backend = self.effective_backend()
        if backend == "mock":
            self._mock_generate(img_path, prompt=prompt)
            return
        if backend == "runpod":
            self._runpod_generate(img_path, prompt=prompt, negative=negative, seed=seed)
            return
        if backend == "runpod_pod":
            self._runpod_pod_generate(
                img_path, prompt=prompt, negative=negative, seed=seed
            )
            return
        if backend == "api":
            self._image_api_generate(img_path, prompt=prompt)
            return
        raise VisualModuleError(f"Unknown image_backend: {backend}")

    def _runpod_pod_generate(
        self, img_path: Path, *, prompt: str, negative: str = "", seed: int
    ) -> None:
        if self._pod_session is None or getattr(self._pod_session, "comfy", None) is None:
            raise VisualModuleError(
                "runpod_pod backend requires an active EphemeralStillsSession "
                "(opened by synthesize_script)"
            )
        workflow = self._build_comfy_workflow(prompt, negative=negative, seed=seed)
        self._pod_session.generate_to_path(workflow, img_path)

    def _mock_generate(self, img_path: Path, *, prompt: str) -> None:
        from PIL import Image, ImageDraw

        w, h = self.dims.width, self.dims.height
        style = (self.style_hint or "").lower()
        # Legacy stickman mock only if style lock still mentions it
        if "stickman" in style or "stick man" in style or "whiteboard" in style:
            img = Image.new("RGB", (w, h), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            cx, cy = w // 2, h // 2
            r = max(20, h // 12)
            draw.ellipse([cx - r, cy - 3 * r, cx + r, cy - r], outline=(255, 255, 255), width=6)
            draw.line([(cx, cy - r), (cx, cy + 2 * r)], fill=(255, 255, 255), width=6)
            draw.line([(cx - 2 * r, cy), (cx + 2 * r, cy)], fill=(255, 255, 255), width=6)
            draw.line([(cx, cy + 2 * r), (cx - 2 * r, cy + 4 * r)], fill=(255, 255, 255), width=6)
            draw.line([(cx, cy + 2 * r), (cx + 2 * r, cy + 4 * r)], fill=(255, 255, 255), width=6)
            draw.ellipse(
                [cx - r // 2 - 3, cy - 2 * r - 3, cx - r // 2 + 3, cy - 2 * r + 3],
                fill=(255, 255, 255),
            )
            draw.ellipse(
                [cx + r // 2 - 3, cy - 2 * r - 3, cx + r // 2 + 3, cy - 2 * r + 3],
                fill=(255, 255, 255),
            )
            img.save(img_path, format="JPEG", quality=92)
            return

        # Cinematic Tudor mock: warm amber gradient + figure silhouette
        seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16], 16)
        img = Image.new("RGB", (w, h))
        draw = ImageDraw.Draw(img)
        for y in range(h):
            t = y / max(1, h - 1)
            r = int(28 + (120 - 28) * (1 - t) + (seed % 20))
            g = int(18 + (70 - 18) * (1 - t))
            b = int(12 + (35 - 12) * t)
            draw.line([(0, y), (w, y)], fill=(r, g, b))
        # soft "god ray" wedge
        draw.polygon(
            [(w * 0.55, 0), (w * 0.85, 0), (w * 0.45, h), (w * 0.15, h)],
            fill=(160, 110, 50),
        )
        # focal figure block (noble silhouette)
        cx = w // 2 + (seed % 40) - 20
        body = [
            (cx - 40, h * 0.35),
            (cx + 40, h * 0.35),
            (cx + 55, h * 0.85),
            (cx - 55, h * 0.85),
        ]
        draw.polygon(body, fill=(20, 55, 35))
        draw.ellipse([cx - 28, h * 0.22, cx + 28, h * 0.35], fill=(70, 45, 30))
        img.save(img_path, format="JPEG", quality=92)

    def _runpod_generate(
        self, img_path: Path, *, prompt: str, negative: str = "", seed: int
    ) -> None:
        api_key = self.s.runpod_api_key
        if not api_key:
            raise VisualModuleError("RUNPOD_API_KEY missing in .env")
        if not self.s.runpod_endpoint_id:
            raise VisualModuleError("RUNPOD_ENDPOINT_ID missing in .env")

        url = f"{self.s.runpod_base_url.rstrip('/')}/{self.s.runpod_endpoint_id}/runsync"
        worker = (self.s.runpod_worker_type or "comfyui").lower()

        if worker == "comfyui":
            payload = {
                "input": {
                    "workflow": self._build_comfy_workflow(
                        prompt, negative=negative, seed=seed
                    )
                }
            }
        elif self.s.runpod_payload_mode == "flat":
            payload = {
                self.s.runpod_prompt_field: prompt,
                self.s.runpod_width_field: self.dims.width,
                self.s.runpod_height_field: self.dims.height,
            }
        else:
            payload = {
                "input": {
                    self.s.runpod_prompt_field: prompt,
                    self.s.runpod_width_field: self.dims.width,
                    self.s.runpod_height_field: self.dims.height,
                }
            }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        resp = httpx.post(url, headers=headers, json=payload, timeout=600.0)
        if resp.status_code == 403:
            raise VisualModuleError(
                "RunPod HTTP 403 Forbidden — RUNPOD_API_KEY is invalid or belongs to a "
                f"different account than endpoint {self.s.runpod_endpoint_id}. "
                "Create/copy an API key from the same RunPod account that owns ComfyUI, "
                "paste it into .env as RUNPOD_API_KEY, then retry."
            )
        if resp.status_code >= 400:
            raise VisualModuleError(f"RunPod HTTP {resp.status_code}: {resp.text[:600]}")
        data = resp.json()
        # Some workers return immediately with IN_QUEUE — poll /status
        if isinstance(data, dict) and data.get("status") in {"IN_QUEUE", "IN_PROGRESS"} and data.get("id"):
            data = self._poll_runpod_job(data["id"], headers=headers)
        if isinstance(data, dict) and data.get("status") == "FAILED":
            raise VisualModuleError(f"RunPod job FAILED: {data.get('error') or data}")
        img_path.write_bytes(self._extract_image_bytes(data))

    def _poll_runpod_job(self, job_id: str, *, headers: dict[str, str], timeout_s: float = 600.0) -> dict[str, Any]:
        import time

        status_url = (
            f"{self.s.runpod_base_url.rstrip('/')}/{self.s.runpod_endpoint_id}/status/{job_id}"
        )
        deadline = time.time() + timeout_s
        last: dict[str, Any] = {}
        while time.time() < deadline:
            r = httpx.get(status_url, headers=headers, timeout=60.0)
            if r.status_code >= 400:
                raise VisualModuleError(f"RunPod status HTTP {r.status_code}: {r.text[:400]}")
            last = r.json()
            st = last.get("status")
            if st in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
                return last
            time.sleep(2.0)
        raise VisualModuleError(f"RunPod job {job_id} timed out; last={str(last)[:300]}")

    def _build_comfy_workflow(
        self, prompt: str, *, negative: str = "", seed: int
    ) -> dict[str, Any]:
        path = Path(self.s.comfy_workflow_path)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            path = CONFIG_DIR / "comfy_flux_workflow.json"
        workflow = json.loads(path.read_text(encoding="utf-8"))
        seed_i = int(seed) % (2**31 - 1) or random.randint(1, 2**30)

        if "6" in workflow and "inputs" in workflow["6"]:
            workflow["6"]["inputs"]["text"] = prompt
        if "33" in workflow and "inputs" in workflow["33"]:
            workflow["33"]["inputs"]["text"] = negative or ""
        if "27" in workflow and "inputs" in workflow["27"]:
            workflow["27"]["inputs"]["width"] = int(self.dims.width)
            workflow["27"]["inputs"]["height"] = int(self.dims.height)
        if "30" in workflow and "inputs" in workflow["30"]:
            workflow["30"]["inputs"]["ckpt_name"] = self.s.comfy_ckpt_name
        if "31" in workflow and "inputs" in workflow["31"]:
            workflow["31"]["inputs"]["seed"] = seed_i
            workflow["31"]["inputs"]["steps"] = int(self.s.comfy_steps)
        if "35" in workflow and "inputs" in workflow["35"]:
            workflow["35"]["inputs"]["guidance"] = float(self.s.comfy_guidance)
        return workflow

    def _image_api_generate(self, img_path: Path, *, prompt: str) -> None:
        if not self.s.image_api_base_url or not self.s.image_api_key:
            raise VisualModuleError("IMAGE_API_BASE_URL / IMAGE_API_KEY missing")
        url = self.s.image_api_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.s.image_api_key}"}
        payload = {
            self.s.image_api_prompt_field: prompt,
            "width": self.dims.width,
            "height": self.dims.height,
        }
        resp = httpx.post(url, headers=headers, json=payload, timeout=300.0)
        resp.raise_for_status()
        img_path.write_bytes(self._extract_image_bytes(resp.json()))

    def _maybe_generate_video_clip(
        self, scene: SceneImage, *, clip_path: Path, resume: bool
    ) -> None:
        if resume and clip_path.exists() and clip_path.stat().st_size > 1000:
            scene.video_path = str(clip_path.resolve())
            scene.video_duration_s = float(self.s.ai_video_seconds_per_clip)
            return

        api_key = (self.s.video_api_key or self.s.runpod_api_key or "").strip()
        endpoint = (self.s.video_endpoint_id or "").strip()
        if not api_key or not endpoint:
            raise VisualModuleError(
                "ENABLE_AI_VIDEO_CLIPS=true but VIDEO_ENDPOINT_ID / API key missing"
            )

        url = f"{self.s.runpod_base_url.rstrip('/')}/{endpoint}/runsync"
        # Cheapest path: Pruna T2V (no public image URL required yet)
        motion = (
            f"{scene.visual_prompt}. Subtle cinematic camera motion, "
            f"Tudor historical documentary style, smooth slow pan, atmospheric haze"
        )
        # Pruna p-video: duration is int 1–10; draft=true cuts cost ~75% (lower quality)
        duration = max(1, min(10, int(round(float(self.s.ai_video_seconds_per_clip)))))
        input_body: dict[str, Any] = {
            "prompt": motion,
            "duration": duration,
            "resolution": self.s.video_resolution,
            "aspect_ratio": "16:9",
        }
        if getattr(self.s, "video_draft", False):
            input_body["draft"] = True
        payload = {"input": input_body}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        resp = httpx.post(url, headers=headers, json=payload, timeout=600.0)
        if resp.status_code >= 400:
            raise VisualModuleError(f"video HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "FAILED":
            raise VisualModuleError(f"video FAILED: {data.get('error') or data}")

        video_bytes = self._extract_video_bytes(data)
        clip_path.write_bytes(video_bytes)
        scene.video_path = str(clip_path.resolve())
        scene.video_duration_s = float(self.s.ai_video_seconds_per_clip)

    def _extract_image_bytes(self, data: dict[str, Any]) -> bytes:
        """Handle ComfyUI + generic RunPod image shapes."""
        if not isinstance(data, dict):
            raise VisualModuleError(f"unexpected response type: {type(data)}")

        # Nested output
        output = data.get("output", data)

        candidates: list[Any] = []
        if isinstance(output, dict):
            for k in ("message", "image", "image_base64", "image_b64"):
                if output.get(k):
                    candidates.append(output[k])
            images = output.get("images")
            if isinstance(images, list):
                for item in images:
                    if isinstance(item, str):
                        candidates.append(item)
                    elif isinstance(item, dict):
                        for k in ("data", "image", "image_base64", "base64", "url", "image_url"):
                            if item.get(k):
                                candidates.append(item[k])
            if output.get("image_url"):
                candidates.append(output["image_url"])
        if isinstance(data.get("image_url"), str):
            candidates.append(data["image_url"])

        for c in candidates:
            if not isinstance(c, str) or not c.strip():
                continue
            s = c.strip()
            if s.startswith("http://") or s.startswith("https://"):
                return httpx.get(s, timeout=120.0).content
            if "base64," in s:
                s = s.split("base64,", 1)[1]
            try:
                raw = base64.b64decode(s, validate=False)
                if len(raw) > 500:
                    return raw
            except Exception:  # noqa: BLE001
                continue

        # Last resort: dump keys for debugging (truncate)
        preview = json.dumps(data, default=str)[:800]
        raise VisualModuleError(f"Could not extract image bytes from response: {preview}")

    def _extract_video_bytes(self, data: dict[str, Any]) -> bytes:
        output = data.get("output", data) if isinstance(data, dict) else {}
        if isinstance(output, dict):
            for k in ("video_url", "url"):
                if isinstance(output.get(k), str) and output[k].startswith("http"):
                    return httpx.get(output[k], timeout=180.0).content
            for k in ("video_base64", "video_b64", "video", "mp4_base64"):
                v = output.get(k)
                if isinstance(v, str) and v.strip():
                    s = v.split("base64,", 1)[-1] if "base64," in v else v
                    return base64.b64decode(s)
        if isinstance(output, list) and output:
            first = output[0]
            if isinstance(first, dict):
                return self._extract_video_bytes({"output": first})
        raise VisualModuleError(f"Could not extract video from: {str(data)[:500]}")

    def _normalize_to_target_jpeg(self, img_path: Path) -> None:
        """Force exact Plan C aspect 1280×720 JPEG on disk."""
        from PIL import Image

        with Image.open(img_path) as im:
            im = im.convert("RGB")
            target = (self.dims.width, self.dims.height)
            if im.size != target:
                # Cover-crop then resize for 16:9
                src_w, src_h = im.size
                target_ratio = target[0] / target[1]
                src_ratio = src_w / src_h
                if src_ratio > target_ratio:
                    new_w = int(src_h * target_ratio)
                    left = (src_w - new_w) // 2
                    im = im.crop((left, 0, left + new_w, src_h))
                else:
                    new_h = int(src_w / target_ratio)
                    top = (src_h - new_h) // 2
                    im = im.crop((0, top, src_w, top + new_h))
                im = im.resize(target, Image.Resampling.LANCZOS)
            im.save(img_path, format="JPEG", quality=92)

    def _image_ready(self, img_path: Path) -> bool:
        """True when a still exists on disk and opens as an image (no luma QA)."""
        try:
            if not img_path.exists() or img_path.stat().st_size < int(self.s.image_min_bytes):
                return False
            self._read_dims(img_path)
            return True
        except Exception:  # noqa: BLE001
            return False

    # Back-compat alias for any callers still using the old name
    def _is_valid_image(self, img_path: Path) -> bool:
        return self._image_ready(img_path)

    def _read_dims(self, img_path: Path) -> tuple[int, int]:
        from PIL import Image

        with Image.open(img_path) as im:
            return int(im.size[0]), int(im.size[1])


def synthesize_visuals(
    script_path: Path | str, *, out_dir: Path | str | None = None
) -> VisualResult:
    return VisualModule().synthesize_script(script_path, out_dir=out_dir)
