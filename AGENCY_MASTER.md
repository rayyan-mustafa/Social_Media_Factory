# AGENCY_MASTER.md — living source of truth

**Last updated:** 2026-08-15  
**Revision:** 2  
**Rule:** When Rayyan drops new `.md` / assets, Cursor patches **this file only**. Chapter files become history.

---

## 0. Update protocol

1. Read the new file(s)  
2. Diff against sections here  
3. Patch **only** `AGENCY_MASTER.md` (merge; newer explicit decisions win)  
4. Append to **Source registry** with date + 1-line summary  
5. Bump `Last updated` + `Revision`  
6. Never invent a second master  

**Conflict rule:** once recorded here, **AGENCY_MASTER wins**.

---

## 1. Source registry

| File | Role | Absorbed |
|---|---|---|
| `master_plan.md` | Offer, niche, tech, roadmap | Yes |
| `customer_reaching_plan.md` | $0 outreach, weekly time | Yes |
| `test_plan_portfoliobuild.md` | Prerequisites, QA, portfolio | Yes |
| `lead_agent.md` | Scrape→draft→human-send | Yes |
| `master_index.md` | Prior all-in-one + competitors | Yes (superseded) |
| `opportunity_card.md` | Scorecard A–G, dual-lane | Yes |
| `client-chatbot-demo.html` | Tier 1 delivery/demo widget | Yes (asset) |
| `sales-chatbot-portfolio.html` | Portfolio inbound closer | Yes (asset) |
| *(future)* | Drop 1–2 more files anytime | Pending |

---

## 2. Dual-lane + Lane 0 (6 months)

**6 months is OK** as a salary burn window **if** Lane 0 (reuse + PKR bridge) is real. Do not score motivation on YT views.

| Lane | Role | Weekly time | Money |
|---|---|---|---|
| **0 — Reuse + PKR** | Multipost existing content + micro-gigs | ≤2 hrs + light local sell | Fastest path to insurance |
| **1 — Agency** | Outreach + delivery (home services) | ~4–6 hrs | Controllable scale M2–M3 |
| **2 — YT factory** | Auto publish under CostGuardian | ≤1–2 hrs ops | Unstable; background only |

**Insurance floor:** **PKR 4,000/day** ≈ **PKR 120,000/mo** ≈ **~$430/mo** (~278 PKR/USD). Survivable cost cover — not wealth. Closest to “insured” = selling what you already built, not ads/views.

**HTML files are subsets of the offer**, not new businesses:
- `client-chatbot-demo.html` — client/demo bot (CONFIG per business)  
- `sales-chatbot-portfolio.html` — your sales closer  

---

## 3. Niche ice-breakers (HVAC + home services gaps)

Primary niche: **home services / trades**. Salons secondary. Ice-break with **one concrete gap**, not “we do AI.”

### Trends (web + industry / Reddit-adjacent contractor pain)

| Gap signal | Why it hurts them | Our fill (Tier 1 now) | Later (Tier 2) |
|---|---|---|---|
| **Missed calls while on the job** (~60%+ unanswered in trades) | Crew can’t pick up; caller skips voicemail → next Google result | Site + chat that captures name/phone/job type 24/7 | Missed-call SMS text-back |
| **After-hours emergencies** (evening/weekend spikes; AC/heat/flood) | Phone-only = dead after 5pm | Bot qualifies urgency + books callback / form | On-call escalate rules |
| **No online book button** | Homeowners want self-serve; phone tag kills jobs | One-pager + “Book / request slot” capture | Calendar sync |
| **GBP only / Instagram only / no real site** | Looks small; no place to explain services/areas | Branded one-pager from their Google listing | GBP link to site |
| **Reviews but no chat** | Trust exists; conversion doesn’t | Widget answers FAQs + captures lead | WhatsApp/Messenger |
| **Peak season overwhelm** (HVAC summer/winter) | Same staff, 3–4× calls | Bot absorbs FAQ + queues jobs | Voice (Tier 3, after paying clients) |
| **No-shows / forgotten appointments** | Truck rolls wasted | Confirm in chat copy | Reminder SMS/WhatsApp |
| **Estimate photo / “what’s wrong”** | Phone description is vague | Chat asks for photo + ZIP + urgency | CRM sheet of leads |
| **Service-area confusion** | Out-of-area calls waste time | Bot asks ZIP / area first | Soft decline out of area |

