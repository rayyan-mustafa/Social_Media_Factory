# ANTIGRAVITY_TASK_05.md — WhatsApp Integration (PREP ONLY — GATED)

Paste this whole file into Antigravity as one task. This PREPARES the
WhatsApp integration code so it's ready the moment Meta/Twilio verification
completes — it does not require verification to be done first, but it
cannot be tested end-to-end or go live until verification clears.

---

## Step 0 — Check verification status first

Before writing code, ask the founder to confirm: has Meta Business/WhatsApp
Cloud API verification been approved yet? (Applied for in AGENCY_MASTER
§16, item 5 — approval can take days to weeks.)

- If NOT yet approved: proceed with this task anyway, building against
  Twilio's WhatsApp sandbox (available immediately, no verification wait)
  so the code can be fully tested in sandbox mode. Flag clearly in the
  report that production WhatsApp numbers require verification first.
- If approved: build the same code, can test against the real number.

---

## Step 1 — Read first

Read `scripts/demo_chat_proxy.py` and the Task 01 proxy wiring. This
integration reuses the same Claude-calling logic — WhatsApp is just a new
input/output channel on top of the same chatbot brain, not a separate bot.

---

## Step 2 — Build the WhatsApp webhook handler

New file: `src/channels/whatsapp/webhook.py`

- Receives incoming WhatsApp messages via Twilio's webhook format
  (inbound message + sender phone number)
- Looks up which client this number belongs to (map phone number → client
  config, using the `clients/<client-slug>/config.js` structure from Task
  04's onboarding output)
- Passes the message + that client's system prompt to the same proxy logic
  used by the web chatbot (reuse, don't duplicate, the Claude-calling code)
- Sends the reply back via Twilio's WhatsApp send API
- Preserves the same LEAD_CAPTURED detection and logging behavior as the
  web chatbot — a lead captured via WhatsApp should log to the same place
  a web-captured lead does

---

## Step 3 — Client-to-number mapping

New file: `config/whatsapp_numbers.yaml` — maps each client's dedicated
WhatsApp number (or shared sandbox number + keyword routing, if using one
shared sandbox number during testing) to their client slug.

---

## Step 4 — Tests

- Mock Twilio webhook payloads, confirm correct client lookup and correct
  proxy call
- Confirm LEAD_CAPTURED logging works identically to the web chatbot path
- Confirm a message from an unrecognized number doesn't crash — should log
  and reply with a generic "we'll get back to you" fallback, not error out

---

## Step 5 — Report back

- [ ] Confirm whether this was tested against Twilio sandbox or a verified
      production number
- [ ] Confirm the proxy/Claude-calling logic is reused, not duplicated
- [ ] Confirm lead capture from WhatsApp lands in the same tracker as web
      leads (or flag if a separate tracking mechanism was needed and why)
- [ ] List what still needs to happen before this can go live for a real
      client (verification status, number provisioning, etc.)

---

## Hard constraints

- No outbound messages except in direct reply to an inbound message from
  a real user — no proactive/broadcast messaging of any kind
- No automated marketing messages via WhatsApp to leads (that would violate
  both AGENCY_MASTER's no-auto-DM rule and WhatsApp's own business
  messaging policies)
- This channel is for client's customers talking to the bot — NOT for
  the founder's own cold outreach to prospects. Do not connect this to
  the leadgen pipeline's draft-sending in any way.