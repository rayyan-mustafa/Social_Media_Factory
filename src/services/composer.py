"""EditModule — VPS FFmpeg Ken Burns + concat (Plan C Module 4).

inputs:  scene_i.jpg + scene_i.wav  (+ optional scene_i_clip.mp4)
process: per-scene Ken Burns @ 1280x720 → scene_i.mp4 → concat → final.mp4
backend: COMPOSE_BACKEND=vps_ffmpeg only
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.domain.models import (
    EditResult,
    EditValidation,
    Scene,
    SceneClip,
    ScriptResult,
    VisualResult,
    VoiceResult,
)
from src.services.compose_sfx import (
    ComposeSfxError,
    mix_compose_sfx_into_final,
    plan_compose_sfx,
)
from src.services.premium_overlays import (
    PremiumOverlayPlan,
    channel_motion_style,
    plan_premium_overlays,
    render_lower_third_png,
    render_pulse_marker_png,
    resolve_channel,
)
from src.services.settings import ROOT, get_settings
from src.services.visual_modalities import (
    load_script_dict,
    plan_infographic_overlays,
    profile_enabled as modality_profile_enabled,
)

_YEAR_RE = re.compile(r"\b((?:1[0-9]|20)\d{2})\b")
_DRAWTEXT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


class EditModuleError(RuntimeError):
    pass


class EditModule:
    def __init__(self):
        get_settings.cache_clear()
        self.s = get_settings()
        if (self.s.compose_backend or "").lower() != "vps_ffmpeg":
            raise EditModuleError(
                f"COMPOSE_BACKEND must be vps_ffmpeg (got {self.s.compose_backend!r})"
            )
        self._require_bin("ffmpeg")
        self._require_bin("ffprobe")

    def compose(
        self,
        *,
        voice_manifest: Path | str,
        visual_manifest: Path | str,
        out_dir: Path | str | None = None,
        resume: bool = True,
        allow_placeholders: bool | None = None,
        keep_scene_clips: bool | None = None,
    ) -> EditResult:
        voice_path = Path(voice_manifest)
        visual_path = Path(visual_manifest)
        if not voice_path.exists():
            raise EditModuleError(f"voice manifest missing: {voice_path}")
        if not visual_path.exists():
            raise EditModuleError(f"visual manifest missing: {visual_path}")

        voice = VoiceResult.model_validate(
            json.loads(voice_path.read_text(encoding="utf-8"))
        )
        visual = VisualResult.model_validate(
            json.loads(visual_path.read_text(encoding="utf-8"))
        )

        script_scenes: dict[int, Scene] = {}
        sp = Path(voice.script_path) if voice.script_path else None
        script_raw: dict[str, Any] | None = None
        if sp and sp.exists():
            script_raw = load_script_dict(sp)
            script = ScriptResult.model_validate(
                json.loads(sp.read_text(encoding="utf-8"))
            )
            script_scenes = {s.index: s for s in script.scenes}

        allow_ph = (
            self.s.edit_allow_placeholders
            if allow_placeholders is None
            else allow_placeholders
        )
        keep_clips = (
            self.s.keep_scene_clips if keep_scene_clips is None else keep_scene_clips
        )

        if out_dir is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in (voice.title or "job")
            )[:60]
            out_dir = ROOT / "output" / "video" / f"{stamp}_{safe}"
        out_dir = Path(out_dir)
        clips_dir = out_dir / "scenes"
        clips_dir.mkdir(parents=True, exist_ok=True)

        audio_by_idx = {s.index: s for s in voice.scenes}
        image_by_idx = {s.index: s for s in visual.scenes}
        indices = sorted(set(audio_by_idx) & set(image_by_idx))
        if not indices:
            raise EditModuleError("no matching scene indices between voice and visuals")
        missing_a = sorted(set(image_by_idx) - set(audio_by_idx))
        missing_i = sorted(set(audio_by_idx) - set(image_by_idx))
        warnings: list[str] = []
        if missing_a:
            warnings.append(f"scenes missing audio (skipped): {missing_a}")
        if missing_i:
            warnings.append(f"scenes missing images (skipped): {missing_i}")

        # Code 2D infographic + $0 premium documentary chrome (selected pack).
        overlay_by_scene: dict[int, Any] = {}
        overlays_meta: list[dict[str, Any]] = []
        premium_plan: PremiumOverlayPlan | None = None
        premium_on = bool(self.s.compose_premium_overlays_enabled)
        infographic_on = bool(
            self.s.compose_infographic_overlays_enabled
            and modality_profile_enabled("infographic")
        )
        channel = resolve_channel(
            script_raw, fallback=str(self.s.compose_channel_default or "napstorian")
        )
        motion = channel_motion_style(channel)
        # Channel tone nudges Ken Burns for this compose only.
        self._kb_zoom_end = float(motion.zoom_end)
        self._kb_zoom_end_phase_b = float(motion.zoom_end_phase_b)
        try:
            from src.services.editing_overrides import apply_channel_editing_to_composer

            apply_channel_editing_to_composer(self, channel)
        except Exception:  # noqa: BLE001
            pass

        # Optional animated map clips + motion-graphics plates (high-effort P0).
        map_motion_meta: list[dict[str, Any]] = []
        motion_plates_meta: list[dict[str, Any]] = []
        motion_mode_label = "ken_burns"
        try:
            from src.services.editing_overrides import get_merged_channel_editing
            from src.services.map_motion import (
                generate_map_clips_for_job,
                motion_mode_wants_maps,
            )
            from src.services.motion_graphics import (
                motion_mode_wants_graphics,
                plan_motion_plates_for_script,
            )

            editing = get_merged_channel_editing(channel)
            motion_cfg = dict(editing.get("motion") or {})
            motion_mode_label = str(motion_cfg.get("motion_mode") or "ken_burns")
            stills_root = Path(str(getattr(visual, "out_dir", "") or ""))
            if script_raw is not None and motion_mode_wants_maps(motion_cfg):
                # Prefer stills dir so scene_XXX_clip.mp4 is picked by mux path.
                map_out = stills_root if stills_root.is_dir() else (out_dir / "map_motion")
                map_motion_meta = generate_map_clips_for_job(
                    script_raw,
                    out_dir=map_out,
                    max_clips=int(motion_cfg.get("pd_clips_count_max") or 15),
                    duration_s=4.0,
                )
            if script_raw is not None and (
                motion_mode_wants_graphics(motion_cfg)
                or channel in {"napping_historian", "historian", "sleep"}
            ):
                plates_dir = out_dir / "motion_graphics"
                if channel in {"napping_historian", "historian", "sleep"} or motion_mode_wants_graphics(
                    motion_cfg
                ):
                    motion_plates_meta = plan_motion_plates_for_script(
                        script_raw,
                        channel=channel,
                        out_dir=plates_dir,
                    )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"map/motion graphics skipped: {exc}")

        if premium_on and script_raw is not None:
            try:
                est_dur = 0.0
                for _idx in indices:
                    try:
                        est_dur += float(audio_by_idx[_idx].duration_s or 0.0)
                    except (TypeError, ValueError, AttributeError):
                        continue
                min_cards, max_cards, target_interval = self._overlay_card_budget(
                    est_dur
                )
                if (min_cards, max_cards) != (
                    int(self.s.compose_infographic_min_cards),
                    int(self.s.compose_infographic_max_cards),
                ):
                    warnings.append(
                        f"overlay density scaled for {est_dur:.0f}s VO → "
                        f"cards {min_cards}–{max_cards} (interval {target_interval:.0f}s)"
                    )
                premium_plan = plan_premium_overlays(
                    script_raw,
                    out_dir=out_dir / "premium",
                    channel=channel,
                    min_cards=min_cards,
                    max_cards=max_cards,
                    overlay_s=float(self.s.compose_infographic_overlay_s),
                    fog_enabled=bool(self.s.compose_fog_drift_enabled),
                    vignette_enabled=bool(self.s.compose_vignette_enabled),
                    letterbox_enabled=bool(self.s.compose_letterbox_enabled),
                    year_stamp_enabled=bool(self.s.compose_year_stamp_enabled),
                    progress_rail_enabled=bool(self.s.compose_progress_rail_enabled),
                    lower_thirds_enabled=bool(self.s.compose_lower_thirds_enabled),
                    cold_open_enabled=bool(self.s.compose_cold_open_title_enabled),
                    pulse_enabled=bool(self.s.compose_pulse_marker_enabled),
                    scene_indices=indices if indices else None,
                    target_interval_s=target_interval,
                    estimated_duration_s=est_dur or None,
                )
                overlays_meta = list(premium_plan.infographic_overlays or [])
                for ov in overlays_meta:
                    try:
                        overlay_by_scene[int(ov["scene_index"])] = ov
                    except (KeyError, TypeError, ValueError):
                        continue
                channel = premium_plan.channel or channel
                if overlays_meta:
                    warnings.append(
                        f"premium_overlays_v1 planned cards on scenes "
                        f"{sorted(overlay_by_scene)} ({channel})"
                    )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"premium plan skipped: {exc}")
                premium_plan = None

        if (
            not overlays_meta
            and infographic_on
            and script_raw is not None
        ):
            try:
                # Recompute VO length for density (same as premium path).
                est_dur_ig = 0.0
                for _idx in indices:
                    try:
                        est_dur_ig += float(audio_by_idx[_idx].duration_s or 0.0)
                    except (TypeError, ValueError, AttributeError):
                        continue
                min_cards, max_cards, target_interval = self._overlay_card_budget(
                    est_dur_ig
                )
                planned = plan_infographic_overlays(
                    script_raw,
                    out_dir=out_dir / "infographics",
                    min_cards=min_cards,
                    max_cards=max_cards,
                    overlay_s=float(self.s.compose_infographic_overlay_s),
                    target_interval_s=target_interval,
                    estimated_duration_s=est_dur_ig or None,
                    scene_indices=indices if indices else None,
                )
                for ov in planned:
                    overlay_by_scene[int(ov.scene_index)] = ov
                overlays_meta = [ov.to_dict() for ov in planned]
                if overlays_meta:
                    warnings.append(
                        f"infographic overlays planned on scenes "
                        f"{sorted(overlay_by_scene)}"
                    )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"infographic plan skipped: {exc}")

        chrome_by_scene = premium_plan.chrome_by_scene() if premium_plan else {}
        first_idx = indices[0] if indices else None

        scene_clips: list[SceneClip] = []
        w, h = int(self.s.image_width), int(self.s.image_height)

        for pos, idx in enumerate(indices):
            a = audio_by_idx[idx]
            im = image_by_idx[idx]
            scene_meta = script_scenes.get(idx)
            img_p = Path(im.path)
            wav_p = Path(a.path)
            if not img_p.exists():
                raise EditModuleError(f"scene {idx}: image missing {img_p}")
            if not wav_p.exists():
                raise EditModuleError(f"scene {idx}: audio missing {wav_p}")
            if im.placeholder and not allow_ph:
                raise EditModuleError(
                    f"scene {idx}: placeholder/mock still refused "
                    "(set EDIT_ALLOW_PLACEHOLDERS=true for tests)"
                )
            if im.width != w or im.height != h:
                raise EditModuleError(
                    f"scene {idx}: wrong still size {im.width}x{im.height}, need {w}x{h}"
                )

            clip_path = clips_dir / f"scene_{idx:03d}.mp4"
            used_ai = False
            kb = ""
            resumed = False

            if resume and self._is_valid_clip(clip_path, expect_w=w, expect_h=h):
                resumed = True
                kb = "resumed"
            else:
                ai_clip = Path(im.video_path) if im.video_path else None
                # Also accept on-disk PD/open motion inserts next to stills
                if ai_clip is None or not ai_clip.exists():
                    disk_clip = img_p.parent / f"scene_{idx:03d}_clip.mp4"
                    if disk_clip.is_file() and disk_clip.stat().st_size > 1000:
                        ai_clip = disk_clip
                if ai_clip and ai_clip.exists() and ai_clip.stat().st_size > 1000:
                    self._mux_ai_clip(
                        ai_clip, wav_p, clip_path, duration_s=float(a.duration_s)
                    )
                    used_ai = True
                    kb = (
                        "pd_motion_clip"
                        if "pd_motion" in str(ai_clip) or ai_clip.name.endswith("_clip.mp4")
                        else "ai_video_trim"
                    )
                else:
                    hold = self._dramatic_hold_s(scene_meta)
                    phase = (
                        (scene_meta.pacing_phase or "a").lower()
                        if scene_meta
                        else "a"
                    )
                    scene_text = (scene_meta.text if scene_meta else "") or ""
                    kb = self._render_scene_ken_burns(
                        img_p,
                        wav_p,
                        clip_path,
                        clips_dir=clips_dir,
                        scene_index=idx,
                        duration_s=float(a.duration_s) + hold,
                        pacing_phase=phase,
                        scene_text=scene_text,
                    )

            # Burn chapter / quote / map / split cards (works for resumed clips too).
            ov = overlay_by_scene.get(idx)
            if ov is not None:
                if isinstance(ov, dict):
                    card_path = str(ov.get("card_path") or "")
                    ov_title = str(ov.get("title") or "")
                    ov_style = str(ov.get("style") or "")
                    ov_s = float(ov.get("overlay_s") or self.s.compose_infographic_overlay_s)
                else:
                    card_path = str(ov.card_path)
                    ov_title = str(ov.title)
                    ov_style = str(ov.style)
                    ov_s = float(ov.overlay_s)
                marker = clips_dir / f"scene_{idx:03d}.infographic.json"
                need_burn = True
                if resume and marker.is_file():
                    try:
                        prev = json.loads(marker.read_text(encoding="utf-8"))
                        if (
                            prev.get("card_path") == card_path
                            and prev.get("ok")
                            and prev.get("pack") == ("premium_overlays_v1" if premium_on else "infographic")
                        ):
                            need_burn = False
                            kb = f"{kb}+infographic"
                    except (OSError, json.JSONDecodeError):
                        need_burn = True
                if need_burn and card_path:
                    self._burn_infographic_overlay(
                        clip_path,
                        Path(card_path),
                        overlay_s=ov_s,
                        expect_w=w,
                        expect_h=h,
                    )
                    kb = f"{kb}+infographic"
                    marker.write_text(
                        json.dumps(
                            {
                                "ok": True,
                                "scene_index": idx,
                                "card_path": card_path,
                                "title": ov_title,
                                "style": ov_style,
                                "overlay_s": ov_s,
                                "pack": "premium_overlays_v1" if premium_on else "infographic",
                            },
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )

            # Premium chrome: year stamp, progress rail, fog, lower-third, vignette,
            # cold-open title on first scene — applied even when Ken Burns resumed.
            chrome = chrome_by_scene.get(idx)
            if premium_on and chrome is not None:
                p_marker = clips_dir / f"scene_{idx:03d}.premium.json"
                need_premium = True
                chrome_sig = {
                    "year": chrome.year_stamp,
                    "progress": round(float(chrome.progress), 4),
                    "fog": chrome.fog,
                    "lt": chrome.lower_third_name,
                    "cold": bool(
                        first_idx is not None
                        and idx == first_idx
                        and premium_plan
                        and premium_plan.cold_open_path
                    ),
                    "pack": "premium_overlays_v1",
                }
                if resume and p_marker.is_file():
                    try:
                        prev = json.loads(p_marker.read_text(encoding="utf-8"))
                        if prev.get("ok") and prev.get("sig") == chrome_sig:
                            need_premium = False
                            kb = f"{kb}+premium"
                    except (OSError, json.JSONDecodeError):
                        need_premium = True
                if need_premium:
                    self._burn_premium_chrome(
                        clip_path,
                        chrome=chrome,
                        premium_plan=premium_plan,
                        is_cold_open=(first_idx is not None and idx == first_idx),
                        expect_w=w,
                        expect_h=h,
                    )
                    kb = f"{kb}+premium"
                    p_marker.write_text(
                        json.dumps({"ok": True, "scene_index": idx, "sig": chrome_sig}, indent=2)
                        + "\n",
                        encoding="utf-8",
                    )

            if not self._is_valid_clip(clip_path, expect_w=w, expect_h=h):
                raise EditModuleError(f"scene {idx}: clip failed validation {clip_path}")

            scene_clips.append(
                SceneClip(
                    index=idx,
                    image_path=str(img_p.resolve()),
                    audio_path=str(wav_p.resolve()),
                    clip_path=str(clip_path.resolve()),
                    duration_s=self._probe_duration(clip_path),
                    width=w,
                    height=h,
                    ken_burns=kb,
                    used_ai_video=used_ai,
                    skipped=resumed,
                )
            )

        chapter_boundaries = set()
        for pos, idx in enumerate(indices[:-1]):
            cur = script_scenes.get(idx)
            nxt = script_scenes.get(indices[pos + 1])
            if cur and nxt and (cur.chapter_id or 0) != (nxt.chapter_id or 0):
                chapter_boundaries.add(pos)

        final_path = out_dir / "final.mp4"
        self._concat_clips(
            [Path(s.clip_path) for s in scene_clips],
            final_path,
            chapter_boundary_after=chapter_boundaries,
        )

        # Cinematic SFX (napstorian default ON; historian OFF) — before loudnorm.
        sfx_meta: dict[str, Any] = {"compose_sfx": False}
        try:
            sfx_plan = plan_compose_sfx(
                channel=channel,
                scene_clips=scene_clips,
                chapter_boundary_after=chapter_boundaries,
                overlays=overlays_meta,
                settings=self.s,
            )
            sfx_meta = sfx_plan.to_meta()
            if sfx_plan.enabled and sfx_plan.events:
                applied = mix_compose_sfx_into_final(final_path, sfx_plan)
                sfx_meta = {**sfx_meta, **applied}
                if applied.get("applied"):
                    warnings.append(
                        f"compose_sfx applied ({applied.get('event_count', 0)} events, "
                        f"channel={channel})"
                    )
        except ComposeSfxError as exc:
            warnings.append(f"compose_sfx skipped: {exc}")
            sfx_meta = {**sfx_meta, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — never fail final on SFX
            warnings.append(f"compose_sfx skipped: {exc}")
            sfx_meta = {**sfx_meta, "error": str(exc)}

        if self.s.loudness_normalize:
            norm_path = out_dir / "final_loudnorm.mp4"
            self._loudnorm(final_path, norm_path)
            norm_path.replace(final_path)

        probe = self._probe_final(final_path, expect_w=w, expect_h=h)
        errors: list[str] = []
        if not probe["ok"]:
            errors.extend(probe["errors"])

        if not keep_clips:
            for sc in scene_clips:
                Path(sc.clip_path).unlink(missing_ok=True)

        validation = EditValidation(
            ok=not errors and probe["ok"],
            scene_count=len(indices),
            clip_count=len(scene_clips),
            width=w,
            height=h,
            has_audio=bool(probe["has_audio"]),
            duration_s=probe.get("duration_s"),
            warnings=warnings + probe.get("warnings", []),
            errors=errors,
        )
        if not validation.ok:
            raise EditModuleError(
                f"final.mp4 failed gate: {validation.errors}"
            )

        # Belt-and-suspenders: any leftover chapter-xfade tmp next to a good final.
        xfade_leftover = out_dir / "_xfade_tmp"
        if xfade_leftover.is_dir() and final_path.is_file():
            shutil.rmtree(xfade_leftover, ignore_errors=True)

        result = EditResult(
            title=voice.title or visual.title,
            topic=voice.topic or visual.topic,
            voice_manifest=str(voice_path.resolve()),
            visual_manifest=str(visual_path.resolve()),
            out_dir=str(out_dir.resolve()),
            final_path=str(final_path.resolve()),
            backend="vps_ffmpeg",
            scenes=scene_clips,
            validation=validation,
            meta={
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "fps": self.s.compose_fps,
                "preset": self.s.compose_preset,
                "crf": self.s.compose_crf,
                "ken_burns_zoom_end": getattr(
                    self, "_kb_zoom_end", self.s.ken_burns_zoom_end
                ),
                "ken_burns_zoom_end_phase_b": getattr(
                    self, "_kb_zoom_end_phase_b", self.s.ken_burns_zoom_end_phase_b
                ),
                "compose_max_still_s": self.s.compose_max_still_s,
                "compose_subclip_crossfade_s": self.s.compose_subclip_crossfade_s,
                "compose_chapter_crossfade_s": self.s.compose_chapter_crossfade_s,
                "compose_dramatic_hold_s": self.s.compose_dramatic_hold_s,
                "compose_date_overlay_enabled": self.s.compose_date_overlay_enabled,
                "compose_infographic_overlays_enabled": bool(
                    infographic_on or (premium_on and overlays_meta)
                ),
                "infographic_overlays": overlays_meta,
                "compose_premium_overlays_enabled": premium_on,
                "premium_overlays_v1": bool(premium_on and premium_plan is not None),
                "premium_plan": premium_plan.to_dict() if premium_plan else None,
                "compose_sfx": bool(sfx_meta.get("compose_sfx")),
                "compose_sfx_meta": sfx_meta,
                "channel": channel,
                "map_motion_clips": map_motion_meta,
                "motion_graphics_plates": motion_plates_meta,
                "motion_mode": motion_mode_label,
                "selected_pack": True,
                "new_format": bool(overlays_meta)
                or infographic_on
                or bool(premium_on and premium_plan is not None),
                "loudness_normalize": self.s.loudness_normalize,
                "allow_placeholders": allow_ph,
            },
        )
        (out_dir / "edit_manifest.json").write_text(
            json.dumps(result.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return result

    def _ken_burns_direction(self, index: int) -> str:
        # Alternate motion so gallery doesn't feel robotic
        return ("zoom_in_right", "zoom_in_left", "zoom_in_up", "zoom_in_down")[
            index % 4
        ]

    def _burn_infographic_overlay(
        self,
        clip_path: Path,
        card_png: Path,
        *,
        overlay_s: float,
        expect_w: int,
        expect_h: int,
    ) -> None:
        """Composite a PIL card over the first ``overlay_s`` seconds of a scene clip."""
        if not card_png.is_file():
            raise EditModuleError(f"infographic card missing: {card_png}")
        if not clip_path.is_file():
            raise EditModuleError(f"scene clip missing for overlay: {clip_path}")
        dur = self._probe_duration(clip_path)
        burn_s = min(float(overlay_s), max(0.8, float(dur) * 0.85))
        tmp = clip_path.with_suffix(".infographic_tmp.mp4")
        # Full-frame card with soft fade-out via enable window.
        filt = (
            f"[1:v]scale={expect_w}:{expect_h}:force_original_aspect_ratio=decrease,"
            f"pad={expect_w}:{expect_h}:(ow-iw)/2:(oh-ih)/2:color=0x0E1218[ov];"
            f"[0:v][ov]overlay=0:0:enable='between(t,0,{burn_s:.3f})'[v]"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(clip_path),
            "-i",
            str(card_png),
            "-filter_complex",
            filt,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            self.s.compose_preset,
            "-crf",
            str(self.s.compose_crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(tmp),
        ]
        try:
            self._run(cmd, label=f"infographic {clip_path.name}")
        except EditModuleError:
            # Re-encode audio if stream copy fails
            cmd_re = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(clip_path),
                "-i",
                str(card_png),
                "-filter_complex",
                filt,
                "-map",
                "[v]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                self.s.compose_preset,
                "-crf",
                str(self.s.compose_crf),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(tmp),
            ]
            self._run(cmd_re, label=f"infographic-re {clip_path.name}")
        if not self._is_valid_clip(tmp, expect_w=expect_w, expect_h=expect_h):
            tmp.unlink(missing_ok=True)
            raise EditModuleError(f"infographic overlay failed validation: {clip_path}")
        tmp.replace(clip_path)

    def _burn_premium_chrome(
        self,
        clip_path: Path,
        *,
        chrome: Any,
        premium_plan: PremiumOverlayPlan | None,
        is_cold_open: bool,
        expect_w: int,
        expect_h: int,
    ) -> None:
        """Burn year stamp, progress rail, fog, lower-third, vignette, cold-open.

        Runs on resumed Ken Burns clips so stills-ready jobs get the pack
        without a Flux reburn.
        """
        if not clip_path.is_file():
            raise EditModuleError(f"scene clip missing for premium chrome: {clip_path}")
        dur = float(self._probe_duration(clip_path))
        tmp = clip_path.with_suffix(".premium_tmp.mp4")
        font = _DRAWTEXT_FONT if Path(_DRAWTEXT_FONT).exists() else "Sans"

        inputs: list[str] = ["-i", str(clip_path)]
        input_idx = 1
        filter_parts: list[str] = []
        v_label = "0:v"

        # Vignette + optional letterbox
        vf_chain: list[str] = []
        if getattr(chrome, "vignette", True) and self.s.compose_vignette_enabled:
            vf_chain.append("vignette=PI/5")
        if getattr(chrome, "letterbox", False) and self.s.compose_letterbox_enabled:
            bar = 36
            vf_chain.append(
                f"drawbox=x=0:y=0:w=iw:h={bar}:color=black@0.85:t=fill,"
                f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black@0.85:t=fill"
            )
        # Progress / timeline rail
        prog = float(getattr(chrome, "progress", 0) or 0)
        if prog > 0 and self.s.compose_progress_rail_enabled:
            rail_w = max(8, int((expect_w - 80) * min(1.0, max(0.0, prog))))
            vf_chain.append(
                f"drawbox=x=40:y=h-16:w={expect_w - 80}:h=3:color=white@0.25:t=fill,"
                f"drawbox=x=40:y=h-16:w={rail_w}:h=3:color=0xC4A058@0.90:t=fill"
            )
        # Year stamp (corner)
        year = getattr(chrome, "year_stamp", None)
        if year and self.s.compose_year_stamp_enabled:
            escaped = (
                str(year)
                .replace("\\", "\\\\")
                .replace(":", "\\:")
                .replace("'", "\\'")
            )
            vf_chain.append(
                f"drawtext=fontfile={font}:text='{escaped}':"
                f"fontsize=28:fontcolor=white@0.88:"
                f"borderw=2:bordercolor=black@0.55:"
                f"x=w-text_w-36:y=28"
            )

        if vf_chain:
            filter_parts.append(f"[{v_label}]{','.join(vf_chain)}[vbase]")
            v_label = "vbase"
        else:
            filter_parts.append(f"[{v_label}]null[vbase]")
            v_label = "vbase"

        # Fog drift (map / infographic beats)
        fog_png = premium_plan.fog_png if premium_plan else None
        if getattr(chrome, "fog", False) and fog_png and Path(fog_png).is_file():
            inputs.extend(["-loop", "1", "-t", f"{dur:.3f}", "-i", str(fog_png)])
            fi = input_idx
            input_idx += 1
            filter_parts.append(
                f"[{fi}:v]scale={expect_w}:{expect_h},format=rgba,"
                f"colorchannelmixer=aa=0.32[fog];"
                f"[{v_label}][fog]overlay=x='mod(-t*28\\,{expect_w})':y=0:format=auto[vfog]"
            )
            v_label = "vfog"

        # Pulse marker
        if getattr(chrome, "pulse", False) and self.s.compose_pulse_marker_enabled:
            pulse_path = (
                Path(premium_plan.fog_png).parent / f"pulse_s{int(chrome.scene_index):03d}.png"
                if premium_plan and premium_plan.fog_png
                else clip_path.parent / f"pulse_s{int(chrome.scene_index):03d}.png"
            )
            if not pulse_path.is_file():
                render_pulse_marker_png(pulse_path, width=expect_w, height=expect_h)
            inputs.extend(["-loop", "1", "-t", f"{dur:.3f}", "-i", str(pulse_path)])
            pi = input_idx
            input_idx += 1
            # Soft blink ONLY while the infographic/card window is active —
            # never for the whole scene (was leaving dots pulsing after the card).
            pulse_window = float(
                getattr(chrome, "card_overlay_s", None)
                or self.s.compose_infographic_overlay_s
                or 3.5
            )
            pulse_end = min(pulse_window, max(0.8, dur * 0.85), dur)
            filter_parts.append(
                f"[{pi}:v]format=rgba[pul];"
                f"[{v_label}][pul]overlay=0:0:format=auto:"
                f"enable='between(t,0,{pulse_end:.3f})*lt(mod(t\\,1.2)\\,0.55)'[vpul]"
            )
            v_label = "vpul"

        # Lower-third
        lt_name = getattr(chrome, "lower_third_name", None)
        lt_role = getattr(chrome, "lower_third_role", None)
        if lt_name and self.s.compose_lower_thirds_enabled:
            lt_dir = (
                Path(premium_plan.fog_png).parent
                if premium_plan and premium_plan.fog_png
                else clip_path.parent
            )
            lt_path = lt_dir / f"lower_third_s{int(chrome.scene_index):03d}.png"
            if not lt_path.is_file():
                render_lower_third_png(
                    str(lt_name),
                    str(lt_role or ""),
                    out_path=lt_path,
                    width=expect_w,
                    height=expect_h,
                    channel=(premium_plan.channel if premium_plan else "napstorian"),
                )
            inputs.extend(["-i", str(lt_path)])
            li = input_idx
            input_idx += 1
            lt_s = min(3.2, max(1.2, dur * 0.55))
            filter_parts.append(
                f"[{li}:v]format=rgba[lt];"
                f"[{v_label}][lt]overlay=0:0:format=auto:"
                f"enable='between(t,0.15,{lt_s:.3f})'[vlt]"
            )
            v_label = "vlt"

        # Cold-open title (~2s) on first scene
        if (
            is_cold_open
            and premium_plan
            and premium_plan.cold_open_path
            and Path(premium_plan.cold_open_path).is_file()
            and self.s.compose_cold_open_title_enabled
        ):
            inputs.extend(["-i", str(premium_plan.cold_open_path)])
            ci = input_idx
            input_idx += 1
            cold_s = min(float(premium_plan.cold_open_s or 2.0), max(1.0, dur * 0.9))
            filter_parts.append(
                f"[{ci}:v]scale={expect_w}:{expect_h}:force_original_aspect_ratio=decrease,"
                f"pad={expect_w}:{expect_h}:(ow-iw)/2:(oh-ih)/2:color=0x080A0E[cold];"
                f"[{v_label}][cold]overlay=0:0:enable='between(t,0,{cold_s:.3f})'[vcold]"
            )
            v_label = "vcold"

        filter_parts.append(f"[{v_label}]format=yuv420p[vout]")
        filt = ";".join(filter_parts)

        def _cmd(audio_copy: bool) -> list[str]:
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                *inputs,
                "-filter_complex",
                filt,
                "-map",
                "[vout]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                self.s.compose_preset,
                "-crf",
                str(self.s.compose_crf),
                "-pix_fmt",
                "yuv420p",
            ]
            if audio_copy:
                cmd.extend(["-c:a", "copy"])
            else:
                cmd.extend(["-c:a", "aac", "-b:a", "128k"])
            cmd.extend(["-movflags", "+faststart", str(tmp)])
            return cmd

        try:
            self._run(_cmd(True), label=f"premium {clip_path.name}")
        except EditModuleError:
            self._run(_cmd(False), label=f"premium-re {clip_path.name}")
        if not self._is_valid_clip(tmp, expect_w=expect_w, expect_h=expect_h):
            tmp.unlink(missing_ok=True)
            raise EditModuleError(f"premium chrome failed validation: {clip_path}")
        tmp.replace(clip_path)

    def _dramatic_hold_s(self, scene: Scene | None) -> float:
        if scene is None:
            return 0.0
        beat = (scene.beat_type or "").lower()
        text = scene.text or ""
        if beat in ("interrupt", "reengage") or "!" in text or "..." in text:
            return float(self.s.compose_dramatic_hold_s)
        return 0.0

    @staticmethod
    def _extract_year_label(text: str) -> str | None:
        m = _YEAR_RE.search(text or "")
        return m.group(1) if m else None

    def _split_durations(self, total_s: float, max_still_s: float) -> list[float]:
        total = max(0.5, float(total_s))
        max_s = max(1.0, float(max_still_s))
        if total <= max_s:
            return [total]
        n = max(2, int(math.ceil(total / max_s)))
        base = total / n
        parts = [base] * n
        # Keep floating error on last part
        parts[-1] = max(0.5, total - sum(parts[:-1]))
        return parts

    def _render_scene_ken_burns(
        self,
        image: Path,
        audio: Path,
        out: Path,
        *,
        clips_dir: Path,
        scene_index: int,
        duration_s: float,
        pacing_phase: str = "a",
        scene_text: str = "",
    ) -> str:
        """Ken Burns one still; split into alternating subclips if hold > max."""
        max_still = float(self.s.compose_max_still_s)
        parts = self._split_durations(duration_s, max_still)
        year = (
            self._extract_year_label(scene_text)
            if self.s.compose_date_overlay_enabled and duration_s > max_still
            else None
        )
        directions = [
            self._ken_burns_direction(scene_index + i) for i in range(len(parts))
        ]

        if len(parts) == 1:
            self._render_ken_burns(
                image,
                audio,
                out,
                duration_s=parts[0],
                direction=directions[0],
                pacing_phase=pacing_phase,
                date_label=year,
            )
            return directions[0]

        tmp_dir = clips_dir / f"_kb_split_{scene_index:03d}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        part_paths: list[Path] = []
        try:
            for i, (pd, direction) in enumerate(zip(parts, directions)):
                part_path = tmp_dir / f"part_{i:02d}.mp4"
                self._render_ken_burns(
                    image,
                    None,
                    part_path,
                    duration_s=pd,
                    direction=direction,
                    pacing_phase=pacing_phase,
                    date_label=year if i == 0 else None,
                    video_only=True,
                )
                part_paths.append(part_path)

            video_concat = tmp_dir / "video_concat.mp4"
            xf = float(self.s.compose_subclip_crossfade_s)
            if xf > 0 and len(part_paths) > 1:
                try:
                    self._concat_video_xfade_chain(part_paths, video_concat, xf)
                except EditModuleError:
                    self._concat_video_copy(part_paths, video_concat)
            else:
                self._concat_video_copy(part_paths, video_concat)

            self._mux_video_audio(
                video_concat, audio, out, duration_s=float(duration_s)
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return f"split{len(parts)}:" + "+".join(directions)

    def _render_ken_burns(
        self,
        image: Path,
        audio: Path | None,
        out: Path,
        *,
        duration_s: float,
        direction: str,
        pacing_phase: str = "a",
        date_label: str | None = None,
        video_only: bool = False,
    ) -> None:
        fps = int(self.s.compose_fps)
        w, h = int(self.s.image_width), int(self.s.image_height)
        dur = max(0.5, float(duration_s))
        frames = max(1, int(round(dur * fps)))
        z_end = (
            float(getattr(self, "_kb_zoom_end_phase_b", self.s.ken_burns_zoom_end_phase_b))
            if pacing_phase == "b"
            else float(getattr(self, "_kb_zoom_end", self.s.ken_burns_zoom_end))
        )
        # zoom step so we reach ~z_end over `frames`
        z_step = max(0.0001, (z_end - 1.0) / max(frames, 1))

        # Zoom toward a corner/edge so pan is visible
        if direction == "zoom_in_left":
            x_expr = "0"
            y_expr = "ih/2-(ih/zoom/2)"
        elif direction == "zoom_in_right":
            x_expr = "iw-iw/zoom"
            y_expr = "ih/2-(ih/zoom/2)"
        elif direction == "zoom_in_up":
            x_expr = "iw/2-(iw/zoom/2)"
            y_expr = "0"
        else:  # zoom_in_down
            x_expr = "iw/2-(iw/zoom/2)"
            y_expr = "ih-ih/zoom"

        # scale up first so zoompan has room; force exact output size
        vf = (
            f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,"
            f"crop={w * 2}:{h * 2},"
            f"zoompan=z='min(1.0+on*{z_step:.6f},{z_end})':"
            f"x='{x_expr}':y='{y_expr}':d={frames}:s={w}x{h}:fps={fps},"
            f"setsar=1"
        )
        if date_label:
            overlay_s = min(float(self.s.compose_date_overlay_s), dur)
            escaped = (
                str(date_label)
                .replace("\\", "\\\\")
                .replace(":", "\\:")
                .replace("'", "\\'")
            )
            font = _DRAWTEXT_FONT if Path(_DRAWTEXT_FONT).exists() else "Sans"
            vf += (
                f",drawtext=fontfile={font}:text='{escaped}':"
                f"fontsize=36:fontcolor=white@0.92:"
                f"borderw=2:bordercolor=black@0.55:"
                f"x=(w-text_w)/2:y=h-80:"
                f"enable='between(t,0,{overlay_s:.3f})'"
            )

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(image),
        ]
        if not video_only:
            if audio is None:
                raise EditModuleError("ken burns requires audio unless video_only")
            cmd.extend(["-i", str(audio)])
        cmd.extend(
            [
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                self.s.compose_preset,
                "-crf",
                str(self.s.compose_crf),
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if video_only:
            cmd.extend(["-an", "-t", f"{dur:.3f}", str(out)])
        else:
            cmd.extend(
                [
                    "-af",
                    f"apad=whole_dur={dur:.3f}",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-t",
                    f"{dur:.3f}",
                    str(out),
                ]
            )
        self._run(cmd, label=f"kenburns {image.name}")

    def _concat_video_copy(self, clips: list[Path], out: Path) -> None:
        list_file = out.parent / f"{out.stem}_list.txt"
        lines = []
        for c in clips:
            p = str(c.resolve()).replace("'", "'\\''")
            lines.append(f"file '{p}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(out),
        ]
        try:
            self._run(cmd, label="kb-subclip-concat")
        except EditModuleError:
            cmd_re = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:v",
                "libx264",
                "-preset",
                self.s.compose_preset,
                "-crf",
                str(self.s.compose_crf),
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(out),
            ]
            self._run(cmd_re, label="kb-subclip-concat-re")

    def _concat_video_xfade_chain(
        self, clips: list[Path], out: Path, crossfade_s: float
    ) -> None:
        """Tiny xfade between Ken Burns direction changes (video-only)."""
        if len(clips) == 1:
            shutil.copy2(clips[0], out)
            return
        current = clips[0]
        tmp_dir = out.parent / "_xfade_parts"
        tmp_dir.mkdir(exist_ok=True)
        try:
            for i in range(1, len(clips)):
                nxt = clips[i]
                step = tmp_dir / f"xf_{i:04d}.mp4"
                d0 = self._probe_duration(current)
                offset = max(0.0, d0 - crossfade_s)
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(current),
                    "-i",
                    str(nxt),
                    "-filter_complex",
                    (
                        f"[0:v][1:v]xfade=transition=fade:duration={crossfade_s:.3f}:"
                        f"offset={offset:.3f}[v]"
                    ),
                    "-map",
                    "[v]",
                    "-c:v",
                    "libx264",
                    "-preset",
                    self.s.compose_preset,
                    "-crf",
                    str(self.s.compose_crf),
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    str(step),
                ]
                self._run(cmd, label=f"kb-xfade-{i}")
                current = step
            shutil.copy2(current, out)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _mux_video_audio(
        self, video: Path, audio: Path, out: Path, *, duration_s: float
    ) -> None:
        dur = max(0.5, float(duration_s))
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            "-af",
            f"apad=whole_dur={dur:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            f"{dur:.3f}",
            "-movflags",
            "+faststart",
            str(out),
        ]
        self._run(cmd, label=f"mux {video.name}")

    def _overlay_card_budget(
        self, estimated_duration_s: float
    ) -> tuple[int, int, float]:
        """Scale infographic density to actual VO length (short jobs ≠ 14-min pack).

        Returns (min_cards, max_cards, target_interval_s).
        """
        min_c = int(self.s.compose_infographic_min_cards)
        max_c = int(self.s.compose_infographic_max_cards)
        interval = float(
            getattr(self.s, "compose_infographic_target_interval_s", 75.0) or 75.0
        )
        dur = max(0.0, float(estimated_duration_s or 0.0))
        if dur <= 0:
            return min_c, max_c, interval
        # ~1 card per interval, but never spam a short cut.
        by_len = max(1, int(round(dur / max(30.0, interval))))
        if dur < 90:  # ~30–90s samples
            return 1, min(3, max_c), max(interval, dur)
        if dur < 300:  # under ~5 min
            lo = 1
            hi = min(max_c, max(2, by_len + 1))
            return lo, hi, interval
        if dur < 600:
            lo = min(min_c, max(2, by_len))
            hi = min(max_c, max(lo, by_len + 2))
            return lo, hi, interval
        return min_c, max_c, interval

    def _mux_ai_clip(
        self, video: Path, audio: Path, out: Path, *, duration_s: float
    ) -> None:
        """Mux motion/AI clip with full VO length.

        Never use ``-shortest`` — short PD/map clips were chopping narration
        (e.g. 27s WAV → 8s scene). Loop video to cover the audio duration.
        """
        w, h = int(self.s.image_width), int(self.s.image_height)
        dur = max(0.5, float(duration_s))
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},fps={self.s.compose_fps},setsar=1"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-stream_loop",
            "-1",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-vf",
            vf,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            self.s.compose_preset,
            "-crf",
            str(self.s.compose_crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            f"{dur:.3f}",
            "-movflags",
            "+faststart",
            str(out),
        ]
        self._run(cmd, label=f"ai_clip {video.name}")

    def _concat_clips(
        self,
        clips: list[Path],
        final_path: Path,
        *,
        chapter_boundary_after: set[int] | None = None,
    ) -> None:
        if not clips:
            raise EditModuleError("no clips to concat")
        boundaries = chapter_boundary_after or set()
        xf = float(self.s.compose_chapter_crossfade_s)
        if boundaries and xf > 0 and len(clips) > 1:
            try:
                self._concat_with_crossfades(clips, final_path, boundaries, xf)
                return
            except EditModuleError:
                pass  # fallback to demuxer concat
        list_file = final_path.parent / "concat_list.txt"
        lines = []
        for c in clips:
            # ffmpeg concat demuxer needs escaped single quotes
            p = str(c.resolve()).replace("'", "'\\''")
            lines.append(f"file '{p}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(final_path),
        ]
        try:
            self._run(cmd, label="concat")
        except EditModuleError:
            # fallback re-encode if copy fails (timestamp issues)
            cmd_re = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:v",
                "libx264",
                "-preset",
                self.s.compose_preset,
                "-crf",
                str(self.s.compose_crf),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(final_path),
            ]
            self._run(cmd_re, label="concat-reencode")

    def _concat_with_crossfades(
        self,
        clips: list[Path],
        final_path: Path,
        boundaries: set[int],
        crossfade_s: float,
    ) -> None:
        """Chain xfade at chapter boundaries; hard cut elsewhere."""
        if len(clips) == 1:
            shutil.copy2(clips[0], final_path)
            return

        current = clips[0]
        tmp_dir = final_path.parent / "_xfade_tmp"
        tmp_dir.mkdir(exist_ok=True)

        for i in range(1, len(clips)):
            nxt = clips[i]
            out = tmp_dir / f"xf_{i:04d}.mp4"
            use_xfade = (i - 1) in boundaries
            if use_xfade:
                d0 = self._probe_duration(current)
                offset = max(0.0, d0 - crossfade_s)
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(current),
                    "-i",
                    str(nxt),
                    "-filter_complex",
                    (
                        f"[0:v][1:v]xfade=transition=fade:duration={crossfade_s:.3f}:"
                        f"offset={offset:.3f}[v];"
                        f"[0:a][1:a]acrossfade=d={crossfade_s:.3f}[a]"
                    ),
                    "-map",
                    "[v]",
                    "-map",
                    "[a]",
                    "-c:v",
                    "libx264",
                    "-preset",
                    self.s.compose_preset,
                    "-crf",
                    str(self.s.compose_crf),
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    str(out),
                ]
                self._run(cmd, label=f"xfade-{i}")
            else:
                list_file = tmp_dir / f"pair_{i}.txt"
                lines = []
                for p in (current, nxt):
                    esc = str(p.resolve()).replace("'", "'\\''")
                    lines.append(f"file '{esc}'")
                list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_file),
                    "-c",
                    "copy",
                    str(out),
                ]
                try:
                    self._run(cmd, label=f"concat-pair-{i}")
                except EditModuleError:
                    cmd[-2] = "libx264"
                    self._run(cmd, label=f"concat-pair-re-{i}")
            current = out

        shutil.copy2(current, final_path)
        # Biggest historical disk leak: chapter-xfade left multi-GB temps behind.
        # Only wipe after final.mp4 is written; failures keep tmp for resume/debug.
        shutil.rmtree(tmp_dir, ignore_errors=True)

    def _loudnorm(self, src: Path, dst: Path) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(dst),
        ]
        self._run(cmd, label="loudnorm")

    def _is_valid_clip(self, path: Path, *, expect_w: int, expect_h: int) -> bool:
        if not path.exists() or path.stat().st_size < 1000:
            return False
        try:
            info = self._probe_streams(path)
            return (
                info.get("width") == expect_w
                and info.get("height") == expect_h
                and bool(info.get("has_audio"))
            )
        except Exception:  # noqa: BLE001
            return False

    def _probe_final(self, path: Path, *, expect_w: int, expect_h: int) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not path.exists():
            return {"ok": False, "has_audio": False, "errors": ["final.mp4 missing"], "warnings": []}
        info = self._probe_streams(path)
        if info.get("width") != expect_w or info.get("height") != expect_h:
            errors.append(
                f"wrong size {info.get('width')}x{info.get('height')}, need {expect_w}x{expect_h}"
            )
        if not info.get("has_audio"):
            errors.append("no audio stream")
        if not info.get("has_video"):
            errors.append("no video stream")
        dur = info.get("duration_s")
        if dur is not None and dur < 1.0:
            warnings.append(f"very short final duration: {dur:.2f}s")
        return {
            "ok": not errors,
            "has_audio": bool(info.get("has_audio")),
            "duration_s": dur,
            "errors": errors,
            "warnings": warnings,
        }

    def _probe_streams(self, path: Path) -> dict[str, Any]:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height:format=duration",
            "-of",
            "json",
            str(path),
        ]
        raw = subprocess.check_output(cmd, text=True)
        data = json.loads(raw)
        width = height = None
        has_audio = has_video = False
        for st in data.get("streams") or []:
            if st.get("codec_type") == "video":
                has_video = True
                width = int(st.get("width") or 0) or width
                height = int(st.get("height") or 0) or height
            elif st.get("codec_type") == "audio":
                has_audio = True
        dur = None
        try:
            dur = float((data.get("format") or {}).get("duration") or 0) or None
        except (TypeError, ValueError):
            dur = None
        return {
            "width": width,
            "height": height,
            "has_audio": has_audio,
            "has_video": has_video,
            "duration_s": dur,
        }

    def _probe_duration(self, path: Path) -> float:
        info = self._probe_streams(path)
        return float(info.get("duration_s") or 0.0)

    def _run(self, cmd: list[str], *, label: str) -> None:
        def _farm_preexec() -> None:
            # Live RTMP encodes must win CPU on the shared 4-core VPS.
            try:
                os.nice(10)
            except Exception:
                pass

        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                preexec_fn=_farm_preexec,
            )
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "")[-800:]
            raise EditModuleError(f"ffmpeg {label} failed: {err}") from exc

    @staticmethod
    def _require_bin(name: str) -> None:
        if shutil.which(name) is None:
            raise EditModuleError(f"{name} not found on PATH")
