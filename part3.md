# ANTIGRAVITY_TASK_03.md — Production Tracker + Go-Live Switch

Paste this whole file into Antigravity as one task. This builds the
PRODUCTION path for the lead-gen pipeline, kept structurally separate from
TEST MODE (Task 02) so the two can never be accidentally mixed up.

**Do not run this in production mode until the founder has personally
completed PRE_CUSTOMER_QA.md and explicitly says outreach is approved to
start.** This task builds the capability — it does not turn it on.

---

## Step 1 — Read first

Read `src/agents/leadgen/tracker.py` and `src/agents/leadgen/run.py` from
Task 02's build. Confirm TEST_MODE structure before extending it.

---

## Step 2 — Add a `--mode` flag, defaulting to test

Modify `run.py` CLI to accept `--mode test` (default) or `--mode live`.

- `--mode test` (default, unchanged from Task 02): writes to
  `output/leadgen_test/leads_TEST_ONLY.csv`, TEST_MODE=true on every row
- `--mode live`: writes to a new `output/leadgen_live/leads_LIVE.csv`,
  TEST_MODE=false on every row

**Hard gate**: `--mode live` must require an additional explicit
confirmation flag `--i-have-completed-qa` to even run. If that flag is
missing, the script must refuse to run in live mode and print a message
pointing to `config/sop/PRE_CUSTOMER_QA.md`. This is a deliberate friction
point — it should not be easy to accidentally run live mode.

---

## Step 3 — Live tracker schema

`output/leadgen_live/leads_LIVE.csv` columns:
`business_name, phone, address, website_url, category, tags, score,
draft_text, date_scraped, date_sent, sent_by, replied, followup_sent,
priced, closed, tier, value_pkr_or_usd, notes`

Most fields (`date_sent`, `sent_by`, `replied`, etc.) start empty — they
get filled in manually by the founder as outreach actually happens. This
file is the same lead tracker referenced throughout AGENCY_MASTER §12-13,
now finally wired to real pipeline output instead of a manual spreadsheet
copy-paste.

---

## Step 4 — Draft generation stays the same, output destination changes

Reuse `draft.py` exactly as built in Task 02 (LLM-generated, ice-breaker
matched). The only difference in live mode is where the output lands —
`leads_LIVE.csv` instead of `leads_TEST_ONLY.csv`. No change to how drafts
are generated.

**Unchanged from Task 02, repeated here because it's critical**: this
pipeline NEVER sends anything, in test or live mode. It generates leads and
drafts only. Sending stays 100% manual, done by the founder, capped at
20-30/day, per AGENCY_MASTER §8/§15. Do not add any sending capability to
this task, live mode or otherwise.

---

## Step 5 — Weekly scoreboard helper

Build a small script `src/agents/leadgen/scoreboard.py` that reads
`leads_LIVE.csv` and prints a summary matching AGENCY_MASTER §13's weekly
scoreboard format:
- Sends this week (count of rows with `date_sent` in last 7 days)
- Replies this week
- Leads currently in each pipeline stage (sent / replied / priced / closed)
- Simple text output, no dashboard needed — this is a `python -m
  src.agents.leadgen.scoreboard` CLI command

---

## Step 6 — Report back

- [ ] Confirm `--mode live` requires the explicit confirmation flag
- [ ] Confirm test mode (Task 02 behavior) is completely unaffected
- [ ] Confirm no sending capability exists anywhere in this task
- [ ] Show the live CSV schema with an example row (using a real scraped
      business is fine here — no draft has been sent, this is just
      building the data structure)
- [ ] Confirm scoreboard script runs and produces readable output

---

## Hard constraints

- No sending capability, ever, in either mode
- Live mode must require the explicit `--i-have-completed-qa` flag
- Do not pre-fill `date_sent`, `replied`, `closed`, or any outcome field —
  those are manually entered by the founder as real outreach happens