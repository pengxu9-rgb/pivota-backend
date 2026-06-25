"""Merchant-facing citation observations read — the get-cited proof loop (B⑤).

`GET /merchant/citations` returns who cited THIS merchant's products (which
provider, for which query, mentioned vs recommended), read straight from the
`citation_observations` table instead of buried in a per-audit `report_jsonb`
blob. STRICTLY scoped to the authenticated merchant — a merchant only ever sees
their own audit observations.

Closes the loop: a merchant who invested in get-cited work (enrichment, claims,
canonical PDP) can now query the proof that frontier models are citing them.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from db.audit_evidence import fetch_citation_observations
from utils.auth import get_current_user

router = APIRouter(prefix="/merchant/citations", tags=["merchant-citations"])

# Columns surfaced to the OWNING merchant (their own audit data, so the
# competitive fields are fine here — unlike a public projection).
_OBSERVATION_FIELDS = (
    "observation_id",
    "audit_run_id",
    "content_key",
    "product_key",
    "provider",
    "query",
    "axis",
    "query_class",
    "cited_host",
    "host_type",
    "citation_role",
    "first_party",
    "is_competitor",
    "evidence_url",
)


def _iso(value: Any) -> Optional[str]:
    try:
        return value.isoformat()
    except Exception:
        return None


def _project(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {key: row.get(key) for key in _OBSERVATION_FIELDS}
    out["observed_at"] = _iso(row.get("observed_at"))
    return out


def _summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_provider: Counter = Counter()
    by_role: Counter = Counter()
    products = set()
    first_party = 0
    last_observed = None
    for row in rows:
        by_provider[(row.get("provider") or "unknown")] += 1
        by_role[(row.get("citation_role") or "unspecified")] += 1
        if row.get("content_key"):
            products.add(row["content_key"])
        if row.get("first_party") is True:
            first_party += 1
        observed = row.get("observed_at")
        if observed is not None and (last_observed is None or observed > last_observed):
            last_observed = observed
    return {
        "total": len(rows),
        "products_cited": len(products),
        "by_provider": dict(by_provider),
        "by_role": dict(by_role),
        "first_party": first_party,
        "last_observed_at": _iso(last_observed),
    }


@router.get("")
async def list_merchant_citations(
    content_key: Optional[str] = Query(None, description="filter to one product"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    from utils.auth import get_current_merchant

    merchant_id = await get_current_merchant(current_user=current_user)
    # fetch_citation_observations is itself merchant-scoped (WHERE merchant_id),
    # so the response can never include another merchant's observations.
    rows = await fetch_citation_observations(
        merchant_id, content_key=content_key, limit=limit
    )
    return {
        "merchant_id": merchant_id,
        "content_key": content_key,
        "count": len(rows),
        "summary": _summarize(rows),
        "observations": [_project(row) for row in rows],
    }
