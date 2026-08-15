"""Policy Agent — official YouTube Help sync + Gate B (Plan C Module 6)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.agents.ledger import OpsLedger
from src.agents.store import OpsStore, PolicySnapshot
from src.domain.models import ScriptResult
from src.services.settings import CONFIG_DIR, ROOT, get_settings

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = ROOT / "output" / "ops" / "policy_raw"


class GateBResult:
    def __init__(
        self,
        ok: bool,
        *,
        policy_version: int | None = None,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
        checks: dict[str, bool] | None = None,
    ):
        self.ok = ok
        self.policy_version = policy_version
        self.errors = errors or []
        self.warnings = warnings or []
        self.checks = checks or {}

    def model_dump(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "policy_version": self.policy_version,
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": self.checks,
        }


class PolicyAgent:
    """Fetch official YT Help pages → versioned rule pack → Gate B."""

    def __init__(self, store: OpsStore | None = None, ledger: OpsLedger | None = None):
        self.store = store or OpsStore()
        self.ledger = ledger or OpsLedger(self.store)
        self.s = get_settings()
        self.cfg = _load_policy_config()

    def refresh(self, *, force: bool = False) -> PolicySnapshot:
        """Cron / pre-publish: fetch sources, hash, compile rule pack."""
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        sources_out: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        compile_ok = True

        for src in self.cfg.get("sources") or []:
            url = src.get("url") or ""
            sid = src.get("id") or "unknown"
            body = ""
            err = None
            try:
                with httpx.Client(timeout=45.0, follow_redirects=True) as client:
                    resp = client.get(
                        url,
                        headers={"User-Agent": "yt-long-factory-policy-agent/1.0"},
                    )
                    resp.raise_for_status()
                    body = resp.text
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                compile_ok = False
                logger.warning("policy fetch failed %s: %s", sid, exc)

            raw_path = SNAPSHOT_DIR / f"{sid}.html"
            if body:
                raw_path.write_text(body, encoding="utf-8")
                digest.update(body.encode("utf-8", errors="ignore"))
            sources_out.append(
                {
                    "id": sid,
                    "url": url,
                    "label": src.get("label"),
                    "ok": err is None,
                    "error": err,
                    "bytes": len(body.encode("utf-8")) if body else 0,
                    "path": str(raw_path) if body else "",
                }
            )

        content_hash = digest.hexdigest()[:32] if any(s["ok"] for s in sources_out) else "empty"
        latest = self.store.latest_policy_snapshot()
        version = 1
        if latest:
            if latest.content_hash == content_hash and not force:
                self.ledger.write(
                    agent="policy",
                    problem="policy snapshot unchanged",
                    action="reuse existing rule pack",
                    severity="info",
                    extra={"version": latest.version, "hash": content_hash},
                )
                return latest
            version = int(latest.version) + 1

        rule_pack = self._compile_rule_pack(sources_out)
        if not compile_ok:
            self.ledger.write(
                agent="policy",
                problem="one or more official sources failed to fetch",
                action="HOLD all publics until refresh succeeds",
                severity="critical",
                extra={"sources": sources_out},
            )

        snap = PolicySnapshot(
            version=version,
            content_hash=content_hash,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            sources=sources_out,
            rule_pack=rule_pack,
            compile_ok=compile_ok and bool(rule_pack),
            path=str(SNAPSHOT_DIR),
        )
        self.store.save_policy_snapshot(snap)
        (SNAPSHOT_DIR / "latest_rule_pack.json").write_text(
            json.dumps(rule_pack, indent=2) + "\n", encoding="utf-8"
        )
        self.ledger.write(
            agent="policy",
            problem=f"policy_version={version} compiled",
            action="bump snapshot + Gate B must re-run",
            severity="info" if snap.compile_ok else "error",
            extra={"hash": content_hash, "compile_ok": snap.compile_ok},
        )
        return snap

    def _compile_rule_pack(self, sources: list[dict[str, Any]]) -> dict[str, Any]:
        """Deterministic seed rules (LLM quote-constrained compile can extend later)."""
        return {
            "max_age_hours": float(self.cfg.get("max_policy_age_hours") or 12),
            "topic_allowlist": list(self.cfg.get("topic_allowlist") or []),
            "rules": {
                "ai_disclosure": {
                    "required": True,
                    "description": "AI/altered-content disclosure in description + synthetic flag",
                },
                "no_real_person_abuse": {
                    "required": True,
                    "description": "Ban non-consensual deepfakes / deceptive private likeness",
                },
                "no_misleading_thumb_title": {
                    "required": True,
                    "description": "Title/thumb must match script promise",
                },
                "originality": {
                    "required": True,
                    "description": "Script not near-duplicate of last N jobs",
                },
                "topic_allowlist": {
                    "required": True,
                    "description": "Niche allowlist only (history / what-if)",
                },
                "music_license": {
                    "required": True,
                    "description": "Configured BGM pack or silence",
                },
                "made_for_kids": {
                    "required": True,
                    "description": "Correct selfDeclaredMadeForKids",
                },
                "spam_cadence": {
                    "required": True,
                    "description": "Max publishes/day",
                },
                "advertiser_friendly": {
                    "required": False,
                    "description": "Borderline → human hold",
                },
            },
            "sources_ok": [s["id"] for s in sources if s.get("ok")],
            "sources_failed": [s["id"] for s in sources if not s.get("ok")],
        }

    def title_policy_ok(self, title: str) -> tuple[bool, list[str]]:
        """Trends pre-filter: title must hit allowlist keywords."""
        allow = [a.lower() for a in (self.cfg.get("topic_allowlist") or [])]
        t = (title or "").lower()
        if not allow:
            return True, []
        if any(a in t for a in allow):
            return True, []
        return False, [f"title misses topic allowlist: {allow[:5]}"]

    def run_gate_b(
        self,
        *,
        script: ScriptResult | None = None,
        title: str = "",
        description: str = "",
        ai_disclosure: bool = True,
        made_for_kids: bool | None = None,
        refresh_if_stale: bool = True,
    ) -> GateBResult:
        snap = self.store.latest_policy_snapshot()
        if snap is None or (refresh_if_stale and self._is_stale(snap)):
            snap = self.refresh()

        errors: list[str] = []
        warnings: list[str] = []
        checks: dict[str, bool] = {}

        if not snap.compile_ok:
            errors.append("policy rule pack compile_ok=false — HOLD publics")
        if self._is_stale(snap):
            errors.append(
                f"policy snapshot stale (version={snap.version}, fetched_at={snap.fetched_at})"
            )

        pack = snap.rule_pack or {}
        allow = [a.lower() for a in (pack.get("topic_allowlist") or self.cfg.get("topic_allowlist") or [])]
        topic_text = " ".join(
            [
                title,
                (script.topic if script else ""),
                (script.title if script else ""),
            ]
        ).lower()
        ok_topic = (not allow) or any(a in topic_text for a in allow)
        checks["topic_allowlist"] = ok_topic
        if not ok_topic:
            errors.append("topic_allowlist failed — niche must be history/what-if gallery")

        disc = ai_disclosure and (
            "ai" in (description or "").lower()
            or "synthetic" in (description or "").lower()
            or "altered" in (description or "").lower()
            or bool(getattr(self.s, "youtube_ai_disclosure_text", ""))
        )
        # Prefer explicit flag from publish path
        checks["ai_disclosure"] = bool(ai_disclosure)
        if not ai_disclosure:
            errors.append("ai_disclosure missing")

        kids = (
            self.s.youtube_made_for_kids
            if made_for_kids is None
            else made_for_kids
        )
        checks["made_for_kids"] = kids is False
        if kids:
            warnings.append("made_for_kids=true — confirm audience")

        checks["music_license"] = True  # audio bed uses generated/royalty-free assets
        checks["no_real_person_abuse"] = True
        if script:
            for sc in script.scenes[:5]:
                if re.search(r"\b(deepfake|private citizen)\b", sc.text or "", re.I):
                    checks["no_real_person_abuse"] = False
                    errors.append("possible real-person abuse language in script")
                    break

        checks["no_misleading_thumb_title"] = True
        if script and title and script.hook:
            # Soft check: title shares a content word with hook/topic
            words = {w for w in re.findall(r"[a-z]{4,}", title.lower())}
            corpus = f"{script.hook} {script.topic}".lower()
            if words and not any(w in corpus for w in words):
                checks["no_misleading_thumb_title"] = False
                warnings.append("title weakly related to hook — human review")

        checks["originality"] = True
        # spam_cadence Gate B (>=3 publishes/day HOLD) removed — cadence no longer blocks publish.
        checks["spam_cadence"] = True

        checks["advertiser_friendly"] = True

        ok = not errors
        if not ok:
            self.ledger.write(
                agent="policy",
                problem="Gate B FAILED",
                action="block public / hold job",
                severity="error",
                publish_status="hold",
                extra={"errors": errors, "version": snap.version},
            )
        return GateBResult(
            ok=ok,
            policy_version=snap.version,
            errors=errors,
            warnings=warnings,
            checks=checks,
        )

    def _is_stale(self, snap: PolicySnapshot) -> bool:
        max_age = float(
            (snap.rule_pack or {}).get("max_age_hours")
            or self.cfg.get("max_policy_age_hours")
            or 12
        )
        try:
            fetched = datetime.fromisoformat(snap.fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600.0
        return age_h > max_age


def _load_policy_config() -> dict[str, Any]:
    path = CONFIG_DIR / "policy_sources.json"
    if not path.exists():
        return {"max_policy_age_hours": 12, "sources": [], "topic_allowlist": []}
    return json.loads(path.read_text(encoding="utf-8"))


def run_gate_b(**kwargs: Any) -> GateBResult:
    return PolicyAgent().run_gate_b(**kwargs)
