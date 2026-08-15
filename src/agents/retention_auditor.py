"""Retention Auditor — cheap pre-stills gates (Gate R + visual-leak) before RunPod burn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agents.ledger import OpsLedger
from src.agents.store import OpsStore
from src.domain.models import ScriptResult
from src.services.gate_retention import run_gate_r
from src.services.script_validate import _VISUAL_MARKERS


class RetentionAuditor:
    """Run before expensive visuals when possible; always before publish."""

    def __init__(self, store: OpsStore | None = None, ledger: OpsLedger | None = None):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)

    def audit_script(self, script: ScriptResult) -> dict[str, Any]:
        leaks = [
            sc.index
            for sc in script.scenes
            if any(m in (sc.text or "").lower() for m in _VISUAL_MARKERS)
        ]
        n = len(script.scenes)
        ok = len(leaks) == 0 and n > 0
        result = {
            "ok": ok,
            "scene_count": n,
            "visual_leak_scenes": leaks[:20],
            "estimated_duration_s": script.validation.estimated_duration_s
            if script.validation
            else None,
        }
        if not ok:
            self.ledger.write(
                agent="retention_auditor",
                problem=f"script audit failed leaks={len(leaks)} scenes={n}",
                action="block stills until script fix",
                severity="error",
            )
        return result

    def audit_before_publish(
        self,
        *,
        final_path: Path | str,
        script_path: Path | str | None = None,
        voice_manifest: Path | str | None = None,
        job_dir: Path | str | None = None,
        enforce: bool = False,
    ) -> dict[str, Any]:
        gate = run_gate_r(
            final_path=final_path,
            script_path=script_path,
            voice_manifest=voice_manifest,
            job_dir=job_dir,
            enforce=enforce,
        )
        payload = gate.model_dump()
        if not gate.ok and enforce:
            self.ledger.write(
                agent="retention_auditor",
                problem="Gate R failed before publish",
                action="block upload",
                severity="error",
                extra={"errors": gate.errors},
            )
        elif gate.warnings:
            self.ledger.write(
                agent="retention_auditor",
                problem="Gate R warnings",
                action="warn only",
                severity="warn",
                extra={"warnings": gate.warnings},
            )
        return payload
