#!/usr/bin/env python3
"""Refresh cinematic SFX pack into assets/sfx/ from free commercial-use sources.

Default: generate procedural placeholders (always legal, $0).
Optional: download Mixkit free SFX (Mixkit License — commercial OK, no Adobe scrape).

Usage:
  .venv/bin/python scripts/download_cinematic_sfx.py
  .venv/bin/python scripts/download_cinematic_sfx.py --mixkit
  .venv/bin/python scripts/download_cinematic_sfx.py --force

License notes are written to assets/sfx/ATTRIBUTION.md.
Do NOT scrape Adobe / login walls.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SFX_DIR = ROOT / "assets" / "sfx"

# Mixkit free SFX — preview CDN (legal free tier; Mixkit License).
# IDs from mixkit.co/free-sound-effects/{cinematic,woosh}/ catalog (2026-08).
# Preview MP3s are redistributable under Mixkit License for project use; prefer
# full WAV via browser download if you need lossless masters.
MIXKIT_PACK: list[dict[str, str]] = [
    {
        "id": "1492",
        "name": "cold_whoosh.wav",
        "title": "Cinematic whoosh fast transition (Mixkit #1492)",
        "page": "https://mixkit.co/free-sound-effects/cinematic/",
        "preview": "https://assets.mixkit.co/active_storage/sfx/1492/1492-preview.mp3",
    },
    {
        "id": "1143",
        "name": "cold_impact.wav",
        "title": "Cinematic whoosh deep impact (Mixkit #1143)",
        "page": "https://mixkit.co/free-sound-effects/cinematic/",
        "preview": "https://assets.mixkit.co/active_storage/sfx/1143/1143-preview.mp3",
    },
    {
        "id": "788",
        "name": "chapter_hit.wav",
        "title": "Big cinematic impact (Mixkit #788)",
        "page": "https://mixkit.co/free-sound-effects/cinematic/",
        "preview": "https://assets.mixkit.co/active_storage/sfx/788/788-preview.mp3",
    },
    {
        "id": "1490",
        "name": "soft_whoosh.wav",
        "title": "Fast whoosh transition (Mixkit woosh pack)",
        "page": "https://mixkit.co/free-sound-effects/woosh/",
        "preview": "https://assets.mixkit.co/active_storage/sfx/1490/1490-preview.mp3",
    },
    {
        "id": "561",
        "name": "ambient_bed.wav",
        "title": "Cinematic heartbeat ambience (Mixkit #561)",
        "page": "https://mixkit.co/free-sound-effects/cinematic/",
        "preview": "https://assets.mixkit.co/active_storage/sfx/561/561-preview.mp3",
    },
]

UA = (
    "Mozilla/5.0 (compatible; napstorian-sfx-refresh/1.0; +local ops script)"
)


def _ffmpeg_to_wav(src: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-ar",
        "48000",
        "-ac",
        "2",
        str(dest),
    ]
    subprocess.run(cmd, check=True)


def write_attribution(rows: list[dict[str, str]], *, mode: str) -> None:
    lines = [
        "# SFX attribution / license",
        "",
        f"Pack mode: **{mode}**",
        "",
        "## Shipping files",
        "",
        "| File | Source | License | Notes |",
        "|------|--------|---------|-------|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['name']}` | {r.get('title', r['name'])} | "
            f"{r.get('license', 'see note')} | {r.get('notes', '')} |"
        )
    lines += [
        "",
        "## Rules",
        "",
        "- Prefer **Mixkit License** free SFX or **CC0** (Freesound).",
        "- **CC-BY**: keep this file + credit in YouTube description when required.",
        "- **Do not** scrape Adobe Express/Firefly or login-walled packs.",
        "- Historian channel does **not** use this cinematic pack by default.",
        "",
        "## Refresh",
        "",
        "```bash",
        ".venv/bin/python scripts/download_cinematic_sfx.py          # placeholders",
        ".venv/bin/python scripts/download_cinematic_sfx.py --mixkit # Mixkit previews→wav",
        "```",
        "",
        "Mixkit license: https://mixkit.co/license/#sfxFree",
        "",
    ]
    (SFX_DIR / "ATTRIBUTION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_placeholders(*, force: bool = False) -> list[dict[str, str]]:
    sys.path.insert(0, str(ROOT))
    from src.services.compose_sfx import ensure_sfx_assets

    if force:
        for name in (
            "cold_whoosh.wav",
            "cold_impact.wav",
            "chapter_hit.wav",
            "soft_whoosh.wav",
            "ambient_bed.wav",
        ):
            p = SFX_DIR / name
            if p.exists():
                p.unlink()
    ensure_sfx_assets(SFX_DIR)
    return [
        {
            "name": n,
            "title": f"Procedural placeholder ({n})",
            "license": "generated in-house (ffmpeg lavfi)",
            "notes": "Replace via --mixkit when convenient",
        }
        for n in (
            "cold_whoosh.wav",
            "cold_impact.wav",
            "chapter_hit.wav",
            "soft_whoosh.wav",
            "ambient_bed.wav",
        )
    ]


def download_mixkit(*, force: bool = False) -> list[dict[str, str]]:
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    for item in MIXKIT_PACK:
        dest = SFX_DIR / item["name"]
        if dest.exists() and dest.stat().st_size > 1000 and not force:
            rows.append(
                {
                    "name": item["name"],
                    "title": item["title"],
                    "license": "Mixkit License (free SFX)",
                    "notes": f"cached; page {item['page']}",
                }
            )
            continue
        tmp = SFX_DIR / f".tmp_{item['id']}.mp3"
        req = urllib.request.Request(item["preview"], headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                tmp.write_bytes(resp.read())
            _ffmpeg_to_wav(tmp, dest)
            tmp.unlink(missing_ok=True)
            rows.append(
                {
                    "name": item["name"],
                    "title": item["title"],
                    "license": "Mixkit License (free SFX)",
                    "notes": f"preview→wav; page {item['page']}",
                }
            )
            print(f"OK  {item['name']} ← Mixkit #{item['id']}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {item['name']}: {exc}", file=sys.stderr)
            tmp.unlink(missing_ok=True)
            # Fall back to placeholder for this slot
            ensure_placeholders(force=False)
            rows.append(
                {
                    "name": item["name"],
                    "title": item["title"],
                    "license": "placeholder fallback",
                    "notes": f"download failed: {exc}",
                }
            )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--mixkit",
        action="store_true",
        help="Download Mixkit free preview MP3s and convert to WAV",
    )
    ap.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = ap.parse_args()
    SFX_DIR.mkdir(parents=True, exist_ok=True)

    if args.mixkit:
        rows = download_mixkit(force=args.force)
        write_attribution(rows, mode="mixkit_preview_pack")
    else:
        rows = ensure_placeholders(force=args.force)
        write_attribution(rows, mode="procedural_placeholders")
        print(f"Placeholders ready under {SFX_DIR}")
        print("Run with --mixkit to pull Mixkit free previews (commercial OK).")
    print(f"Wrote {SFX_DIR / 'ATTRIBUTION.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
