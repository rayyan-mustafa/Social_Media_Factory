# Pre-customer QA — 100% gate before any real client

**Status legend:** PASS = verified on this VPS · BLOCKED = needs your purchase/action · N/A = later tier  

**Rule:** No real customer until **GATE A + GATE B** are green. Fake testimonials remain forbidden.

---

## GATE A — What already passed (automated / structural)

| # | Check | Result |
|---|---|---|
| A1 | `pytest tests/test_leadgen.py` | **PASS** (21) |
| A2 | Leadgen CLI: hunt/watchdog/smm/audit/phantom/email-check | **PASS** |
| A3 | Watchdog dry-run | **PASS** (warns: no send domain yet) |
| A4 | Email-check rejects `@gmail.com` as outreach From | **PASS** |
| A5 | HVAC `no_site` score + audit HTML render | **PASS** |
| A6 | Demo HTML files present + CONFIG / LEAD_CAPTURED / Anthropic endpoint | **PASS** structural |
| A7 | Browser fetch has **no** API key in HTML (correct — must use proxy) | **PASS** (safe design; live chat needs proxy) |
| A8 | ffmpeg available (Shorts reuse later) | **PASS** |
| A9 | Local chat proxy + live HVAC book → `LEAD_CAPTURED` via OpenRouter | **PASS** (2026-08-15) |
| A10 | `scripts/client_demo_qa.py --live` (13 chat anomalies + structural) | **PASS** (2026-08-15) |

---

## GATE B — Must pass before first customer (you + Cursor)

| # | Check | How | Status |
|---|---|---|---|
| B1 | Fictional **HVAC** demo CONFIG filled | Edit `client-chatbot-demo.html` | Do today |
| B2 | Local chat proxy running (API key never in browser) | `scripts/demo_chat_proxy.py` | Do today |
| B3 | 15–20 QA questions (FAQ, book, edge, off-topic, complaint) | Manual in browser | Do today |
| B4 | Booking intent shows **lead banner** (LEAD_CAPTURED) | Say “book tomorrow 3pm AC check, name Ali” | Do today |
| B5 | Mobile width + slow network | Chrome DevTools | Do today |
| B6 | Mic / browser TTS (demo-grade only) | Click mic if Chrome | Optional |
| B7 | Sales portfolio bot answers pricing + no fake claims | `sales-chatbot-portfolio.html` | Do today |
| B8 | SAMPLE banner; **zero** fake testimonials | Visual | Do today |
| B9 | Deploy demo + portfolio to Vercel/Netlify **with serverless proxy** | After domain optional | Before outreach |
| B10 | Know where leads go (Sheet/Airtable/email) | Document path | Before outreach |
| B11 | Domain + Workspace + SPF/DKIM/DMARC green | `leadgen email-check --domain YOURS` | Before cold email |
| B12 | Send **yourself** one audit+draft email only | Ops SMTP OK | Before any salon/HVAC |

**Phone / WhatsApp / missed-call (Tier 2):** not required for client #1 Tier 1 web bot. Mark N/A until Meta + Twilio purchased and verified.

**Voice receptionist (Tier 3):** N/A — browser Speech API is demo-only; real calls = Twilio later.

---

## GATE C — Soft launch (first cheap/free client)

| # | Check |
|---|---|
| C1 | Client CONFIG uses **their** real hours/services only |
| C2 | Read-only bot: no inventing prices |
| C3 | Handoff text for complaints works |
| C4 | Written permission to use as case study if free/cheap |
| C5 | Real testimonial only after delivery |

---

## Purchase list (buy in this order)

### Buy now (unlock GATE B live chat + professional send)

| Item | Why | Approx cost |
|---|---|---|
| **1. Domain** (Porkbun/Cloudflare/Namecheap) | Portfolio URL + email | **$10–15 / year** |
| **2. Google Workspace** on that domain | Real From-address; SPF/DKIM/DMARC | **~$6–7 / mo** |
| **3. Anthropic API account** *or* use existing **OpenRouter** credit | Powers both chatbots | **Pay-as-you-go** — budget **$5–10** for full QA |
| **4. Vercel or Netlify** account | Host demo + portfolio + **serverless proxy** | **$0** free tier |

**Do not put the API key in the HTML file.** Proxy only (local script now; Netlify/Vercel function when public).

### Buy / apply early (not blocking fictional demo)

| Item | Why | Cost |
|---|---|---|
| **5. Meta Business + WhatsApp Cloud API** | Tier 2 channels | **$0** to apply (slow approval) |
| **6. Twilio** trial | Missed-call SMS / later voice | **$0** trial credit |
| **7. Payoneer or Wise Business** | Receive EU/US payments (no Stripe from PK) | **$0** to open |
| **8. Google Cloud** (Calendar API later) | Booking sync | **$0** free tier |

### Do **not** buy yet

| Item | Why wait |
|---|---|
| Paid ads | After 2–3 clients |
| Mailchimp paid / Instantly | No cold blast; after domain warm |
| ServiceTitan / Jobber | Client already has it — integrate later |
| Extra VPS / cPanel | You already have VPS; sites on Vercel |
| ElevenLabs / paid TTS for demos | Browser TTS for demo; Kokoro only for factory/Tier 3 later |
| Shopify Partner paid themes | Only after ecom client says yes |

**Cash to clear GATE B on this machine:** ~**$15–25** (domain) + **$5–10** API QA + Workspace month ~**$7** ≈ **~$30–40 first month**, then ~$7/mo + tiny API.

---

## QA question pack (run on HVAC demo)

1. What are your hours?  
2. Do you do AC repair? Price?  
3. Service area / do you cover [ZIP]?  
4. Emergency tonight — furnace out  
5. Book Saturday morning maintenance — name + phone  
6. Cancel policy?  
7. Something not offered (e.g. roofing)  
8. Complaint / wrong tech last time  
9. Write in Spanish (sales bot) / short English (client bot)  
10. Empty / spam message  

Pass if: no invented prices, LEAD_CAPTURED on book, complaint escalates, tone human.

**Automated runner (same checks + structural):**
```bash
.venv/bin/python scripts/demo_chat_proxy.py          # terminal 1
.venv/bin/python scripts/client_demo_qa.py --live    # terminal 2 → output/ops/client_demo_qa_report.md
```

---

## Tool matrix vs plan

| Tool | Plan role | Test method | Customer-ready when |
|---|---|---|---|
| Client chatbot HTML | Tier 1 delivery | Proxy + QA pack | B1–B6, B9–B10 |
| Sales chatbot HTML | Portfolio closer | Proxy + pricing questions | B7–B9 |
| Leadgen hunt/audit | Outreach prep | CLI + Nominatim | Already OK; Places key optional |
| Email SPF/DKIM/DMARC | Send trust | `email-check` | Domain + Workspace |
| Watchdog / cost sheet | Anti-overspend | CLI | Already OK |
| Shorts / ffmpeg | Reuse Lane 0 | Cross-post later | Not blocking client #1 |
| YT factory | Lane 2 | CostGuardian | Separate from agency delivery |
| Twilio / WhatsApp | Tier 2 | After Meta | Not blocking Tier 1 |
| Phone AI | Tier 3 | After paying clients | Not now |

---

## Sign-off

- [ ] GATE A green (this doc)  
- [ ] GATE B green (Rayyan)  
- [ ] First client = cheap/free + written case-study permission  
- [ ] No fake reviews on portfolio  

When GATE B is checked, you may start **soft** outreach with the SAMPLE HVAC demo link.
