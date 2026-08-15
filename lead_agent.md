# Autonomous Lead Agent — Build Spec & SOP

## Purpose
Find local businesses (salons, clinics, dentists, gyms, HVAC, small shops) that
lack a proper website/booking system, auto-generate a personalized sample page
for each, and produce a ready-to-send (human-approved) outreach message —
without getting any account banned.

Two offers this pipeline feeds:
1. AI Website + Booking Chatbot (primary — cold outreach)
2. Done-for-you Social Content Engine (upsell — only pitched to existing clients)

---

## Pipeline Overview

```
[1] SCRAPE  -->  [2] SCORE/FILTER  -->  [3] ENRICH (sample page)  -->
[4] DRAFT OUTREACH  -->  [5] HUMAN REVIEW & SEND  -->  [6] TRACK
```

---

## Step 1 — Scrape leads (FULLY AUTOMATABLE)

- Input: city + niche (e.g. "salons in Rawalpindi")
- Source: Google Places API (preferred, reliable, ToS-safe) — Playwright/Puppeteer
  scraping of Maps/IG as fallback only, since scraping social platforms directly
  risks IP/account flags
- Extract per lead: business name, category, phone, address, review count,
  rating, website URL (if any), Instagram/Facebook link (if listed)
- Store raw output in a Google Sheet or Airtable table `leads_raw`
- Run on a schedule (e.g. daily/weekly per city+niche batch) — this step can run
  headlessly with zero human involvement

## Step 2 — Score & filter (FULLY AUTOMATABLE)

Rules-based filter, no AI needed:
- INCLUDE if: no website field OR website is a dead link OR website is just a
  Facebook/Instagram page OR site has no visible "Book Now" / contact form
- EXCLUDE if: review count > ~200 (likely already has a marketing setup),
  belongs to a national chain/franchise keyword list, or category is
  government/medical-licensed-only (compliance risk)
- Score remaining leads by opportunity: fewer reviews + decent rating (3.5-5★)
  + active-looking profile = higher priority (they're real and growing, just
  behind on tech)
- Output: `leads_qualified` table, sorted by score

## Step 3 — Enrich: auto-generate sample page (SEMI-AUTOMATABLE)

- For each qualified lead, auto-fill your reusable site template with:
  business name, services (pull from IG captions/Google listing categories),
  brand colors (screenshot-sample dominant colors from their profile pic/posts
  if available, else default palette), phone/contact
- Auto-generate a starter chatbot config (FAQ answers from their listed hours,
  services, location) using Claude API
- Deploy each sample to a throwaway subdomain/preview link
  (e.g. `bellashair.yourdomain.com`)
- **Human checkpoint**: quick manual glance before sending — auto-generated
  content occasionally pulls a wrong detail (wrong hours, misspelled service).
  30 seconds per lead, not a full rebuild.

## Step 4 — Draft outreach message (FULLY AUTOMATABLE)

- Claude API call per lead: input = business name, the one specific gap you
  found (no booking link, unanswered comments, no website), + your DM template
  tone
- Output: one personalized DM draft per lead, saved to `outreach_queue`
- Never auto-personalize with fake specifics — only reference things actually
  found in Step 1 scrape data (real gap, not invented)

## Step 5 — Send (HUMAN ONLY — DO NOT AUTOMATE)

**This is the one step that must stay manual.** Reasons:
- Instagram/Facebook/WhatsApp aggressively detect and ban accounts that send
  automated/bulk DMs — losing your sending account kills the entire pipeline
- A human sending 20-30/day from a real, warmed-up account stays under spam
  detection thresholds; a bot sending the same volume gets flagged within days
- Process: review the drafted message from Step 4, tweak if needed, copy-paste
  send manually from your business IG/FB account
- Cap: 20-30 DMs/day per platform account, spaced out (not all at once)
- Follow-up: manually send one follow-up after 3-4 days if no reply — track
  this in Step 6

## Step 6 — Track pipeline (FULLY AUTOMATABLE, human updates status)

Sheet/Airtable columns: `lead name | scraped date | sample link | DM sent date
| replied? | follow-up sent? | priced? | closed? | tier sold | value`

- Auto-calculate weekly conversion rate (replies/sent, closes/replied) so you
  know if the message or targeting needs adjusting
- Once `closed = true`, auto-flag lead for content-engine upsell after 2-3
  weeks of successful chatbot delivery (Step 7, separate flow — not part of
  cold pipeline)

---

## What NOT to automate (summary)

| Step | Automate? | Why |
|---|---|---|
| Scraping leads | Yes | No platform risk via Places API |
| Filtering/scoring | Yes | Pure rules logic |
| Sample page generation | Yes (+ 30s human glance) | Occasional wrong detail |
| DM draft writing | Yes | Just text generation |
| **Sending DMs** | **No** | Ban risk kills the whole pipeline |
| Follow-ups | No | Same ban risk |
| Pricing negotiation / closing | No | Needs real judgment + relationship |
| Actual site/chatbot build for paying client | No (reuse template, but review) | Client-facing quality control |

---

## Tech stack suggestion (buildable in Cursor)

- Scraper: Google Places API + Node/Python script
- Storage: Airtable (better than raw Sheets for status tracking + filtering)
- Sample site generator: your existing template, script that injects variables
  per lead, deploy via Vercel/Netlify preview URLs
- Chatbot: Claude API + small widget embed, FAQ config generated per client
- Outreach draft generator: Claude API call per lead
- Scheduling: cron job or Cursor background task for Steps 1-2 (e.g. runs
  weekly per target city)

## First build milestone

Get Steps 1-2 fully working end-to-end for ONE niche + ONE city first (e.g.
salons in your own city). Confirm the qualified-lead list is actually good
(manually check 10 of them) before building Steps 3-4 on top. Don't build the
whole pipeline before validating the scrape/filter quality — bad leads in =
wasted sample pages out.
