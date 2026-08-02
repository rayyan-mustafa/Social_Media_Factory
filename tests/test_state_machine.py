"""Unit tests for job stage state machine."""

from src.domain import JobStage, can_transition, format_family, next_stage, queue_for_model


def test_stage_order_happy_path():
    stage = JobStage.QUEUED
    expected = [
        JobStage.SCRIPTING,
        JobStage.TTS,
        JobStage.MEDIA,
        JobStage.COMPOSING,
        JobStage.UPLOADING,
        JobStage.SUCCEEDED,
    ]
    for exp in expected:
        stage = next_stage(stage)
        assert stage == exp
    assert next_stage(JobStage.SUCCEEDED) is None


def test_failed_has_no_next():
    assert next_stage(JobStage.FAILED) is None


def test_can_transition_forward_and_fail():
    assert can_transition(JobStage.QUEUED, JobStage.SCRIPTING)
    assert can_transition(JobStage.TTS, JobStage.FAILED)
    assert can_transition(JobStage.COMPOSING, JobStage.QUEUED)  # retry
    assert not can_transition(JobStage.SUCCEEDED, JobStage.TTS)


def test_queue_and_family_factory():
    assert queue_for_model("YouTube_Shorts") == "YouTube_Shorts"
    assert format_family("Podcast_Audio") == "audio"
    assert format_family("EBooks_KDP") == "text"
    assert format_family("Online_Courses_Teachable") == "video"
