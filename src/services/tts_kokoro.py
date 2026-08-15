"""VoiceModule — TTS (Kokoro local by default; optional RunPod CosyVoice).

Modes:
  - scene (legacy): one short WAV per sentence/scene
  - hybrid: TTS per chapter (or ~60–90s chunks), inner sentence-only
    splits under the 510-phoneme cap with trim=False, then map audio
    back to scene WAVs by word-time proportion

Default: Kokoro on VPS ($0). Opt-in GPU: TTS_BACKEND=cosyvoice_runpod
(ephemeral RTX 2000 Ada CosyVoice pod — create → synthesize → terminate).
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import numpy as np
import soundfile as sf

from src.domain.models import (
    Scene,
    SceneAudio,
    ScriptResult,
    VoiceResult,
    VoiceValidation,
)
from src.services.settings import ROOT, get_settings
from src.services.tts_chunking import (
    DEFAULT_MAX_PHONEMES,
    estimate_phoneme_count,
    group_scenes_into_chapter_chunks,
    pack_sentences_by_phoneme_limit,
)
from src.services.tts_sanitize import sanitize_scene_text
from src.services.voice_prosody import (
    append_silence_to_wav,
    prosody_for_scene,
    strip_ellipsis_for_tts,
)

logger = logging.getLogger(__name__)

# Tiny silence between inner phoneme batches (matches prosody apad style).
INNER_BATCH_PAD_MS = 80

# Lazy import so Module 1 CLI still works on the py3.14 venv without kokoro
_Kokoro = None


def _load_kokoro_cls():
    global _Kokoro
    if _Kokoro is None:
        try:
            from kokoro_onnx import Kokoro as _K
        except ImportError as exc:
            raise RuntimeError(
                "kokoro-onnx is not installed in this Python. "
                "Use: .venv-kokoro/bin/python -m src.cli.generate_voice ..."
            ) from exc
        _Kokoro = _K
    return _Kokoro


class VoiceModuleError(RuntimeError):
    pass


class VoiceValidationError(RuntimeError):
    def __init__(self, result: VoiceResult):
        self.result = result
        msg = "; ".join(result.validation.errors) or "voice validation failed"
        super().__init__(msg)


class VoiceModule:
    """Synthesize script audio (Kokoro local, or CosyVoice on ephemeral RunPod)."""

    def __init__(
        self,
        *,
        voice: str | None = None,
        speed: float | None = None,
        lang: str | None = None,
        model_path: Path | None = None,
        voices_path: Path | None = None,
    ):
        s = get_settings()
        self.voice = voice or s.kokoro_voice
        self.speed = float(speed if speed is not None else s.kokoro_speed)
        self.lang = lang or s.kokoro_lang
        self.model_path = _resolve_path(model_path or s.kokoro_model_path)
        self.voices_path = _resolve_path(voices_path or s.kokoro_voices_path)
        self.target_seconds = float(s.target_seconds_per_scene)
        self.min_scene_s = float(s.voice_min_scene_seconds)
        self.max_scene_s = float(s.voice_max_scene_seconds)
        self._engine = None
        self._gpu_session = None  # EphemeralVoiceSession when cosyvoice/fish runpod

    @staticmethod
    def _tts_backend() -> str:
        return (get_settings().tts_backend or "kokoro").strip().lower()

    def _is_gpu_voice(self) -> bool:
        return self._tts_backend() in {"cosyvoice_runpod", "fish_runpod"}

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        if not self.model_path.exists():
            raise VoiceModuleError(f"Kokoro model missing: {self.model_path}")
        if not self.voices_path.exists():
            raise VoiceModuleError(f"Kokoro voices missing: {self.voices_path}")
        Kokoro = _load_kokoro_cls()
        self._engine = Kokoro(str(self.model_path), str(self.voices_path))
        return self._engine

    def synthesize_script(
        self,
        script_path: Path,
        *,
        out_dir: Path | None = None,
        resume: bool = True,
        save_manifest: bool = True,
        mode: Literal["scene", "hybrid"] = "scene",
        target_chunk_words: int | None = None,
        max_phonemes: int = DEFAULT_MAX_PHONEMES,
    ) -> VoiceResult:
        script_path = Path(script_path)
        if not script_path.exists():
            raise VoiceModuleError(f"Script JSON not found: {script_path}")

        raw = json.loads(script_path.read_text(encoding="utf-8"))
        script = ScriptResult.model_validate(raw)
        if not script.scenes:
            raise VoiceModuleError("Script has zero scenes")

        if out_dir is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in (script.topic or "job")
            )[:60]
            out_dir = ROOT / "output" / "audio" / f"{stamp}_{safe}"
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if mode == "hybrid" and self._is_gpu_voice():
            logger.warning(
                "tts_backend=%s does not support hybrid mode yet — using scene mode",
                self._tts_backend(),
            )
            mode = "scene"

        if self._is_gpu_voice():
            from src.services.tts_cosyvoice_runpod import EphemeralVoiceSession

            with EphemeralVoiceSession() as sess:
                self._gpu_session = sess
                try:
                    return self._synthesize_script_body(
                        script,
                        script_path=script_path,
                        out_dir=out_dir,
                        resume=resume,
                        save_manifest=save_manifest,
                        mode=mode,
                        target_chunk_words=target_chunk_words,
                        max_phonemes=max_phonemes,
                        gpu_pod_id=sess.pod_id,
                    )
                finally:
                    self._gpu_session = None

        return self._synthesize_script_body(
            script,
            script_path=script_path,
            out_dir=out_dir,
            resume=resume,
            save_manifest=save_manifest,
            mode=mode,
            target_chunk_words=target_chunk_words,
            max_phonemes=max_phonemes,
            gpu_pod_id=None,
        )

    def _synthesize_script_body(
        self,
        script: ScriptResult,
        *,
        script_path: Path,
        out_dir: Path,
        resume: bool,
        save_manifest: bool,
        mode: Literal["scene", "hybrid"],
        target_chunk_words: int | None,
        max_phonemes: int,
        gpu_pod_id: str | None,
    ) -> VoiceResult:
        if mode == "hybrid":
            return self._synthesize_script_hybrid(
                script,
                script_path=script_path,
                out_dir=out_dir,
                resume=resume,
                save_manifest=save_manifest,
                target_chunk_words=target_chunk_words,
                max_phonemes=max_phonemes,
            )

        scene_audios: list[SceneAudio] = []
        scenes = script.scenes
        meta = script.meta if isinstance(script.meta, dict) else {}
        channel = meta.get("channel") if isinstance(meta.get("channel"), str) else None
        cast_voices_used: dict[str, str] = {}
        for i, scene in enumerate(scenes):
            next_ch = scenes[i + 1].chapter_id if i + 1 < len(scenes) else None
            scene_audios.append(
                self._synthesize_scene(
                    scene,
                    out_dir=out_dir,
                    resume=resume,
                    next_chapter_id=next_ch,
                    channel=channel,
                    script_meta=meta,
                    cast_voices_used=cast_voices_used,
                )
            )

        backend_label = {
            "kokoro": "kokoro_onnx_vps",
            "elevenlabs": "elevenlabs",
            "cosyvoice_runpod": "cosyvoice_runpod_ephemeral",
            "fish_runpod": "fish_runpod_ephemeral",
        }.get(self._tts_backend(), self._tts_backend())

        validation = self.validate(scene_audios, expected_count=len(script.scenes))
        result = VoiceResult(
            script_path=str(script_path.resolve()),
            title=script.title,
            topic=script.topic,
            voice=self.voice,
            speed=self.speed,
            lang=self.lang,
            out_dir=str(out_dir.resolve()),
            scenes=scene_audios,
            validation=validation,
            meta={
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "backend": backend_label,
                "tts_backend": self._tts_backend(),
                "voice_profile": {"voice": self.voice, "speed": self.speed},
                "model_path": str(self.model_path),
                "resumed": resume,
                "prosody": True,
                "tts_mode": "scene",
                "ephemeral_pod_id": gpu_pod_id,
                "voice_cast_used": cast_voices_used,
                "voice_mode": meta.get("voice_mode")
                or (meta.get("editing_directive") or {}).get("voice_mode"),
            },
        )

        if save_manifest:
            manifest = out_dir / "voice_manifest.json"
            manifest.write_text(
                json.dumps(result.model_dump(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            result.meta["manifest"] = str(manifest)

        if not validation.ok:
            raise VoiceValidationError(result)
        return result

    def _synthesize_script_hybrid(
        self,
        script: ScriptResult,
        *,
        script_path: Path,
        out_dir: Path,
        resume: bool,
        save_manifest: bool,
        target_chunk_words: int | None,
        max_phonemes: int,
    ) -> VoiceResult:
        """Chapter (or ~60–90s) TTS + sentence-only inner packs + scene slice."""
        scenes_by_index = {int(s.index): s for s in script.scenes}
        # Sanitize scene texts in place for packing / slicing labels.
        cleaned_scenes: list[Scene] = []
        for scene in script.scenes:
            text, warnings = sanitize_scene_text((scene.text or "").strip())
            for w in warnings:
                logger.warning("scene %d TTS sanitize: %s", scene.index, w)
            text = strip_ellipsis_for_tts(text) if text else ""
            cleaned_scenes.append(scene.model_copy(update={"text": text}))

        chunks = group_scenes_into_chapter_chunks(
            cleaned_scenes,
            target_chunk_words=target_chunk_words,
        )
        if not chunks:
            raise VoiceModuleError("Hybrid TTS: no chapter chunks")

        chapters_dir = out_dir / "chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)

        engine = self._ensure_engine()
        chapter_meta: list[dict[str, Any]] = []
        scene_audios: list[SceneAudio] = []
        total_inner_splits = 0

        for chunk in chunks:
            label = f"ch{chunk.chapter_id:02d}_{chunk.chunk_index:02d}"
            chapter_wav = chapters_dir / f"{label}.wav"
            batches = pack_sentences_by_phoneme_limit(
                chunk.text,
                max_phonemes=max_phonemes,
                lang=self.lang,
            )
            if not batches:
                raise VoiceModuleError(f"Chapter chunk {label} has empty text")

            inner_split = len(batches) > 1
            if inner_split:
                total_inner_splits += len(batches) - 1
                logger.info(
                    "hybrid %s: %d phoneme batches (cap=%d)",
                    label,
                    len(batches),
                    max_phonemes,
                )

            phoneme_counts = [
                estimate_phoneme_count(b, lang=self.lang) for b in batches
            ]

            if resume and chapter_wav.exists() and chapter_wav.stat().st_size > 44:
                samples, sample_rate = sf.read(str(chapter_wav), dtype="float32")
                if getattr(samples, "ndim", 1) > 1:
                    samples = samples.mean(axis=1)
                samples = np.asarray(samples, dtype=np.float32)
                skipped_synth = True
            else:
                skipped_synth = False
                parts: list[np.ndarray] = []
                sample_rate: int | None = None
                for bi, batch_text in enumerate(batches):
                    # Fixed voice+speed for continuity inside the chapter chunk.
                    audio, sr = engine.create(
                        batch_text,
                        voice=self.voice,
                        speed=self.speed,
                        lang=self.lang,
                        trim=False,
                    )
                    audio = np.asarray(audio, dtype=np.float32)
                    if audio.size == 0:
                        raise VoiceModuleError(
                            f"Chapter {label} batch {bi} returned empty audio"
                        )
                    if sample_rate is None:
                        sample_rate = int(sr)
                    elif int(sr) != sample_rate:
                        raise VoiceModuleError(
                            f"sample rate mismatch in {label}: {sr} vs {sample_rate}"
                        )
                    parts.append(audio)
                    if bi < len(batches) - 1 and INNER_BATCH_PAD_MS > 0:
                        pad_n = int(sample_rate * (INNER_BATCH_PAD_MS / 1000.0))
                        if pad_n > 0:
                            parts.append(np.zeros(pad_n, dtype=np.float32))
                assert sample_rate is not None
                samples = np.concatenate(parts)
                sf.write(str(chapter_wav), samples, int(sample_rate))

            duration_s = float(samples.shape[0]) / float(sample_rate)
            chapter_meta.append(
                {
                    "label": label,
                    "chapter_id": chunk.chapter_id,
                    "chunk_index": chunk.chunk_index,
                    "path": str(chapter_wav.resolve()),
                    "duration_s": duration_s,
                    "scene_indices": list(chunk.scene_indices),
                    "inner_batches": len(batches),
                    "inner_split": inner_split,
                    "phoneme_counts": phoneme_counts,
                    "skipped": skipped_synth,
                }
            )

            # Map chapter audio → scene WAVs by word-count time proportion.
            chunk_scenes = [
                scenes_by_index[i]
                for i in chunk.scene_indices
                if i in scenes_by_index
            ]
            # Prefer cleaned text for proportion + labels.
            cleaned_by_index = {int(s.index): s for s in cleaned_scenes}
            weights: list[float] = []
            for sc in chunk_scenes:
                cs = cleaned_by_index.get(int(sc.index), sc)
                wc = int(cs.word_count or 0)
                if wc <= 0:
                    wc = max(1, len((cs.text or "").split()))
                weights.append(float(wc))
            total_w = sum(weights) or float(len(weights)) or 1.0
            cursor = 0
            n_samples = int(samples.shape[0])
            for si, sc in enumerate(chunk_scenes):
                cs = cleaned_by_index.get(int(sc.index), sc)
                if si == len(chunk_scenes) - 1:
                    end = n_samples
                else:
                    share = weights[si] / total_w
                    end = min(n_samples, cursor + int(round(share * n_samples)))
                    end = max(end, cursor + 1)
                slice_audio = samples[cursor:end]
                cursor = end
                wav_path = out_dir / f"scene_{int(sc.index):03d}.wav"
                sf.write(str(wav_path), slice_audio, int(sample_rate))
                scene_dur = (
                    float(slice_audio.shape[0]) / float(sample_rate)
                    if sample_rate
                    else 0.0
                )
                scene_audios.append(
                    SceneAudio(
                        index=int(sc.index),
                        text=(cs.text or sc.text or ""),
                        path=str(wav_path.resolve()),
                        duration_s=scene_dur,
                        sample_rate=int(sample_rate),
                        skipped=skipped_synth,
                    )
                )

        scene_audios.sort(key=lambda s: s.index)
        validation = self.validate(scene_audios, expected_count=len(script.scenes))
        result = VoiceResult(
            script_path=str(script_path.resolve()),
            title=script.title,
            topic=script.topic,
            voice=self.voice,
            speed=self.speed,
            lang=self.lang,
            out_dir=str(out_dir.resolve()),
            scenes=scene_audios,
            validation=validation,
            meta={
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "backend": "kokoro_onnx_vps",
                "tts_backend": get_settings().tts_backend,
                "voice_profile": {"voice": self.voice, "speed": self.speed},
                "model_path": str(self.model_path),
                "resumed": resume,
                "prosody": False,
                "tts_mode": "hybrid",
                "chapters": chapter_meta,
                "chapter_count": len(chapter_meta),
                "inner_batch_pad_ms": INNER_BATCH_PAD_MS,
                "max_phonemes": max_phonemes,
                "target_chunk_words": target_chunk_words,
                "inner_splits_fired": total_inner_splits > 0,
                "inner_extra_batches": total_inner_splits,
            },
        )

        chapters_manifest = chapters_dir / "chapters_manifest.json"
        chapters_manifest.write_text(
            json.dumps(
                {
                    "tts_mode": "hybrid",
                    "chapters": chapter_meta,
                    "inner_batch_pad_ms": INNER_BATCH_PAD_MS,
                    "max_phonemes": max_phonemes,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        result.meta["chapters_manifest"] = str(chapters_manifest.resolve())

        if save_manifest:
            manifest = out_dir / "voice_manifest.json"
            manifest.write_text(
                json.dumps(result.model_dump(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            result.meta["manifest"] = str(manifest)

        if not validation.ok:
            raise VoiceValidationError(result)
        return result

    def _synthesize_scene(
        self,
        scene: Scene,
        *,
        out_dir: Path,
        resume: bool,
        next_chapter_id: int | None = None,
        channel: str | None = None,
        script_meta: dict[str, Any] | None = None,
        cast_voices_used: dict[str, str] | None = None,
    ) -> SceneAudio:
        wav_path = out_dir / f"scene_{scene.index:03d}.wav"
        raw_text = (scene.text or "").strip()
        text, sanitize_warnings = sanitize_scene_text(raw_text)
        for w in sanitize_warnings:
            logger.warning("scene %d TTS sanitize: %s", scene.index, w)
        if not text:
            raise VoiceModuleError(
                f"Scene {scene.index} has empty text after TTS sanitization"
            )
        text = strip_ellipsis_for_tts(text)
        prosody = prosody_for_scene(
            scene,
            base_speed=self.speed,
            next_chapter_id=next_chapter_id,
            chapter_end_pause_ms=int(get_settings().compose_chapter_end_pause_ms),
        )

        scene_voice = self.voice
        try:
            from src.services.voice_cast import voice_for_scene

            resolved = voice_for_scene(
                scene, channel=channel, meta=script_meta or {}
            )
            if resolved.get("voice_id"):
                scene_voice = str(resolved["voice_id"])
            if cast_voices_used is not None:
                cast_voices_used[str(resolved.get("role") or "narrator")] = scene_voice
            if resolved.get("backend_hint") == "human_wav" and resolved.get("human_wav"):
                logger.info(
                    "scene %d human_wav present — Kokoro fallback voice=%s until clone path",
                    scene.index,
                    scene_voice,
                )
        except Exception:  # noqa: BLE001
            logger.debug("voice_cast resolve failed", exc_info=True)

        if resume and wav_path.exists() and wav_path.stat().st_size > 44:
            duration_s, sample_rate = _wav_meta(wav_path)
            return SceneAudio(
                index=scene.index,
                text=text,
                path=str(wav_path.resolve()),
                duration_s=duration_s,
                sample_rate=sample_rate,
                skipped=True,
            )

        backend = self._tts_backend()
        if backend == "elevenlabs":
            from src.services.tts_elevenlabs import synthesize_scene as el_syn

            el_syn(scene, out_path=wav_path)
            duration_s, sample_rate = _wav_meta(wav_path)
            return SceneAudio(
                index=scene.index,
                text=text,
                path=str(wav_path.resolve()),
                duration_s=duration_s,
                sample_rate=sample_rate,
                skipped=False,
            )

        if backend in {"cosyvoice_runpod", "fish_runpod"}:
            if self._gpu_session is None:
                raise VoiceModuleError(
                    f"{backend} requires an active EphemeralVoiceSession "
                    "(opened by synthesize_script)"
                )
            self._gpu_session.synthesize_to_path(text, wav_path)
            if prosody.pause_after_ms > 0:
                append_silence_to_wav(wav_path, pause_ms=prosody.pause_after_ms)
            duration_s, sample_rate = _wav_meta(wav_path)
            return SceneAudio(
                index=scene.index,
                text=text,
                path=str(wav_path.resolve()),
                duration_s=duration_s,
                sample_rate=sample_rate,
                skipped=False,
            )

        engine = self._ensure_engine()
        samples, sample_rate = engine.create(
            text,
            voice=scene_voice,
            speed=prosody.speed,
            lang=self.lang,
            trim=False,
        )
        samples = np.asarray(samples, dtype=np.float32)
        if samples.size == 0:
            raise VoiceModuleError(f"Scene {scene.index} TTS returned empty audio")

        sf.write(str(wav_path), samples, int(sample_rate))
        if prosody.pause_after_ms > 0:
            append_silence_to_wav(wav_path, pause_ms=prosody.pause_after_ms)
        duration_s, sample_rate = _wav_meta(wav_path)
        return SceneAudio(
            index=scene.index,
            text=text,
            path=str(wav_path.resolve()),
            duration_s=duration_s,
            sample_rate=int(sample_rate),
            skipped=False,
        )

    def validate(
        self, scenes: list[SceneAudio], *, expected_count: int
    ) -> VoiceValidation:
        n = len(scenes)
        durs = [s.duration_s for s in scenes]
        warnings: list[str] = []
        errors: list[str] = []

        if n != expected_count:
            errors.append(f"wav_count={n} != scene_count={expected_count}")

        missing = [s.index for s in scenes if not Path(s.path).exists()]
        if missing:
            errors.append(f"missing wav files for scenes: {missing[:10]}")

        empty = [s.index for s in scenes if s.duration_s <= 0.05]
        if empty:
            errors.append(f"near-empty audio for scenes: {empty[:10]}")

        median = float(statistics.median(durs)) if durs else None
        mn = float(min(durs)) if durs else None
        mx = float(max(durs)) if durs else None
        total = float(sum(durs)) if durs else 0.0

        if median is not None and (
            median < self.min_scene_s or median > self.max_scene_s
        ):
            warnings.append(
                f"median duration {median:.2f}s outside configured band "
                f"{self.min_scene_s:.0f}–{self.max_scene_s:.0f}s "
                f"(dual pacing: Phase A ~3s, Phase B ~60–120s; tweak KOKORO_SPEED)"
            )

        short = sum(1 for d in durs if d < 1.0)
        long = sum(1 for d in durs if d > 45.0)
        if short:
            warnings.append(f"{short} scenes shorter than 1.0s")
        if long:
            warnings.append(f"{long} scenes longer than 45.0s")

        return VoiceValidation(
            ok=not errors,
            scene_count=expected_count,
            wav_count=n,
            total_duration_s=total,
            median_duration_s=median,
            min_duration_s=mn,
            max_duration_s=mx,
            target_seconds_per_scene=self.target_seconds,
            warnings=warnings,
            errors=errors,
        )


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (ROOT / p).resolve()


def _wav_meta(path: Path) -> tuple[float, int]:
    info = sf.info(str(path))
    frames = int(info.frames)
    sr = int(info.samplerate)
    dur = float(frames) / float(sr) if sr else 0.0
    return dur, sr


def synthesize_from_script(
    script_path: Path | str,
    *,
    out_dir: Path | str | None = None,
    voice: str | None = None,
    speed: float | None = None,
    resume: bool = True,
    mode: Literal["scene", "hybrid"] = "scene",
    target_chunk_words: int | None = None,
) -> VoiceResult:
    return VoiceModule(voice=voice, speed=speed).synthesize_script(
        Path(script_path),
        out_dir=Path(out_dir) if out_dir else None,
        resume=resume,
        mode=mode,
        target_chunk_words=target_chunk_words,
    )
