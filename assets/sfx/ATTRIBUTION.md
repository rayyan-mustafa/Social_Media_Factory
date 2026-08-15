# SFX attribution / license

Pack mode: **procedural_placeholders**

## Shipping files

| File | Source | License | Notes |
|------|--------|---------|-------|
| `cold_whoosh.wav` | Procedural placeholder (cold_whoosh.wav) | generated in-house (ffmpeg lavfi) | Replace via --mixkit when convenient |
| `cold_impact.wav` | Procedural placeholder (cold_impact.wav) | generated in-house (ffmpeg lavfi) | Replace via --mixkit when convenient |
| `chapter_hit.wav` | Procedural placeholder (chapter_hit.wav) | generated in-house (ffmpeg lavfi) | Replace via --mixkit when convenient |
| `soft_whoosh.wav` | Procedural placeholder (soft_whoosh.wav) | generated in-house (ffmpeg lavfi) | Replace via --mixkit when convenient |
| `ambient_bed.wav` | Procedural placeholder (ambient_bed.wav) | generated in-house (ffmpeg lavfi) | Replace via --mixkit when convenient |

## Rules

- Prefer **Mixkit License** free SFX or **CC0** (Freesound).
- **CC-BY**: keep this file + credit in YouTube description when required.
- **Do not** scrape Adobe Express/Firefly or login-walled packs.
- Historian channel does **not** use this cinematic pack by default.

## Refresh

```bash
.venv/bin/python scripts/download_cinematic_sfx.py          # placeholders
.venv/bin/python scripts/download_cinematic_sfx.py --mixkit # Mixkit previews→wav
```

Mixkit license: https://mixkit.co/license/#sfxFree

