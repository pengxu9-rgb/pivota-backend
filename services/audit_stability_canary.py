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
delta can never disagree about "same basis" or "what the scores were". That reuse
is why this module reads `audit_basis` rows: "same basis" is now the FULL basis
(model, official-domain set, tier mix, market), and audit_delta answers "unknown"
without both runs' recorded rows — so a canary that did not fetch them would
compare nothing at all, quietly, with its negative tests still green.
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
    current_report: Dict[str, Any],
    prior_report: Dict[str, Any],
    *,
    current_basis: Optional[Dict[str, Any]] = None,
    prior_basis: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Pure: |Δ| per score between two runs, but ONLY when they share a measurement
    basis. Returns None when not comparable — a different or unknown pinned set
    (which also covers "the two runs were different SKUs", since their
    prompt_set_ids differ), a diverged RUN-LEVEL basis, or no overlapping scores.
    Else {deltas, max_delta, breach, basis_id}.

    THE TWO RUNS' RECORDED BASES ARE REQUIRED, not optional colour. Comparability
    is now the full basis — model, official-domain set, tier mix, market — and
    `measurement_basis_between` answers `None` when either side is missing, so a
    caller that passes nothing gets `None` for every pair and this canary is
    INERT while its own negative tests stay green. That is the failure mode this
    signature exists to make visible: the fetch lives in
    :func:`_bases_by_run_id`, :func:`_stability_pairs` threads it through, and
    `test_pairs_without_bases_yield_nothing` pins the empty result so a dropped
    fetch is a red test rather than a quiet page that never comes.

    Requiring the full basis is also what the canary MEANS. It pages on residual
    noise between two runs measured the same way; a model swap between them
    explains the delta, so paging on it would be a false alarm about our own
    measurement stability.
    """
    from services import audit_delta as ad

    cur_primary = ad._primary_report(current_report)
    pri_primary = ad._primary_report(prior_report)
    basis = ad.measurement_basis_between(
        current_report, prior_report, current_basis, prior_basis
    )
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


def _group_by_merchant(
    rows: List[Dict[str, Any]], allow: set
) -> Dict[str, List[Dict[str, Any]]]:
    """Window-scoped rows grouped by merchant, order preserved (newest-first).

    Shared by :func:`_candidate_run_ids` and :func:`_stability_pairs` so the two
    can never disagree about WHICH runs form a pair — a basis fetched for a run
    the pairing does not use, or missing for one it does, would silently mute
    the canary.
    """
    by_merchant: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        mid = r.get("merchant_id")
        if not mid or (allow and mid not in allow):
            continue
        by_merchant.setdefault(mid, []).append(r)
    return by_merchant


def _candidate_run_ids(rows: List[Dict[str, Any]], allow: set) -> List[str]:
    """The run ids that can form a pair — the two most recent per merchant.

    Bounded on purpose: the basis fetch is one query per id, and only these can
    ever reach `stability_delta`.
    """
    out: List[str] = []
    for runs in _group_by_merchant(rows, allow).values():
        if len(runs) < 2:
            continue
        for r in runs[:2]:
            rid = r.get("run_id")
            if rid and rid not in out:
                out.append(str(rid))
    return out


async def _bases_by_run_id(run_ids: List[str]) -> Dict[str, Any]:
    """`{run_id: recorded basis or None}`. Best-effort per id: a lookup failure
    mutes that ONE pair (its basis is unknown, so it is not comparable), never
    the scan."""
    from db.audit_basis import get_basis_for_run

    out: Dict[str, Any] = {}
    for rid in run_ids:
        try:
            out[rid] = await get_basis_for_run(rid)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("stability canary basis read failed for %s: %s", rid, str(exc)[:200])
            out[rid] = None
    return out


def _stability_pairs(
    rows: List[Dict[str, Any]], allow: set, bases: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Pure: from window-scoped completed runs (grouped by merchant, newest-first),
    form the comparable pair per merchant and evaluate stability. Only the two most
    recent runs per merchant are considered; the pair must share a basis AND fall
    within STABILITY_WINDOW_HOURS. Returns one result row per merchant that yielded
    a genuine comparable pair (skips insufficient / not-comparable / too-far-apart).

    ``bases`` is `{run_id: recorded basis}` from :func:`_bases_by_run_id` — the
    two runs' comparability evidence. Still pure: the caller does the reading.
    """
    bases = bases or {}
    by_merchant = _group_by_merchant(rows, allow)

    out: List[Dict[str, Any]] = []
    for mid, runs in by_merchant.items():
        if len(runs) < 2:
            continue
        a, b = runs[0], runs[1]  # two most recent (rows arrive newest-first)
        gap_h = _hours_between(a.get("requested_at"), b.get("requested_at"))
        if gap_h is not None and gap_h > STABILITY_WINDOW_HOURS:
            continue  # comparable basis but time-separated → real trend, not a canary pair
        result = stability_delta(
            a.get("report_jsonb") or {},
            b.get("report_jsonb") or {},
            current_basis=bases.get(str(a.get("run_id") or "")),
            prior_basis=bases.get(str(b.get("run_id") or "")),
        )
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

    allow = set(_configured_merchants())
    # The pair's comparability evidence. Without it every pair reads as
    # not-comparable and the canary never fires — see stability_delta.
    bases = await _bases_by_run_id(_candidate_run_ids(rows, allow))
    results = _stability_pairs(rows, allow, bases)
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
