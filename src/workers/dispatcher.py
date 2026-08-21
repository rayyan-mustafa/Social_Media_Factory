"""Registry and dispatcher for pipeline stages."""

from typing import Sequence

from src.services.formats import get_adapter
from src.workers.stages import StageProcessor
from src.workers.stages.audio_production import AudioProductionStage
from src.workers.stages.composing import ComposingStage
from src.workers.stages.media import MediaStage
from src.workers.stages.publishing import PublishingStage
from src.workers.stages.scripting import ScriptingStage
from src.workers.stages.text_production import TextProductionStage
from src.workers.stages.tts import TTSStage


from src.domain import JobStage
from src.workers.stages.routing import STAGE_ROUTING, get_route_key
from src.workers.stages.thumbnail import ThumbnailStage

STAGE_PROCESSOR_MAP = {
    JobStage.SCRIPTING: ScriptingStage,
    JobStage.TEXT_PRODUCTION: TextProductionStage,
    JobStage.TTS: TTSStage,
    JobStage.AUDIO_PRODUCTION: AudioProductionStage,
    JobStage.MEDIA: MediaStage,
    JobStage.COMPOSING: ComposingStage,
    JobStage.THUMBNAIL: ThumbnailStage,
    JobStage.UPLOADING: PublishingStage,
}


def get_stages_for_model(model: str) -> Sequence[StageProcessor]:
    """Returns the ordered list of stage processors for a given business model."""
    route_key = get_route_key(model)
    return [STAGE_PROCESSOR_MAP[stage]() for stage in STAGE_ROUTING[route_key]]

