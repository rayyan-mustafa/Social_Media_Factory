# YouTube Automation Engine

Production-oriented **Backend / Platform** project: an automated documentary content pipeline with durable job state, async workers, object storage, and optional GPU offload.

> Portfolio framing for German employers: *“I built a fault-tolerant content pipeline with job queues, typed APIs, migrations, CI, and cost-aware GPU offload.”*

## Architecture

```text
OVH VPS (always on)                    RunPod (on demand, $0 idle)
├── FastAPI  /health /metrics /v1/jobs ├── CLIP ranking
├── ARQ workers (Redis)                ├── Kokoro TTS
├── PostgreSQL + Alembic               └── FFmpeg assemble
├── MinIO (S3 artifacts)
└── YouTube upload (refresh token)
         │
         ├── WaveSpeed / OpenRouter (LLM scripts)
         └── Wikimedia Commons (media)
```

**Honest split:** the VPS runs the control plane and can execute the full pipeline. RunPod is for CLIP/TTS burst capacity — not for “rendering a still image on an RTX 4090.”

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI + Pydantic v2 |
| Workers | Redis + ARQ |
| DB | PostgreSQL 16 + SQLAlchemy 2 + Alembic |
| Storage | MinIO (S3 API) |
| TTS | Kokoro (open, self-hosted) |
| Media | Wikimedia + CLIP + ChromaDB cache |
| Compose | FFmpeg (Ken Burns / zoompan scenes) |
| Publish | YouTube Data API v3 (refresh token) |
| Quality | ruff, mypy, pytest, GitHub Actions |
| Observability | JSON logs, Prometheus `/metrics`, Discord pager-only |

## Quick start (local)

```bash
cp .env.example .env
# Edit POSTGRES_PASSWORD, S3_SECRET_KEY, LLM_API_KEY

docker compose up -d --build
curl http://localhost:8000/health

# Enqueue a job
curl -X POST http://localhost:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{"topic":"The Silk Road","niche":"documentary","idempotency_key":"demo-1"}'
```

Without Docker (API + tests only):

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
ruff check src tests
mypy src
```

ML extras (Kokoro/CLIP) for a full local worker:

```bash
pip install -e ".[ml]"
# also need ffmpeg + espeak-ng on the host
```

## Job lifecycle

`queued → scripting → tts → media → composing → uploading → succeeded | failed`

Artifacts land in MinIO under `jobs/{id}/…`. Events are append-only in `job_events`.

## YouTube auth (headless VPS)

1. Create a Google Cloud OAuth desktop client; download `client_secret.json` → `secrets/`.
2. Locally: `python scripts/youtube_auth_bootstrap.py`
3. Copy `secrets/youtube_token.json` to the VPS `secrets/` mount (never commit it).

## OVH deploy

```bash
bash deploy/ovh_bootstrap.sh
# then on VPS:
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
```

## RunPod image

```bash
docker build -f deploy/Dockerfile.render -t youtube-render:latest .
# Push to your registry and attach as a Serverless endpoint.
# Set RUNPOD_ENABLED=true, RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID on the VPS.
```

## Cost sheet (indicative)

| Item | Est. |
|------|------|
| OVHcloud VPS-2 (4 vCPU / 8 GB) | ~€7–9 / mo |
| RunPod serverless | pay-per-second, $0 idle |
| LLM (WaveSpeed/OpenRouter) | per token |
| Kokoro TTS | $0 (self-hosted) |
| MinIO on VPS disk | included |

Daily 1× ~1.5 GB upload ≈ ~45 GB/mo outbound — well within a 1 TB VPS quota.

## GDPR / secrets

- No end-user PII in this system (content jobs only).
- Secrets via `.env` / Docker mounts — never hardcoded in Compose.
- Rotate API keys; keep `client_secret.json` and tokens out of git.

## Docs

- [ADR-001: ARQ over Celery](docs/adr/001-arq-over-celery.md)
- [ADR-002: MinIO for artifacts](docs/adr/002-minio-artifacts.md)
- [ADR-003: Kokoro over ElevenLabs](docs/adr/003-kokoro-tts.md)
- [Demo checklist](docs/DEMO_CHECKLIST.md)

## API sketch

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness + DB/Redis checks |
| GET | `/metrics` | Prometheus |
| POST | `/v1/jobs` | Create + enqueue |
| GET | `/v1/jobs` | List |
| GET | `/v1/jobs/{id}` | Status |
| GET | `/v1/jobs/{id}/artifacts` | S3 keys |

OpenAPI: `http://localhost:8000/docs`
