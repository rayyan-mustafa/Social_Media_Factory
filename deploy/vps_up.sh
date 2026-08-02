#!/usr/bin/env bash
# Production bring-up on VPS: control plane locally, heavy video on RunPod.
# Usage (from repo root on the VPS):
#   bash deploy/vps_up.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$APP_DIR"

COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)

if [[ ! -f .env ]]; then
  echo "ERROR: missing .env — copy from your workstation (never commit it)."
  exit 1
fi

mkdir -p secrets
missing=0
for f in secrets/client_secret.json secrets/youtube_token.json; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: missing $f (YouTube OAuth for YouTube_Shorts direct upload)"
    missing=1
  fi
done

# Fail closed if RunPod credentials missing (production requires GPU offload)
if ! grep -qE '^RUNPOD_API_KEY=.+' .env; then
  echo "ERROR: RUNPOD_API_KEY empty in .env"
  missing=1
fi
if ! grep -qE '^RUNPOD_ENDPOINT_ID=.+' .env; then
  echo "ERROR: RUNPOD_ENDPOINT_ID empty in .env"
  missing=1
fi
if ! grep -qE '^POSTGRES_PASSWORD=.+' .env; then
  echo "ERROR: POSTGRES_PASSWORD empty in .env"
  missing=1
fi
if ! grep -qE '^S3_SECRET_KEY=.+' .env; then
  echo "ERROR: S3_SECRET_KEY empty in .env"
  missing=1
fi
if [[ "$missing" -ne 0 ]]; then
  exit 1
fi

# Force production + RunPod + in-compose service DNS (override any laptop localhost URLs)
python3 - <<'PY' || true
from pathlib import Path
p = Path(".env")
lines = p.read_text(encoding="utf-8").splitlines()
kv = {}
order = []
for line in lines:
    if not line.strip() or line.strip().startswith("#") or "=" not in line:
        order.append(("raw", line))
        continue
    k, _, v = line.partition("=")
    kv[k] = v
    order.append(("kv", k))
# rebuild preserving unknown keys
seen = set()
out = []
for kind, val in order:
    if kind == "raw":
        out.append(val)
        continue
    k = val
    if k in seen:
        continue
    seen.add(k)
    out.append(f"{k}={kv[k]}")
def setv(k, v):
    global out
    kv[k] = v
    if k not in seen:
        out.append(f"{k}={v}")
        seen.add(k)
    else:
        out = [f"{k}={v}" if x.startswith(k + "=") else x for x in out]
setv("APP_ENV", "production")
setv("RUNPOD_ENABLED", "true")
setv("S3_ENDPOINT_URL", "http://minio:9000")
setv("REDIS_URL", "redis://redis:6379/0")
# Keep DATABASE_URL host as postgres for containers; compose also overrides per-service
user = kv.get("POSTGRES_USER", "youtube")
password = kv.get("POSTGRES_PASSWORD", "change_me")
db = kv.get("POSTGRES_DB", "youtube_automation")
setv("DATABASE_URL", f"postgresql+asyncpg://{user}:{password}@postgres:5432/{db}")
p.write_text("\n".join(out) + "\n", encoding="utf-8")
print("env_normalized: APP_ENV=production RUNPOD_ENABLED=true hosts=compose")
PY

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker missing — running deploy/ovh_bootstrap.sh"
  bash deploy/ovh_bootstrap.sh
  echo "Re-login for docker group if needed, then re-run: bash deploy/vps_up.sh"
  exit 0
fi

echo "=== preflight ==="
python3 scripts/prod_preflight.py

echo "=== compose up ==="
"${COMPOSE[@]}" up -d --build
"${COMPOSE[@]}" ps
echo "=== health ==="
sleep 5
curl -sf http://127.0.0.1:8000/health | python3 -m json.tool || curl -sf http://127.0.0.1:8000/health || true
echo
echo "Production stack is up."
echo "  API:     http://127.0.0.1:8000/health"
echo "  Render:  RunPod (RUNPOD_ENABLED=true) — ensure endpoint image is deploy/Dockerfile.render"
echo "  Models:  see docs/PRODUCTION_MODELS.md"
echo "  Docs:    docs/VPS_PRODUCTION.md"
