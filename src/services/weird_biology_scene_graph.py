"""Validated scene graph for complex Weird Biology visuals.

The planner produces this renderer-neutral contract. SVG and Pillow backends can
consume the same scene without embedding layout decisions in either renderer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class SceneGraphError(ValueError):
    """Raised when a planned scene cannot be rendered safely."""


@dataclass(frozen=True)
class SceneCharacter:
    id: str
    role: str = "adult"
    pose: str = "stand"
    emotion: str = "neutral"
    x: float = 0.5
    y: float = 0.5
    scale: float = 1.0
    z: int = 50


@dataclass(frozen=True)
class SceneObject:
    id: str
    asset: str
    x: float = 0.5
    y: float = 0.5
    scale: float = 1.0
    rotation: float = 0.0
    z: int = 20
    visible: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SceneBackground:
    asset: str = "default"
    color: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SceneCamera:
    shot: str = "wide"
    focus_id: str | None = None
    x: float = 0.5
    y: float = 0.5
    zoom: float = 1.0


@dataclass(frozen=True)
class SceneSpec:
    """Complete visual description for one narration beat."""

    beat_index: int
    beat_text: str
    background: SceneBackground = field(default_factory=SceneBackground)
    characters: tuple[SceneCharacter, ...] = ()
    objects: tuple[SceneObject, ...] = ()
    camera: SceneCamera = field(default_factory=SceneCamera)
    width: int = 1280
    height: int = 720
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SceneSpec":
        if not isinstance(raw, dict):
            raise SceneGraphError("scene must be an object")

        background_raw = raw.get("background") or {}
        camera_raw = raw.get("camera") or {}
        characters = tuple(
            SceneCharacter(**item)
            for item in (raw.get("characters") or [])
            if isinstance(item, dict)
        )
        objects = tuple(
            SceneObject(**item)
            for item in (raw.get("objects") or [])
            if isinstance(item, dict)
        )
        scene = cls(
            beat_index=int(raw.get("beat_index", 0)),
            beat_text=str(raw.get("beat_text") or "").strip(),
            background=SceneBackground(**background_raw),
            characters=characters,
            objects=objects,
            camera=SceneCamera(**camera_raw),
            width=int(raw.get("width", 1280)),
            height=int(raw.get("height", 720)),
            metadata=dict(raw.get("metadata") or {}),
        )
        scene.validate()
        return scene

    def validate(self) -> "SceneSpec":
        if self.beat_index < 0:
            raise SceneGraphError("beat_index must be non-negative")
        if not self.beat_text:
            raise SceneGraphError("beat_text is required")
        if self.width < 320 or self.height < 180:
            raise SceneGraphError("canvas is too small")
        if not self.characters:
            raise SceneGraphError("scene requires at least one character")
        if len({item.id for item in self.characters}) != len(self.characters):
            raise SceneGraphError("character ids must be unique")
        ids = [item.id for item in self.characters] + [item.id for item in self.objects]
        if len(set(ids)) != len(ids):
            raise SceneGraphError("all scene ids must be unique")
        if self.camera.focus_id and self.camera.focus_id not in ids:
            raise SceneGraphError("camera focus_id does not exist")
        for item in (*self.characters, *self.objects):
            if not 0.0 <= item.x <= 1.0 or not 0.0 <= item.y <= 1.0:
                raise SceneGraphError(f"{item.id} position must be normalized 0..1")
            if item.scale <= 0.0 or item.scale > 8.0:
                raise SceneGraphError(f"{item.id} scale must be between 0 and 8")
        if not 0.25 <= self.camera.zoom <= 8.0:
            raise SceneGraphError("camera zoom must be between 0.25 and 8")
        return self

    def layers(self) -> list[SceneCharacter | SceneObject]:
        """Return visible objects and characters in deterministic depth order."""
        items = [*self.characters, *(item for item in self.objects if item.visible)]
        return sorted(items, key=lambda item: (item.z, item.id))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
