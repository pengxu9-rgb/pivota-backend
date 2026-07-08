"""Tier-1 retailer-evidence recycling for prompt generation (2026-07-03).

Long-tail brand PDPs often fetch THIN (Anuko's imweb site yields title+brand
only), while the product's real story lives on retailer listings — Olive Young
descriptions, Coupang titles carrying attributes the brand page never states
("아누코 본드 앤 리페어 헤어 오일 극손상 에센스 아르간오일 75ml"). Every audit
already CAPTURES that text as grounded evidence; this module recycles the
PRIOR run's retailer excerpts into the next run's attribute graph and
LLM prompt-extraction content — provenance-tagged, never as claim
substantiation, and never from gray-market/secondhand listings.

Guardrails (founder-set, 2026-07-03):
  - vocabulary/analysis input ONLY — the merchant-facing verdict still reports
    the thin own-page honestly; retailer copy never enters the evidence/trust
    (claim substantiation) layer;
  - first-party hosts excluded (they're the merchant's own distribution, not a
    third-party description — and own-page text already flows in directly);
  - gray-market / secondhand / proxy-shopping hosts excluded (wrong variants,
    stale claims, counterfeits must not feed the graph).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

# Secondhand marketplaces, gray-market resellers, and proxy-shopping services
# seen in real runs (DamDam: ebay/mercari/gosupps/bigapplebuddy; Anuko:
# bunjang/dokodemo/qoo10 cross-border). Token match on the resolved host.
_GRAY_MARKET_HOST_TOKENS = frozenset({
    "bunjang", "mercari", "ebay", "gosupps", "dokodemo", "qoo10",
    "joongna", "karrot", "buyee", "zenmarket", "bigapplebuddy",
    "fromjapan", "jauce", "depop", "poshmark", "vinted", "grailed",
})

_MAX_EXCERPTS = 6
_MAX_TOTAL_CHARS = 1500
_MIN_EXCERPT_CHARS = 80


def _is_gray_market_host(host: str) -> bool:
    tokens = set((host or "").lower().replace("-", ".").split("."))
    return bool(tokens & _GRAY_MARKET_HOST_TOKENS)


def _entry_hosts(entry: Mapping[str, Any]) -> List[str]:
    """Resolved third-party hosts for one verbatim_grounding_evidence entry.

    Grounding sources carry Vertex redirector URIs with the real publisher in
    `title` — reuse the report service's resolver so we never key off the
    opaque redirector host. Lazy import: this module is imported BY the report
    service at call time.
    """
    from services.agent_center_bd_report_service import _grounding_source_host

    hosts: List[str] = []
    for source in entry.get("grounding_sources") or []:
        if not isinstance(source, Mapping):
            continue
        host = _grounding_source_host(source)
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def harvest_retailer_excerpts(
    report_jsonb: Mapping[str, Any],
    *,
    sku_key: str,
    merchant_domain: Optional[str],
    merchant_brand: Optional[str],
) -> Dict[str, Any]:
    """Pull third-party retailer/publisher excerpts for `sku_key` out of one
    prior run's report. Returns {"excerpts": [str], "hosts": [str]} — empty
    when nothing usable. Pure + synchronous so it's trivially testable."""
    from services.agent_center_bd_report_service import (
        _host_is_first_party,
        normalize_host,
    )
    from services.brand_alias import derive_brand_aliases

    report = report_jsonb if isinstance(report_jsonb, Mapping) else {}
    brand_report = report.get("brand_report")
    if isinstance(brand_report, Mapping):
        report = brand_report
    per_sku = report.get("per_sku_reports")
    if not isinstance(per_sku, list):
        return {"excerpts": [], "hosts": []}

    merchant_hosts = frozenset(
        h for h in {normalize_host(merchant_domain or "")} if h
    )
    aliases = derive_brand_aliases(merchant_brand, merchant_domain, ())

    excerpts: List[str] = []
    hosts_used: List[str] = []
    total = 0
    for sku_report in per_sku:
        if not isinstance(sku_report, Mapping):
            continue
        if str(sku_report.get("sku_key") or "") != str(sku_key):
            continue
        for entry in sku_report.get("verbatim_grounding_evidence") or []:
            if not isinstance(entry, Mapping):
                continue
            excerpt = str(entry.get("evidence_excerpt") or "").strip()
            if len(excerpt) < _MIN_EXCERPT_CHARS:
                continue
            entry_hosts = _entry_hosts(entry)
            third_party = [
                h for h in entry_hosts
                if not _host_is_first_party(h, merchant_hosts, aliases)
                and not _is_gray_market_host(h)
            ]
            if not third_party:
                continue  # own-listing-only or gray-market-only evidence
            if excerpt in excerpts:
                continue
            if total + len(excerpt) > _MAX_TOTAL_CHARS:
                break
            excerpts.append(excerpt)
            total += len(excerpt)
            for h in third_party:
                if h not in hosts_used:
                    hosts_used.append(h)
            if len(excerpts) >= _MAX_EXCERPTS:
                break
    return {"excerpts": excerpts, "hosts": hosts_used}


async def load_prior_retailer_evidence(
    *,
    merchant_id: str,
    sku_key: str,
    max_runs: int = 3,
) -> Dict[str, Any]:
    """Best-effort: scan the merchant's most recent completed runs (newest
    first) and return the first run's harvested retailer excerpts for this
    sku_key. Never raises — prompt generation must not depend on this."""
    empty: Dict[str, Any] = {"excerpts": [], "hosts": []}
    if not merchant_id or not sku_key:
        return empty
    try:
        from db.merchant_audit_runs import (
            COMPLETED_RUN_STATUSES,
            fetch_audit_run_by_id,
            recent_runs_for_merchant,
        )
        from services.prompt_basis import subject_type_for_sku_key

        runs = await recent_runs_for_merchant(
            merchant_id=merchant_id, limit=max(1, max_runs) + 2,
            # Same scoping as load_prior_prompt_basis: only the run kind that
            # can carry this SKU (wedge synthetic SKUs ↔ merchant_url runs).
            subject_type=subject_type_for_sku_key(sku_key),
        )
        for run in runs or []:
            # Completion is a STATUS question — recent_runs_for_merchant rows
            # carry no `stage` key, so the old `stage == 'completed'` filter
            # skipped every run and Tier-1 recycling silently never engaged.
            if str(run.get("status") or "") not in COMPLETED_RUN_STATUSES:
                continue
            row = await fetch_audit_run_by_id(run_id=str(run.get("run_id")))
            if not row:
                continue
            report = row.get("report_jsonb")
            if not isinstance(report, Mapping):
                continue
            harvested = harvest_retailer_excerpts(
                report,
                sku_key=sku_key,
                merchant_domain=(
                    report.get("merchant_domain")
                    or (report.get("brand_report") or {}).get("merchant_domain")
                    if isinstance(report, Mapping) else None
                ),
                merchant_brand=(
                    report.get("merchant_name")
                    or (report.get("brand_report") or {}).get("merchant_name")
                    if isinstance(report, Mapping) else None
                ),
            )
            if harvested["excerpts"]:
                logger.info(
                    "retailer-evidence: recycled %d excerpts (%s) from prior "
                    "run %s for sku=%s",
                    len(harvested["excerpts"]),
                    ",".join(harvested["hosts"][:5]),
                    run.get("run_id"), sku_key,
                )
                return harvested
    except Exception:  # noqa: BLE001 — enrichment must never block probing
        logger.warning(
            "retailer-evidence load failed for merchant=%s sku=%s",
            merchant_id, sku_key, exc_info=True,
        )
    return empty
