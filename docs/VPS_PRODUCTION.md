# VPS production deploy (control plane + RunPod GPU render)

## Architecture

```text
Your VPS (docker compose)              RunPod Serverless
├── FastAPI / Postgres / Redis / MinIO ├── Kokoro TTS
├── 9 lean ARQ workers                 ├── CLIP media rank
└── enqueues video → RunPod            └── FFmpeg assemble
```

YouTube_Shorts: temp render on RunPod → **direct YouTube upload** (no MinIO folder).  
Other models: files under `production/{Model}/job_{id}/` in MinIO.

## One-time: RunPod image

On a machine with Docker + registry access:

```bash
docker build -f deploy/Dockerfile.render -t YOUR_REGISTRY/youtube-render:latest .
docker push YOUR_REGISTRY/youtube-render:latest
```

In RunPod console: Serverless endpoint → set that image. Copy **Endpoint ID** into `.env` as `RUNPOD_ENDPOINT_ID`.

The handler must be the **async** `deploy/runpod_handler.py` (already in this repo).

## One-time: secrets on the VPS

```bash
git clone <your-repo> ~/youtube_automation
cd ~/youtube_automation
# copy .env from your workstation (with real keys)
# copy secrets/client_secret.json and secrets/youtube_token.json
```

Required `.env` keys: `POSTGRES_PASSWORD`, `S3_SECRET_KEY`, `WAVESPEED_API_KEY`,
`RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID`. Recommended: `API_KEY`.

## Start production

```bash
bash deploy/vps_up.sh
```

Equivalent:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
curl http://127.0.0.1:8000/health
```

Preflight only:

```bash
python3 scripts/prod_preflight.py
```

## Verify

```bash
curl http://127.0.0.1:8000/health
# with API_KEY if set:
curl -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"topic":"The Silk Road","niche":"documentary","business_model":"YouTube_Shorts"}'
```

Worker logs should show `runpod_attempt_start` then `runpod_completed`.  
If RunPod fails in production, the job **fails** (no silent CPU fallback on the VPS).

## Notes

- Prod overlay keeps Postgres/Redis/MinIO off the public ports (API on `:8000` only).
- All VPS workers use the lean `Dockerfile`; ML stays on RunPod.
- Growth tools: add `--profile growth` only if you need Airtable/Apify/watchdog.
