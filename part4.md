# ANTIGRAVITY_TASK_04.md — Client Onboarding Script

Paste this whole file into Antigravity as one task. Builds a script so that
onboarding client #2, #3, #4... doesn't require manually copy-pasting and
editing the CONFIG object inside client-chatbot-demo.html by hand each time.

---

## Step 1 — Read first

Read `client-chatbot-demo.html`'s current CONFIG structure (from Task 01)
and `src/agents/leadgen/enricher.py`'s CONFIG-shaped output (from Task 02).
Confirm both use the same shape — if they've drifted, flag this before
building, since onboarding should produce a CONFIG usable by both.

---

## Step 2 — Build the onboarding CLI

New file: `src/agents/onboarding/new_client.py`

Interactive CLI (or accepts a `--from-lead <business_name>` flag to
pre-fill from an existing scraped lead in `leads_LIVE.csv`):

Prompts for (or pre-fills from lead data, founder confirms/edits each):
- Business name
- Services + prices + durations (list, add as many as needed)
- Hours (including after-hours/emergency note if relevant to vertical)
- Location, phone
- Booking policy (cancellation notice, etc.)
- Extra FAQs (list)
- Brand color (hex, optional — default to a neutral color if skipped)
- Client tier (1, 2, or 3)

Output: a complete CONFIG object written to a new file
`clients/<client-slug>/config.js` (or `.json` — match whatever format is
easiest to import into the HTML template).

---

## Step 3 — Generate the client's deployable files

From the CONFIG, generate:
- `clients/<client-slug>/chatbot.html` — copy of the base
  client-chatbot-demo.html template with this client's CONFIG injected,
  SAMPLE banner removed (this is a real client now, not a demo)
- `clients/<client-slug>/README.md` — a short internal note: client name,
  date onboarded, tier, any special notes founder adds

Do NOT auto-deploy this anywhere. Output stays local until the founder
manually reviews and deploys it.

---

## Step 4 — Tests

- `test_onboarding.py` — verify the CLI produces a valid CONFIG shape
  matching what the chatbot HTML expects, using a fixture business
- Verify `--from-lead` correctly pre-fills from a sample `leads_LIVE.csv`
  row without inventing any data not present in that row

---

## Step 5 — Report back

- [ ] Confirm CONFIG shape matches both the chatbot template and the
      leadgen enricher's shape (or flag the mismatch found in Step 1)
- [ ] Show one example run: onboarding a fictional test client end to end
- [ ] Confirm no auto-deploy happens — output is local files only

---

## Why this matters

Per AGENCY_MASTER's own economics: manual CONFIG editing is fine for one
client, but becomes the actual bottleneck once you're onboarding 2-3
clients a month (§7, M3 target). This script removes that friction before
it becomes a real problem, without changing anything about the read-only,
human-confirms-bookings design principle already established.