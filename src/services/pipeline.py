"""Pipeline — Script → Voice → Visual → Edit → Publish (Plan C Modules 1–5).

Output: output/jobs/<stamp>_<slug>/video/final.mp4
Optional: private YouTube upload after Gate A (Module 5).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

from src.services.settings import ROOT, get_settings


class PipelineError(RuntimeError):
    pass


@dataclass
class PipelineResult:
    job_dir: Path
    script_path: Path
    voice_manifest: Path
    visual_manifest: Path | None
    final_path: Path | None
    title: str
    topic: str
    scene_count: int
    meta: dict[str, Any]
    publish_manifest: Path | None = None
    video_id: str | None = None
    watch_url: str | None = None
    privacy_status: str | None = None
    stopped_after: str | None = None


StageCallback = Callable[[str], None]


def _slug(text: str, max_len: int = 48) -> str:
    s = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text.strip())
    s = "_".join(p for p in s.split("_") if p)
    return (s or "job")[:max_len]


def _kokoro_python() -> Path:
    p = ROOT / ".venv-kokoro" / "bin" / "python"
    if not p.exists():
        raise PipelineError(
            f"Kokoro venv missing: {p}\n"
            "VoiceModule needs .venv-kokoro (Python 3.12)."
        )
    return p


class Pipeline:
    """Runs Modules 1–5 for one topic/title into a job folder."""

    def run(
        self,
        topic: str,
        *,
        niche_notes: str = "",
        job_dir: Path | None = None,
        mock_images: bool = False,
        test_mode: bool = False,
        allow_placeholders: bool | None = None,
        limit_visual_scenes: int | None = None,
        voice: str | None = None,
        speed: float | None = None,
        publish: bool = True,
        publish_dry_run: bool = False,
        resume: bool = False,
        stop_after_voice: bool = False,
        from_visuals: bool = False,
        channel: str | None = None,
        on_stage: StageCallback | None = None,
    ) -> PipelineResult:
        topic = (topic or "").strip()
        if not topic:
            raise PipelineError("topic/title is required")
        if stop_after_voice and from_visuals:
            raise PipelineError("stop_after_voice and from_visuals are mutually exclusive")

        def _emit(stage: str) -> None:
            if on_stage is None:
                return
            try:
                on_stage(stage)
            except Exception as exc:  # noqa: BLE001
                # Hard SOP stage gates must stop advance; other on_stage errors stay soft.
                from src.agents.smm_sop import SopStageGateError

                if isinstance(exc, SopStageGateError):
                    raise
                pass

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if job_dir is None:
            job_dir = ROOT / "output" / "jobs" / f"{stamp}_{_slug(topic)}"
        job_dir = Path(job_dir)
        script_dir = job_dir / "script"
        audio_dir = job_dir / "audio"
        images_dir = job_dir / "images"
        video_dir = job_dir / "video"
        for d in (script_dir, audio_dir, images_dir, video_dir):
            d.mkdir(parents=True, exist_ok=True)

        # --- optional cheap test pacing (does not rewrite .env file) ---
        prev_env: dict[str, str | None] = {}
        if test_mode:
            overrides = {
                "MIN_SCENES": "1",
                "MAX_SCENES": "12",
                "TARGET_SCENES": "5",
                "TARGET_DURATION_MIN": "1",
            }
            for k, v in overrides.items():
                prev_env[k] = os.environ.get(k)
                os.environ[k] = v
            get_settings.cache_clear()

        if mock_images:
            prev_env.setdefault("IMAGE_BACKEND", os.environ.get("IMAGE_BACKEND"))
            os.environ["IMAGE_BACKEND"] = "mock"
            get_settings.cache_clear()

        if channel:
            os.environ["SCRIPT_CHANNEL"] = channel.strip()

        # Direct CLI / non-farm compose path: still stamp new-format into result meta
        # so every Brand-channel produce honors the every-video SOP.
        from src.agents.smm_sop import stamp_new_format_sop_checklist
        from src.services.youtube_channel_auth import normalize_youtube_channel

        pipe_channel = normalize_youtube_channel(
            channel or os.environ.get("SCRIPT_CHANNEL") or ""
        )
        from src.services.editing_overrides import stamp_editing_directive_meta

        result_meta_seed = stamp_editing_directive_meta(
            stamp_new_format_sop_checklist(
                {"channel": pipe_channel, "source": "pipeline"},
                channel=pipe_channel,
                stage="pipeline_start",
            ),
            pipe_channel,
        )

        try:
            # 1) Script — skip regenerate when resuming / from_visuals
            from src.domain.models import ScriptResult
            from src.services.script import ScriptModule

            if not from_visuals:
                _emit("scripting")
            script_path = script_dir / "script.json"
            if (resume or from_visuals) and script_path.exists():
                script = ScriptResult.model_validate(
                    json.loads(script_path.read_text(encoding="utf-8"))
                )
                narr = script_dir / "narration.txt"
                if not narr.exists():
                    narr.write_text(script.narration_text() + "\n", encoding="utf-8")
            elif from_visuals:
                raise PipelineError(
                    f"from_visuals requires existing script.json: {script_path}"
                )
            else:
                script = ScriptModule(channel=channel).generate(
                    topic,
                    niche_notes=niche_notes,
                    save=True,
                    out_path=script_path,
                )
                (script_dir / "narration.txt").write_text(
                    script.narration_text() + "\n", encoding="utf-8"
                )

            # 2) Voice — skip when Phase B (from_visuals) already has WAVs
            existing_voice = audio_dir / "voice_manifest.json"
            if from_visuals:
                if not existing_voice.exists():
                    raise PipelineError(
                        f"from_visuals requires voice_manifest.json: {existing_voice}"
                    )
                voice_manifest = existing_voice
            else:
                _emit("tts")
                voice_manifest = self._run_voice(
                    script_path, audio_dir, voice=voice, speed=speed
                )

            # 2b) Audio bed (ambient + SFX under voice)
            from src.services.audio_bed import AudioBedModule

            bed = AudioBedModule().mix_voice_manifest(
                voice_manifest,
                script_path=script_path,
            )
            compose_voice_manifest = bed.mixed_manifest

            # Phase A: script + Kokoro (+ bed) only — park before RunPod
            if stop_after_voice:
                result = PipelineResult(
                    job_dir=job_dir,
                    script_path=script_path,
                    voice_manifest=Path(voice_manifest),
                    visual_manifest=None,
                    final_path=None,
                    title=script.title,
                    topic=script.topic,
                    scene_count=len(script.scenes),
                    meta={
                        **result_meta_seed,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "test_mode": test_mode,
                        "mock_images": mock_images,
                        "voice": voice,
                        "speed": speed,
                        "audio_bed": bed.meta,
                        "publish": False,
                        "publish_dry_run": False,
                        "resumed": bool(resume),
                        "stop_after_voice": True,
                        "phase": "prep",
                    },
                    stopped_after="voice",
                )
                self._write_manifest(result)
                return result

            # 3) Visuals — RMagine ladder (both channels):
            # Every scene tries Wiki+Met first (per-scene query distill + vision).
            # PASS → place archival still. REJECT / no candidate → empty → Flux.
            # NO product hero cap. ASSET_FETCHER=0 disables (emergency/debug).
            from src.services.visuals_ai import VisualModule

            _emit("visuals")
            try:
                if str(os.environ.get("ASSET_FETCHER", "1")).strip().lower() in {
                    "1",
                    "true",
                    "on",
                    "yes",
                }:
                    from src.services.rmagine_scene_fetch import (
                        fetch_archival_for_all_scenes,
                    )

                    # Wiki+Met runs on the VPS (HTTP fetch + vision). RunPod is
                    # only for Flux fill of empty/reject slots below.
                    fetch_archival_for_all_scenes(
                        job_dir,
                        settings=get_settings(),
                        skip_if_stamped=True,
                    )
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning(
                    "rmagine Wiki+Met per-scene stage skipped: %s", exc
                )

            # Flux fills only empty slots (resume skips existing scene_XXX.jpg)
            visual = VisualModule().synthesize_script(
                script_path,
                out_dir=images_dir,
                resume=True,
                limit_scenes=limit_visual_scenes,
            )
            visual_manifest = images_dir / "visual_manifest.json"

            # 3b) PD motion inserts — napstorian 10–15; historian soft 4–6 — non-blocking
            try:
                from src.services.pd_clippings import maybe_apply_pd_motion_for_compose

                ch = "napstorian"
                meta = getattr(script, "meta", None)
                if isinstance(meta, dict) and meta.get("channel"):
                    ch = str(meta.get("channel"))
                elif getattr(script, "channel", None):
                    ch = str(script.channel)
                pd_motion = maybe_apply_pd_motion_for_compose(job_dir, channel=ch)
                if pd_motion.get("ok"):
                    import logging

                    logging.getLogger(__name__).info(
                        "pd motion clips applied: %s",
                        pd_motion.get("pd_motion_count"),
                    )
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning(
                    "pd motion prepare skipped: %s", exc
                )

            # 4) Edit — pod already terminated; release gpu_lock for Video B
            from src.services.composer import EditModule

            _emit("edit")
            allow_ph = (
                True
                if mock_images
                else (True if allow_placeholders else None)
            )
            edit = EditModule().compose(
                voice_manifest=compose_voice_manifest,
                visual_manifest=visual_manifest,
                out_dir=video_dir,
                resume=True,
                allow_placeholders=allow_ph,
            )

            # 4b) Thumbnail A/B variants from final cut + hook text (non-blocking)
            try:
                from src.services.thumbnail_generator import generate_thumbnails

                if str(os.environ.get("THUMBNAIL_GENERATOR", "1")).strip().lower() not in {
                    "0",
                    "false",
                    "off",
                    "no",
                }:
                    final_p = Path(edit.final_path)
                    if final_p.is_file():
                        hook_opts = []
                        if getattr(script, "hook", None):
                            words = str(script.hook).split()
                            if words:
                                hook_opts.append(" ".join(words[:5]).upper())
                        if getattr(script, "title", None):
                            hook_opts.append(
                                " ".join(str(script.title).split()[:5]).upper()
                            )
                        thumbs = generate_thumbnails(
                            str(final_p),
                            hook_opts or ["WHAT IF"],
                            str(job_dir / "thumbnails"),
                            num_variants=3,
                            channel=pipe_channel or channel,
                        )
                        if thumbs:
                            (job_dir / "thumbnails" / "variants.json").write_text(
                                json.dumps(thumbs, indent=2),
                                encoding="utf-8",
                            )
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning(
                    "thumbnail_generator skipped: %s", exc
                )

            result = PipelineResult(
                job_dir=job_dir,
                script_path=script_path,
                voice_manifest=Path(voice_manifest),
                visual_manifest=Path(visual_manifest),
                final_path=Path(edit.final_path),
                title=script.title,
                topic=script.topic,
                scene_count=len(script.scenes),
                meta={
                    **result_meta_seed,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "test_mode": test_mode,
                    "mock_images": mock_images,
                    "limit_visual_scenes": limit_visual_scenes,
                    "image_backend": visual.backend,
                    "voice": voice,
                    "speed": speed,
                    "audio_bed": bed.meta,
                    "publish": publish,
                    "publish_dry_run": publish_dry_run,
                    "resumed": bool(resume),
                    "from_visuals": bool(from_visuals),
                    "phase": "visuals" if from_visuals else "full",
                },
            )

            # End-of-compose SOP audit (+ soft/hard gate before package/publish).
            # Soft by default (sop_stage_gates_hard=false); always writes ops/SOP_COMPLIANCE.md.
            try:
                from src.agents.smm_sop import (
                    SopStageGateError,
                    audit_stage_complete,
                    gate_stage_advance,
                    job_from_dir,
                    sop_stage_gates_enabled,
                    sop_stage_gates_hard_enabled,
                    stamp_new_format_sop_checklist,
                )

                if sop_stage_gates_enabled():
                    gate_meta = stamp_new_format_sop_checklist(
                        {
                            **result_meta_seed,
                            "channel": pipe_channel or channel,
                            "title": script.title,
                        },
                        channel=pipe_channel or channel,
                        stage="compose_done",
                    )
                    gate_job = job_from_dir(
                        job_dir,
                        channel=pipe_channel or channel,
                        meta=gate_meta,
                        status="farming",
                    )
                    compose_audit = audit_stage_complete(
                        gate_job, completed_stage="compose", hard=False
                    )
                    result.meta["sop_compose_audit"] = {
                        "ok": compose_audit.get("ok"),
                        "fingerprint": compose_audit.get("fingerprint"),
                        "resend_stage": (compose_audit.get("remediation") or {}).get(
                            "resend_stage"
                        ),
                        "compliance_path": compose_audit.get("compliance_path"),
                    }
                    if publish or publish_dry_run:
                        # Primary wired gate: compose → package/publish
                        gate_stage_advance(
                            gate_job,
                            completed_stage="compose",
                            next_stage="package",
                            hard=sop_stage_gates_hard_enabled(),
                            write_compliance=True,
                        )
            except SopStageGateError:
                raise
            except Exception as sop_exc:  # noqa: BLE001
                result.meta["sop_compose_audit_error"] = str(sop_exc)

            # 4b) YouTube packaging — title/description/tags + Seedream thumbnail
            if publish or publish_dry_run:
                from src.services.youtube_meta_generate import (
                    YoutubeMetaGenerateError,
                    generate_youtube_pack,
                )

                try:
                    pack = generate_youtube_pack(
                        job_dir,
                        script_path=script_path,
                        seed_title=result.title,
                        skip_thumbnail_image=bool(mock_images),
                        force=False,
                        channel=channel,
                    )
                except YoutubeMetaGenerateError as exc:
                    raise PipelineError(
                        f"YouTube meta/thumbnail generation failed:\n{exc}"
                    ) from exc
                result.meta["youtube_meta_dir"] = str(pack.meta_dir)
                result.meta["youtube_meta_warnings"] = list(pack.warnings or [])
                thumb_path = Path(pack.thumbnail_path) if pack.thumbnail_path else None
                if thumb_path is not None and not thumb_path.exists():
                    thumb_path = None
                if thumb_path is None and not mock_images:
                    # Soft-fail: still publish private video; YouTube keeps auto thumb.
                    warn = "youtube_meta/thumbnail.jpg missing — uploading without custom thumbnail"
                    result.meta.setdefault("youtube_meta_warnings", []).append(warn)
                result.meta["thumbnail_path"] = (
                    str(thumb_path.resolve()) if thumb_path else None
                )

                # Package-stage SOP audit before publish advance (soft unless hard flag).
                try:
                    from src.agents.smm_sop import (
                        SopStageGateError,
                        audit_stage_complete,
                        job_from_dir,
                        sop_stage_gates_enabled,
                        sop_stage_gates_hard_enabled,
                        stamp_new_format_sop_checklist,
                    )

                    if sop_stage_gates_enabled():
                        pkg_meta = stamp_new_format_sop_checklist(
                            {
                                **result_meta_seed,
                                "channel": pipe_channel or channel,
                                "title": result.title,
                                "publish_dry_run": bool(publish_dry_run or not publish),
                            },
                            channel=pipe_channel or channel,
                            stage="package_done",
                        )
                        pkg_job = job_from_dir(
                            job_dir,
                            channel=pipe_channel or channel,
                            meta=pkg_meta,
                            status="farming",
                        )
                        pkg_audit = audit_stage_complete(
                            pkg_job,
                            completed_stage="package",
                            hard=sop_stage_gates_hard_enabled(),
                        )
                        result.meta["sop_package_audit"] = {
                            "ok": pkg_audit.get("ok"),
                            "fingerprint": pkg_audit.get("fingerprint"),
                            "resend_stage": (pkg_audit.get("remediation") or {}).get(
                                "resend_stage"
                            ),
                            "compliance_path": pkg_audit.get("compliance_path"),
                        }
                except SopStageGateError:
                    raise
                except Exception as pkg_exc:  # noqa: BLE001
                    result.meta["sop_package_audit_error"] = str(pkg_exc)

            # 5) Publish — Gate A + private YouTube (Plan C Module 5)
            if publish or publish_dry_run:
                from src.services.publish_youtube import (
                    PublishModule,
                    PublishModuleError,
                )

                _emit("publish")
                try:
                    pub = PublishModule(channel=channel).publish_private(
                        final_path=result.final_path,
                        title=result.title,
                        script_path=script_path,
                        visual_manifest=visual_manifest,
                        job_dir=job_dir,
                        channel=channel,
                        dry_run=publish_dry_run or not publish,
                        # test / short clips skip 7–12 min band
                        allow_short=test_mode,
                        allow_placeholders=bool(mock_images or allow_placeholders),
                    )
                except PublishModuleError as exc:
                    raise PipelineError(f"PublishModule failed:\n{exc}") from exc

                result.publish_manifest = (
                    Path(pub.publish_manifest_path)
                    if pub.publish_manifest_path
                    else job_dir / "publish_manifest.json"
                )
                result.video_id = pub.video_id
                result.watch_url = pub.watch_url
                result.privacy_status = pub.privacy_status
                result.meta["gate_a_ok"] = pub.gate_a.ok
                result.meta["publish_dry_run"] = pub.dry_run
                result.meta["ai_disclosure"] = pub.ai_disclosure
                result.meta["thumbnail_uploaded"] = bool(
                    (pub.meta or {}).get("thumbnail_uploaded")
                )

            self._write_manifest(result)
            return result
        finally:
            for k, old in prev_env.items():
                if old is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old
            if prev_env:
                get_settings.cache_clear()

    def _write_manifest(self, result: PipelineResult) -> None:
        payload: dict[str, Any] = {
            "job_dir": str(result.job_dir),
            "title": result.title,
            "topic": result.topic,
            "scene_count": result.scene_count,
            "script_path": str(result.script_path),
            "voice_manifest": str(result.voice_manifest),
            "visual_manifest": str(result.visual_manifest)
            if result.visual_manifest
            else None,
            "final_path": str(result.final_path) if result.final_path else None,
            "publish_manifest": str(result.publish_manifest)
            if result.publish_manifest
            else None,
            "video_id": result.video_id,
            "watch_url": result.watch_url,
            "privacy_status": result.privacy_status,
            "stopped_after": result.stopped_after,
            "meta": result.meta,
        }
        (result.job_dir / "pipeline_manifest.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _run_voice(
        self,
        script_path: Path,
        audio_dir: Path,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> Path:
        py = _kokoro_python()
        cmd = [
            str(py),
            "-m",
            "src.cli.generate_voice",
            str(script_path),
            "--out-dir",
            str(audio_dir),
        ]
        if voice:
            cmd.extend(["--voice", voice])
        if speed is not None:
            cmd.extend(["--speed", str(speed)])
        try:
            subprocess.run(
                cmd,
                cwd=str(ROOT),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "")[-1200:]
            raise PipelineError(f"VoiceModule failed:\n{err}") from exc
        manifest = audio_dir / "voice_manifest.json"
        if not manifest.exists():
            raise PipelineError(f"voice_manifest.json missing after TTS: {manifest}")
        return manifest
