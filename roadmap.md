# ANTIGRAVITY_TASK_02.md — Lead-Gen Pipeline (SAFE TEST MODE)

Paste this whole file into Antigravity as one task. This builds the
scrape → score → enrich → draft pipeline in TEST MODE ONLY — every output
routes to the founder's own contact info, never a real prospect. This task
does NOT unlock real outreach; it validates the machinery works end to end.

**Context**: AGENCY_MASTER.md §10, §15. Send stays human-only, always — this
task doesn't change that, it just proves the pipeline produces good output
before GATE B / client #1 outreach begins for real.

---

## Step 0 — Read first (read-only)

Check `src/agents/leadgen/` for existing scrape/score modules and the 21
pytest tests mentioned in AGENCY_MASTER §16. Report what already exists vs.
what's missing before writing any new code. Do not rebuild anything that
already passes its tests.

---

## Step 1 — Scraper (real data, safe destination)

Build/confirm a scraper module that:
- Takes a city + niche as input (start with: HVAC, one real city of the
  founder's choosing — this can be a real search, real businesses, that
  part is fine and necessary to test properly)
- Uses Nsomething (i dont know the name but it starts with N and it is used to scrape) (not raw social-media scraping)
- Pulls: business name, phone, address, review count, rating, website URL,
  category
- Outputs to a local file or test sheet — NOT to any live tracker used for
  real outreach yet — name it clearly, e.g. `leads_TEST_ONLY.csv`

This step only reads public business listing data — no message is sent to
anyone at this stage, so this part is safe to run against real businesses.

---

## Step 2 — Scorer

Apply the rules-based filter from AGENCY_MASTER §3/§10:
- Tag each lead `no_site` / `social_only` / `poor_site` / (or `skip` if
  none apply — e.g. already has a strong site + booking system)
- Sort qualified leads by opportunity (fewer reviews + decent rating = real
  but behind on tech, per earlier scoring logic)

Output to the same `leads_TEST_ONLY.csv` / test sheet, scored column added.

---

## Step 3 — Enrich (CONFIG generation per lead)

For each qualified test lead, auto-generate a CONFIG object (same shape as
the fictional HVAC demo CONFIG) filled with that business's REAL public
data — name, services inferred from category, hours if publicly listed.

**Important honesty rule**: only use data that's actually publicly
available from the scrape. If hours/services aren't found, leave those
fields marked `[[NOT FOUND — confirm before real use]]` rather than
inventing plausible-sounding details.

---

## Step 4 — Draft outreach message (TEST MODE — safe destination)

Generate one personalized outreach draft per scored lead, using the
ice-breaker gap lines matched to their detected gap type (from
AGENCY_MASTER §3's ice-breaker table).

**Critical safety instruction — read carefully**:
This step must NOT send anything to the real business's contact info found
in the scrape. Instead:
- Write each generated draft to the test output file/sheet only, clearly
  labeled with which real business it was generated for (for founder's own
  review of draft quality)
- If the task includes any actual email-sending capability (per the
  `leadgen_email_sop.md` SPF/DKIM setup mentioned in AGENCY_MASTER), the
  "to" address for every single test send must be hardcoded to the
  founder's own personal email address — never the real scraped business
  contact — for this entire task
- Do not send anything via WhatsApp, SMS, or any social DM channel in this
  test task — draft generation and, if applicable, self-addressed test
  email only

---

## Step 5 — Tracker integration (test row only)

Write each test lead + its score + its draft to the tracker structure
(reuse existing schema if `src/agents/leadgen/` already defines one).
Mark every row from this task clearly as `TEST_MODE = true` so it's
obviously distinguishable from real future outreach records and won't be
mistaken for something already sent to a real business.

---

## Step 6 — Report back

- [ ] Number of real businesses scraped and scored
- [ ] Number of qualified leads (by gap type)
- [ ] Sample of 2-3 generated CONFIGs + drafts, shown in the report for
      founder review
- [ ] Confirmation: zero messages sent to any real business contact info
      anywhere in this task
- [ ] Confirmation: any test email/message sent, if applicable, went only
      to the founder's own address — state that address was hardcoded, not
      pulled from scraped data
- [ ] List of pytest/test coverage added or confirmed for this pipeline

---

## Hard constraints (do not violate — same as always)

- NEVER send anything to a real scraped business's contact info — not
  email, not WhatsApp, not SMS, not any social DM, anywhere in this task
- No auto-send loop of any kind, even "for testing" — every send in this
  task is either to the founder's own hardcoded address or doesn't happen
  at all (draft-only, saved to a file)
- No fake reviews or testimonials anywhere
- Real business data scraped in Step 1 is fine (public listing info) —
  the constraint is entirely about where outreach OUTPUT goes, not about
  the input data