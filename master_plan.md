# Master Plan — AI Website + Chatbot Agency (for Cursor)

This file is the single source of truth. Read this first before building anything.
It covers: what we're building, why, the tech decisions already made, the
roadmap, and the exact to-do list — split into what Cursor builds vs. what the
founder (Rayyan) does manually.

---

## 1. Business context (read this first)

- Founder is solo, based in Pakistan, targeting small local businesses in
  Europe (secondary: UK, US, Canada, Australia)
- Existing assets: Cursor Pro (dev), Kokoro TTS (voice), a proven autonomous
  content pipeline (built before, for video), headless streaming setup
  (currently used for personal channels only — not part of this business), a
  UK VPS (used for hosting/infra, NOT claimed as a "head office" or company
  location — that would be misrepresentation)
- Strategy: cold outreach to local businesses (not Upwork/Fiverr — those are
  saturated, race-to-bottom). Lead with a working demo, not a pitch deck.
- Competitors researched: Panabotics, Riin.eu, AI Beauty Bot, CitaFlow,
  Ainisa/GaliChat/Voiceflow/Jotform (generic template platforms). Pricing in
  that space runs roughly €19-109/mo in add-on modules, €249+ setup fees, or
  $65+/mo for voice add-ons.
- **Niche decision**: originally scoped for salons/beauty, but salon/beauty
  chatbot space is now crowded with dedicated SaaS competitors. **Pivoting
  primary niche to home services** (HVAC-equivalents, auto repair, plumbing,
  electrical, and similar local trade businesses) — same missed-call/booking
  problem, far less competition, higher provable ROI story (a missed call =
  a lost job worth real money, easy to explain to an owner).
  Salons remain a valid secondary niche if a good lead appears there.

## 2. The offer (what we're actually selling)

**Tier 1 — Launch offer (build first, sell first):**
- One-page branded website with booking capture
- AI chatbot (web widget + WhatsApp) trained on the business's real info —
  services, hours, pricing, FAQs — via a system prompt, NOT RAG (client's
  data is small enough to fit directly in context; RAG is unnecessary
  complexity at this scale)
- Delivered with a live working demo before any payment is discussed

**Tier 2 — Add after 2-3 paying clients:**
- Missed-call auto-text-back (Twilio)
- Multi-channel bot (Instagram, Messenger, Telegram in addition to WhatsApp)
- Automated reminders (WhatsApp/SMS/email) to reduce no-shows
- Simple CRM view (Airtable/Sheets — not a custom-built CRM product)

**Tier 3 — Differentiator (build once Tier 1 is validated with real revenue):**
- AI voice receptionist: Twilio Voice (speech-to-text) → Claude (reply logic)
  → Kokoro TTS (voice response). This is the most technically complex piece
  and the biggest differentiator — most small competitors don't offer voice.
  Do NOT start here. Build only after Tier 1 has paying clients.

**Design principles to build in from day one (these are also sales
differentiators, not just internal decisions):**
- Bot is read-only by default — it can look up and suggest, but a human
  confirms any actual booking write, unless the client explicitly wants full
  automation later. Reduces client anxiety about handing control to an AI.
- Explicit handoff rules defined per client at setup: bot handles hours,
  pricing, service descriptions, booking links, FAQs; escalates to human for
  complaints, sensitive issues, high-value/complex inquiries.
- Monthly manual review/update of each bot's knowledge base — sold as a
  service line, not "set and forget" like most SaaS competitors.

## 3. Tech stack decisions (already made — don't re-litigate)

- Hosting: Vercel/Netlify for client sites (NOT the VPS — no per-client ops
  overhead, free tier is enough at this scale)
- VPS: reserved for Kokoro TTS compute and any always-on backend service,
  NOT for hosting individual client sites
- Chatbot: Claude API, system-prompt-based knowledge injection (no RAG for
  v1 — only add RAG later if a specific client's data genuinely outgrows a
  prompt, e.g. hundreds of FAQ/policy documents)
- WhatsApp: Meta Cloud API (or Twilio as a wrapper) — requires Meta Business
  verification, apply early since it can take days-to-weeks