**Sources (directional, 2025–26 industry writeups):** missed-call loss narratives for HVAC/plumbing/electrical; after-hours booking share; “won’t leave voicemail”; preference for online booking. Treat dollar claims as **pitch math**, not audited facts — always tie the line to **their** public listing.

### Priority verticals (hunt order)

| Rank | Vertical | Ice-breaker line (example) | Specific service angle |
|---|---|---|---|
| 1 | **HVAC** | “Your Google listing has strong reviews but no way to book when the AC dies at 9pm.” | Emergency triage + after-hours capture |
| 2 | **Plumbing** | “Techs are under the sink — callers who hit voicemail rarely call back.” | Leak/emergency FAQ + callback capture |
| 3 | **Electrical** | “Lots of small questions clog the phone; emergencies get buried.” | FAQ deflect + urgent escalate |
| 4 | **Auto repair** | “Maps listing works; still no book / no hours chatbot.” | Drop-off booking + “send photo of dash light” |
| 5 | **Roofing** | “Storm week = missed calls = lost replacements.” | Inspection request capture |
| 6 | **Landscaping / lawn** | “Seasonal rush, phone-only quotes.” | Quote form + service-area check |
| 7 | **Cleaning / pest** | “Recurring jobs, no self-book.” | Recurring slot request |
| 8 | **Salon / beauty** (secondary) | “Instagram only — can’t book without DMing.” | Booking bot (crowded; only clear gaps) |
| 9 | **Dentist / clinic** | “Phone-only appointments after hours.” | Appointment request + hours FAQ |
| 10 | **Estate / trucking / ecom** | Use existing leadgen vertical packs | Listing/chat gaps |

### Gaps we do **not** claim day-1 (honest scope)

- Full ServiceTitan/Jobber deep integration (upsell later)  
- Live phone AI receptionist (Tier 3 — after paying clients)  
- Licensed medical diagnosis / government work  
- Chains with Intercom/HubSpot already  

### More specific “productized” packages (same stack, different CONFIG)

1. **After-Hours Capture** — HVAC/plumbing (evening window messaging)  
2. **Missed-Call Recovery** — text-back story (Tier 2)  
3. **Estimate Inbox** — auto/roofing (photo + ZIP + callback)  
4. **No-Show Shield** — reminders (Tier 2)  
5. **Maps → Site** — GBP/social-only → one-pager + bot  

---

## 4. Business one-pager

Solo founder (Pakistan) → local SMBs in EU/UK/US/CA/AU (+ PK/Gulf micro-bridge). Cold outreach with **live demo**, not Fiverr race. Competitors: Panabotics, Riin.eu, AI Beauty Bot, CitaFlow. Pricing sanity: ~**€250–800 setup** + **€25–75/mo**; US/Gulf upper band can be higher.

**Bans:** no multi-tenant SaaS, no Stripe-from-PK, no RAG v1, no auto-DM, no voice before paying Tier 1, no “UK head office” lie, **no fake testimonials**, no Postfix on VPS for outreach, no Mailchimp cold lists.

---

## 5. Offer + delivery kit

**Tier 1:** one-page site + booking capture + AI chatbot (web; WhatsApp when verified) — system prompt, not RAG.  
**Tier 2 (after 2–3 clients):** missed-call text-back, multi-channel, reminders, simple CRM sheet.  
**Tier 3 (later):** voice receptionist (Twilio + Kokoro) — only after Tier 1 revenue.  

**Offer 2 (upsell only):** Done-for-you social/video using existing YT factory — never cold as primary.  

**Differentiators:** bot read-only by default; human confirms bookings; monthly knowledge review as a service.

**Assets:** `client-chatbot-demo.html`, `sales-chatbot-portfolio.html`, leadgen audits/phantoms under `src/agents/leadgen/`.

