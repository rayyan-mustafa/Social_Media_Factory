"""Format-family routing map and stage transition helpers."""

from src.domain import FormatFamilyName, JobStage, format_family

STAGE_ROUTING: dict[str, list[JobStage]] = {
    "text": [JobStage.SCRIPTING, JobStage.TEXT_PRODUCTION, JobStage.UPLOADING],
    "audio": [JobStage.SCRIPTING, JobStage.TTS, JobStage.AUDIO_PRODUCTION, JobStage.UPLOADING],
    "video": [JobStage.SCRIPTING, JobStage.TTS, JobStage.MEDIA, JobStage.COMPOSING, JobStage.THUMBNAIL, JobStage.UPLOADING],
    "course": [JobStage.SCRIPTING, JobStage.TTS, JobStage.MEDIA, JobStage.COMPOSING, JobStage.UPLOADING], # Teachable
}

def get_route_key(business_model: str) -> str:
    """Gets the routing key for a business model, handling exceptions."""
    if business_model == "Online_Courses_Teachable":
        return "course"
    return format_family(business_model)

def should_skip_stage(job_stage: JobStage, executing_stage: JobStage, business_model: str) -> bool:
    """Idempotency check that skips stages we've already completed."""
    if job_stage in (JobStage.QUEUED, JobStage.FAILED):
        return False
        
    route_key = get_route_key(business_model)
    route = STAGE_ROUTING[route_key]
    
    try:
        return route.index(job_stage) > route.index(executing_stage)
    except ValueError:
        return False

def get_next_stage_for_job(business_model: str, current_stage: JobStage) -> JobStage | None:
    """Returns the next stage in the route for a given business model."""
    route_key = get_route_key(business_model)
    route = STAGE_ROUTING[route_key]
    
    try:
        idx = route.index(current_stage)
        if idx + 1 < len(route):
            return route[idx + 1]
    except ValueError:
        pass
    return None
