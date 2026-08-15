# Lead-gen SOP (agency hunter) — SEPARATE from YouTube factory

**This SOP is not `NEW_FORMAT_EVERY_VIDEO_SOP`.** Do not hook `sleep_factory`, `runpod_watchdog`, Kokoro, Flux, or Seedream.

Lesson from YouTube overspend: GPU stills + paid LLM + always-on pods ate the budget. Lead-gen is **VPS CPU only**.

## What this product is

Hunt public Google/OSM listings → gap-score (no site / no booking / no chat) → draft email or WhatsApp → optional phantom HTML demo. **Rayyan sends by hand.** No auto-blast.

## Hard bans (watchdog must FAIL CLOSED)

| Tool | Lead-gen | YouTube factory |
|---|---|---|
| RunPod / Comfy / Flux | **FORBIDDEN** | allowed under CostGuardian |
| Kokoro / CosyVoice / ElevenLabs | **FORBIDDEN** | factory TTS |
| WaveSpeed paid LLM / Seedream | **FORBIDDEN** | factory script/thumbs |
| OpenRouter paid VL | **FORBIDDEN** | vision judge is free-locked anyway |
| SMTP to leads | **FORBIDDEN** day-1 | ops alerts only |
| TikTok/IG auto-DM | **FORBIDDEN** | n/a |

If any hunt tries to import `src.runpod`, start a pod, or call TTS — **abort**.

## Allowed spend (benchmarks)

| Resource | Benchmark / job (25 leads) | Monthly cap |
|---|---|---|
| VPS CPU + egress | $0.00 incremental (already-paid VPS) | n/a — do not add a second VPS |
| Nominatim fallback | $0.00 (rate-limit 1 req/s) | n/a |
| Google Places API (New) | **do not enable until key is dedicated**; if enabled: ≤ $0.032 / text search × pages. Cap **$5/mo** | **$5.00** |
| Google Sheets append | ~$0 (existing project) | n/a |
| OpenRouter `openrouter/free` (optional copy polish later) | $0.00 | 0 paid models |
| Phantom HTML | $0.00 disk | n/a |

**Job must print a cost sheet BEFORE start and AFTER finish.** If forecast > monthly remaining cap → do not hunt.

## Performance benchmarks

| Step | Target |
|---|---|
| Places/Nominatim hunt 25 | ≤ 90s |
| Homepage fetch 25 | ≤ 120s (8–10s timeout each, skip dead) |
| Drafts + phantom | ≤ 5s |
| End-to-end 25-lead job | ≤ 4 min |
| RAM | ≤ 400 MB extra; **never** overlap a GREEN GPU farm on purpose |

## Cadence (SMM agent)

- Max **3 hunts / UTC day**, **100 leads / UTC day**
- One region+vertical per hunt (gap hunter, not spray)
- Human send ≤ 10/day until a sending domain exists (week 2+)
- Log `sent` / `reply` on the `leadgen` sheet — SMM reads that, does not send

## Listing + website check-up (add-on)

Every kept lead gets a **1-page HTML** in `audits/` a shop owner can read (Yes/No, not Lighthouse).

Tracks: `no_site` (Google has no website) · `social_only` (Google URL is Facebook / Instagram / YouTube / TikTok / Linktree) · `poor_site` (real site, weak booking/chat/phone/https).

**Bans for this add-on:** do not scrape Instagram/Facebook/YouTube; do not auto-email the report; do not create Shopify stores for cold leads. Attach the HTML check-up (screenshot or later a hosted link). Build a real site **after they reply**.

Cost: $0 extra (same homepage fetch). Same $1 / $2 / $5 caps.

`python -m src.cli.leadgen audit --run-dir DIR --lead-id ID`

## Cost sheet columns (every job)

`vps_usd`, `runpod_usd` (must be 0), `kokoro_usd` (must be 0), `places_usd`, `llm_usd`, `other_paid_usd`, `total_usd`, `elapsed_s`, `leads_n`, `source`.

Before = forecast from limit × unit rates. After = actual counts × rates + wall clock.

## Region / vertical

`--region us|eu|gulf` `--vertical salon|estate|trucking|ecommerce|freelance|tiktoker|dentist|hvac|autoshop|clinic|gym|wedding`

Compliance lives in `config/leadgen/regions.json`. EU = no purchased lists. Gulf = WhatsApp, quiet hours.

## Watchdog tick

`python -m src.cli.leadgen watchdog`

Checks: bans, daily/monthly caps, last job over-benchmark, RunPod processes **not** started by this agent (warn only — do not kill YouTube pods).

## SMM tick

`python -m src.cli.leadgen smm`

Separate from `ceo_smm_beat`. Scorecard of hunts, gap density, suggested next city. No YouTube metrics.

## Sending email (SPF / DKIM / DMARC)

Follow [`config/sop/leadgen_email_sop.md`](leadgen_email_sop.md) **before** any outreach From `@yourdomain`.

- **SPF** authenticates the mail host (Google Workspace `include:_spf.google.com`, not this VPS).
- **DKIM** signs the message.
- **DMARC** tells Gmail what to do on fail (`p=none` → later `quarantine` / `reject`).

Also: no Postfix on this VPS, no Mailchimp cold lists, no `@gmail.com` as the salon From, CAN-SPAM footer, human send ≤ 10/day.

```bash
.venv/bin/python -m src.cli.leadgen email-check --domain yourdomain.com
```