---

## 6. Today booster (start sequence)

**Correct:** fictional demo → real portfolio → screen-record.  
**Wrong:** fake star reviews / fake client quotes.

| Block | Do |
|---|---|
| T1–T2 | CONFIG fictional **HVAC or auto** demo; 5 test questions |
| T3–T4 | Portfolio skeleton + SAMPLE banner; no testimonials section |
| T5 | 60–90s screen-record |
| T6 | Log “today booster done” |

**Not today:** 50 cold emails, fake reviews, RunPod spend, lead-agent volume.

```text
Today → Demo + portfolio skeleton
This week → Deploy + R1–R4 reuse + soft PKR if demo live
Week 2+ → Outreach with ice-breaker gaps → Client #1 → real testimonial
```

---

## 7. Six-month roadmap (Agency / YT / Insurance)

| Month | Lane 1 Agency | Lane 2 YT | Lane 0 / insurance |
|---|---|---|---|
| M1 | Demo+portfolio live; ≥50 human sends; domain/SPF path | Factory + caps; ≤2d YPP optional | R1–R4; try S13/S14 PKR |
| M2 | Client #1 + written testimonial | Auto only | ≥50% of PKR 120k once |
| M3 | 2–3 clients / pipeline; retainer ask | Steady | Stack toward full 120k |
| M4 | Lead-agent under SOP; content upsell to 1 client | Same | Referrals S18 |
| M5 | 2+ retainers | Same | Insurance from retainers+micros |
| M6 | Keep/kill/double; cover Workspace+domain+VPS slice | Separate P&L | Document 4k/day path |

---

## 8. Customer reaching ($0)

Manual send only (20–30/day cap per platform). AI drafts; human sends. LinkedIn/IG clips from reuse. Communities helpful, not spammy. Paid ads only after 2–3 real clients. Weekly ~4–6 hrs agency + ≤2 hrs reuse.

---

## 9. Build / test / portfolio

Prerequisites: domain ~$10–15/yr, Vercel/Netlify $0, Claude API, Twilio later, Meta WhatsApp apply early, Payoneer/Wise, Calendar API.  
Fake company QA: 15–20 questions, booking capture, mobile, slow network, tone, know where leads land.  
Portfolio: EN first; +1–2 languages static; sales bot handles visitor language.  
**Total cash to first proof:** ~$15–25.

---

## 10. Lead-agent pipeline

```text
SCRAPE → SCORE → ENRICH (sample/audit) → DRAFT → HUMAN SEND → TRACK
```

Send = **human only**. Volume automation after client #1. Codebase: `src/agents/leadgen/`, `config/sop/leadgen_sop.md`, `config/sop/leadgen_email_sop.md` (SPF/DKIM/DMARC). Score tracks: `no_site` | `social_only` | `poor_site`.

---

## 11. Opportunity scorecard (condensed)

| Rank | Option | /50 | Action |
|---|---|---|---|
| 1 | AI site/chatbot agency | 37 | **Primary** |
| 2 | Tudor YT | 35 | **Parallel auto** |
| 3 | DFY content for locals | 34 | Upsell only |
| 4 | Freelance / Upwork niche | 30 | Parked M6 |
| 6 | Streaming as product | 28 | Parked |
| 7 | Micro-SaaS | 27 | Parked 12+ mo |

---

## 12. Earning streams (S) + reuse (R)

| ID | Stream | Target | Status |
|---|---|---|---|
| S1 | Agency Tier 1 EU/US | Cash M2–M3 | Primary scale |
| S2 | YT factory | Ads M4–M6+ | Parallel |
| S3 | Monthly bot review | M3–M6 | Attach S1 |
| S4 | DFY video upsell | M4 warm | Warm only |
| S5 | Portfolio inbound | M1 live | Attach S1 |
| S6 | Shopify Partner | M5+ ecom | Opportunistic |
| S7–S9 | Freelance/SaaS/streaming B2B | M6 review | Parked |
| S10 | Audit lead-magnet | M1 | Active |
| S11 | Referral bounty | M2+ | Active |
| S12 | Template/Loom pack | M4–M5 | Later |
| S13 | PK/Gulf micro-chatbot PKR 25–80k | M1–M2 | **Bridge** |
| S14 | Listing+audit PKR 10–25k | M1 | Bridge |
| S15 | Tiny maintenance retainer | M2–M3 | Bridge |
| S16 | Shorts → portfolio bio | M1 | Traffic |
| S17 | YPP push ≤2 days | M1 | Optional |
| S18 | Referral after #1 | M2+ | Active |

