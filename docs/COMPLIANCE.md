# Compliance notes (EU / GDPR-friendly framing)

This system processes **content production jobs**, not end-user personal profiles.

## Data we store
- Job metadata (topic, niche, business_model, status, stages)
- Append-only `job_events` (audit trail)
- Artifact object keys + SHA-256 checksums in MinIO
- Optional YouTube video IDs after publish

## Data we do not store by design
- Viewer PII, emails, or analytics profiles in the core pipeline
- Secrets in git (use `.env` / `secrets/` mounts)

## Media licensing
- Default stills come from **Wikimedia Commons** (check license per asset before public monetization)
- Prefer attribution metadata in artifact `meta` when available

## Secrets
- Rotate Discord / API keys if ever exposed
- Set `API_KEY` in production so `/v1/*` requires `X-API-Key`
- YouTube OAuth refresh token lives only under `/secrets`

## Operator duties
- Retention: prune MinIO prefixes and old jobs per your policy
- Logs: structured JSON; do not log API keys or tokens
- Health: `/health` returns **503** when DB/Redis are down
