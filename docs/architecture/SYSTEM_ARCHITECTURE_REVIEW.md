# System Architecture Review — YouTube Automation Engine

Scope: full trace of `src/`, `deploy/`, `scripts/`, `docker-compose*.yml`, `config/`, `alembic/`, `docs/adr/`. All findings below are cited to real files/lines, not generic advice.

---

## 1. Current System — Traced Execution Flow

### 1.1 Entry points (who creates a `Job`)


| #   | Entry point                          | File                                                           | Trigger                                                       |
| --- | ------------------------------------ | -------------------------------------------------------------- | ------------------------------------------------------------- |
| 1   | `POST /v1/jobs`                      | `src/api/main.py:124-159`                                      | Human/external caller, `X-API-Key` header                     |
| 2   | Business Manager Daemon (autonomous) | `src/services/business_manager_daemon.py:89`                   | Auto-fires when a business line is "starved" (no job in >24h) |
| 3   | Ops/test scripts                     | `scripts/night_test_main.py`, `api_test.py`, `api_test_vps.py` | Manual harness / smoke tests, call the same API               |




### 1.2 Core execution path (per job)

```text
1. Job row created — status=queued, stage=queued (src/db/repository.py:create_job)
2. JobEvent(created) appended — append-only audit trail
3. ARQ enqueue: run_pipeline(job_id) → queue named after business_model
                (queue_for_model() — 1 queue + 1 worker container per business model)
4. Worker picks up job → src/workers/pipeline.py:run_pipeline()
   a. attempt += 1; if attempt > MAX_JOB_ATTEMPTS(3) → FAIL job, Discord alert, stop
   b. stage → SCRIPTING
      → ScriptGenerator.generate() (WaveSpeed/OpenRouter LLM, OpenAI-compatible)
      → on missing key OR exception → generate_fallback() (deterministic template)
      → script.json persisted to MinIO (skipped for YouTube_Shorts)
   c. format_family(business_model) routes to one of 3 branches:

      TEXT (SEO_Blogs, EBooks_KDP)
        → build Markdown, or PDF via fpdf for EBooks_KDP
        → upload to MinIO → stage → UPLOADING → SUCCEEDED

      AUDIO (Podcast_Audio, Radio_FM, Audiobooks_ACX)
        → stage → TTS → Kokoro TTS (self-hosted)
        → on TTS failure → synthesize_silence() fallback (silent WAV)
        → upload to MinIO → stage → UPLOADING → SUCCEEDED

      VIDEO (YouTube_Shorts, Web_Series, Sleep_Stories, Online_Courses_Teachable)
        → IF RUNPOD_ENABLED:
            trigger_render() → poll status every 5s → decode base64 mp4
            ON FAILURE in production → raise (job fails, NO local CPU fallback — deliberate cost control)
            ON FAILURE in dev → fallback to local CPU path below
        → ELSE (local path):
            stage → TTS (per scene, Kokoro, silence fallback per scene)
            stage → MEDIA → Wikimedia Commons search → optional CLIP re-rank → Chroma cache
                     → on no result/failure → gray placeholder image
            stage → COMPOSING → FFmpeg Ken Burns/zoompan per scene → concat → final.mp4
        → upload final.mp4 to MinIO (skipped for YouTube_Shorts — direct upload only)
        → IF must_upload_yt: stage → UPLOADING → YouTube Data API v3 upload
                              → record YouTubeUpload row
                              → job_id:video_id pushed to Redis set "youtube:pending_processing"
   d. stage → SUCCEEDED, JOBS_SUCCEEDED++, webhook `job.succeeded` fired
5. ON any uncaught exception at any stage:
   → if attempt < MAX_JOB_ATTEMPTS: stage → QUEUED (retry), re-raise (ARQ also retries independently — see §2.3)
   → else: stage → FAILED, JOBS_FAILED++, Discord alert, webhook `job.failed`
```



### 1.3 The 9 business models (canonical: `src.domain.BUSINESS_MODELS`)


