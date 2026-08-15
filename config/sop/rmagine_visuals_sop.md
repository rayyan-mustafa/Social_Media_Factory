# RMagine autonomous visuals SOP (both channels + staged empire)

Policy: `per_scene_wiki_met_primary_flux_on_reject`

## Ladder (every scene, zero manual refine)

1. Per-scene query distill (`src/services/query_distill.py`)
2. Wiki + Met fetch + license verify
3. Vision PASS ≥ 8 with `historical_figure` when known
4. PASS → place archival still as `scene_XXX.jpg`
5. REJECT / no candidate after all phases → **Flux only for that slot**
6. Portrait reuse limit → symbolic Wiki+Met B-roll (still archival-first)
7. Curated PD motion packs remain additive overlays (not a still-slot cap)

## Non-negotiable

- **No product hero cap** that stops Wiki+Met while scenes remain empty
- Rejected PD never appears in the final cut
- `ASSET_FETCHER=0` emergency kill switch only
- Same ladder for napstorian, napping_historian, and every future Brand Account skin

## Ops evidence

- Job stamp: `assets/fetched/vision_judge.json` with `policy` + `archival_pass_rate`
- Expansion gate requires archival majority before Wave 1 activation
- See `output/ops/CHANNEL_EMPIRE_STATUS.md` and `empire-status` CLI
