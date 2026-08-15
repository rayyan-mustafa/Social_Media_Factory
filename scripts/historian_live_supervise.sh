#!/usr/bin/env bash
# DEPRECATED / HARD-DISABLED — do NOT run.
#
# Dual supervisors on the SAME historian RTMP key (this script + systemd
# vod-loop@napping_historian / python vod_loop supervise) caused reconnect
# storms and YouTube Studio "not reliably live".
#
# Production owner (only):
#   sudo systemctl enable --now vod-loop@napping_historian
#
# This file always exits 0 and never starts ffmpeg, even if
# VOD_LOOP_FORCE_DIRECT=1 is set. Emergency direct encode belongs in a
# one-off manual ffmpeg command under ops supervision — not this wrapper.
set -euo pipefail
ROOT=/home/ubuntu/new_yt_automation
LOG="$ROOT/output/ops/vod_loop_napping_historian.direct.stderr"
mkdir -p "$ROOT/output/ops"
echo "# historian_direct_hard_refused $(date -u +%Y-%m-%dT%H:%M:%SZ) use_systemd_vod-loop@napping_historian" >>"$LOG"
echo "refused: historian_live_supervise.sh is hard-disabled; use systemd vod-loop@napping_historian" >&2
exit 0
