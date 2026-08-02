"""Domain enums, DTOs, and job state machine."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobStage(StrEnum):
    QUEUED = "queued"
    SCRIPTING = "scripting"
    TTS = "tts"
    MEDIA = "media"
    COMPOSING = "composing"
    UPLOADING = "uploading"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ArtifactKind(StrEnum):
    SCRIPT = "script"
    AUDIO = "audio"
    IMAGE = "image"
    SCENE_VIDEO = "scene_video"
    FINAL_VIDEO = "final_video"
    EBOOK_DOCUMENT = "ebook_document"
    BLOG_POST = "blog_post"
    PODCAST_AUDIO = "podcast_audio"


# Valid stage transitions for the pipeline
STAGE_ORDER: list[JobStage] = [
    JobStage.QUEUED,
    JobStage.SCRIPTING,
    JobStage.TTS,
    JobStage.MEDIA,
    JobStage.COMPOSING,
    JobStage.UPLOADING,
    JobStage.SUCCEEDED,
]

# Production product lines (all 9 are live business services).
# Alias BOOKMARKED_MODELS kept for older imports.
BUSINESS_MODELS: tuple[str, ...] = (
    "YouTube_Shorts",
    "Podcast_Audio",
    "SEO_Blogs",
    "Web_Series",
    "Radio_FM",
    "EBooks_KDP",
    "Audiobooks_ACX",
    "Sleep_Stories",
    "Online_Courses_Teachable",
)
BOOKMARKED_MODELS = BUSINESS_MODELS

# YouTube_Shorts publishes directly — no durable production folder in object storage.
DIRECT_UPLOAD_MODELS: frozenset[str] = frozenset({"YouTube_Shorts"})

FormatFamilyName = Literal["video", "audio", "text"]

FORMAT_FAMILY: dict[str, FormatFamilyName] = {
    "YouTube_Shorts": "video",
    "Web_Series": "video",
    "Sleep_Stories": "video",
    "Online_Courses_Teachable": "video",
    "Podcast_Audio": "audio",
    "Radio_FM": "audio",
    "Audiobooks_ACX": "audio",
    "SEO_Blogs": "text",
    "EBooks_KDP": "text",
}


def is_valid_model(business_model: str) -> bool:
    return business_model in BUSINESS_MODELS


def format_family(business_model: str) -> FormatFamilyName:
    return FORMAT_FAMILY.get(business_model, "video")


def queue_for_model(business_model: str) -> str:
    """ARQ queue name equals the business model string."""
    if business_model in BUSINESS_MODELS:
        return business_model
    return "YouTube_Shorts"


def next_stage(current: JobStage) -> JobStage | None:
    if current in {JobStage.SUCCEEDED, JobStage.FAILED}:
        return None
    try:
        idx = STAGE_ORDER.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[idx + 1]


def can_transition(from_stage: JobStage | str, to_stage: JobStage) -> bool:
    """Allow FAILED from any stage; QUEUED for retries; otherwise forward-only."""
    if to_stage == JobStage.FAILED:
        return True
    if to_stage == JobStage.QUEUED:
        return True
    try:
        current = from_stage if isinstance(from_stage, JobStage) else JobStage(from_stage)
    except ValueError:
        return to_stage == JobStage.QUEUED
    if current == to_stage:
        return True
    if current in {JobStage.SUCCEEDED, JobStage.FAILED}:
        return False
    try:
        return STAGE_ORDER.index(to_stage) >= STAGE_ORDER.index(current)
    except ValueError:
        return False


class SceneScript(BaseModel):
    index: int
    text: str = Field(min_length=10)
    visual_query: str = Field(min_length=2)


class ScriptPayload(BaseModel):
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    scenes: list[SceneScript] = Field(default_factory=list)
    content: str | None = None


class JobCreate(BaseModel):
    topic: str = Field(min_length=3, max_length=255)
    niche: str = Field(default="documentary", max_length=128)
    business_model: str = Field(default="YouTube_Shorts", max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)
    upload_to_youtube: bool = False

    @field_validator("business_model")
    @classmethod
    def _validate_business_model(cls, value: str) -> str:
        if value not in BUSINESS_MODELS:
            allowed = ", ".join(BUSINESS_MODELS)
            raise ValueError(f"business_model must be one of: {allowed}")
        return value

    @model_validator(mode="after")
    def _youtube_shorts_direct_upload(self) -> JobCreate:
        """YouTube_Shorts always uploads to YouTube; no production folder."""
        if self.business_model in DIRECT_UPLOAD_MODELS:
            self.upload_to_youtube = True
        return self


class JobRead(BaseModel):
    id: int
    topic: str
    niche: str
    business_model: str
    status: JobStatus
    stage: JobStage
    attempt: int
    idempotency_key: str | None
    error: str | None
    cost_cents: int
    youtube_video_id: str | None = None

    model_config = {"from_attributes": True}


class ArtifactRead(BaseModel):
    id: int
    kind: ArtifactKind
    s3_key: str
    checksum: str | None
    meta: dict

    model_config = {"from_attributes": True}
