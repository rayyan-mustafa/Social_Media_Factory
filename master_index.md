# ALL-IN-ONE — AI Website + Chatbot Agency Build

> **SUPERSEDED:** Living source of truth is now [`AGENCY_MASTER.md`](AGENCY_MASTER.md). Update that file only when new plans/assets arrive. This file is historical.

This is the single consolidated reference. Everything discussed is either
inline here or pointed to its own file below. Read this top to bottom before
building or outreach begins.

---

## 0. File index (everything produced in this project)

1. `master-plan-cursor.md` — full business context + roadmap + tech decisions
   + founder to-do list, for feeding directly to Cursor
2. `lead-agent-SOP.md` — scrape → score → enrich → draft → human-send →
   track pipeline, with automate-vs-manual boundaries
3. `test-plan-and-portfolio-build.md` — prerequisites, fake-company test
   checklist, multilingual portfolio page plan, full costing
4. `client-chatbot-demo.html` — working demo chatbot template (text + voice)
   used for client-facing demos, retrained per client by editing one CONFIG
   object
5. `sales-chatbot-portfolio.html` — working sales/pitch chatbot for the
   founder's own portfolio site, multilingual, handles objections and
   competitor positioning
6. This file — the master index and summary of every decision made

---

## 1. The business, in one paragraph

Solo agency (founder: Rayyan, based in Pakistan) selling AI websites +
booking chatbots to small local businesses in Europe/US/UK/Canada/Australia.
Sold via cold outreach (not Upwork/Fiverr — saturated), leading with a live
working demo instead of a pitch. Pricing modeled on researched competitors
(Panabotics, Riin.eu, AI Beauty Bot, CitaFlow) — roughly €250-800 setup +
€25-75/mo depending on tier.

## 2. Niche decision

- Originally scoped for salons/beauty — now known to be a crowded niche
  (multiple dedicated SaaS competitors found in research)
- **Primary niche pivoted to home services** (auto repair, HVAC-equivalent
  trades, plumbing, electrical) — same missed-call/booking problem, far less
  AI-chatbot-specific competition, strong and easy-to-explain ROI story
  (a missed call = a real lost job worth real money)
- Salons remain a valid secondary niche opportunistically

## 3. Service tiers (the actual offer)

**Tier 1 — Launch offer:**
- One-page branded website + booking capture
- AI chatbot (web widget + WhatsApp), system-prompt trained (no RAG needed
  at this scale)
- Delivered with a live demo before payment is discussed

**Tier 2 — Add after 2-3 clients:**
- Multi-channel bot (Instagram, Messenger, Telegram)
- Missed-call auto-text-back (Twilio)
- Automated reminders (WhatsApp/SMS/email)
- Simple CRM view (Airtable/Sheets)

**Tier 3 — Differentiator, build only once Tier 1 has paying clients:**
- AI voice receptionist: Twilio Voice + Claude + Kokoro TTS
- Most competitors don't offer this — it's the standout feature once ready

**Built-in differentiators (process, not code — zero extra cost):**
- Bot is read-only by default; human confirms actual booking writes
- Explicit bot-vs-human handoff rules defined per client at setup
- Monthly manual review/update of each bot's knowledge — sold as ongoing
  service, not "set and forget" like most SaaS competitors

## 4. Tech stack decisions (locked in — don't re-litigate)

| Layer | Choice | Why |
|---|---|---|
| Client site hosting | Vercel/Netlify free tier | Not the VPS — no per-client ops overhead |
| VPS | Kokoro TTS compute + always-on backend only | Not for hosting client sites |
| Chatbot brain | Claude API, system-prompt injection | No RAG needed at small-client scale |
| WhatsApp | Meta Cloud API (or Twilio wrapper) | Apply for verification early |
| Voice (Tier 3) | Twilio Voice + Kokoro TTS | Real differentiator, build last |
| Calendar | Google Calendar API | Well-documented, reliable |
| Payments | Payoneer / Wise Business / merchant-of-record (Lemon Squeezy, Paddle) | Stripe unavailable for Pakistan-based accounts |
| Data hosting claim | Honest "EU/UK-hosted" via UK VPS or EU cloud region | Real GDPR trust signal — never claim a "UK head office" without actually registering a company |

## 5. Roadmap (condensed — full detail in master-plan-cursor.md)

- **Week 1**: build fake-company demo (site + chatbot), build portfolio page
- **Week 2**: manual outreach, 10-20 real prospects, no automation yet
- **Week 3-4**: close client #1 (free/cheap for testimonial), deliver in 3-5 days
- **Month 2**: 2-3 more clients using real testimonial as proof
- **Month 3+**: build the lead-agent automation pipeline (Section 6 below),
  consider Tier 2/3 expansion

