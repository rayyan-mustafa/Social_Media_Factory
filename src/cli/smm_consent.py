"""Rayyan consent CLI for coding restores / test-channel flags."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.agents.store import OPS_DIR
from src.services.smm_pipeline_ab import apply_coding_restore_if_consented


def main() -> int:
    p = argparse.ArgumentParser(description="SMM consent actions (Rayyan)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("accept_restore", help="Consent to restore a coding snapshot")
    a.add_argument("--id", required=True, help="Snapshot id")

    d = sub.add_parser("decline_restore", help="Decline a proposed coding restore")
    d.add_argument("--id", required=True)

    t = sub.add_parser("accept_test_channel", help="Consent flag for test-channel validation")
    t.add_argument("--video-id", required=True)

    args = p.parse_args()
    OPS_DIR.mkdir(parents=True, exist_ok=True)

    if args.cmd == "accept_restore":
        flag = OPS_DIR / f"smm_consent_restore_{args.id}.flag"
        flag.write_text("accepted\n", encoding="utf-8")
        result = apply_coding_restore_if_consented(args.id, force=True)
        print(result)
        return 0 if result.get("ok") else 1

    if args.cmd == "decline_restore":
        flag = OPS_DIR / f"smm_consent_restore_{args.id}.declined"
        flag.write_text("declined\n", encoding="utf-8")
        print({"ok": True, "declined": args.id})
        return 0

    if args.cmd == "accept_test_channel":
        flag = OPS_DIR / f"smm_consent_accept_{args.video_id}.flag"
        flag.write_text("accepted\n", encoding="utf-8")
        print({"ok": True, "flag": str(flag)})
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
