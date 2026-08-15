# Lead-gen cost report (agency hunter)

**Separate from the YouTube factory.** The YouTube overspend lesson (GPU stills, paid LLM, always-on RunPod) does **not** apply here by design: this agent is **VPS CPU only**. It must never start RunPod, Kokoro, Flux, Seedream, or auto-email leads. CostGuardian / `runpod_watchdog` / `ceo_smm_beat` / `sleep_factory` are out of scope.

SOP: `config/sop/leadgen_sop.md` · Rates: `config/leadgen/cost_rates.json`

## Resource distribution

| Resource | Share | Notes |
|---|---|---|
| **VPS** | **100%** | Hunt, homepage fetch, gap score, drafts, phantom HTML, cost sheets |
| **RunPod** | **0%** | Forbidden — never spawn a pod for leadgen |
| **Kokoro / TTS** | **0%** | Forbidden |
| **Places API** | Optional | Paid only if a dedicated key is set; else Nominatim/OSM at $0 |
| **Nominatim** | Default free path | Rate-limit politely; $0 incremental |

## Caps

| Cap | Value |
|---|---|
| Per job | **$1.00** |
| Per UTC day | **$2.00** |
| Per calendar month | **$5.00** |
| Hunts / UTC day | **3** |
| Leads / UTC day | **100** |
| Human sends / day | **10** (until a sending domain exists) |

Watchdog fails closed on ban flags (`LEADGEN_ALLOW_RUNPOD`), hunt/lead daily caps, and monthly USD remaining ≤ 0. Over-benchmark wall-clock on the last spend row is a **warning only** (does not kill YouTube pods).

## Unit rates (`config/leadgen/cost_rates.json`)

| Line | USD |
|---|---:|
| Places Text Search (per page) | 0.032 |
| Places Details (per place) | 0.017 |
| Nominatim | 0.000 |
| VPS CPU (incremental) | 0.000 |
| RunPod (must be) | 0.000 |
| Kokoro (must be) | 0.000 |
| Hunt 25-lead elapsed benchmark | ≤ 240 s |

## Worked examples

### Nominatim · 25 leads

Forecast / actual Places lines are **$0.00**. `runpod_usd` and `kokoro_usd` stay **0**. **Job total = $0.00.**

### Places · 25 leads (worst case, 1 text-search page + 25 details)

\[
0.032 + (25 \times 0.017) = 0.032 + 0.425 = \mathbf{\$0.457}
\]

Under the **$1/job** cap. AFTER sheet uses actual `places_searches` / `places_details` from the hunt (fallback to Nominatim mid-run is fine — AFTER reflects the real source).

## Austin today-test folder

Path: `output/leadgen/20260814T152640Z_us_salon_austin-tx/`

This first hunt **predates cost sheets** — there is no `COST_SHEET.md` / `cost_before.json` / `cost_after.json` in that folder yet. The **next** hunt will write BEFORE/AFTER sheets into the new run directory and append `output/ops/leadgen_spend.jsonl`.

Artifacts present: `leads.csv`, `drafts.md`, `summary.json`, phantom under `phantom/`.

## How to read BEFORE / AFTER on every hunt

1. **BEFORE** (CLI prints + `cost_before.json`): forecast from `--limit` and assumed source (`places` if `places_api_key()` is set, else `nominatim`). Gate with `can_afford` against per-job and remaining monthly caps.
2. **AFTER** (CLI prints + `cost_after.json`): actual source, lead count, Places call counts, `elapsed_s`, line items. Appended to `leadgen_spend.jsonl`.
3. **Combined markdown**: `COST_SHEET.md` in the run folder (BEFORE + AFTER + delta).
4. CLI also prints `remaining_month_usd` after the AFTER sheet.

## Commands

```bash
.venv/bin/python -m src.cli.leadgen hunt --region us --vertical salon --city "Austin, TX" --limit 25
.venv/bin/python -m src.cli.leadgen watchdog
.venv/bin/python -m src.cli.leadgen smm
.venv/bin/python -m src.cli.leadgen watchdog --dry-run
.venv/bin/python -m src.cli.leadgen smm --dry-run
```

- **hunt** — preflight → BEFORE sheet → hunt → write run + AFTER sheet (no auto-email).
- **watchdog** — SOP/cap tick; exit 2 if not ok; never kills YouTube pods.
- **smm** — leadgen-only scorecard (`agent=leadgen_smm`); not YouTube `ceo_smm`.

## Explicit ban

**Never start RunPod for leadgen.** Do not set `LEADGEN_ALLOW_RUNPOD`. Do not import factory pod spawners from this agent path.