**Do not automate outreach before client #1 is closed.** No proof yet =
nothing worth scaling.

## 6. Lead-agent automation (build only after client #1)

Full spec lives in `lead-agent-SOP.md`. Summary of what's automatable:

| Step | Automate? |
|---|---|
| Scraping leads (Google Places API) | Yes |
| Filtering/scoring | Yes |
| Sample page generation | Yes (+30s human glance) |
| DM draft writing | Yes |
| **Sending DMs** | **No — human only, ban risk** |
| Follow-ups | No |
| Closing/pricing | No |
| Real client build QA | No |

## 7. Prerequisites checklist (before building anything)

- [ ] Domain name (~$10-15/yr)
- [ ] Vercel/Netlify account (free)
- [ ] Anthropic API key (pay-as-you-go)
- [ ] Twilio account (free trial credit)
- [ ] Meta Business + WhatsApp API application (apply early — slow approval)
- [ ] Payoneer or Wise Business account
- [ ] Google Cloud account for Calendar API
- [ ] GitHub (already have: github.com/rayyan-mustafa)

**Total prerequisite cost: ~$10-15** (basically just the domain).

## 8. Fake-company test plan (condensed)

1. Invent a fictional business matching the home-services niche
2. Build + deploy the one-page site
3. Fill the `CONFIG` object in `client-chatbot-demo.html` with its fake data
4. Run the full testing checklist (below)
5. Only after it passes cleanly: reuse as portfolio piece #1 AND first cold-DM demo link

**Testing checklist:**
- [ ] 15-20 varied test questions incl. edge cases
- [ ] Booking-capture (lead banner) triggers correctly
- [ ] Mobile screen test
- [ ] Throttled/slow-connection test
- [ ] Tone check — sounds human, not robotic
- [ ] WhatsApp round-trip test if applicable
- [ ] Confirm you know exactly where lead data is stored (GDPR-reasonable)

**Total cost to build + test fake company: ~$2-5** (mostly optional API usage).

## 9. Portfolio page plan (condensed)

- One page: header/pitch → what you do → live demo → pricing → contact
  (use `sales-chatbot-portfolio.html` as the contact-section bot)
- Multilingual approach: static-translate only 1-2 extra languages beyond
  English (Option A, recommended) — the sales chatbot already auto-detects
  and replies in whatever language a visitor types, which covers the rest
  without needing every page pre-translated
- Build English version first and fully live before adding translations

**Total cost for full portfolio build: ~$15-20 all-in** (domain + minor API testing).

## 10. Total cash budget, start to first client

| Phase | Cost |
|---|---|
| Prerequisites | ~$10-15 |
| Fake company test | ~$2-5 |
| Portfolio page | ~$0-5 (beyond domain, already counted) |
| Client #1 delivery | $0 (priced free/cheap intentionally, for testimonial) |
| **Total to reach a real, working, tested, portfolio-backed offer** | **~$15-25** |

This is a time investment phase, not a capital one. Nothing here justifies
spending more until client #1 is closed and delivered.

## 11. Competitor research reference (for pricing/positioning sanity checks)

- **Panabotics** — US/UK/Canada/Australia/Europe, salon-focused, free
  scoping call model
- **Riin.eu** (Estonia/EU) — €249+VAT setup + monthly fee, sends a live
  demo link before the sale (the exact tactic we're using)
- **AI Beauty Bot** — 200+ salons, voice plans from $65/mo
- **CitaFlow** (Spain/EU) — bundled site+booking+chatbot, modular pricing
  from €29/mo base + €19-29/mo add-ons, GDPR/EU-hosted positioning
- **Real gaps found in the wider market** (from deeper research): no-show
  risk prediction + smart deposits, missed-call auto-text-back, proactive
  weekly digest emails (not just reactive Q&A), read-only-AI-by-default
  positioning, explicit human-handoff rules, ongoing monthly maintenance
  (most competitors sell "set and forget")

## 12. Explicitly out of scope (don't build these)

- A multi-tenant SaaS platform (stay a service agency, not a software company)
- Stripe payment infrastructure (use Payoneer/Wise/merchant-of-record)
- RAG for individual small clients (unnecessary at this scale)
- Automated DM/message sending on any platform (ban risk)
- Voice receptionist before Tier 1 has real paying clients
- A "UK head office" claim without an actual registered company
