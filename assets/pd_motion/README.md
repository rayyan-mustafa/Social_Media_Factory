# PD / commercial-safe motion drop-ins

Drop short clips here for Live/compose inserts:
- **napstorian:** **10–15** (target **12**) denser inserts when available
- **napping_historian:** **4–6** soft/slow Ken-Burns-compatible pans only (History Calling calm — not action montages)

## Hard rules (YouTube monetization)
Only use assets that are clearly free for **commercial** reuse:
- Public Domain / PD-Art
- CC0
- US government works
- Met Open Access (CC0)

**Never** drop: CC BY-NC, CC BY-ND, fair use, unstated rights, Pexels/Mixkit labeled as “PD”.

## How to add a clip
1. Put `my_clip.mp4` in this folder (or `job/clips/pd/` / `job/images/pd_motion/raw/`).
2. Add a sidecar license file **required**:

`my_clip.mp4.license.json` **or** `my_clip.license.json`:

```json
{
  "title": "Short description",
  "license": "PD (Wikimedia Commons)",
  "commercial_ok": true,
  "source_url": "https://commons.wikimedia.org/wiki/File:Example.jpg"
}
```

Without a valid sidecar + `commercial_ok` license, the clip is **blocked**.

## Env
- `COMPOSE_PD_MOTION_CLIPS=1` (default on)
- `COMPOSE_PD_MOTION_MIN_CLIPS=10` / `COMPOSE_PD_MOTION_MAX_CLIPS=15` (napstorian)
- `COMPOSE_PD_MOTION_HISTORIAN=1` (soft pans; set `0` to disable historian only)
- `COMPOSE_PD_MOTION_HISTORIAN_MIN_CLIPS=4` / `COMPOSE_PD_MOTION_HISTORIAN_MAX_CLIPS=6`
