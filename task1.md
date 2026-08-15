# ANTIGRAVITY_TASK_01.md — GATE B: Portfolio Site + Proxy Wiring

Paste this whole file into Antigravity as one task. It is self-contained —
you don't need to read other repo files first except where explicitly
listed below.

**Context**: AGENCY_MASTER.md rev 2, GATE A already done (leadgen pytest x21,
watchdog, email-check, HVAC audit render, demo proxy tested locally on VPS).
GATE B is NOT done: portfolio site isn't deployed publicly and the chatbot
HTML files still likely call the Claude API directly from browser JS, which
exposes the API key. This task closes GATE B.

---

## Step 1 — Read these files first (read-only, don't edit yet)

- `scripts/demo_chat_proxy.py`
- `client-chatbot-demo.html`
- `sales-chatbot-portfolio.html`

Report back: does the proxy currently run as a local Flask/FastAPI/Node
server? What port? What's the request/response shape it expects? This
determines how Step 2 wires the HTML to it.

---

## Step 2 — Rewire both HTML files to call the proxy, not the API directly

In both `client-chatbot-demo.html` and `sales-chatbot-portfolio.html`, find
the `fetch("https://api.anthropic.com/v1/messages", ...)` call.

Replace it with a fetch to the proxy's endpoint instead (use whatever
endpoint Step 1 revealed — likely something like `http://localhost:PORT/chat`
locally, and a relative path like `/api/chat` once deployed as a serverless
function).

Requirements:
- No Anthropic API key anywhere in either HTML file after this change
- The proxy should receive: the system prompt (or a reference to which
  CONFIG to use) + the conversation history, and return the same shape
  the HTML currently expects (`data.content` array with text blocks)
- Preserve the existing LEAD_CAPTURED token detection logic exactly as-is
- Preserve the existing error-handling fallback message

---

## Step 3 — Build the fictional HVAC demo CONFIG

In `client-chatbot-demo.html`, replace the current CONFIG object (if it's
still the salon example) with a fictional HVAC business:

- Invented business name (not a real company)
- Services: AC repair, furnace repair, installation, maintenance plans,
  emergency after-hours service — with realistic prices/durations
- Hours: include a note that emergency/after-hours requests are captured
  even outside normal hours (this is the core gap-fill from
  AGENCY_MASTER §3 — "after-hours emergencies" line)
- Add a `SAMPLE` flag/banner element that renders clearly in the UI header
  — visible text like "Sample demo — not a real business" — small but honest

Do not add any testimonial, review, or star-rating content anywhere in this
file or its CONFIG. None exist yet and none should be invented.

---

## Step 4 — Build the portfolio page

Create a new file: `portfolio/index.html` (single file, no framework unless
one already exists in this repo — check first).

Sections, in order:
1. Header — agency name placeholder (`[[AGENCY_NAME]]` token, founder fills
   in later) + one-line pitch about home-services AI websites/chatbots
2. "What I do" — 2-3 sentences, plain language
3. Live demo section — embed `client-chatbot-demo.html` in an iframe, or
   link to it prominently, clearly labeled SAMPLE
4. Pricing — three tiers matching AGENCY_MASTER §5:
   - Tier 1: site + chatbot (placeholder price €250-800 setup + €25-75/mo
     range, marked `[[CONFIRM PRICING]]` for founder to finalize)
   - Tier 2: adds missed-call text-back, multi-channel, reminders
   - Tier 3: adds voice receptionist (mark clearly as "coming soon /
     available once established")
5. Contact — embed `sales-chatbot-portfolio.html` here
6. Leave this exact HTML comment where a testimonials section would go:
   `<!-- TESTIMONIALS: add only after client #1 delivers a real, written
   testimonial. Do not fill with placeholder or fake content. -->`

---

## Step 5 — Deployment config

Check whether Vercel or Netlify config already exists in this repo (check
for `vercel.json`, `netlify.toml`, or a `.vercel`/`.netlify` folder). If
neither exists, set up a minimal Vercel config:

- Static files (`portfolio/index.html`, the two chatbot HTML files) served
  as static assets
- The proxy logic from `scripts/demo_chat_proxy.py` ported to a Vercel
  serverless function (`api/chat.js` or `api/chat.py` depending on the
  proxy's current language) so it can run in production, not just locally
- API key stored as a Vercel environment variable, never committed to the
  repo — confirm `.env` or equivalent is in `.gitignore`

Do not actually deploy yet — prepare the config and report what's ready,
so the founder can review before the first real deploy.

---

## Step 6 — Report back

After Steps 1-5, report:
- [ ] Confirmed no API key exists in any client-facing HTML file
- [ ] Confirmed LEAD_CAPTURED flow still works through the proxy (describe
      how you verified this, or flag if it needs a live human test)
- [ ] Portfolio page built with all 6 sections
- [ ] Deployment config ready but NOT yet pushed live
- [ ] List any placeholders left for the founder to fill in
      (`[[AGENCY_NAME]]`, `[[CONFIRM PRICING]]`, etc.)

---

## Hard constraints (do not violate)

- No fake testimonials, reviews, or client quotes anywhere
- No API key committed to any file, ever
- No automatic message-sending code of any kind
- No Stripe integration
- No RAG/vector-store setup
- No voice/Twilio code in this task — out of scope, Tier 3 only
- Do not deploy to production without founder review first

---

## After this task — what's still manual (not Antigravity's job)

- Founder runs the full QA checklist from `config/sop/PRE_CUSTOMER_QA.md`
  in an actual browser against the deployed version
- Founder fills in real pricing, agency name, and reviews all copy for tone
- Founder records the 60-90s screen demo (Today Booster §6, Step T5)
- Founder approves the first real production deploy — Antigravity prepares,
  human pulls the trigger