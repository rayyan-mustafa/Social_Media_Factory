"""Reference-image generation with a remote img2img provider and Pillow fallback.

The VPS remains the orchestrator. A GPU-backed provider creates the visual when
configured; Pillow remains the deterministic fallback and final compositor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

from src.services.reference_scene_layout import infer_layout, render_scene_layout


class ReferenceGenerationError(RuntimeError):
    """Raised when a configured reference-image provider returns invalid output."""


@dataclass(frozen=True)
class Img2ImgConfig:
    endpoint: str
    api_key: str = ""
    strength: float = 0.28
    timeout_s: float = 900.0

    @classmethod
    def from_environment(cls) -> "Img2ImgConfig | None":
        endpoint = os.getenv("REFERENCE_IMG2IMG_URL", "").strip()
        if not endpoint:
            return None
        strength = float(os.getenv("REFERENCE_IMG2IMG_STRENGTH", "0.28"))
        if not 0.0 < strength <= 1.0:
            raise ValueError("REFERENCE_IMG2IMG_STRENGTH must be between 0 and 1")
        return cls(
            endpoint=endpoint,
            api_key=os.getenv("REFERENCE_IMG2IMG_API_KEY", "").strip(),
            strength=strength,
            timeout_s=float(os.getenv("REFERENCE_IMG2IMG_TIMEOUT_S", "900")),
        )


def _validate_image_bytes(data: bytes, *, width: int, height: int) -> bytes:
    if not data:
        raise ReferenceGenerationError("image provider returned an empty response")
    try:
        from io import BytesIO

        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            if image.width < 320 or image.height < 180:
                raise ReferenceGenerationError("provider image is too small")
            image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    except ReferenceGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ReferenceGenerationError("provider response was not a valid image") from exc
    return data


def generate_with_img2img(
    reference_path: str | Path,
    out_path: str | Path,
    *,
    prompt: str,
    config: Img2ImgConfig,
    width: int = 1280,
    height: int = 720,
) -> Path:
    """Send a reference image to a multipart image-to-image HTTP endpoint.

    The endpoint must return the generated image bytes directly. This contract
    works with a thin ComfyUI/Replicate-compatible gateway without coupling this
    project to a specific vendor SDK.
    """
    reference = Path(reference_path)
    if not reference.exists():
        raise FileNotFoundError(reference)
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    data = {
        "prompt": prompt,
        "width": str(width),
        "height": str(height),
        "strength": str(config.strength),
        "mode": "image_to_image",
    }
    with reference.open("rb") as image_file:
        response = httpx.post(
            config.endpoint,
            headers=headers,
            data=data,
            files={"image": (reference.name, image_file, "application/octet-stream")},
            timeout=config.timeout_s,
        )
    if response.status_code >= 400:
        raise ReferenceGenerationError(
            f"img2img provider HTTP {response.status_code}: {response.text[:300]}"
        )
    image_bytes = _validate_image_bytes(response.content, width=width, height=height)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(image_bytes)
    return out


def generate_reference_image(
    reference_path: str | Path | None,
    out_path: str | Path,
    *,
    prompt: str,
    width: int = 1280,
    height: int = 720,
    config: Img2ImgConfig | None = None,
) -> tuple[Path, str]:
    """Generate using img2img when configured, otherwise render with Pillow.

    Returns ``(path, backend)`` so reports cannot confuse the fallback with a
    model-generated image.
    """
    provider = config or Img2ImgConfig.from_environment()
    if provider and reference_path:
        return (
            generate_with_img2img(
                reference_path,
                out_path,
                prompt=prompt,
                config=provider,
                width=width,
                height=height,
            ),
            "img2img",
        )

    layout = infer_layout(
        str(reference_path) if reference_path else None,
        width=width,
        height=height,
    )
    render_scene_layout(layout, out_path=out_path)
    return Path(out_path), "pillow_fallback"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate a reference-inspired image with remote img2img or Pillow fallback.")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--out", type=Path, default=Path("output/reference_img2img.png"))
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args()
    path, backend = generate_reference_image(args.reference, args.out, prompt=args.prompt, width=args.width, height=args.height)
    print(f"saved {path}")
    print(f"backend {backend}")


if __name__ == "__main__":
    main()
