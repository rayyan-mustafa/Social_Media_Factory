# Demo recording checklist (portfolio / interview)

Use this before showing the project to a German employer.

## Prep
- [ ] Fresh `docker compose up -d --build` on a clean machine or VPS
- [ ] `.env` filled; no secrets visible on screen
- [ ] `GET /health` returns `"status": "ok"`
- [ ] `GET /metrics` scrapes without error
- [ ] OpenAPI at `/docs` loads

## Happy path (5–8 min Loom)
1. Show architecture diagram from README (30s)
2. `POST /v1/jobs` with idempotency key; show `202` + `queued`
3. Poll `GET /v1/jobs/{id}` through stages
4. List artifacts; download/open final MP4 from MinIO console (`:9001`)
5. Show Postgres rows: `jobs`, `job_events`, `job_artifacts`
6. Mention RunPod path and why GPU is optional
7. Show CI green on GitHub Actions

## Failure path (optional, strong signal)
- [ ] Kill worker mid-job; restart; show retry / attempt counter
- [ ] Invalid LLM key → fallback script still produces video
- [ ] Discord webhook fires only on terminal failure (if configured)

## Talking points
- State machine + idempotency keys
- Alembic migrations vs raw SQL
- Refresh-token YouTube auth (not browser OAuth on the server)
- Cost-aware control plane vs burst GPU
