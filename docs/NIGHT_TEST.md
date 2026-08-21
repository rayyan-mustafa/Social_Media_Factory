# Night test — production YouTube_Shorts path

Use after `docker compose up -d --build` with YouTube OAuth secrets present.

## Command

```bash
python scripts/night_test_main.py
# or:
curl -X POST http://localhost:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{"topic":"The Silk Road","niche":"documentary","business_model":"YouTube_Shorts","idempotency_key":"night-test-1"}'
```

`YouTube_Shorts` always sets `upload_to_youtube=true` (direct publish; no MinIO folder).

## Pass criteria

- [ ] Health returns 200 with `"status":"ok"`
- [ ] Job stages reach `succeeded`
- [ ] Artifact recorded as `youtube:{video_id}` (not a `production/...` key)
- [ ] YouTube video id present on job / `youtube_uploads` row
- [ ] Audio is real Kokoro when ML/RunPod path is used
- [ ] `job_events` rows for transitions
- [ ] No secrets in logs

Other models: durable files under `production/{BusinessModel}/job_{id}/` — see [PRODUCTION_MODELS.md](PRODUCTION_MODELS.md).

VPS + RunPod: [VPS_PRODUCTION.md](VPS_PRODUCTION.md).
