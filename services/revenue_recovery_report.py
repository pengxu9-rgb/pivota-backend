"""Versioned recovery projection for retained reports; no network or DB writes."""
from copy import deepcopy
from services.audit_evidence_builder import extract_evidence_items, extract_findings
from services.audit_projection_builder import build_revenue_recovery_projection


def recovery_from_report(report, *, run_id, catalog_available=None):
    report = deepcopy(report) if isinstance(report, dict) else {}
    if catalog_available is not None:
        report["catalog_dimensions_available"] = catalog_available
    return build_revenue_recovery_projection(
        evidence=extract_evidence_items(report), findings=extract_findings(report),
        actions=[], audit_run_row={"run_id": run_id},
    )
