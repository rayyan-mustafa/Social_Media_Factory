# Microstock Vector Engine

AI raster → clean commercial SVG → stock platforms. The second revenue line,
implementing `roadmap.txt` (GRAND MASTER STRATEGY).

Lives in its own folder with its own config (`config/microstock/`), state
(`output/microstock/`) and cron beat. It reuses the video farm's infrastructure
but shares **no state** with it, so a failure here can never stall video
production.

---

## Quick start

```bash
.venv/bin/python -m src.cli.microstock doctor      # what's missing
.venv/bin/python -m src.cli.microstock status      # gates, ledger, platforms

# Works today with zero API keys and zero cost:
.venv/bin/python -m src.cli.microstock run --prompt "flat vector cloud icons" --backend mock
.venv/bin/python -m src.cli.microstock beat --backend mock --skip-gates --no-llm
```

## The chain

```
brief_harvest → generator → vectorizer → svg_cleaner → qa_gate → ai_tagger → drip → ftp/outbox
                                                          ↑                            │
                                                     stock_smm ←── sales reports ←──────┘
```

| Module | Role |
|---|---|
| `brief_harvest.py` | Keeps a stock of asset briefs topped up, weighted by niche and biased by sales. Mirrors `agents/idea_stock.py`. |
| `generator.py` | Brief → raster. Backends: `gemini` (Nano Banana, default), `seedream`, `mock`. |
| `vectorizer.py` | Raster → multi-colour SVG via vtracer, in a subprocess. |
| `svg_cleaner.py` | Recursive DOM scrub, Douglas-Peucker, viewBox, background removal. |
| `qa_gate.py` | **Gate V** — nothing leaves the machine without passing. |
| `ai_tagger.py` | Vision → title/category/keywords, anti-spam rules enforced in code. |
| `drip.py` | Per-platform release queue respecting daily and lifetime caps. |
| `ftp_uploader.py` | Tier A: FTPS push, sequential. |
| `outbox.py` | Tier B: stages Adobe/Freepik batches for manual upload. |
| `stock_smm.py` | Ingests sales reports → niche scores → biases harvest. Mirrors `smm_harvest_bridge.py`. |
| `stock_factory.py` | The unattended beat. Mirrors `agents/sleep_factory.py`. |

## Safety model

Distribution is behind **two independent gates**, both off by default:

1. `settings.json → enabled` — master switch; the beat is a no-op while false.
2. `settings.json → distribution.enabled` — nothing leaves the machine while false.

Plus: every platform is individually `enabled: false`, `distribution.dry_run` is
`true`, and the asset ledger refuses to send the same asset to a platform twice.

**Shutterstock is permanently excluded.** It bans contributor uploads of
third-party AI assets; `config.load_platforms()` will never return it, whatever
the filters. Do not re-enable it — one upload risks the whole account.

## Turning it on

1. `GEMINI_API_KEY=...` in `.env` (get one at https://aistudio.google.com/apikey).
2. `OPENROUTER_API_KEY=...` for tagging (free tier is enough).
3. `.venv/bin/python -m src.cli.microstock doctor` until everything is green.
4. Run beats manually with `--backend mock` first, then for real, and inspect
   `output/microstock/clean_svg/` **by eye**. The gate proves an asset is
   structurally valid; it cannot tell you the art is worth buying.
5. Create contributor accounts, then set `enabled: true` per platform.
6. Add FTP credentials as `MICROSTOCK_FTP_<PLATFORM>_{USER,PASS}` in `.env`.
7. `microstock release --dry-run` and read the transcript before `--live`.
8. Only then: `settings.json → enabled: true`, `distribution.enabled: true`,
   `dry_run: false`, and install the cron line from `microstock crontab`.

## Tuning

Everything lives in `config/microstock/settings.json`; no code edits needed.

- **Too many nodes / files too big** → raise `cleaner.simplify_epsilon`.
  Measured on a photographic worst case: 1.0 → 33%, 2.0 → 53%, 3.0 → 61%
  node reduction. Flat vector art reduces considerably more.
- **Art looks mangled** → lower `simplify_epsilon`, or lower
  `gate.max_mean_pixel_diff` to make the fidelity check stricter.
- **Traces look noisy** → raise `vectorizer.filter_speckle`.
- **Need smooth curves** → `vectorizer.mode: "spline"` plus
  `cleaner.simplify_mode: "preserve_curves"`. Expect larger files.

---

## Money and tax (documentation only — no code does any of this)

`roadmap.txt` section 4 covers payout routing. It is deliberately **not
automated**: it involves your identity documents and tax residency, and getting
it wrong has consequences no script should own.

- Direct PayPal transfers are unavailable in Pakistan. The working path is
  platform royalties → **Payoneer** (USD/EUR/GBP) → local bank (Bank Alfalah /
  Askari) or **JazzCash** for instant PKR.
- Payout thresholds are typically $25–$50/month per platform.
- **Submit Form W-8BEN** in each contributor dashboard (Adobe Stock, 123RF,
  Vecteezy). Under the US–Pakistan tax treaty this reduces US withholding on
  sales to US buyers from the default 30% to the reduced treaty rate. This is
  per-platform and easy to forget — do it before your first payout, not after.

## Expectations

At 200 assets/day the 3,000–5,000 asset library target is roughly 3–4 weeks of
production. **Revenue lags months behind that** — royalties depend on search
indexing and rank, not upload date. Judge the first 200 assets on *acceptance
rate*, not earnings, and fix prompts before scaling the backlog.
