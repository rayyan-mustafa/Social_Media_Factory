"""Domain enums, DTOs, and job state machine."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


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
