"""Independent trust signals for the served PDP — the moat's non-merchant half.

SEPARATION invariant (trust-layer design): independent, non-merchant endorsements
(editorial/creator/forum citations gathered by the audit engine, in
citation_observations) are served as a DISTINCT block, never merged into the
merchant-asserted evidence_profile. An agent must be able to tell "the brand says
it contains X" (evidence_claims) apart from "an independent source cited this
product" (independent_signals).

Only CREDIBLE independent citations qualify: non-first-party, non-competitor, and
a citation_role that is a genuine third-party endorsement (editorial/creator/
forum) — NOT a competitor mention or the merchant's own marketplace listing.

This is the foundational pipe: coverage is thin today (gated on audit coverage),
but the architecture + gate are correct so it fills as more products are audited.
Pure filter (filter_independent) + a thin async reader (independent_signals_for).
"""

from __future__ import annotations

from typing import Any, Dict, List

# citation_role values that are genuine independent endorsements.
CREDIBLE_ROLES = frozenset({"editorial_review", "creator", "forum", "independent"})


def _is_credible(row: Dict[str, Any]) -> bool:
    if row.get("first_party") or row.get("is_competitor"):
        return False
    return str(row.get("citation_role") or "").strip().lower() in CREDIBLE_ROLES


def filter_independent(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only credible independent citations, one per cited_host (first wins),
    shaped for the served independent_signals block. Pure; no I/O."""
    out: List[Dict[str, Any]] = []
    seen_hosts: set = set()
    for r in rows or []:
        if not _is_credible(r):
            continue
        host = str(r.get("cited_host") or "").strip().lower()
        if not host or host in seen_hosts:
            continue
        seen_hosts.add(host)
        out.append(
            {
                "cited_host": r.get("cited_host"),
                "host_type": r.get("host_type"),
                "citation_role": r.get("citation_role"),
                "evidence_url": r.get("evidence_url"),
                "provider": r.get("provider"),
            }
        )
    return out


async def independent_signals_for(content_key: str, *, db: Any) -> List[Dict[str, Any]]:
    """Credible independent citations for a content_key, ready to serve. Returns []
    when none (the common case today). One indexed lookup on content_key."""
    if not content_key:
        return []
    rows = await db.fetch_all(
        """
        SELECT cited_host, host_type, citation_role, evidence_url, provider,
               first_party, is_competitor, observed_at
        FROM citation_observations
        WHERE content_key = :ck
        ORDER BY observed_at DESC
        """,
        {"ck": content_key},
    )
    return filter_independent([dict(r) for r in rows or []])