**Reuse (≤2 hrs/week, no new RunPod):** R1 Shorts cross-post → R2 demo screen-record cuts → R3 audit on outreach → R4 LinkedIn from script → R5 soft WA/community → R6 case Loom after #1 → R7 content sample pack for warm upsell.

---

## 13. Weekly scoreboard + monthly insurance

1. Demo/portfolio live  
2. ≥15 sends (or ≥5 delivery week) **or** 1 PKR micro advanced  
3. Tracker updated (incl. PKR column)  
4. ≥1 reuse artifact  
5. Spend under CostGuardian + leadgen caps  

Target **≥3/5**. Monthly: PKR earned ÷ 120,000 → M1 any>0 → M2 ≥0.5 → M3+ aim ≥1.0.

---

## 14. Founder checkboxes

- [ ] Domain + Google Workspace + SPF/DKIM/DMARC (`leadgen email-check`)  
- [ ] Payoneer or Wise Business  
- [ ] Meta Business / WhatsApp apply  
- [ ] Today booster: fictional HVAC/auto demo + portfolio SAMPLE  
- [ ] Deploy Vercel/Netlify  
- [ ] Lead tracker sheet (incl. vertical, gap, PKR earned)  
- [ ] Cap outreach 20–30/day; follow up once at 3–4 days  

---

## 15. Do not build / do not do

- Fake testimonials or fake reviews  
- Auto-DM / Mailchimp cold / VPS mail server for outreach  
- Paid ads before 2–3 clients  
- Voice Tier 3 / micro-SaaS / Upwork as main lane before M6 review  
- Drop Lane 1 forever for PKR micros (bridge only)  
- New creative factories — **reuse first**  

---

## 16. Pre-customer QA + what to buy

Full checklist: [`config/sop/PRE_CUSTOMER_QA.md`](config/sop/PRE_CUSTOMER_QA.md)

**Already tested on VPS:** leadgen pytest (21), watchdog, email-check (blocks Gmail From), HVAC audit render, demo proxy live chat with booking `LEAD_CAPTURED`.

**How to run demos locally:**
```bash
.venv/bin/python scripts/demo_chat_proxy.py
# then open client-chatbot-demo.html and sales-chatbot-portfolio.html in a browser
```

### Buy now (your shopping list)

| # | Buy | ~Cost | Unlocks |
|---|---|---|---|
| 1 | Domain (Porkbun / Cloudflare) | $10–15/yr | Portfolio URL + email |
| 2 | Google Workspace on that domain | ~$7/mo | SPF/DKIM/DMARC outreach From |
| 3 | Keep OpenRouter credit (or Anthropic) | $5–10 QA | Live chatbots (via proxy only) |
| 4 | Vercel or Netlify free | $0 | Public demo + serverless proxy later |

### Apply / open (not blocking SAMPLE demo)

| # | Item | Cost |
|---|---|---|
| 5 | Meta Business + WhatsApp API | $0 apply |
| 6 | Twilio trial | $0 trial |
| 7 | Payoneer or Wise Business | $0 open |

### Do not buy yet

Paid ads, Mailchimp cold tools, Instantly, extra VPS, ElevenLabs for demos, Shopify paid themes, ServiceTitan.

**Phone AI / real inbound calls = Tier 3 — not required for client #1.** Browser mic is demo-only.

**Customer-ready rule:** GATE A (done) + GATE B (you finish QA pack in browser + deploy with proxy) before any real HVAC/salon.

---

*End of AGENCY_MASTER revision 1. Next file drops → update this file only.*
