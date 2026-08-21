# Factory Console — UI/UX Design Specification (for Figma Make)

## 0. Framing — read this before designing

**There is no existing UI in this codebase.** This is a backend-only system: FastAPI JSON API + 9 ARQ worker containers + Postgres + Redis + MinIO. Verified — no React/Vue/Streamlit/Jinja2 templates/static assets anywhere in the repo; the only human-facing surfaces today are:

- Raw `curl` / Postman calls to the API
- The FastAPI auto-generated Swagger page (`/docs`)
- `docker compose logs -f` in a terminal
- The generic MinIO console (raw S3 bucket browser, port `:9001` — local dev only; the production overlay removes its public port, so production has no visual surface at all)
- Passive push alerts to Discord / Telegram / Gmail

(A Gradio/Colab-era UI existed historically and was deliberately removed — the project's own rules forbid reintroducing it — so greenfield is the intended direction, not an accident.)

So this spec is **not a redesign** — it is the **first operator UI**, designed directly from the real backend data model (Jobs, Artifacts, Events, 9 business models) and the system's existing autonomous agents. Every module below states what manual/terminal-based process it replaces and why that's better. This is the "current flow" being improved throughout this document.

**Product name:** Factory Console
**Product framing:** Operator control plane for a 9-line autonomous content factory (video / audio / text), with visibility into an already-partially-autonomous backend (self-driving topic sourcing + self-healing infra).
**Primary persona:** Solo operator / small ops team supervising an automated pipeline — not an end-consumer app.
**Tone:** Dense, functional, ops/dev-tool aesthetic (think Linear × Grafana × Vercel dashboard), not marketing-site polish.

---

## 1. Information Architecture (sitemap)

```text
Factory Console
├── Sign In                          [unauthenticated]
├── Dashboard (Home)                 [/]
├── Jobs
│   ├── New Content Request          [/jobs/new]
│   ├── Jobs List                    [/jobs]
│   └── Job Detail                   [/jobs/:id]
├── Business Lines
│   ├── Overview (9 cards)           [/business-lines]
│   └── Business Line Detail         [/business-lines/:model]
├── Growth Engine                    [/growth]
│   ├── Activity Feed
│   ├── Trend Sources
│   └── Intervention Ledger
├── Artifacts                        [/artifacts]
│   └── Artifact Preview
├── System Health                    [/health]
│   └── Self-Heal Action Log
├── Notifications                    [drawer, global]
├── Settings                         [/settings]
│   ├── Integrations
│   ├── Benchmarks & Thresholds
│   └── API Access
└── Account menu                     [global, top-right]
```

**Persistent shell:** left sidebar (primary nav, 8 items above) + top bar (global "New Content Request" button, search, notification bell, account menu) + optional persistent top banner reserved *only* for system-wide degraded state (see §12). This single shell is reused by all modules — no module gets a bespoke layout.

---

## 2. Module: Authentication & Access

**Purpose:** Gate the control plane behind an operator identity instead of a single shared secret.

**Screens/views:**
- Sign In
- API Key / Access Token management (maps to backend `X-API-Key`)
- Session expired / unauthorized interstitial

**Key UI components & states:**
- Email/credential form, primary submit button
- *Loading:* button spinner, disabled form
- *Error:* "Invalid or missing API key" (mirrors backend `401`), inline field error
- *Empty:* n/a
- *Success:* redirect to originally-requested deep link, else Dashboard

**User flow / navigation:**
- Entry point for the entire app when unauthenticated
- Deep link while signed out → Sign In → return to original URL after success
- Account menu → Sign Out → back to Sign In

**Improvement notes:**
- Today: one shared `X-API-Key` header value for every caller — no identity, no per-user audit trail, rotated manually via `.env` redeploy.
- Improved: design for per-operator accounts/roles (Owner / Operator / Viewer) even if the backend still validates a single key server-side at first. This unlocks attribution everywhere else in the app ("who approved this job," "who paused the growth engine") instead of "someone with the key."

---

## 3. Module: Dashboard / Mission Control (Home)

**Purpose:** Answer "is the factory healthy and producing right now?" in one screen.

**Screens/views:**
- Single-page Dashboard (no sub-navigation; optional time-range toggle: 24h / 7d / 30d)

**Key UI components & states:**
- KPI strip: active jobs, succeeded/failed today, success rate %, cost-to-date (flag as "instrumented, not yet populated" — see §10)
- Pipeline funnel: `queued → scripting → tts → media → composing → uploading → succeeded`, live counts per stage
- Business Lines grid: 9 cards, one per business model, health chip (OK / Starved / Suspended)
- System Health strip: DB / Redis / RunPod / Disk mini-scorecard + Medic heartbeat freshness
- Recent activity feed: latest job events + latest autonomous interventions, interleaved
- Primary CTA: "New Content Request"
- *Loading:* skeleton KPI cards, skeleton funnel, skeleton grid
- *Empty:* fresh install, zero jobs ever → centered "Create your first job" state with CTA, health strip still renders
- *Error:* `/health` degraded → persistent red top banner (not a dismissible toast — see §12)
- *Success:* fully populated, all cards clickable

**User flow / navigation:**
- Landing screen post-login
- Every card/KPI is a drill-in: funnel stage → Jobs List filtered by stage; business line card → Business Line Detail; health strip → System Health; activity item → its source Job/Ledger entry
- Bell icon → Notifications drawer (global, available from every module)

**Improvement notes:**
- Today: "checking the dashboard" means separately hitting `/health`, `/v1/jobs`, the raw `/metrics` Prometheus text dump, and whatever last landed in Discord/Telegram — four disconnected surfaces.
- Improved: one glanceable screen fuses job-state + business performance + infra health + autonomous-agent activity, because right now none of those four signals can be seen together anywhere.

---

## 4. Module: Job Submission (New Content Request)

**Purpose:** Create a new content job with the correct shape for its business model, without hand-writing JSON.

**Screens/views:**
- New Content Request form (modal or full page)
- Idempotency-collision interstitial
- Submission success confirmation

**Key UI components & states:**
- Business-model picker: 9 options grouped into 3 visual categories — **Video** (YouTube_Shorts, Web_Series, Sleep_Stories, Online_Courses_Teachable), **Audio** (Podcast_Audio, Radio_FM, Audiobooks_ACX), **Text** (SEO_Blogs, EBooks_KDP) — not a flat 9-item dropdown
- Topic field (3–255 chars, mirrors backend validation), Niche field (default "documentary")
- Idempotency key field (optional, auto-suggested)
- "Upload to YouTube" toggle — auto-enabled and disabled/locked with an explainer when `YouTube_Shorts` is selected (backend force-sets this)
- Submit button
- *Loading:* disabled submit + spinner
- *Error:* inline 422 field errors (invalid business model, length violations), 401 banner
- *Empty:* n/a
- *Success:* 202 accepted → auto-navigate to Job Detail in `queued` stage
- *Collision state:* "A job with this idempotency key already exists" → link straight to the existing job instead of erroring

**User flow / navigation:**
- Entry points: Dashboard CTA, Business Line Detail ("+ New job for this line," pre-filled model), global top-bar "Create" button
- On success → Job Detail (Pipeline Monitoring)
- Cancel → back to origin screen

**Improvement notes:**
- Today: creating a job means hand-crafting a `curl POST` with an exact JSON body and remembering which of 9 case-sensitive strings is valid (typo = generic 422).
- Improved: a guided, format-grouped picker with client-side validation and pre-filled defaults turns a copy-pasted terminal command into a 30-second flow; idempotency collisions become a helpful redirect instead of a raw duplicate error.

---

## 5. Module: Pipeline Monitoring (Job Detail & Jobs List)

**Purpose:** Show exactly where a job is in its lifecycle in real time, with enough detail to diagnose failure without reading container logs.

**Screens/views:**
- Jobs List (all jobs, filterable/sortable)
- Job Detail (single job deep view)
- Retry/requeue confirmation

**Key UI components & states:**
- Stage stepper: `queued → scripting → tts → media → composing → uploading → succeeded`, current stage highlighted, failed stage flagged red
- Status badge: `queued` / `running` / `succeeded` / `failed`, one shared color token (see §13)
- Attempt counter: "Attempt 2 of 3" (from `attempt` / `max_job_attempts`)
- Event timeline: vertical feed of every `job_events` row (`from_stage → to_stage`, message, timestamp) — this is the system's existing audit trail, currently invisible
- Artifacts panel: every `JobArtifact` (script / audio / image / scene_video / final_video / ebook_document / blog_post / podcast_audio) with inline preview + presigned-download action; YouTube-direct deliveries render as a "Watch on YouTube" card instead of a file row
- Error panel: prominent when `status = failed`, shows `job.error`, "Retry" action
- List columns: id, topic, business model (+ family icon), status, stage, attempt, created_at, quick artifact/YouTube link
- List filters: business model, status, date range, topic search
- *Loading:* skeleton stepper, skeleton rows
- *Empty:* "No jobs match this filter" / "No jobs yet"
- *Error:* job not found → "Job #123 doesn't exist" with back-to-list CTA; list fetch failure → inline retry banner
- *Success:* fully populated, live-updating while non-terminal (see §11 polling note)

**User flow / navigation:**
- Entry points: Dashboard, Jobs List row click, Business Line Detail, post-submission redirect
- Back → Jobs List
- Artifact click → inline preview or Artifact & Delivery Center
- Retry → re-enqueues, stepper resets to `queued`, stays on same Job Detail URL

**Improvement notes:**
- Today: this is `curl /v1/jobs/{id}` + `curl /v1/jobs/{id}/artifacts`, repeated manually with no live update and no visual sense of "6 stages, currently on stage 4."
- Improved: a live stage stepper turns an 8-value string enum into an actual progress bar; surfacing `job_events` as a timeline exposes an audit trail that today only exists as invisible database rows.

---

## 6. Module: Business Lines (9-model registry & performance)

**Purpose:** Give each of the 9 business models a home for health/benchmarks/output — via one reusable template, not nine bespoke screens.

**Screens/views:**
- Business Lines Overview (grid of 9)
- Business Line Detail (one parameterized screen/route reused for all 9 models)

**Key UI components & states:**
- Overview cards (grouped by format family): model name, format icon, jobs today, success rate, health chip — **OK** / **Starved** (no job in >24h, configurable) / **Suspended** (success rate or failure-count breach)
- Detail: benchmark thresholds panel (starvation hours, minimum success rate, max failed/day — from live config, not hardcoded), success/fail trend chart, jobs table scoped to this model, "Suspended" banner with reason + manual "Resume line" action, "Queue job manually" CTA
- *Loading:* skeleton cards/detail
- *Empty:* a model with zero jobs ever → "No jobs yet" + CTA
- *Error:* stats query failure → inline error, cards still render with "—" values
- *Success:* fully populated

**User flow / navigation:**
- Entry points: Dashboard grid, top nav "Business Lines"
- Card click → Business Line Detail
- Detail job rows → Job Detail
- "Queue job manually" → Job Submission pre-filled with this model
- Suspended banner → Growth Engine's Intervention Ledger (the entry that caused the suspension)

**Improvement notes:**
- Today: the 9 models are only distinguished by which container/queue they run in and a string column in Postgres — there is no per-model view; "how is Podcast_Audio doing" requires manual log-grepping or SQL.
- Improved: one reusable, parameterized template directly surfaces logic that already runs today (starvation/suspension) but is otherwise invisible except as a log line or a Telegram ping.

---

## 7. Module: Autonomous Growth Engine

**Purpose:** Make the system's self-driving behavior (trend sourcing, auto-approved topics, line suspension) visible, auditable, and operator-overridable — not a silent background loop.

**Screens/views:**
- Growth Engine Activity Feed
- Trend Sources detail (per-platform status)
- Intervention Ledger (full history)

**Key UI components & states:**
- Master on/off switch + per-source toggles (Reddit, TikTok/Instagram/Facebook via Apify, YouTube/Google Trends)
- "Next evaluation in Xs" live countdown (mirrors the 60s benchmark loop)
- Auto-approved topics feed: topic, source platform, engagement score, target business model, timestamp, resulting job link
- Intervention Ledger: every autonomous action as a row (e.g. "Worker Suspended," "Pipeline Starved — Topic Auto-Approved"), expandable key/value payload
- Manual overrides: "Approve a topic myself" (bypass scraper), "Force-resume a suspended line"
- *Loading:* skeleton feed
- *Empty:* no interventions yet (fresh system)
- *Error/degraded-source state:* e.g. "TikTok/Instagram/Facebook sourcing disabled — no Apify token configured" shown as an explicit info state, not a silent gap
- *Success:* live feed + ledger populated

**User flow / navigation:**
- Top nav "Growth Engine"
- Feed entries → the Job they created (Pipeline Monitoring)
- Ledger entries → the affected Business Line when applicable

**Improvement notes:**
- Today this is the single biggest invisible feature in the system: a daemon autonomously suspends revenue lines and auto-publishes AI-picked topics to YouTube, with its only trace being a Telegram/Gmail message and a log line — easy to miss for days.
- Improved: promote it to a first-class module with a live feed, a permanent ledger, and a manual kill switch in front of every autonomous action. "Autonomous" should mean *supervisable*, not *invisible*.

---

## 8. Module: Artifact & Delivery Center

**Purpose:** One place to browse, preview, and retrieve every output the factory has produced, across all formats and models.

**Screens/views:**
- Artifact Library (searchable/filterable grid, cross-job)
- Artifact Preview (per-kind inline viewer)

**Key UI components & states:**
- Filters: business model, artifact kind (8 enum values), date range, delivery type (stored file vs. direct-to-YouTube)
- Cards: thumbnail (video/image), waveform icon (audio), doc icon (text/ebook/blog); kind badge; truncated SHA-256 checksum with copy action; "Get link" (presigned URL + expiry countdown) or "Watch on YouTube"
- Inline preview: video player / audio player / markdown-or-PDF viewer / image viewer
- *Loading:* skeleton grid
- *Empty:* "No artifacts yet" (unfiltered) or "No artifacts match this filter"
- *Error:* presigned URL generation failed, artifact 404
- *Success:* populated grid with working previews

**User flow / navigation:**
- Entry points: top nav "Artifacts," or drilled into from a specific Job Detail (pre-filtered to that job)
- Preview → open/download (presigned URL) or open on YouTube

**Improvement notes:**
- Today: browsing artifacts means the raw MinIO console (generic S3 browser, no concept of "script.json vs. final.mp4," no in-browser preview) or hand-built presigned-URL calls per artifact ID.
- Improved: a delivery-aware library that understands all 8 artifact kinds and the YouTube-direct exception, with previews inline instead of "download and hope."

---

## 9. Module: System Health & Self-Healing

**Purpose:** Surface infra health scoring and the system's own self-healing actions (container restarts, disk purges, forced re-encodes) so an autonomously-rebooting system stays trustworthy and interruptible.

**Screens/views:**
- System Health Overview
- Self-Heal Action Log
- Resource detail (Disk / Postgres / Redis / RunPod, each expandable)

**Key UI components & states:**
- Composite score gauge (0–100, weighted across Postgres/Redis/RunPod/MinIO), color bands at warning/critical thresholds
- Medic heartbeat card: last-seen timestamp, memory %, TTS/FFmpeg average duration vs. configured SLA limits, staleness alarm
- Disk gauge: used % vs. warning/critical thresholds, log of artifact purges triggered
- Self-heal action log: trigger reason (critical memory / heartbeat silence / TTS SLA breach / FFmpeg SLA breach), containers affected, timestamp
- YouTube post-upload monitor: pending-processing list with per-video status, forced-re-encode events
- Manual controls: "Restart workers now," "Pause self-heal for 1 hour" (maintenance mode)
- *Loading:* skeleton gauges
- *Empty:* clean history (healthy system, no interventions ever)
- *Error:* an individual health probe failing (e.g. RunPod unreachable) renders as its own degraded sub-score, never a page crash
- *Success:* all-green scorecard

**User flow / navigation:**
- Top nav "System Health," also linked from Dashboard's health strip
- Self-heal log entries → affected Business Line if a restart interrupted its jobs

**Improvement notes:**
- Today: self-healing is a fully autonomous, invisible loop that can hard-restart **all 9** worker containers over a single bad benchmark reading, with feedback arriving only via Telegram/Gmail after the fact.
- Improved: a live score plus an explicit, permanent action log turns "the system quietly rebooted itself at 3am" into something inspectable — and flags today's coarseness (restarts everything, not just the unhealthy queue) as visible, not hidden.

---

## 10. Module: Settings

**Purpose:** Manage integrations, credentials, and tunable thresholds without hand-editing `.env` / JSON config files.

**Screens/views:**
- Settings Overview (categorized)
- Integrations (per-provider cards)
- Benchmarks & Thresholds
- API Access

**Key UI components & states:**
- Integration cards, each with Connected / Not Configured / Error status: LLM (WaveSpeed/OpenRouter), YouTube OAuth (connection + token expiry), RunPod, MinIO/S3, Alerts (Telegram, Gmail, Discord, generic job webhook), Growth (Apify)
- Benchmarks form: topic-starvation hours, minimum success rate %, max failed jobs/day, TTS/FFmpeg SLA limits, disk capacity threshold, watchdog score thresholds — editing the system's real tunable config through a form
- API Access: current key display/rotate; reserved section for future per-operator tokens (ties to §2 improvement)
- Max job attempts, YouTube privacy status selector (private/unlisted/public)
- *Loading:* skeleton cards/forms
- *Empty:* n/a (schema always renders; blank fields show "Not Configured")
- *Error:* save/validation failure, inline
- *Success:* saved confirmation toast

**User flow / navigation:**
- Account menu / top nav → Settings
- Integration card → inline form or modal → Save → back to grid with updated status chip

**Improvement notes:**
- Today: every configuration value requires editing `.env` or a JSON file and redeploying, with zero in-app visibility into what's currently set.
- Improved: an inspectable, editable surface for integrations and thresholds — even if changes still require a controlled reload underneath, the operator no longer needs shell access just to *see* current state.

---

## 11. Module: Notifications

**Purpose:** Unify existing outbound channels (Discord, Telegram, Gmail, generic webhook) plus in-app events into one filterable center.

**Screens/views:**
- Notification Center (drawer from bell icon, global)
- Notification Preferences

**Key UI components & states:**
- Feed grouped by category: job terminal failures, business interventions (suspensions, starvation auto-approvals), watchdog critical alerts (self-heal, disk purge, YouTube rejection), generic job lifecycle events
- Per-category channel routing preferences (in-app / Telegram / Gmail / Discord)
- Unread badge on bell icon; mark-as-read / mark-all-read
- Deep link from each item to its source (Job / Business Line / System Health)
- *Loading:* skeleton feed
- *Empty:* "You're all caught up"
- *Error:* feed failed to load
- *Success:* populated, grouped feed

**User flow / navigation:**
- Bell icon opens drawer from any module
- Item click → routes to relevant Job Detail / Business Line / System Health entry

**Improvement notes:**
- Today: alerts are scattered by hardcoded channel — Discord only gets terminal pipeline failures, Telegram/Gmail only get business+watchdog interventions, a generic webhook fires separately — with no in-app record of any of it.
- Improved: one in-app feed becomes the source of truth; external channels become paging duplicates of it, so nothing is lost if a phone is off.

---

## 12. Data/State Layer — cross-cutting touchpoints

**Purpose:** Keep every screen above consistent with real backend shapes and sync behavior.

**Core entities every module reads from:**
- **Job** — id, topic, niche, business_model, status (`queued`/`running`/`succeeded`/`failed`), stage (`queued`→`scripting`→`tts`→`media`→`composing`→`uploading`→`succeeded`, or `failed`), attempt, idempotency_key, error, cost_cents, upload_to_youtube, timestamps
- **JobArtifact** — id, job_id, kind (8 values: script/audio/image/scene_video/final_video/ebook_document/blog_post/podcast_audio), s3_key, checksum, freeform meta, created_at
- **JobEvent** — id, job_id, from_stage, to_stage, message, created_at — **append-only audit trail**, currently has no UI at all
- **YouTubeUpload** — id, job_id, video_id, privacy, processing_status, published_at

**Live-update strategy:**
- No WebSocket/SSE exists in the backend today — design Dashboard and Job Detail as **polling** views (e.g., every 3–5s while a job is non-terminal, stop on `succeeded`/`failed`)
- Flag every "live" surface in this spec explicitly so loading/refresh states are designed on purpose, not assumed away
- Treat a future push channel (SSE/WebSocket) as an additive upgrade, not a blocker

**Shared component rules (do not re-implement per module):**
- One status-color token set for `JobStatus`, reused in Dashboard funnel, Jobs List, Job Detail stepper, Business Line tables
- One 9-item business-model legend (icon + color), reused in Dashboard, Job Submission, Business Lines, Artifact filters
- One artifact-kind icon set, reused in Job Detail and Artifact Library

---

## 13. Error / Empty States — shared library

Define once, reuse across every module above:

| State | Trigger (backend behavior) | Pattern |
|---|---|---|
| Unauthorized | Missing/invalid API key (`401`) | Route to Sign In; if already signed in, banner + link to Settings → API Access |
| Not found | Job/artifact doesn't exist (`404`) | Friendly "doesn't exist" page, back-to-list CTA |
| Validation | Bad business model / topic length (`422`) | Inline field-level errors, no full-page failure |
| Degraded | `/health` returns `503` (DB or Redis down) | **Persistent** top-of-app banner, not a dismissible toast — this means the whole factory is impaired |
| Empty | Zero rows for current filter/scope | Centered message + relevant CTA (never a blank screen) |
| **Degraded success** *(new — proposed)* | Job succeeded via a fallback path (template script instead of LLM, silent audio instead of Kokoro TTS, placeholder image instead of sourced media) | Distinct "Succeeded (degraded)" badge, visually different from a clean success, everywhere the job/artifact appears |

**Improvement note:**
Today, fallback behavior is silent — a job can reach `succeeded` while quietly having used a placeholder image or silent audio track, and nothing in the API response distinguishes that from a fully clean run. Surfacing "degraded success" is the single most valuable *new* state in this spec: it stops quality regressions from hiding behind a green checkmark. (Requires the backend to record which fallback fired per job/artifact — call out as a backend follow-up alongside the UI work.)

---

## 14. Design System Notes (for Figma Make to anchor on)

No frontend, CSS, or design tokens exist in this codebase today — there is nothing to extract literally. The anchors below are derived from the backend's **real domain semantics** (enums, thresholds, integrations actually in the code), not invented from scratch.

**Color — semantic, not decorative:**
- **Status colors** (4, map 1:1 to `JobStatus`): `queued` = neutral gray, `running` = blue, `succeeded` = green, `failed` = red. Use only for status, everywhere, identically.
- **Stage progression** (6 sequential steps): single-hue intensity ramp or icon-based stepper — do **not** give each of the 6 stages its own hue; that's 6 colors competing with the 4 status colors.
- **Format-family accents** (3): Video / Audio / Text — one accent + icon per family (film strip / waveform / document), used on business-model chips throughout.
- **Business-model categorical palette** (9): one fixed categorical palette (9 distinct, low-saturation swatches so they don't fight status colors), applied identically in every module's legend.
- **Health severity** (3-tier): OK (green) / Warning (amber) / Critical (red) for System Health and Business Line suspension states — mirrors the system's own two-threshold benchmark pattern (warn threshold, critical threshold).

**Typography:**
- Monospace for anything that is an identifier, checksum, S3 key, video ID, or raw log/JSON snippet — this system communicates in structured JSON logs and content-addressed keys throughout its backend.
- Standard UI sans-serif for all labels, nav, body copy.
- Tabular figures for the Dashboard KPI strip (counts must align).

**Iconography:**
- Brand-adjacent icons for real, named third-party integrations that appear throughout the config: YouTube, Discord, Telegram, Gmail, RunPod, MinIO/S3, PostgreSQL, Redis, Apify — use recognizable marks so Settings/Integrations reads instantly rather than generically.

**Density & tone:**
- Operator/ops tool, not a consumer app — closer to Grafana/Datadog/Linear than a marketing dashboard.
- Dark-mode-first, information-dense, minimal illustration, functional over decorative.
- Persistent left-sidebar + top-bar shell is the one layout primitive reused across all 13 modules above — no module gets a bespoke frame.

**Spacing:**
- No existing scale to inherit — adopt a standard 4/8px base spacing unit as the single spacing system for the whole app.

**Golden rule for this spec:** every color/icon/state defined in §12–§14 is a shared token used identically everywhere it appears — Figma Make should build a small shared component library first (status badge, stage stepper, business-model chip, health gauge) and compose all 13 modules from it, rather than styling each module independently.
