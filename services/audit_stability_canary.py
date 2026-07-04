"""W7 stability-pair canary — the real measurement of W2's "comparable scores".

W2 pins the prompt set so a re-run of the same SKU is measured on the same
questions; the delta only calls a move "material" at >=5 when the basis matched.
That threshold is a *claim* until something actually re-runs a SKU and checks the
residual is within noise. This canary is that check: for each configured house
account, it compares the two most recent COMPLETED runs and — only when they share
a measurement basis (same pinned prompt set) — asserts every score is within
STABILITY_TOLERANCE. A breach means either W2 regressed or the tolerance is
optimistic; either way it should page, not hide.

Env-gated: AUDIT_STABILITY_CANARY_MERCHANTS is a comma-separated merchant_id list;
empty (default) → the tick is a no-op. Passive by design — it does not enqueue
runs, so it never spends metered audits on its own. (Active pair-enqueue is a
deliberate follow-up: it costs house-account runs and wants an explicit budget.)

Reuses audit_delta's basis + score extraction so the canary and the merchant-facing
delta can never disagree about "same basis" or "what the scores were".
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STABILITY_TOLERANCE = int(os.getenv("AUDIT_STABILITY_TOLERANCE", "5"))
SCORE_KEYS = ("visibility", "attribution", "category_visibility")


def _configured_merchants() -> List[str]:
    return [m.strip() for m in os.getenv("AUDIT_STABILITY_CANARY_MERCHANTS", "").split(",") if m.strip()]


def stability_delta(
    current_report: Dict[str, Any], prior_report: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Pure: |Δ| per score between two runs, but ONLY when they share a measurement
    basis (same pinned prompt set). Returns None when not comparable — different or
    unknown basis (which also covers "the two runs were different SKUs", since their
    prompt_set_ids differ), or no overlapping scores. Else
    {deltas, max_delta, breach, basis_id}.
    """
    from services import audit_delta as ad

    cur_primary = ad._primary_report(current_report)
    pri_primary = ad._primary_report(prior_report)
    basis = ad._measurement_basis(current_report, prior_report, cur_primary, pri_primary)
    if basis.get("same") is not True:
        return None

    cur = ad._scores(cur_primary)
    pri = ad._scores(pri_primary)
    deltas: Dict[str, int] = {}
    for k in SCORE_KEYS:
        if cur.get(k) is not None and pri.get(k) is not None:
            deltas[k] = abs(int(cur[k]) - int(pri[k]))
    if not deltas:
        return None
    max_delta = max(deltas.values())
    return {
        "deltas": deltas,
        "max_delta": max_delta,
        "tolerance": STABILITY_TOLERANCE,
        "breach": max_delta > STABILITY_TOLERANCE,
        "basis_id": basis.get("prompt_set_id"),
    }


async def check_merchant_stability(merchant_id: str) -> Dict[str, Any]:
    """Read the two most recent completed runs for one merchant and apply
    stability_delta. Never raises."""
    from db.merchant_audit_runs import recent_completed_reports_for_merchant

    reports = await recent_completed_reports_for_merchant(merchant_id=merchant_id, limit=2)
    if len(reports) < 2:
        return {"merchant_id": merchant_id, "status": "insufficient_runs"}
    result = stability_delta(reports[0].get("report_jsonb") or {}, reports[1].get("report_jsonb") or {})
    if result is None:
        return {"merchant_id": merchant_id, "status": "not_comparable"}
    return {
        "merchant_id": merchant_id,
        "status": "breach" if result["breach"] else "stable",
        "run_ids": [reports[0].get("run_id"), reports[1].get("run_id")],
        **result,
    }


async def run_stability_canary() -> Dict[str, Any]:
    """Scheduler entry: check each configured house account, alert on any breach.
    No-op when unconfigured. Never raises."""
    merchants = _configured_merchants()
    results: List[Dict[str, Any]] = []
    for mid in merchants:
        try:
            res = await check_merchant_stability(mid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stability canary failed for %s: %s", mid, str(exc)[:200])
            res = {"merchant_id": mid, "status": "error", "error": str(exc)[:200]}
        results.append(res)
        if res.get("status") == "breach":
            msg = (
                f"stability canary: merchant={mid} max_delta={res.get('max_delta')} "
                f"> tolerance={res.get('tolerance')} on the same pinned basis "
                f"(deltas={res.get('deltas')}, runs={res.get('run_ids')})"
            )
            logger.error("W7_STABILITY_BREACH %s", msg)
            try:
                from services.agent_anomaly_detector import create_alert

                await create_alert(
                    agent_id="audit_pipeline",
                    alert_type="audit_stability.breach",
                    severity="critical",
                    message=msg,
                    metadata={k: res.get(k) for k in ("merchant_id", "max_delta", "deltas", "run_ids")},
                )
            except Exception:  # noqa: BLE001
                logger.warning("stability canary create_alert failed", exc_info=True)
            try:
                import sentry_sdk

                sentry_sdk.capture_message(msg, level="error")
            except Exception:  # noqa: BLE001
                pass
    return {"checked": len(merchants), "results": results}
