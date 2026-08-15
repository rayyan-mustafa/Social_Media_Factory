"""Voice cast resolution — narrator + quote speakers with human / Kokoro fallback.

Config: ``config/voice_cast.json``
Job meta may carry ``voice_cast`` from editing_directive.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.services.settings import CONFIG_DIR, ROOT

VOICE_CAST_PATH = CONFIG_DIR / "voice_cast.json"
HUMAN_SAMPLES_DIR = ROOT / "assets" / "voice_cast"

_QUOTE_OPEN = re.compile(r'[“"]')


def load_voice_cast(path: Path | None = None) -> dict[str, Any]:
    p = path or VOICE_CAST_PATH
    if not p.is_file():
        return {"version": 1, "channels": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"version": 1, "channels": {}}
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "channels": {}}


def channel_cast(channel: str | None, *, cast: dict[str, Any] | None = None) -> dict[str, Any]:
    data = cast or load_voice_cast()
    ch = (channel or "napstorian").strip().lower()
    channels = data.get("channels") or {}
    return dict(channels.get(ch) or channels.get("napstorian") or {})


def resolve_voice_mode(
    *,
    channel: str | None = None,
    editing: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    """tts_single | tts_multi | human_brand."""
    for src in (meta, editing):
        if not isinstance(src, dict):
            continue
        audio = src.get("audio") if isinstance(src.get("audio"), dict) else src
        mode = str(audio.get("voice_mode") or src.get("voice_mode") or "").strip().lower()
        if mode:
            return mode
    ch_cast = channel_cast(channel)
    return str(ch_cast.get("voice_mode") or "tts_single").strip().lower()


def infer_speaker_role(text: str, *, default: str = "narrator") -> str:
    """Heuristic: quoted dialogue → quote_a / quote_b alternating; else narrator."""
    t = (text or "").strip()
    if not t:
        return default
    if _QUOTE_OPEN.search(t) or (t.startswith("'") and t.endswith("'")):
        # Stable-ish split: longer quotes → quote_a
        return "quote_a" if len(t) >= 80 else "quote_b"
    return default


def resolve_speaker_voice(
    role: str,
    *,
    channel: str | None = None,
    cast: dict[str, Any] | None = None,
    voice_mode: str | None = None,
) -> dict[str, Any]:
    """Resolve one speaker to a concrete TTS / human path.

    Returns dict with keys:
      role, voice_id (Kokoro), elevenlabs_id?, human_wav?, backend_hint, fallback_used
    """
    ch_cast = channel_cast(channel, cast=cast)
    speakers = dict(ch_cast.get("speakers") or {})
    mode = (voice_mode or ch_cast.get("voice_mode") or "tts_single").strip().lower()
    entry = dict(speakers.get(role) or speakers.get("narrator") or {})

    human_rel = str(entry.get("human_wav") or entry.get("sample_wav") or "").strip()
    human_path = None
    if human_rel:
        p = Path(human_rel)
        if not p.is_absolute():
            p = ROOT / p
        if p.is_file():
            human_path = p

    fallback_used = False
    backend_hint = "kokoro"
    voice_id = str(entry.get("kokoro_voice") or entry.get("voice") or "am_michael")

    if mode == "human_brand" and role == "narrator":
        if human_path is not None:
            backend_hint = "human_wav"
        else:
            fallback_used = True
            backend_hint = "kokoro"
    elif mode == "human_brand" and human_path is not None:
        backend_hint = "human_wav"
    elif entry.get("elevenlabs_id") and mode in {"human_brand", "tts_multi"}:
        # ID present but we still default Kokoro unless ElevenLabs backend selected.
        backend_hint = "elevenlabs_optional"

    if mode == "tts_single" and role != "narrator":
        # Force narrator voice for single mode
        narr = dict(speakers.get("narrator") or {})
        voice_id = str(narr.get("kokoro_voice") or narr.get("voice") or voice_id)
        human_path = None
        backend_hint = "kokoro"

    return {
        "role": role,
        "voice_id": voice_id,
        "elevenlabs_id": entry.get("elevenlabs_id"),
        "human_wav": str(human_path) if human_path else None,
        "backend_hint": backend_hint,
        "fallback_used": fallback_used or (
            mode == "human_brand" and human_path is None and role == "narrator"
        ),
        "voice_mode": mode,
    }


def voice_for_scene(
    scene: Any,
    *,
    channel: str | None = None,
    meta: dict[str, Any] | None = None,
    cast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pick cast voice for a Scene / dict scene."""
    editing = None
    if isinstance(meta, dict):
        editing = meta.get("editing_directive") or meta
    mode = resolve_voice_mode(channel=channel, editing=editing if isinstance(editing, dict) else None, meta=meta)
    role = ""
    if hasattr(scene, "speaker"):
        role = str(getattr(scene, "speaker") or "").strip()
    elif isinstance(scene, dict):
        role = str(scene.get("speaker") or "").strip()
    text = ""
    if hasattr(scene, "text"):
        text = str(getattr(scene, "text") or "")
    elif isinstance(scene, dict):
        text = str(scene.get("text") or "")
    if not role:
        if mode in {"tts_multi", "human_brand"}:
            role = infer_speaker_role(text)
        else:
            role = "narrator"
    return resolve_speaker_voice(role, channel=channel, cast=cast, voice_mode=mode)
