"""Domain models for ScriptModule (Module 1)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class OutlineChapter(BaseModel):
    id: int
    title: str
    goal: str = ""
    key_points: list[str] = Field(default_factory=list)
    target_sentences: int = Field(ge=1, le=200)
    pacing_phase: str = "a"  # a = short 3s beats; b = long ~60s beats

    @field_validator("target_sentences", mode="before")
    @classmethod
    def _coerce_target_sentences(cls, v: Any) -> int:
        """LLM/truncated outlines often emit '' / -1 / 0; coerce to a valid beat count."""
        if v is None or v == "":
            return 1
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 1
        if n < 1:
            return 1
        if n > 200:
            return 200
        return n


class Outline(BaseModel):
    title: str
    hook: str = ""
    estimated_sentence_budget: int | None = None
    chapters: list[OutlineChapter]
    closer: str = ""

    @field_validator("estimated_sentence_budget", mode="before")
    @classmethod
    def _coerce_sentence_budget(cls, v: Any) -> int | None:
        """Empty string / junk from truncated JSON → None (normalized later)."""
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @field_validator("chapters")
    @classmethod
    def _need_chapters(cls, v: list[OutlineChapter]) -> list[OutlineChapter]:
        if not v:
            raise ValueError("outline must include at least one chapter")
        return v


class ExpandedSentence(BaseModel):
    text: str
    visual_prompt: str
    beat_type: str = ""  # interrupt|reengage|""

    @field_validator("text", "visual_prompt")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("text and visual_prompt must be non-empty")
        return v


class ChapterExpansion(BaseModel):
    chapter_id: int
    sentences: list[ExpandedSentence]

    @field_validator("sentences")
    @classmethod
    def _need_sentences(cls, v: list[ExpandedSentence]) -> list[ExpandedSentence]:
        if not v:
            raise ValueError("chapter expansion produced zero sentences")
        return v


class Scene(BaseModel):
    index: int
    text: str
    visual_prompt: str
    chapter_id: int | None = None
    word_count: int = 0
    pacing_phase: str = "a"  # a|b
    beat_type: str = ""  # interrupt|reengage|""
    target_duration_s: float | None = None
    speaker: str = ""  # narrator|quote_a|quote_b|source_reader|…


class ScriptValidation(BaseModel):
    ok: bool
    scene_count: int
    min_scenes: int
    max_scenes: int
    target_scenes: int
    estimated_duration_s: float
    median_word_count: float | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ScriptResult(BaseModel):
    topic: str
    title: str
    hook: str = ""
    outline: Outline
    scenes: list[Scene]
    validation: ScriptValidation
    meta: dict[str, Any] = Field(default_factory=dict)

    def narration_text(self) -> str:
        return "\n".join(s.text for s in self.scenes)


# --- VoiceModule (Module 2) ---


class SceneAudio(BaseModel):
    index: int
    text: str
    path: str
    duration_s: float
    sample_rate: int
    skipped: bool = False  # True if resumed from existing WAV


class VoiceValidation(BaseModel):
    ok: bool
    scene_count: int
    wav_count: int
    total_duration_s: float
    median_duration_s: float | None = None
    min_duration_s: float | None = None
    max_duration_s: float | None = None
    target_seconds_per_scene: float
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class VoiceResult(BaseModel):
    script_path: str
    title: str
    topic: str
    voice: str
    speed: float
    lang: str
    out_dir: str
    scenes: list[SceneAudio]
    validation: VoiceValidation
    meta: dict[str, Any] = Field(default_factory=dict)


# --- VisualModule (Module 3) ---


class SceneImage(BaseModel):
    index: int
    visual_prompt: str
    path: str
    width: int
    height: int
    retries_used: int = 0
    skipped: bool = False  # True if resumed from existing image
    backend: str = ""
    placeholder: bool = False  # True for mock/known test images
    # Character bible locks applied for this still
    characters: list[str] = Field(default_factory=list)
    # Plan C sparse AI video (§7.1)
    is_video_scene: bool = False
    video_path: str | None = None
    video_duration_s: float | None = None


class VisualValidation(BaseModel):
    ok: bool
    scene_count: int
    image_count: int
    width: int
    height: int
    expected_count: int
    median_image_bytes: int | None = None
    video_scene_count: int = 0
    video_clip_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class VisualResult(BaseModel):
    script_path: str
    title: str
    topic: str
    out_dir: str
    backend: str
    scenes: list[SceneImage]
    validation: VisualValidation
    meta: dict[str, Any] = Field(default_factory=dict)


# --- EditModule (Module 4) ---


class SceneClip(BaseModel):
    index: int
    image_path: str
    audio_path: str
    clip_path: str
    duration_s: float
    width: int
    height: int
    ken_burns: str = ""
    used_ai_video: bool = False
    skipped: bool = False


class EditValidation(BaseModel):
    ok: bool
    scene_count: int
    clip_count: int
    width: int
    height: int
    has_audio: bool
    duration_s: float | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class EditResult(BaseModel):
    title: str
    topic: str
    voice_manifest: str
    visual_manifest: str
    out_dir: str
    final_path: str
    backend: str
    scenes: list[SceneClip]
    validation: EditValidation
    meta: dict[str, Any] = Field(default_factory=dict)


# --- PublishModule (Module 5) ---


class GateAResult(BaseModel):
    ok: bool
    width: int | None = None
    height: int | None = None
    has_video: bool = False
    has_audio: bool = False
    duration_s: float | None = None
    file_size_bytes: int | None = None
    placeholder_backend: bool = False
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class GateRResult(BaseModel):
    """Retention quality gate before publish (warn-only configurable)."""

    ok: bool
    runtime_s: float | None = None
    hook_scene_count: int | None = None
    duplicate_beat_rate: float | None = None
    visual_leak_count: int = 0
    median_phase_a_duration_s: float | None = None
    audio_bed_present: bool = False
    loudness_lufs: float | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class GateBResult(BaseModel):
    """Policy Gate B before public / private promote."""

    ok: bool
    policy_version: int | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)


class PublishResult(BaseModel):
    ok: bool
    dry_run: bool = False
    privacy_status: str = "private"
    video_id: str | None = None
    watch_url: str | None = None
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    final_path: str
    gate_a: GateAResult
    gate_b: GateBResult | None = None
    ai_disclosure: bool = True
    contains_synthetic_media: bool = True
    publish_manifest_path: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
