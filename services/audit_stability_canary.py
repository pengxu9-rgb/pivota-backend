"""W7 stability-pair canary — the real measurement of W2's "comparable scores".

W2 pins the prompt set so a re-run of the same SKU is measured on the same
questions; the delta only calls a move "material" at >=5 when the basis matched.
That threshold is a *claim* until something actually re-runs a SKU and checks the
residual is within noise. This canary is that check — and it tests the MEASUREMENT
SYSTEM, not a merchant's real visibility (which legitimately moves over time).

AUTO-DETECT (no allowlist needed): each tick scans every merchant's completed runs
inside a TIGHT time window (STABILITY_WINDOW_HOURS, default 48h) and compares the
two most recent that share a measurement basis. The tight window is the trick — in
48h a merchant's real visibility barely moves, so a >tolerance delta on the SAME
pinned prompts is SYSTEM noise (our bug), not real change. Beyond the window a delta
is the merchant's genuine trend (the tracking chart's job), so we don't page on it.
A same-basis pair separated by more than the window is skipped. AUDIT_STABILITY_
CANARY_MERCHANTS, if set, narrows the scan to those ids (default = all merchants).

Passive by design — it does not enqueue runs, so it never spends metered audits on
its own (active pair-enqueue is a deliberate follow-up needing a run budget).

Reuses audit_delta's basis + score extraction so the canary and the merchant-facing
delta can never disagree about "same basis" or "what the scores were".
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STABILITY_TOLERANCE = int(os.getenv("AUDIT_STABILITY_TOLERANCE", "5"))
# Only a same-basis pair THIS close in time is a system-noise signal; wider gaps are
# the merchant's real trend, not a canary breach.
STABILITY_WINDOW_HOURS = int(os.getenv("AUDIT_STABILITY_WINDOW_HOURS", "48"))
SCORE_KEYS = ("visibility", "attribution", "category_visibility")


def _configured_merchants() -> List[str]:
    """Optional narrowing allowlist. Empty (default) → scan ALL merchants."""
    return [m.strip() for m in os.getenv("AUDIT_STABILITY_CANARY_MERCHANTS", "").split(",") if m.strip()]


def _hours_between(a: Any, b: Any) -> Optional[float]:
    """|a - b| in hours for two datetimes; None if either is missing/not a datetime."""
    try:
        return abs((a - b).total_seconds()) / 3600.0
    except Exception:  # noqa: BLE001
        return None


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


def _stability_pairs(rows: List[Dict[str, Any]], allow: set) -> List[Dict[str, Any]]:
    """Pure: from window-scoped completed runs (grouped by merchant, newest-first),
    form the comparable pair per merchant and evaluate stability. Only the two most
    recent runs per merchant are considered; the pair must share a basis AND fall
    within STABILITY_WINDOW_HOURS. Returns one result row per merchant that yielded
    a genuine comparable pair (skips insufficient / not-comparable / too-far-apart)."""
    by_merchant: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        mid = r.get("merchant_id")
        if not mid or (allow and mid not in allow):
            continue
        by_merchant.setdefault(mid, []).append(r)

    out: List[Dict[str, Any]] = []
    for mid, runs in by_merchant.items():
        if len(runs) < 2:
            continue
        a, b = runs[0], runs[1]  # two most recent (rows arrive newest-first)
        gap_h = _hours_between(a.get("requested_at"), b.get("requested_at"))
        if gap_h is not None and gap_h > STABILITY_WINDOW_HOURS:
            continue  # comparable basis but time-separated → real trend, not a canary pair
        result = stability_delta(a.get("report_jsonb") or {}, b.get("report_jsonb") or {})
        if result is None:
            continue  # different/unknown basis → not a comparable pair
        out.append({
            "merchant_id": mid,
            "status": "breach" if result["breach"] else "stable",
            "gap_hours": round(gap_h, 1) if gap_h is not None else None,
            "run_ids": [a.get("run_id"), b.get("run_id")],
            **result,
        })
    return out


async def run_stability_canary() -> Dict[str, Any]:
    """Scheduler entry: auto-detect same-basis pairs across all merchants inside the
    tight window and alert on any breach. Never raises."""
    from db.merchant_audit_runs import completed_runs_in_window

    try:
        rows = await completed_runs_in_window(window_seconds=STABILITY_WINDOW_HOURS * 3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stability canary scan failed: %s", str(exc)[:200])
        return {"error": str(exc)[:200]}

    results = _stability_pairs(rows, set(_configured_merchants()))
    for res in results:
        if res.get("status") != "breach":
            continue
        mid = res["merchant_id"]
        msg = (
            f"stability canary: merchant={mid} max_delta={res.get('max_delta')} "
            f"> tolerance={res.get('tolerance')} on the same pinned basis "
            f"{res.get('gap_hours')}h apart (deltas={res.get('deltas')}, runs={res.get('run_ids')})"
        )
        logger.error("W7_STABILITY_BREACH %s", msg)
        try:
            from services.agent_anomaly_detector import create_alert

            await create_alert(
                agent_id="audit_pipeline",
                alert_type=f"audit_stability.breach:{mid}",
                severity="critical",
                message=msg,
                metadata={k: res.get(k) for k in ("merchant_id", "max_delta", "deltas", "gap_hours", "run_ids")},
            )
        except Exception:  # noqa: BLE001
            logger.warning("stability canary create_alert failed", exc_info=True)
        try:
            import sentry_sdk

            sentry_sdk.capture_message(msg, level="error")
        except Exception:  # noqa: BLE001
            pass
    return {
        "window_hours": STABILITY_WINDOW_HOURS,
        "pairs_evaluated": len(results),
        "breaches": sum(1 for r in results if r.get("status") == "breach"),
        "results": results,
    }
