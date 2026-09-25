"""Versioned recovery projection for retained reports; no network or DB writes."""
from copy import deepcopy
from services.audit_evidence_builder import (
    extract_actions, extract_evidence_items, extract_findings,
)
from services.audit_projection_builder import build_revenue_recovery_projection


def recovery_from_report(report, *, run_id, catalog_available=None):
    """Rebuild the recovery projection from a retained report.

    ACTIONS ARE PART OF IT. This passed `actions=[]`, which made every stage's
    `actions` list empty on every retained report — so the surface lost the
    "what to do" layer entirely, and `_strip_actions_for_free_tier` (which
    counts and empties `stages[].actions`) had nothing to strip and stamped a
    free-tier lock over zero counts. `extract_actions` is the same reader
    persist_canonical_evidence uses to write action_plan_items, so a rebuilt
    projection now carries what a persisted one carries.
    """
    report = deepcopy(report) if isinstance(report, dict) else {}
    if catalog_available is not None:
        report["catalog_dimensions_available"] = catalog_available
    return build_revenue_recovery_projection(
        evidence=extract_evidence_items(report), findings=extract_findings(report),
        actions=extract_actions(report), audit_run_row={"run_id": run_id},
    )
