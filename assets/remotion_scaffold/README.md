# Remotion motion-graphics scaffold (P2)

P0 ships **Pillow + ffmpeg** map / HUD plates via:

- `src/services/map_motion.py`
- `src/services/motion_graphics.py`

This folder is reserved for a future Remotion (React) project when Node + Remotion
are installed on the VPS. Do **not** block farms waiting on Remotion.

## Planned comps

| Comp | Purpose |
|------|---------|
| `CampaignMapHUD` | Date stamp, faction counters, front line wipe |
| `TimelineCounter` | Year scrubber synced to narration markers |
| `FactionBars` | Color legend lower-third |

## Install (later)

```bash
cd assets/remotion_scaffold
npm init -y
npm i remotion @remotion/cli react react-dom
npx remotion studio
```

Wire rendered MP4s into the same compose path as map clips:
`output/jobs/<job>/images/scene_XXX_clip.mp4`.

## Decision

Prefer `$0` Pillow/ffmpeg until Rayyan signs off on Node toolchain + design tokens.
