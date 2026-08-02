"""Format-family adapters (video / audio / text).

Every business model maps to one family via src.domain.FORMAT_FAMILY.
The pipeline routes through get_adapter() — this is the factory wiring point.
"""

from __future__ import annotations

from src.core.logging import get_logger
from src.domain import FormatFamilyName, format_family
from src.services.formats import audio as audio_mod
from src.services.formats import text as text_mod
from src.services.formats import video as video_mod

logger = get_logger(__name__)

_FAMILY_MODULES = {
    "video": video_mod,
    "audio": audio_mod,
    "text": text_mod,
}


def get_adapter(business_model: str) -> FormatFamilyName:
    """Resolve business model → format family for pipeline routing."""
    family = format_family(business_model)
    module = _FAMILY_MODULES[family]
    logger.info(
        "format_adapter_selected",
        extra={"model": business_model, "family": family, "module": module.FAMILY},
    )
    return family
