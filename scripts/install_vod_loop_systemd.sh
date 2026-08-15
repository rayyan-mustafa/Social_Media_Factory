#!/usr/bin/env bash
# Install vod-loop@.service for dual-channel 24/7 YouTube Live (CPU only).
# Isolated from RunPod GPU farm. Does NOT touch StreamCast binaries.
#
# Usage (from repo root on VPS):
#   bash scripts/install_vod_loop_systemd.sh
#   bash scripts/install_vod_loop_systemd.sh --enable
#   bash scripts/install_vod_loop_systemd.sh --enable --crontab
#
# Requires: sudo for system unit install. Stream keys must already be in .env
# (never pass keys on the CLI). Rotate keys if they were exposed via StreamCast.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENABLE=0
INSTALL_CRON=0
USER_NAME="${VOD_LOOP_SYSTEMD_USER:-ubuntu}"

for arg in "$@"; do
  case "$arg" in
    --enable) ENABLE=1 ;;
    --crontab) INSTALL_CRON=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

echo "==> Rendering vod-loop@.service (user=${USER_NAME})"
"$PY" -m src.cli.vod_loop systemd --write --user "$USER_NAME"

UNIT_SRC="${ROOT}/output/ops/vod-loop@.service"
if [[ ! -f "$UNIT_SRC" ]]; then
  echo "Missing generated unit: $UNIT_SRC" >&2
  exit 1
fi

echo "==> Installing system unit (sudo)"
sudo cp "$UNIT_SRC" /etc/systemd/system/vod-loop@.service
sudo systemctl daemon-reload

if [[ "$ENABLE" -eq 1 ]]; then
  echo "==> Enabling dual channels"
  sudo systemctl enable --now vod-loop@napstorian
  sudo systemctl enable --now vod-loop@napping_historian
  sudo systemctl --no-pager --full status 'vod-loop@napstorian' 'vod-loop@napping_historian' || true
else
  echo "Unit installed but not enabled. When RTMP keys are set in .env:"
  echo "  sudo systemctl enable --now vod-loop@napstorian"
  echo "  sudo systemctl enable --now vod-loop@napping_historian"
fi

if [[ "$INSTALL_CRON" -eq 1 ]]; then
  echo "==> Optional cron */10 healthcheck (backup; systemd is primary)"
  "$PY" -m src.cli.vod_loop crontab --install
fi

echo "==> Status snapshot (no keys printed)"
"$PY" -m src.cli.vod_loop status || true

echo
echo "Docs: ${ROOT}/output/ops/VOD_LOOP.md"
echo "After 48–72h green on VPS: retire StreamCast for production Live."
echo "If StreamCast ever exposed keys: rotate in YouTube Studio + python -m src.cli.vod_loop key-hygiene"