| Business model           | Format | Queue / worker          | Durable output                                  |
| ------------------------ | ------ | ----------------------- | ----------------------------------------------- |
| YouTube_Shorts           | video  | `worker_youtube_shorts` | **None** — direct YouTube upload only           |
| Web_Series               | video  | `worker_web_series`     | `production/Web_Series/job_{id}/`               |
| Sleep_Stories            | video  | `worker_sleep_stories`  | `production/Sleep_Stories/job_{id}/`            |
| Online_Courses_Teachable | video  | `worker_online_courses` | `production/Online_Courses_Teachable/job_{id}/` |
| Podcast_Audio            | audio  | `worker_podcast_audio`  | `production/Podcast_Audio/job_{id}/`            |
| Radio_FM                 | audio  | `worker_radio_fm`       | `production/Radio_FM/job_{id}/`                 |
| Audiobooks_ACX           | audio  | `worker_audiobooks_acx` | `production/Audiobooks_ACX/job_{id}/`           |
| SEO_Blogs                | text   | `worker_seo_blogs`      | `production/SEO_Blogs/job_{id}/`                |
| EBooks_KDP               | text   | `worker_ebooks_kdp`     | `production/EBooks_KDP/job_{id}/`               |


Each has its own ARQ queue, its own Compose worker container, `max_jobs=2` concurrency, `job_timeout=3600s` (`src/workers/settings.py`).

### 1.4 The autonomous control tier (already exists, partially wired)

The code already implements a **3-agent supervisory pattern** — this matters for §3, since the improvement proposal formalizes and extends it rather than inventing something new:


