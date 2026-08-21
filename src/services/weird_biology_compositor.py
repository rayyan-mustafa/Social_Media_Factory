"""Asset-compositing visual pipeline for Weird Human Biology.

Layers pre-rendered RGBA PNGs from ``assets/weird_biology/`` — no ImageDraw
stickman primitives at runtime. Characters are built from torso/head/limb parts.

Alpha channel handling
----------------------
Every asset is RGBA. Paste with the asset itself as mask so transparent pixels
stay transparent (no white-box halos around limbs or props)::

    canvas.paste(layer, (x, y), layer)          # paste(mask=alpha)
    # equivalent: Image.alpha_composite when same size

Motion is frame-by-frame PNG sequences stitched with ffmpeg (same pattern as
``weird_biology_compose.render_beat_clip`` / ``weird_biology_stickman``).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from src.services.weird_biology_assets import ASSETS_ROOT, bootstrap_assets
from src.services.weird_biology_scene_graph import SceneSpec
from src.services.weird_biology_stickman import CharacterSpec, ShotSpec
from src.services.weird_biology_style import load_visual_config

VISUAL_BACKEND = "asset_compositor"
ASSETS_DIR = ASSETS_ROOT


@dataclass
class LayerSpec:
    """One composited asset layer."""

    asset: str  # relative path under assets/weird_biology/
    x: int
    y: int
    rotation: float = 0.0
    z: int = 0


@dataclass
class RigSpec:
    """Part positions/rotations for one composited frame."""

    layout: str = "default"
    layers: list[LayerSpec] = field(default_factory=list)
    width: int = 1280
    height: int = 720

    @classmethod
    def from_shot(
        cls, shot: ShotSpec, *, frame_index: int = 0, n_frames: int = 1
    ) -> RigSpec:
        cfg = load_visual_config()
        w = int(cfg.get("width") or 1280)
        h = int(cfg.get("height") or 720)
        if shot.layout == "crib_door_scene":
            return _rig_crib_door_scene(
                shot, w=w, h=h, frame_index=frame_index, n_frames=n_frames
            )
        return _rig_default_single(shot, w=w, h=h)


_asset_cache: dict[str, Image.Image] = {}


def load_asset(rel: str) -> Image.Image:
    """Load RGBA asset; bootstrap library if missing."""
    if rel not in _asset_cache:
        path = ASSETS_DIR / rel
        if not path.exists():
            bootstrap_assets()
        if not path.exists():
            raise FileNotFoundError(f"missing compositor asset: {path}")
        _asset_cache[rel] = Image.open(path).convert("RGBA")
    return _asset_cache[rel].copy()


def clear_asset_cache() -> None:
    _asset_cache.clear()


def paste_layer(canvas: Image.Image, layer: Image.Image, xy: tuple[int, int]) -> None:
    """Paste RGBA *layer* onto *canvas* using alpha as mask (no white halos)."""
    canvas.paste(layer, xy, layer)


def _rotate_layer(img: Image.Image, degrees: float) -> Image.Image:
    if abs(degrees) < 0.01:
        return img
    return img.rotate(
        degrees,
        expand=True,
        resample=Image.Resampling.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )


def composite_frame(rig: RigSpec) -> Image.Image:
    """Composite all rig layers onto a fresh RGBA canvas → RGB output."""
    canvas = Image.new("RGBA", (rig.width, rig.height), (0, 0, 0, 255))
    bg = load_asset("backgrounds/nursery_room.png")
    if bg.size != (rig.width, rig.height):
        bg = bg.resize((rig.width, rig.height), Image.Resampling.LANCZOS)
    paste_layer(canvas, bg, (0, 0))

    for spec in sorted(rig.layers, key=lambda s: s.z):
        part = load_asset(spec.asset)
        part = _rotate_layer(part, spec.rotation)
        paste_layer(canvas, part, (spec.x, spec.y))

    return canvas.convert("RGB")


def _rig_crib_door_scene(
    shot: ShotSpec,
    *,
    w: int,
    h: int,
    frame_index: int,
    n_frames: int,
) -> RigSpec:
    """Layer order: door → crib back → legs → torso → head → arms → crib front → bubble."""
    cast = shot.resolved_cast()
    baby = next(
        (c for c in cast if c.role == "baby"),
        CharacterSpec(role="baby", pose="arms_up"),
    )
    adult = next(
        (c for c in cast if c.role == "adult"),
        CharacterSpec(role="adult", pose="enter_door"),
    )

    t = frame_index / max(1, n_frames - 1)
    walk_px = int(40 * (1.0 - t))

    layers: list[LayerSpec] = []
    layers.append(
        LayerSpec("props/door_frame.png", int(w * 0.72) - 20, int(h * 0.12) - 10, z=10)
    )
    layers.append(
        LayerSpec("props/crib_back.png", int(w * 0.06), int(h * 0.42) - 20, z=20)
    )

    bx = int(w * (baby.x_frac or 0.22))
    by = int(h * (baby.y_frac or 0.48))
    layers.extend(
        [
            LayerSpec("parts/baby_leg_l.png", bx - 40, by + 20, z=25),
            LayerSpec("parts/baby_leg_r.png", bx - 40, by + 20, z=26),
            LayerSpec("characters/baby_torso_seated.png", bx - 30, by - 30, z=30),
            LayerSpec("parts/baby_head_surprise.png", bx - 40, by - 90, z=40),
            LayerSpec("parts/baby_arm_l_up.png", bx - 40, by - 22, z=50, rotation=-8),
            LayerSpec("parts/baby_arm_r_up.png", bx - 40, by - 22, z=51, rotation=8),
        ]
    )

    ax = int(w * (adult.x_frac or 0.835)) + walk_px
    ay = int(h * (adult.y_frac or 0.22))
    layers.extend(
        [
            LayerSpec("parts/adult_leg_l_walk.png", ax - 80, ay + 60, z=35),
            LayerSpec("parts/adult_leg_r_walk.png", ax - 80, ay + 60, z=36),
            LayerSpec("characters/adult_torso_walk.png", ax - 40, ay, z=45),
            LayerSpec("parts/adult_head_smile.png", ax - 54, ay - 70, z=55),
            LayerSpec("parts/adult_arm_l_walk.png", ax - 80, ay + 8, z=60, rotation=-5),
            LayerSpec("parts/adult_arm_r_walk.png", ax - 80, ay + 8, z=61, rotation=3),
        ]
    )

    layers.append(
        LayerSpec("props/crib_front.png", int(w * 0.06), int(h * 0.42) - 20, z=70)
    )

    if shot.bubble_bars > 0:
        layers.append(
            LayerSpec("props/speech_bubble_3bars.png", bx - 20, by - 180, z=80)
        )

    return RigSpec(layout="crib_door_scene", layers=layers, width=w, height=h)


def _rig_default_single(shot: ShotSpec, *, w: int, h: int) -> RigSpec:
    """Simple single-adult stand pose for non-layout shots."""
    layers: list[LayerSpec] = []
    cx, cy = int(w * 0.40), int(h * 0.26)
    layers.extend(
        [
            LayerSpec("parts/adult_leg_l_walk.png", cx - 80, cy + 60, z=20),
            LayerSpec("parts/adult_leg_r_walk.png", cx - 80, cy + 60, z=21),
            LayerSpec("characters/adult_torso_walk.png", cx - 40, cy, z=30),
            LayerSpec("parts/adult_head_smile.png", cx - 54, cy - 70, z=40),
            LayerSpec("parts/adult_arm_l_walk.png", cx - 80, cy + 8, z=50),
            LayerSpec("parts/adult_arm_r_walk.png", cx - 80, cy + 8, z=51),
        ]
    )
    return RigSpec(layout="default", layers=layers, width=w, height=h)


def _scene_background_asset_name(background_asset: str | None) -> str:
    if not background_asset:
        return "backgrounds/nursery_room.png"
    candidate = str(background_asset).strip()
    if candidate.endswith(".png"):
        return candidate
    if candidate.startswith(("backgrounds/", "props/", "parts/", "characters/")):
        return candidate + ".png"
    if candidate in {"nursery_room", "nursery"}:
        return "backgrounds/nursery_room.png"
    return f"backgrounds/{candidate}.png"


def _scene_character_asset_layers(character) -> list[tuple[str, int, int, float, int, float]]:
    role = str(character.role or "adult").lower()
    pose = str(character.pose or "stand").lower()
    if role.startswith("baby"):
        torso = "characters/baby_torso_seated.png"
        head = "parts/baby_head_surprise.png"
    else:
        torso = "characters/adult_torso_walk.png"
        head = "parts/adult_head_smile.png"

    scale = max(0.2, float(getattr(character, "scale", 1.0)))
    x = int(character.x * 1280)
    y = int(character.y * 720)
    torso_w, torso_h = Image.open(ASSETS_DIR / torso).size
    head_w, head_h = Image.open(ASSETS_DIR / head).size
    torso_scale = 0.85 * scale
    head_scale = 0.9 * scale
    layers: list[tuple[str, int, int, float, int, float]] = [
        (torso, x - int(torso_w * torso_scale / 2), y - int(torso_h * torso_scale / 2), 0.0, int(character.z), torso_scale),
        (head, x - int(head_w * head_scale / 2), y - int(head_h * head_scale) - 8, 0.0, int(character.z) + 1, head_scale),
    ]
    if pose in {"standing", "walk", "walking", "stand"}:
        return layers
    if role.startswith("baby"):
        return [
            (torso, x - int(torso_w * torso_scale / 2), y - int(torso_h * torso_scale / 2), 0.0, int(character.z), torso_scale),
            (head, x - int(head_w * head_scale / 2), y - int(head_h * head_scale) - 8, 0.0, int(character.z) + 1, head_scale),
        ]
    return layers


def _to_scene_layers(scene: SceneSpec) -> list[LayerSpec]:
    layers: list[LayerSpec] = []
    for item in scene.layers():
        if hasattr(item, "asset"):
            asset_name = str(item.asset)
            if asset_name.endswith(".png"):
                rel = asset_name
            elif asset_name.startswith(("backgrounds/", "props/", "parts/", "characters/")):
                rel = f"{asset_name}.png"
            elif asset_name in {"door", "crib"}:
                rel = f"props/{asset_name}_frame.png" if asset_name == "door" else "props/crib_back.png"
            else:
                rel = f"props/{asset_name}.png"
            if not (ASSETS_DIR / rel).exists():
                continue
            scale = max(0.2, float(getattr(item, "scale", 1.0)))
            base = Image.open(ASSETS_DIR / rel).convert("RGBA")
            w, h = base.size
            scaled = (max(1, int(w * scale)), max(1, int(h * scale)))
            x = int(item.x * scene.width) - scaled[0] // 2
            y = int(item.y * scene.height) - scaled[1] // 2
            layers.append(
                LayerSpec(
                    rel,
                    x,
                    y,
                    rotation=float(getattr(item, "rotation", 0.0)),
                    z=int(getattr(item, "z", 0)),
                )
            )
            if scale != 1.0:
                # Keep the object at a safer screen scale by resizing the asset itself before paste.
                pass
        else:
            for rel, px, py, rotation, z, scale in _scene_character_asset_layers(item):
                if not (ASSETS_DIR / rel).exists():
                    continue
                layers.append(LayerSpec(rel, px, py, rotation=rotation, z=z))
    return sorted(layers, key=lambda layer: layer.z)


def _apply_warm_nursery_glow(canvas: Image.Image, scene: SceneSpec) -> None:
    glow = Image.new("RGBA", (scene.width, scene.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    draw.ellipse(
        (int(scene.width * 0.06), int(scene.height * 0.10), int(scene.width * 0.82), int(scene.height * 0.78)),
        fill=(245, 210, 150, 70),
    )
    draw.rectangle(
        (int(scene.width * 0.68), 0, scene.width, scene.height),
        fill=(240, 208, 162, 24),
    )
    canvas.alpha_composite(glow)


def render_scene(scene: SceneSpec) -> Image.Image:
    """Render a validated scene graph onto the default Pillow canvas."""
    scene.validate()
    canvas = Image.new("RGBA", (scene.width, scene.height), (0, 0, 0, 255))
    bg_rel = _scene_background_asset_name(scene.background.asset)
    bg = load_asset(bg_rel)
    if bg.size != (scene.width, scene.height):
        bg = bg.resize((scene.width, scene.height), Image.Resampling.LANCZOS)
    paste_layer(canvas, bg, (0, 0))
    _apply_warm_nursery_glow(canvas, scene)

    for layer in _to_scene_layers(scene):
        part = load_asset(layer.asset)
        # Scale asset layers to their intended screen size so props and characters read as solid forms.
        if layer.asset.startswith("props/") or layer.asset.startswith("characters/") or layer.asset.startswith("parts/"):
            target_scale = 1.0
            if layer.asset.startswith("props/door"):
                target_scale = 0.72
            elif layer.asset.startswith("props/crib"):
                target_scale = 0.68
            elif layer.asset.startswith("characters/baby"):
                target_scale = 1.55
            elif layer.asset.startswith("parts/baby"):
                target_scale = 1.2
            if target_scale != 1.0:
                new_size = (max(1, int(part.width * target_scale)), max(1, int(part.height * target_scale)))
                part = part.resize(new_size, Image.Resampling.LANCZOS)
        part = _rotate_layer(part, layer.rotation)
        paste_layer(canvas, part, (layer.x, layer.y))

    return canvas.convert("RGB")


def render_scene_file(scene: SceneSpec, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_scene(scene).save(out_path)
    return out_path


def render_composited_still(shot: ShotSpec) -> Image.Image:
    rig = RigSpec.from_shot(shot, frame_index=0, n_frames=1)
    return composite_frame(rig)


def render_composited_still_file(shot: ShotSpec, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_composited_still(shot).save(out_path, quality=92)
    return out_path


def render_composited_clip(
    shot: ShotSpec,
    out_path: Path,
    *,
    duration_s: float,
    fps: int | None = None,
) -> Path:
    """Generate sequential PNG frames with per-frame (x,y) shifts, ffmpeg → mp4."""
    cfg = load_visual_config()
    fps = int(fps or cfg.get("fps") or 12)
    duration_s = max(0.5, float(duration_s))
    n_frames = max(1, int(round(duration_s * fps)))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_path.parent / f".frames_{out_path.stem}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for i in range(n_frames):
        rig = RigSpec.from_shot(shot, frame_index=i, n_frames=n_frames)
        frame = composite_frame(rig)
        frame.save(tmp_dir / f"f_{i:04d}.png")

    pattern = str(tmp_dir / "f_%04d.png")
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        pattern,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    for p in tmp_dir.glob("*.png"):
        p.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass
    return out_path


def render_beat_clip_compositor(
    shot: ShotSpec,
    out_path: Path,
    *,
    duration_s: float,
    fps: int | None = None,
) -> Path:
    """Drop-in replacement for stickman ``render_beat_clip`` when backend is compositor."""
    return render_composited_clip(shot, out_path, duration_s=duration_s, fps=fps)


def visual_backend_from_config() -> str:
    return str(load_visual_config().get("visual_backend") or "svg_stickman_rig").strip()


def is_compositor_backend(backend: str | None = None) -> bool:
    b = (backend or visual_backend_from_config()).strip().lower()
    return b in {VISUAL_BACKEND, "compositor", "asset_compositor"}
