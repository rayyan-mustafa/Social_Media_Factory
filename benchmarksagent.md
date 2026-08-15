# Cursor Build Prompt: Post-Upload Benchmark & Anomaly Detection Agent

Paste this into Cursor to implement:

---

Build a Python module called `benchmark_agent.py` that runs after every video upload: sets a projected performance benchmark, tracks actual vs projected daily, and flags when underperformance looks like a "shadow algorithm" issue rather than a content problem — with a defined test protocol using a secondary channel to isolate the cause.

## Requirements

### 1. Input
A function `set_benchmark(video_id: str, channel_id: str, historical_data: list[dict]) -> dict` where:
- `historical_data`: list of past videos with `{video_id, views_by_day: [...], subs_at_upload, niche_tag}`

### 2. Benchmark projection logic
- Pull the last 5-10 videos of the SAME niche_tag (not channel-wide average — different formats/topics perform differently, mixing them skews the baseline)
- Compute median views_by_day curve (day 1, day 3, day 7, day 14) — use median not mean, so one viral outlier doesn't distort the baseline
- Adjust projection slightly for subscriber count growth since those past videos (simple ratio scaling: current_subs / subs_at_upload_of_comparable_video)
- Output: `{"video_id": ..., "projected": {"day1": int, "day3": int, "day7": int, "day14": int}}`
- Save to `benchmarks.jsonl`

### 3. Daily actual-vs-projected check
Function `check_performance(video_id: str, actual_views_today: int, day_number: int) -> dict`:
- Compare actual vs projected for that day number
- Compute percentage deviation: `(actual - projected) / projected * 100`
- Classify:
  - `on_track`: within ±20% of projection
  - `underperforming`: more than 20% below projection
  - `overperforming`: more than 20% above projection
- Log every check to `performance_log.jsonl`

### 4. Underperformance diagnosis logic (the core ask)
When `underperforming` is flagged for 2+ consecutive days on the SAME video, trigger `diagnose_underperformance(video_id)`:

Run through a checklist BEFORE concluding it's an algorithm/channel-health issue:
1. **Content-level checks first** (cheaper to verify, rule these out before blaming the algorithm):
   - CTR vs channel average (pull from YouTube Analytics API) — low CTR = thumbnail/title problem, not algorithm suppression
   - Average view duration vs channel average — low retention = content/pacing problem, not suppression
   - Compare upload time/day vs your historical best-performing slots — bad timing is a common false alarm
2. **Distribution-level checks** (only if content checks all look normal):
   - Impressions count (from YT Analytics) — if impressions themselves are far below normal channel average even though CTR/retention are fine, THIS is the actual signature of reduced algorithmic distribution, not content quality
   - Compare "traffic source: browse/suggested" percentage vs historical average — a sharp drop specifically in algorithmic surfacing (not search, not external) is the clearest sign
3. **Output a verdict**: `"likely_content_issue"` | `"likely_distribution_suppression"` | `"inconclusive_needs_more_data"` with the specific metric deviations listed as evidence — never conclude "shadowban" from vibes, only from impressions/traffic-source data specifically

### 5. Test-channel isolation protocol (only trigger this after diagnosis suggests distribution suppression, not content)
Function `flag_for_test_channel_validation(video_id: str) -> dict`:
- Output a structured recommendation, NOT an automatic action (this needs your manual decision, not full automation):
  - Suggest re-uploading a similarly-formatted NEW piece of content (not the same video — same style/niche/quality tier) to a secondary/test channel
  - Compare that test channel upload's impressions/distribution pattern against what the main channel would typically get at the same subscriber/history stage
  - If the test channel gets normal distribution and the main channel doesn't, on comparable content — that's real evidence of a main-channel-specific issue (which could be a strike history, manual action flag, or algorithm trust score issue) rather than a content/format problem
  - If BOTH channels show similarly poor distribution, that points to a content/niche/timing issue instead, not a channel-specific penalty
- Log the test result comparison to `test_channel_validation.jsonl` for a clear before/after record

### 6. Reporting
Function `daily_report(channel_id: str) -> str` — generates a plain-text/markdown summary:
- All videos currently in tracking window (day 1-14)
- Status per video (on_track/underperforming/overperforming)
- Any triggered diagnoses and their verdicts
- Any pending test-channel recommendations awaiting your manual action

### 7. Integration point
- Runs on a daily cron job (via your VPS), pulling data from YouTube Analytics API
- Feeds alerts into whatever notification system you're already using for your other agents (Slack/Telegram/email — match existing setup)
- Should NOT auto-pause uploads or auto-create test channels — this agent recommends, you decide. Full automation on channel-health decisions is risky without human judgment in the loop.

### 8. Data requirements
- Requires YouTube Analytics API access (OAuth already needed for your channel — reuse existing credentials if your other agents already authenticate)
- Key metrics to pull: views by day, impressions, impressions CTR, average view duration, traffic source breakdown

---

Important calibration note: most "the algorithm is suppressing me" suspicions turn out to be content/CTR/timing issues when actually checked against impressions data — build this agent to be skeptical by default (require the distribution-specific signature, not just low views) so you don't waste time testing channel-health theories when the real fix is a better thumbnail or hook.