| Agent (code's own naming)           | File                                                           | Role                                                                                                                                                                                   |
| ----------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **"CEO"** — Business Manager Daemon | `business_manager_daemon.py`                                   | Every 60s: evaluates per-model success rate/starvation → suspends failing lines, auto-creates jobs from scraped trends when a line is starved                                          |
| **"Medic"** — heartbeat             | `services/heartbeat.py`, cron'd every 1 min inside each worker | Writes memory %, TTS/FFmpeg avg durations to Redis `worker:heartbeat`                                                                                                                  |
| **"SRE"** — Watchdog Daemon         | `watchdog_daemon.py`                                           | Every 60s: reads Medic heartbeat + provider health (Postgres/Redis/RunPod) → hard-restarts worker containers via Docker socket, purges disk, force-re-encodes rejected YouTube uploads |


Trend sourcing for the CEO agent: `services/viral_pivot.py` — `ViralTrendScraper` aggregates Reddit (direct HTTP), TikTok/Instagram/Facebook (via Apify), with YouTube/Google Trends as unimplemented placeholders; picks the single highest-engagement topic across all sources.

**Important:** `business_manager` and `watchdog` are Compose `profiles: ["growth"]` — off by default (`docker compose up -d` does not start them; requires `--profile growth`). The system's autonomy is opt-in, not the default production posture.

### 1.5 External integrations & storage (as actually wired)


| System                                                       | Purpose                                                                              | Client                                                              |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------- |
| WaveSpeed / OpenRouter (OpenAI-compatible)                   | LLM script generation                                                                | `services/script_generator.py`, `services/content_multiplier.py`    |
| Kokoro TTS                                                   | Self-hosted voice synthesis                                                          | `services/tts_kokoro.py`                                            |
| Wikimedia Commons                                            | Free stock imagery                                                                   | `services/media_retrieval.py`                                       |
| CLIP (ViT-B/32) + ChromaDB                                   | Image re-ranking + semantic cache                                                    | `services/media_retrieval.py`                                       |
| RunPod Serverless                                            | On-demand GPU burst (CLIP/TTS/FFmpeg render)                                         | `services/runpod_client.py`, `deploy/runpod_handler.py`             |
| YouTube Data API v3                                          | Video publish + processing-status polling                                            | `services/youtube_uploader.py`                                      |
| MinIO (S3 API via boto3)                                     | Durable artifact storage                                                             | `services/storage.py`                                               |
| PostgreSQL 16 + Alembic                                      | System of record (jobs, artifacts, events, uploads)                                  | `src/db/`, `alembic/`                                               |
| Redis                                                        | ARQ broker + ephemeral state (heartbeat, pending-YouTube set, emergency-config keys) | throughout                                                          |
| Telegram, Gmail (SMTP), Discord webhook, generic job webhook | Outbound alerting (4 independent channels)                                           | `services/notifications.py`, `utils/alerts.py`, `utils/webhooks.py` |
| Apify                                                        | Social trend scraping (TikTok/Instagram/Facebook)                                    | `services/viral_pivot.py`                                           |
| Docker socket                                                | Watchdog's self-heal container restarts                                              | `watchdog_daemon.py`                                                |
| Prometheus `/metrics`                                        | Job counters, stage duration histogram, active-jobs gauge                            | `utils/metrics.py`                                                  |


---



## 2. Bottlenecks & Missing Production Patterns (cited, not generic)



### 2.1 Orchestration & code structure

- **Monolithic pipeline function.** `run_pipeline()` (`src/workers/pipeline.py:65-456`, ~390 lines) inlines script generation, all 3 format branches, RunPod orchestration, and upload logic in one function. Hard to unit-test a single stage or extend a 10th business model in isolation.
- **Adapters are markers, not strategies.** `services/formats/{video,audio,text}.py` each contain a single `FAMILY = "..."` constant (verified — no methods/classes). `get_adapter()` (`services/formats/__init__.py:24-32`) just logs and returns the family name; the actual behavior per family still lives inline in `pipeline.py`. The Strategy pattern is nominal, not real — adding a 4th format family means editing the monolith, not adding an adapter.



### 2.2 Autonomy gaps — features built but not wired

- `ContentMultiplier` **is orphaned.** `services/content_multiplier.py` implements exactly the right pattern (1 master script → drafts for all 9 models + vertical shorts in a single LLM call) but is never imported or called from `pipeline.py` or `business_manager_daemon.py`. Today, every business line pays for an independent `ScriptGenerator.generate()` LLM call per topic — 9x the LLM cost/latency for what could be 1 call.
- `vertical_shorts` **output is defined but never consumed.** The multiplier's own prompt schema (`content_multiplier.py:35-39`) asks for TikTok/Reels/Shorts cuts; nothing in the pipeline reads that key.
- **Cost tracking is schema-only.** `Job.cost_cents` exists in the DB model, domain DTO, and API response (`src/db/__init__.py:45`, `src/domain/__init__.py:178`, `src/api/main.py:89`) but is **never incremented anywhere** — grepped the full codebase; only ever set to its default `0`. The system cannot answer "what did this job cost" despite exposing the field.
- **Encryption helper is unused.** `core/security.py` implements `encrypt_bytes`/`decrypt_bytes` (Fernet) but no other file in the codebase calls either function — YouTube OAuth tokens and API keys are handled as plaintext files/env vars while a working encryption utility sits idle.
- **Stray module-level `(main())` in the Business Manager daemon.** `business_manager_daemon.py:107` contains a leftover `(main())` call after the `if __name__ == "__main__": asyncio.run(main())` block — it runs at import time and creates an un-awaited coroutine (the loop never executes; Python emits a `RuntimeWarning`). A startup-hygiene bug in the autonomous "CEO" agent itself.
- **Stale Airtable references.** `src/services/airtable_client.py` was deleted and nothing imports it anymore, but the Compose growth-profile comment (`docker-compose.yml:148`) and older docs still advertise "Airtable / Apify" — drift between docs and the actual growth stack (Apify + Business Manager + Watchdog).



### 2.3 Reliability & retry semantics

- **Two uncoordinated retry layers.** `pipeline.py` re-raises on failure so ARQ's own worker-level retry fires, *while simultaneously* tracking `Job.attempt` vs. `MAX_JOB_ATTEMPTS(3)` in Postgres to decide QUEUED-retry vs. terminal FAILED. `WorkerSettings` (`src/workers/settings.py`) does not pin `max_tries`, so ARQ's own default retry count applies underneath the app's own counter — two independent sources of truth for "how many times will this run," risking silent double-billing (LLM calls) or duplicate side effects (a second YouTube upload attempt) if they disagree.
- **No per-call circuit breaker.** Every external call (LLM, Wikimedia, RunPod, YouTube) is a bare `try/except` → fallback or raise. A degraded provider (e.g., LLM API slow/erroring) causes every job on every affected queue to eat the same timeout repeatedly rather than tripping a breaker and failing fast.
- **`tenacity` is installed but unused.** The retry/backoff library is a declared dependency (`pyproject.toml:28`) yet no module under `src/` or `scripts/` imports it — per-call retry/backoff was evidently planned but never wired.
- **RunPod polling has no dedicated deadline.** `pipeline.py:220-235` polls every 5s in a `while True` bounded only by the *entire job's* 3600s ARQ `job_timeout` — a hung RunPod request can occupy one of a queue's 2 concurrency slots for up to an hour before the outer timeout intervenes.
- **No dead-letter/triage path.** Jobs that exhaust `MAX_JOB_ATTEMPTS` become `FAILED` and stop — there's no queue or table that aggregates *why* jobs fail for pattern detection (e.g., "12 failures today were all `RunPod 429`" vs. "12 unrelated causes") before the Business Manager's blunt success-rate counter reacts.



### 2.4 Self-healing is coarse

- **All-or-nothing container restarts.** `watchdog_daemon.py`'s `restart_worker_container()` iterates **all 9** `WORKER_CONTAINERS` regardless of which single benchmark (TTS SLA, FFmpeg SLA, memory) tripped — one slow FFmpeg render on `worker_web_series` restarts `worker_seo_blogs` too, killing unrelated in-flight jobs across every business line.
- **No graceful drain.** Restarts are immediate Docker-socket calls, not a stop-accepting/finish-in-flight/then-restart sequence.



### 2.5 Observability blind spots

- **Fallback usage isn't metered.** Prometheus tracks `JOBS_CREATED/SUCCEEDED/FAILED`, `ACTIVE_JOBS`, `STAGE_DURATION` (`utils/metrics.py`) — but there is no counter for how often `generate_fallback()`, `synthesize_silence()`, or `placeholder_image()` fire. A job can succeed while quietly degraded (template script, silent audio, gray placeholder image) and it is indistinguishable from a clean run in every metric and API response.



### 2.6 Security & multi-tenancy ceiling

- **Single shared secret, no identity.** `require_api_key()` (`src/api/main.py:62-69`) checks one static `X-API-Key` for every caller — no per-operator scoping, no RBAC, no audit-by-user. Acceptable for one operator; a hard ceiling for team growth or multi-tenant use.
- **Hardcoded secrets in root helper scripts.** `download_videos.py:6-8` embeds the MinIO endpoint, access key, and secret key in plaintext; `get_template.py:2` and `test_runpod_restart.py:7` embed the same live RunPod API key. All three should read from `.env`/Settings like the rest of the codebase, and the exposed MinIO + RunPod keys rotated.
- **(Already solved, noted for completeness):** production Compose overlay (`deploy/docker-compose.prod.yml:47-59`) correctly strips public ports from Postgres/Redis/MinIO — good existing practice, not a gap.

---



## 3. Proposed Improvements — Toward a More Autonomous System

Each proposal below directly targets a numbered finding in §2 and builds on the **existing CEO/SRE/Medic tier** rather than replacing it.

1. **Wire** `ContentMultiplier` **into the pipeline** (fixes §2.2) — one master script generation per topic, fanned out to all applicable sibling business models in a single LLM call, instead of N independent `ScriptGenerator.generate()` calls. Cuts LLM cost/latency roughly 9x for cross-posted topics and finally uses the `vertical_shorts` output for TikTok/Reels/Shorts cuts.
2. **Idempotent auto-growth** (fixes §2.2/§2.3) — Business Manager should derive `idempotency_key` from `hash(topic + business_model)` when auto-creating jobs (`business_manager_daemon.py:89` currently passes none), preventing duplicate auto-queued topics across daemon restarts.
3. **Activate the cost ledger** (fixes §2.2) — increment `cost_cents` at every metered call site (LLM tokens, RunPod seconds, TTS characters) so the Business Manager can eventually make cost-aware (not just success-rate-aware) suspension decisions.
4. **Tiered external-call fallback, not binary** (fixes §2.1/§2.5) — LLM: primary model → secondary model/provider → deterministic template (today jumps straight from "any exception" to template, discarding quality unnecessarily). Media: Wikimedia → secondary free source → placeholder (today has only 2 tiers). Track each tier used as a Prometheus counter so "degraded success" is measurable, feeding the UI's proposed "Succeeded (degraded)" state.
5. **Per-provider circuit breakers** (fixes §2.3) — trip after N consecutive failures per external dependency (LLM/RunPod/YouTube/Wikimedia) and fail fast into the existing fallback path instead of repeatedly paying full timeouts; feed breaker state into the Watchdog's health score as a 5th input alongside Postgres/Redis/RunPod/MinIO.
6. **Collapse the two retry layers into one** (fixes §2.3) — pin ARQ `max_tries=1` at the worker level and let the existing Postgres `attempt`/`MAX_JOB_ATTEMPTS` counter be the single source of truth for retry decisions, with explicit backoff/jitter between requeues (the `tenacity` dependency is already installed for this — see §2.3).
7. **Bounded RunPod deadline** (fixes §2.3) — enforce a render-specific timeout (e.g. 10 min) independent of the outer 3600s ARQ job timeout, freeing the concurrency slot faster on a hung request.
8. **Dead-letter/triage table** (fixes §2.3) — persist terminal failures with a structured `reason_code` (LLM quota / RunPod failure / YouTube rejection / unknown) so the Business Manager's suspension logic can react to *why* a line is failing, not just the raw count.
9. **Targeted, graceful self-heal** (fixes §2.4) — Watchdog restarts only the container(s) tied to the specific breached benchmark/queue, and drains (stop-accepting → wait for in-flight → restart) instead of an immediate hard kill of all 9.
10. **Formalize the CEO/SRE/Medic tier + add a Director** (autonomy upgrade) — the code already names these roles in its own docstrings/comments; make that explicit in architecture and add a lightweight **Director/arbiter** that sequences conflicting autonomous actions (e.g., don't let the SRE agent hard-restart workers in the same minute the CEO agent just enqueued a burst of new jobs on them).
11. **Closed-loop growth feedback** (autonomy upgrade) — currently `ViralTrendScraper` only reads external trend signals; pipe actual YouTube performance (views/retention, already available via the Data API this system already authenticates against) back into topic scoring so the CEO agent learns from its own past picks, not just external engagement counts.
12. **Promote growth/health daemons out of opt-in** (fixes §2.4/framing gap) — `--profile growth` currently makes the "autonomous" half of the system optional; default-production posture should include Business Manager + Watchdog unless explicitly disabled, matching how the system is actually described.

---



## 4. AI Diagram Prompt (FigJam AI — ready to paste)

---

**[START OF DIAGRAM PROMPT]**

Act as a Principal System Architect. Draw a production-grade architecture flowchart for an improved YouTube automation content-factory pipeline based on the following specifications:

- **Core System Modules:**
  - API Gateway (FastAPI): `/health`, `/metrics`, `/v1/jobs` CRUD, API-key auth middleware
  - Job Orchestrator: per-stage strategy handlers (Scripting / TTS / Media / Composing / Uploading) replacing the current monolithic pipeline function — one real adapter per format family (video / audio / text), not marker constants
  - Content Multiplier Service: single LLM call fans a master script out to all 9 business-model formats + vertical social cuts (currently built but disconnected — wire it in)
  - 9 Business-Line Workers (one ARQ queue + container each): YouTube_Shorts, Web_Series, Sleep_Stories, Online_Courses_Teachable [video] · Podcast_Audio, Radio_FM, Audiobooks_ACX [audio] · SEO_Blogs, EBooks_KDP [text]
  - Autonomous Control Tier — three cooperating agents plus one new arbiter:
    - CEO Agent (Business Manager): benchmark evaluation, line suspension, trend-driven auto job creation
    - SRE Agent (Watchdog): infra health scoring, targeted self-heal, disk management, YouTube post-upload monitor
    - Medic Agent: per-worker telemetry heartbeat (memory, TTS/FFmpeg SLA timings)
    - Director/Arbiter (new): sequences CEO vs. SRE actions to prevent conflicting autonomous interventions
  - Cost & Fallback Ledger (new): meters LLM/RunPod/TTS cost per job; records which fallback tier fired per stage
  - Dead-Letter & Triage Store (new): structured failure-reason classification for terminal jobs
  - Circuit Breaker layer (new): wraps every external provider call (LLM, RunPod, YouTube, Wikimedia)
  - Notification Hub: unifies Discord, Telegram, Gmail, generic webhook, in-app feed
- **Sequential Execution Flow:**
  1. Trigger: external `POST /v1/jobs` call OR CEO Agent auto-creates a job for a starved business line (with idempotency key derived from topic+model)
  2. API/Orchestrator writes `Job` row (status=queued, stage=queued) + append-only `JobEvent`, enqueues to the business-model-named ARQ queue
  3. Worker claims job → attempt counter check against max-attempts (single retry authority — ARQ-level retries disabled/pinned to 1)
  4. Stage: SCRIPTING → Content Multiplier drafts master + sibling formats in one LLM call → tiered fallback (primary model → secondary provider → deterministic template) → cost ledger increments
  5. Format-family router dispatches to a real per-family strategy handler:
     5a. TEXT family → Markdown/PDF build → object storage upload
     5b. AUDIO family → Kokoro TTS (silence fallback, metered) → object storage upload
     5c. VIDEO family → RunPod GPU burst (bounded dedicated deadline, independent of outer job timeout) OR local CPU path (TTS → Media Retrieval with tiered fallback chain → FFmpeg compose) → object storage upload
  6. Conditional: if YouTube-direct model or upload flag set → YouTube Data API upload → record upload → push to post-upload processing-status monitor (SRE Agent)
  7. Stage → SUCCEEDED (or SUCCEEDED-DEGRADED if any fallback tier fired) → metrics + webhook + notification hub fan-out
  8. Parallel, always-on: Medic heartbeat every 60s → SRE Agent benchmark loop every 60s → CEO Agent benchmark/starvation loop every 60s → Director arbitrates before either agent takes a destructive action
- **Decision Nodes & Error Handling:**
  - LLM API quota/error → Decision: retry with secondary provider? → yes: secondary model → no/exhausted: deterministic fallback template → flag job SUCCEEDED-DEGRADED
  - RunPod render failure or bounded-deadline exceeded → Decision: environment = production? → yes: fail job, alert, no CPU fallback (deliberate cost control) → no (dev): fall back to local CPU render path
  - Missing/failed media asset → Decision: primary source (Wikimedia) empty? → try secondary free source → still empty? → placeholder image + flag SUCCEEDED-DEGRADED
  - TTS engine failure → silent-audio fallback + flag SUCCEEDED-DEGRADED (never hard-fails the job)
  - Job exhausts max attempts → terminal FAILED → structured reason-code written to Dead-Letter/Triage store → Discord pager alert
  - Circuit breaker open on any external provider → fail fast to that stage's fallback tier instead of retrying the live call
  - SRE Agent detects critical memory / heartbeat silence / SLA breach → Decision: which specific queue/container is implicated? → targeted drain-and-restart of only that worker (not all 9)
  - CEO Agent detects starved business line → Decision: trending topic available? → yes: auto-create idempotent job → no: skip cycle, no duplicate/blind job creation
  - CEO Agent detects success-rate/failure-count breach for a line → suspend that line's queue → notify → Director checks SRE isn't mid-restart on same line before suspension takes effect
- **External Integrations & Storage:**
  - LLM providers: WaveSpeed / OpenRouter (OpenAI-compatible), primary + secondary
  - TTS: Kokoro (self-hosted, GPU-optional)
  - Media sourcing: Wikimedia Commons (primary), CLIP re-ranking, ChromaDB semantic cache, secondary free media source (new)
  - GPU burst compute: RunPod Serverless (CLIP/TTS/FFmpeg render)
  - Publish target: YouTube Data API v3 (OAuth refresh-token, headless)
  - Trend sourcing: Reddit (direct), TikTok/Instagram/Facebook (via Apify), YouTube/Google Trends
  - Object storage: MinIO (S3 API) — durable artifacts under `production/{BusinessModel}/job_{id}/`
  - System of record: PostgreSQL (Jobs, Artifacts, Events, YouTubeUploads, Dead-Letter/Triage, Cost Ledger — all with Alembic migrations)
  - Broker/ephemeral state: Redis (ARQ queues, Medic heartbeat, pending-YouTube-processing set, circuit-breaker state)
  - Logging/metrics: structured JSON logs, Prometheus `/metrics` (extended with fallback-tier and cost counters)
  - Alerting: Discord (pager), Telegram + Gmail (business/SRE ledger), generic outbound job webhook, unified in a Notification Hub

**[END OF DIAGRAM PROMPT]**

---