- Voice (Tier 3 only): Twilio Voice + Kokoro TTS
- Calendar: Google Calendar API for sync
- Payments: NOT Stripe directly (Pakistan isn't a supported Stripe country)
  — use Payoneer/Wise Business or a merchant-of-record platform (Lemon
  Squeezy, Paddle) when payments become necessary. Not needed for the first
  free/discounted client.
- Data hosting claim: can honestly say "EU/UK-hosted" if using the UK VPS or
  an EU-region cloud provider for backend data — real GDPR trust signal for
  European clients. Cannot claim a UK company/head office without actually
  registering one (Companies House, ~£12-50, doable as a non-resident) —
  don't do this yet, only after the model is validated.

## 4. Roadmap (realistic timeline)

**Week 1 — Build the reusable core**
- Build ONE fake demo business (fictional name, not a real unauthorized
  client) with full site + chatbot + booking-capture flow
- This demo serves three purposes: proof-of-work, portfolio piece #1, and
  the actual link sent in cold DM #1
- Build simple portfolio page (name, photo, one-line pitch, services,
  link to the demo)

**Week 2 — First outreach, manual only**
- Identify 10-20 real home-services (or salon, if a strong lead appears)
  businesses matching the target profile (no website, or no booking system,
  or slow/no response pattern visible publicly)
- Send personalized, manual cold DMs/emails — no automation yet
- Expect low reply rate (1-3 replies out of 10-20 is normal, not a failure)

**Week 3-4 — Close and deliver client #1**
- Price client #1 low/free explicitly in exchange for a testimonial and
  permission to use as a case study
- Deliver in 3-5 days, over-deliver slightly
- Get the testimonial in writing immediately after delivery

**Month 2 — Second wave**
- Use client #1's real result as social proof in new outreach
- Close 2-3 more clients at closer to real pricing
- Start referral asks with every delivered client

**Month 3+ — Systematize**
- Only now: build out the lead-agent SOP (see section 6) for scraping,
  scoring, and draft-generation at volume
- Consider Tier 2 add-ons as upsells to existing clients
- Evaluate whether Tier 3 (voice) is worth building based on real client
  interest/requests

**Do not build the automated lead-agent pipeline before client #1 is closed.**
Without a real result to reference, automating outreach just scales
unproven pitches faster — it doesn't help close deals.

## 5. Founder's personal to-do / structure (Rayyan's side, not Cursor's)

**Before touching outreach:**
- [ ] Register/prepare a simple business identity (name, logo — doesn't
      need to be a registered company yet)
- [ ] Set up Payoneer or Wise Business account for eventually receiving
      international payments
- [ ] Apply for Meta Business verification (WhatsApp API access) — start
      this early, approval can take time
- [ ] Decide on domain name and register it

**Testing checklist prior to going anywhere near production/real clients:**
- [ ] Build fake demo business end-to-end (site + chatbot deployed live)
- [ ] Test chatbot with at least 15-20 varied questions per client, including:
      normal FAQ questions, booking requests, edge cases (asking about
      services not offered), and attempts to get the bot to say something
      wrong or off-brand
- [ ] Confirm booking-capture flow correctly logs leads (name, contact,
      requested time/service) to the tracking sheet every time
- [ ] Test on mobile (most real users will hit the chatbot from a phone)
- [ ] Check page load speed on a throttled/slow connection (many small
      local clients' own customers won't have fast connections)
- [ ] Manually review chatbot responses for tone — should sound like a
      friendly real front-desk person, not a generic corporate bot
- [ ] If including WhatsApp: send yourself real test messages from a
      separate phone number to confirm delivery/response works end-to-end
- [ ] Confirm GDPR-reasonable data handling: know where captured lead data
      is stored, don't retain more than needed, be ready to state this
      plainly if a European client asks
- [ ] Only after all of the above passes cleanly: use this demo in the
      first real cold outreach message

**Ongoing personal discipline:**
- [ ] Cap manual outreach at 20-30 DMs/day per platform account (ban risk
      if exceeded)
- [ ] Track every lead in one sheet: sent date, replied?, follow-up sent?,
      priced?, closed?, tier, value
- [ ] Follow up once after 3-4 days of silence, not more
- [ ] After every delivered client: request testimonial + ask for referral,
      same day as delivery while goodwill is highest

## 6. Lead-agent automation (build only after client #1 — see prior SOP file)

Reference: `lead-agent-SOP.md` (already written) covers the full scrape →
score → enrich → draft → human-send → track pipeline, with an explicit
table of what is safe to automate and what must stay manual (sending stays
human — automating DM sending risks platform bans and kills the pipeline).

Do not build this pipeline until Section 4's Week 1-4 milestones are done.

## 7. What Cursor should NOT do

- Do not build a multi-tenant SaaS platform (that's a different, much
  bigger business than this one — stay a service agency, not a software
  product company)
- Do not build Stripe payment infrastructure (use Payoneer/Wise/merchant-
  of-record instead)
- Do not build RAG infrastructure for individual small clients (unnecessary
  complexity — system prompt injection is sufficient at this scale)
- Do not automate DM/message sending on any platform
- Do not build the voice receptionist (Tier 3) before Tier 1 has paying,
  validated clients
