"""Ready-now image reuse packs — Pinterest / IG / FB / quote cards → YT.

No RunPod. No video-heavy derivatives. See output/ops/CONTENT_REUSE.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


NETWORKS = (
    "pinterest",
    "instagram_carousel",
    "facebook_image",
    "quote_card",
    "threads_x_image",
)


def _slug(text: str, *, max_len: int = 80) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "pin").strip()).strip("_")
    return (s[:max_len] or "pin").lower()


def list_scene_images(job_dir: Path | str, *, limit: int = 12) -> list[Path]:
    """Prefer evenly spaced scene stills from images/."""
    root = Path(job_dir)
    img_dir = root / "images"
    if not img_dir.is_dir():
        return []
    files = sorted(img_dir.glob("scene_*.jpg")) + sorted(img_dir.glob("scene_*.png"))
    if not files:
        files = sorted(img_dir.glob("*.jpg")) + sorted(img_dir.glob("*.png"))
    if not files:
        return []
    if len(files) <= limit:
        return files
    # Even sample across gallery
    step = max(1, len(files) // limit)
    picked = files[::step][:limit]
    if files[-1] not in picked:
        picked[-1] = files[-1]
    return picked


def _load_title_hook(job_dir: Path) -> dict[str, str]:
    title = ""
    hook = ""
    meta_title = job_dir / "youtube_meta" / "title.txt"
    if meta_title.exists():
        title = meta_title.read_text(encoding="utf-8").strip()
    script_path = job_dir / "script" / "script.json"
    if script_path.exists():
        try:
            raw = json.loads(script_path.read_text(encoding="utf-8"))
            title = title or str(raw.get("title") or "")
            hook = str(raw.get("hook") or "")
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    narr = job_dir / "script" / "narration.txt"
    if not hook and narr.exists():
        hook = narr.read_text(encoding="utf-8").strip().split("\n")[0][:240]
    return {"title": title or job_dir.name, "hook": hook or title}


def _youtube_url(video_id: str | None) -> str:
    if not video_id:
        return ""
    return f"https://www.youtube.com/watch?v={video_id}"


def build_image_reuse_pack(
    job_dir: Path | str,
    *,
    video_id: str | None = None,
    channel: str = "napstorian",
    max_images: int = 10,
    networks: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Write derivatives/image_reuse/ packs for ready-now networks."""
    root = Path(job_dir)
    out_dir = root / "derivatives" / "image_reuse"
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = _load_title_hook(root)
    images = list_scene_images(root, limit=max_images)
    yt = _youtube_url(video_id)
    nets = networks or NETWORKS
    packs: dict[str, Any] = {}

    # Shared pin rows (Pinterest CSV-friendly)
    pin_rows = []
    for i, img in enumerate(images):
        pin_rows.append(
            {
                "title": meta["title"][:100],
                "description": (
                    f"{meta['hook'][:400]}\n\nWatch: {yt}".strip()
                    if yt
                    else meta["hook"][:500]
                ),
                "link": yt,
                "image_path": str(img),
                "board": (
                    "Sleep History"
                    if channel == "napping_historian"
                    else "Normal History"
                ),
                "index": i,
            }
        )

    if "pinterest" in nets:
        pinterest = {
            "network": "pinterest",
            "channel": channel,
            "youtube_url": yt,
            "pins": pin_rows,
            "post_note": "Upload via Pinterest UI or official API — no cookie scrape.",
        }
        path = out_dir / "pinterest.json"
        path.write_text(json.dumps(pinterest, indent=2) + "\n", encoding="utf-8")
        # Simple CSV for bulk
        csv_path = out_dir / "pinterest.csv"
        lines = ["Title,Description,Link,Media URL"]
        for row in pin_rows:
            title = row["title"].replace('"', "'")
            desc = row["description"].replace('"', "'").replace("\n", " ")
            lines.append(
                f'"{title}","{desc}","{row["link"]}","{row["image_path"]}"'
            )
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        packs["pinterest"] = {"json": str(path), "csv": str(csv_path), "n": len(pin_rows)}

    if "instagram_carousel" in nets:
        ig = {
            "network": "instagram_carousel",
            "channel": channel,
            "youtube_url": yt,
            "images": [str(p) for p in images[:10]],
            "caption": (
                f"{meta['title']}\n\n{meta['hook'][:300]}\n\n"
                f"Full documentary on YouTube — link in bio.\n"
                f"{'#history #documentary #sleepstories' if channel == 'napping_historian' else '#history #whatif #documentary'}"
            ).strip(),
        }
        path = out_dir / "instagram_carousel.json"
        path.write_text(json.dumps(ig, indent=2) + "\n", encoding="utf-8")
        packs["instagram_carousel"] = {"json": str(path), "n_images": len(ig["images"])}

    if "facebook_image" in nets:
        fb = {
            "network": "facebook_image",
            "channel": channel,
            "youtube_url": yt,
            "image": str(images[0]) if images else "",
            "caption": (
                f"{meta['title']}\n\n{meta['hook'][:280]}\n\n{yt}"
            ).strip(),
        }
        path = out_dir / "facebook_post.json"
        path.write_text(json.dumps(fb, indent=2) + "\n", encoding="utf-8")
        packs["facebook_image"] = {"json": str(path)}

    if "quote_card" in nets and images:
        quote = {
            "network": "quote_card",
            "channel": channel,
            "youtube_url": yt,
            "image": str(images[min(2, len(images) - 1)]),
            "overlay_text": (meta["hook"] or meta["title"])[:120],
            "note": "Optional: burn text with PIL later; v1 ships paths + copy.",
        }
        path = out_dir / "quote_card.json"
        path.write_text(json.dumps(quote, indent=2) + "\n", encoding="utf-8")
        packs["quote_card"] = {"json": str(path)}

    if "threads_x_image" in nets and images:
        tx = {
            "network": "threads_x_image",
            "channel": channel,
            "youtube_url": yt,
            "image": str(images[0]),
            "text": f"{meta['title']}\n\n{yt}".strip(),
        }
        path = out_dir / "threads_x.json"
        path.write_text(json.dumps(tx, indent=2) + "\n", encoding="utf-8")
        packs["threads_x_image"] = {"json": str(path)}

    manifest = {
        "job_dir": str(root),
        "channel": channel,
        "video_id": video_id,
        "youtube_url": yt,
        "title": meta["title"],
        "n_images": len(images),
        "packs": packs,
        "frozen_video_heavy": True,
    }
    man_path = out_dir / "manifest.json"
    man_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    packs["manifest"] = str(man_path)
    return manifest
