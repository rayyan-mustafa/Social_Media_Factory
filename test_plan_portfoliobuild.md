# Test Plan, Prerequisites & Portfolio Build — Step by Step

Covers: what to set up before touching anything, how to test the fake
company end-to-end, and how to build the multilingual portfolio page —
all with real costs attached so nothing is a surprise.

---

## Part 1 — Prerequisites (set up once, before building anything)

| # | Item | Cost | Notes |
|---|---|---|---|
| 1 | Domain name | $10-15/yr | Namecheap/Porkbun — pick something short, not tied to one niche (you're expanding past salons) |
| 2 | Vercel or Netlify account | $0 | Free tier covers everything at this stage |
| 3 | Anthropic API key (Claude) | $0 to start | Pay-as-you-go, billed on usage — see Part 2 for test costs |
| 4 | Twilio account | $0 to start | Needed later for WhatsApp/voice; free trial credit included |
| 5 | Meta Business account + WhatsApp API application | $0 | Apply early — verification can take days to weeks, don't wait until you need it |
| 6 | Payoneer or Wise Business account | $0 to open | For receiving international client payments (Stripe not available in Pakistan) |
| 7 | Google Cloud account (Calendar API) | $0 | Free tier sufficient for this scale |
| 8 | GitHub account | $0 | Already have — github.com/rayyan-mustafa |

**Total cash cost for prerequisites: ~$10-15** (just the domain). Everything else is free to set up.

---

## Part 2 — Fake company test plan (step by step)

**Goal:** prove the whole pipeline works end-to-end before any real client sees it.

### Step 1 — Invent the fake company
- Pick a fictional business (not a real unauthorized brand) — e.g. a home-services business per the niche pivot: "Prime Auto Care" or similar
- Write out realistic fake data: services + prices, hours, location, phone, booking policy — this becomes the `CONFIG` object in the chatbot file

### Step 2 — Build and deploy the site
- Build the one-page site (Cursor)
- Deploy to Vercel/Netlify free tier
- Cost: $0

### Step 3 — Wire up and test the chatbot
- Drop in the `client-chatbot-demo.html` template, fill `CONFIG` with the fake company's data
- Run it inside Cursor (needs API key access) or deploy as a small serverless function so the API key isn't exposed client-side in production (important: never ship an API key inside client-facing HTML for a real client — route it through a backend function)
- **Test cost estimate**: budget 40-60 test messages during QA. At Claude's per-token pricing this is well under $1-2 total for a full testing session — API testing cost is effectively negligible at this scale

### Step 4 — Run the testing checklist
- [ ] 15-20 varied questions: normal FAQs, booking requests, and deliberately weird/off-topic questions (to check it doesn't break or hallucinate)
- [ ] Confirm booking-capture (lead banner) triggers correctly every time a booking intent is expressed
- [ ] Test on an actual phone screen, not just desktop
- [ ] Test on a throttled/slow connection (Chrome DevTools network throttling — free)
- [ ] Read every response back for tone — should sound human, not robotic
- [ ] If testing WhatsApp integration: send real test messages from a second phone number
- [ ] Confirm you know exactly where captured lead data is stored (a Sheet/Airtable is fine) — be ready to state this plainly if asked

### Step 5 — Only after Step 4 passes cleanly
- This exact fake-company build becomes: (a) your first portfolio piece, (b) the actual demo link sent in your first real cold outreach message

**Total cost to fully build and test the fake company: roughly $2-5**, almost entirely optional API testing cost. This is not a phase that requires real budget.

---

## Part 3 — Portfolio page, step by step (multilingual)

### Step 1 — Decide the structure
One page, sections in this order:
1. Header: your name/brand + one-line pitch
2. What you do (2-3 sentences, plain language)
3. Live demo embed or link (the fake company chatbot from Part 2)
4. Services + pricing tiers
5. Contact / lead capture (can literally be the sales chatbot from before)

### Step 2 — Decide the multilingual approach
Two real options — pick based on effort vs. quality:

**Option A — Static translated versions (recommended for v1)**
- Write the page content once in English, then create translated versions for your top 2-3 target languages (e.g. Spanish, German, French — whichever matches where you're prospecting)
- Simple URL structure: `yoursite.com/en`, `yoursite.com/es`, `yoursite.com/de`
- Cost: $0 if you translate it yourself with Claude's help (just ask for a translation pass, review it, done); avoids any ongoing cost or complexity
- Pro: fast, cheap, fully in your control, no runtime dependency
- Con: only covers the languages you pre-built

**Option B — Dynamic in-browser translation**
- A language switcher that swaps page text client-side (JSON translation files per language, loaded by a dropdown)
- Cost: $0, just more dev time in Cursor to wire up
- Pro: easy to add more languages later, one page instead of several
- Con: slightly more engineering than Option A for the same v1 result

**Recommendation**: start with Option A, 2-3 languages max (English + your top 1-2 target markets). Expand only if you find real demand from a specific language market. Don't over-engineer a 5-language switcher before you have a single client.

### Step 3 — The sales chatbot handles the rest of the multilingual problem for free
Since the sales chatbot (from the previous build) auto-detects and replies in whatever language the visitor types in, you don't need the entire page pre-translated to serve non-English visitors well — a visitor can read an English page and still chat with the bot in their own language, and the bot will follow. This significantly lowers how much static translation work is actually necessary for v1.

### Step 4 — Build order in Cursor
1. Build the English version fully first, get it live
2. Get the fake-company chatbot demo embedded/linked from it
3. Get the sales chatbot embedded on the contact section
4. Only then: translate to 1-2 more languages if you want static multilingual pages too

### Step 5 — Costing summary for the whole portfolio build

| Item | Cost |
|---|---|
| Domain | $10-15/yr (already counted in Part 1) |
| Hosting | $0 |
| Translation (self-service via Claude) | $0 |
| Chatbot API testing during build | $2-5 |
| **Total to launch portfolio page** | **~$15-20 all-in** |

---

## Part 4 — What NOT to spend money on at this stage

- No paid translation services — Claude handles this well enough for a small business page at zero extra cost
- No paid hosting tier — free tier is genuinely sufficient until you have real traffic volume
- No Stripe/payment infra yet — not needed until client #1 is ready to pay
- No premium domain — a clean, simple, cheap domain is fine; don't overspend here
- No SEO tools, ad spend, or marketing software — cold outreach is your channel, not paid acquisition, at this stage

**Bottom line: the entire test + portfolio build costs roughly $15-25 total**, almost entirely the domain name. This whole phase is time investment, not capital investment — which is exactly right for where you are.
