# Lead-gen email SOP — SPF / DKIM / DMARC (+ the rest)

This is **how we send**, not how we hunt. Hunt still writes drafts; you paste/send by hand until this checklist is green.

**SOP files:** this page + [`config/sop/leadgen_sop.md`](leadgen_sop.md). Config: [`config/leadgen/email_sending.json`](../leadgen/email_sending.json).

## The three authenticators (required)

Every outreach From-address must be `@your-agency-domain` (not `@gmail.com`). The domain must publish:

| Record | What it does | Where | Pass when |
|---|---|---|---|
| **SPF** | Authenticates *which servers* may send as you | TXT on `yourdomain.com` | Starts with `v=spf1`, includes the real host (`include:_spf.google.com` for Workspace), ends `~all` or `-all` |
| **DKIM** | Digitally signs the message so it cannot be swapped in transit | TXT on `google._domainkey.yourdomain.com` (Workspace) | `v=DKIM1` and a `p=` public key |
| **DMARC** | Tells Gmail/Outlook what to do if SPF or DKIM fail | TXT on `_dmarc.yourdomain.com` | `v=DMARC1` and `p=none` at first, then `quarantine`, then `reject`; include `rua=` for reports |

Without these, mail looks forged. Personal Gmail already has Google’s records for `@gmail.com` — that does **not** cover `@yourdomain.com`.

### Workspace examples (copy after you buy the domain)

```
yourdomain.com            TXT   v=spf1 include:_spf.google.com ~all
google._domainkey.yourdomain.com   TXT   (paste from Google Admin → Gmail → Authenticate)
_dmarc.yourdomain.com     TXT   v=DMARC1; p=none; rua=mailto:dmarc@yourdomain.com; adkim=s; aspf=s
```

MX must be **Google** (or Microsoft), **not this VPS**:

```
yourdomain.com    MX    1  aspmx.l.google.com.
```

Website `A` / `www` / `demo` may point at the VPS. Mail does not.

## The rest of the send rules (also required)

1. **No Postfix/Exim on this VPS** for outreach. Same IP as the YouTube factory — a blacklist hits everything.
2. **No Mailchimp for cold Google-listing mail.** Mailchimp is opt-in only (replied / paid / form).
3. **No personal Gmail as From** for salon outreach. Gmail is gig replies + ops alerts only.
4. **No auto-SMTP to leads** until a sending domain exists **and** SPF+DKIM+DMARC check green **and** you say so. Default remains copy-paste.
5. **Human send ≤ 10/day** until the domain has been warm for a couple of weeks.
6. **CAN-SPAM / region pack:** physical mailing address in the footer; STOP / unsubscribe; “how we found you: public Google Business Profile.” EU: no bought lists, soft CTA.
7. **Quiet hours** from `regions.json` (do not ping Gulf at 2am).
8. **One shop per email.** No BCC of 25 salons.
9. **Attach the HTML check-up** (or a hosted `https://` link later). Never `file://` paths. Never a live Shopify store in the first mail.
10. Check before you treat a domain as ready:

```bash
.venv/bin/python -m src.cli.leadgen email-check --domain yourdomain.com
```

Watchdog includes this when `LEADGEN_SEND_DOMAIN` is set. Fail closed: missing SPF or DMARC = do not send outreach.

## Order of work

1. Buy agency domain (not the YouTube brand if you can avoid sharing fate).
2. Google Workspace (or Microsoft 365) mailbox `hello@yourdomain.com`.
3. Publish SPF, DKIM, DMARC as above. Wait for DNS (often minutes, sometimes hours).
4. Run `email-check`. All three must be **ok**.
5. Send 5–10 by hand from that mailbox. Then Mailchimp only for people who replied.

## What this VPS may do

| Yes | No |
|---|---|
| Host demo / client **websites** | Send mail on port 25 |
| Receive nothing (MX elsewhere) | Be the SPF `ip4:` for outreach |
| Run hunts and write drafts | Mailchimp API blast of `leads.csv` |
