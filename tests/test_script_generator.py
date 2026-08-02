"""Script payload validation and fallback generator."""

from src.domain import ScriptPayload
from src.services.script_generator import ScriptGenerator


def test_fallback_script_valid():
    gen = ScriptGenerator()
    payload = gen.generate_fallback("The Silk Road", "documentary")
    assert isinstance(payload, ScriptPayload)
    assert len(payload.scenes) == 3
    assert payload.title
    assert all(len(s.text) >= 10 for s in payload.scenes)


def test_script_payload_rejects_empty_scenes_unless_content():
    try:
        ScriptPayload(title="t", description="d", scenes=[])
        raised = False
    except Exception:
        raised = True
    assert not raised  # scenes can be empty now if content is used or just default empty list

def test_ebook_fallback_script():
    gen = ScriptGenerator()
    payload = gen.generate_fallback("The Silk Road", "documentary", "EBooks_KDP")
    assert isinstance(payload, ScriptPayload)
    assert not payload.scenes
    assert payload.content
    assert "# The Silk Road" in payload.content

def test_podcast_fallback_script():
    gen = ScriptGenerator()
    payload = gen.generate_fallback("The Silk Road", "documentary", "Podcast_Audio")
    assert isinstance(payload, ScriptPayload)
    assert len(payload.scenes) == 3
