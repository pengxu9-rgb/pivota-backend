"""Relink EXISTING retailer listings onto the brand's own product (ADR-010 attach_membership).

The crawl door attaches a NEW listing whose title only prefixes the brand's name ("Missha Artemisia
Calming Ampoule" -> misshaus.com's "Artemisia Calming Ampoule", intake Tier-0e). Listings crawled
before that minted their own product and keep it: group membership is never overwritten by a crawl.
Measured 2026-09-26: 51 live listings. This module proposes moving each one -- its content_key and
product-group membership -- onto the brand product; the identity engine applies approved proposals
(drift-guarded, reversible: services.identity_resolution.revert_run).

Same rule as Tier-0e: a leading whole-word brand stripped, sizes never stripped, the family must hold
a non-retailer row, no GTIN conflict.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from services.identity_resolution import new_proposal
from services.intake_identity import _brand_stripped_content_key, _is_retailer_listing_key

STRATEGY = "retailer_listing_brand_prefix"

LISTINGS_SQL = """
SELECT product_key, merchant_id, platform, source_product_id, brand, title, content_key, gtin
FROM catalog_products
WHERE product_key LIKE 'ext:retailer:%' AND suppression_reason IS NULL AND content_key IS NOT NULL
"""

FAMILIES_SQL = """
SELECT product_key, merchant_id, platform, source_product_id, content_key, gtin, created_at
FROM catalog_products
WHERE content_key = ANY($1::text[]) AND suppression_reason IS NULL
"""

FROM_FAMILIES_SQL = """
SELECT product_key, content_key FROM catalog_products
WHERE content_key = ANY($1::text[]) AND suppression_reason IS NULL
"""

GROUP_SIZES_SQL = """
SELECT product_group_id, count(*) AS n FROM product_group_members
WHERE product_group_id = ANY($1::text[]) GROUP BY product_group_id
"""

MEMBERSHIPS_SQL = """
SELECT merchant_id, platform, platform_product_id, product_group_id
FROM product_group_members
WHERE platform_product_id = ANY($1::text[])
"""

MemberKey = Tuple[str, str, str]


def member_key(row: Mapping[str, Any]) -> MemberKey:
    return (str(row.get("merchant_id") or ""), str(row.get("platform") or ""),
            str(row.get("source_product_id") or row.get("platform_product_id") or ""))


def build_proposals(
    listings: Iterable[Mapping[str, Any]],
    families: Mapping[str, List[Mapping[str, Any]]],
    groups: Mapping[MemberKey, str],
    from_family_keys: Optional[Mapping[str, List[str]]] = None,
    group_sizes: Optional[Mapping[str, int]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Pure: one attach_membership proposal per listing that is the brand product under Tier-0e's rule.

    `families` maps a brand-stripped content_key to its live rows; `groups` maps a member to its group.
    `from_family_keys` maps each listing's CURRENT content_key to every live product_key on it, and
    `group_sizes` each group to its member count: a listing moves only if nothing stays behind -- every
    live row on its content_key moves with it in this batch and its group holds only moving listings.
    Otherwise the next crawl resolves the listing back to the row left behind (exact content_key tier),
    snaps its content_key back and has its group refused (review of the attach_membership PR)."""
    listings = list(listings)
    from_family_keys = from_family_keys if from_family_keys is not None else {
        r.get("content_key"): [r["product_key"]] for r in listings}
    group_sizes = group_sizes if group_sizes is not None else {}
    candidates, counts = _candidates(listings, families, groups)
    moving = {c["listing"]["product_key"] for c in candidates}
    moving_by_group: Dict[str, int] = {}
    for c in candidates:
        moving_by_group[c["from_group"]] = moving_by_group.get(c["from_group"], 0) + 1
    proposals: List[Dict[str, Any]] = []
    for c in candidates:
        row, keeper = c["listing"], c["keeper"]
        left_behind = set(from_family_keys.get(row.get("content_key"), [row["product_key"]])) - moving
        if left_behind:
            counts["rows_left_on_old_content_key"] = counts.get("rows_left_on_old_content_key", 0) + 1
            continue
        if group_sizes.get(c["from_group"], 1) > moving_by_group[c["from_group"]]:
            counts["old_group_has_other_members"] = counts.get("old_group_has_other_members", 0) + 1
            continue
        proposals.append(new_proposal(
            kind="attach_membership", strategy=STRATEGY,
            subject_product_keys=[row["product_key"], keeper["product_key"]],
            keeper_product_key=keeper["product_key"], merchant_id=row.get("merchant_id"),
            content_key=c["to_content_key"], confidence=0.9,
            evidence={"listing_product_key": row["product_key"], "listing_title": row.get("title"),
                      "brand": row.get("brand"), "from_content_key": row.get("content_key"),
                      "from_product_group_id": c["from_group"], "to_product_group_id": c["to_group"],
                      "rule": "leading whole-word brand stripped; sizes kept"},
        ))
        counts["proposed"] = counts.get("proposed", 0) + 1
    return proposals, counts


def _candidates(
    listings: List[Mapping[str, Any]],
    families: Mapping[str, List[Mapping[str, Any]]],
    groups: Mapping[MemberKey, str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Per-listing rule (Tier-0e's): which brand product each listing would join, before batch rules."""
    out: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}

    def skip(reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    for row in listings:
        if not _is_retailer_listing_key(row.get("product_key")):
            skip("not_a_listing")
            continue
        stripped = _brand_stripped_content_key(row.get("brand"), row.get("title"))
        if not stripped:
            skip("no_brand_prefix")
            continue
        if stripped == row.get("content_key"):
            skip("already_on_family")
            continue
        family = list(families.get(stripped) or [])
        # Tier-0e's order (_rows_by_content_key): the listing's own merchant first, then oldest.
        brand_rows = sorted((r for r in family if not _is_retailer_listing_key(r.get("product_key"))),
                            key=lambda r: (r.get("merchant_id") != row.get("merchant_id"),
                                           str(r.get("created_at") or ""), str(r.get("product_key"))))
        if not brand_rows:
            skip("no_brand_product")
            continue
        known_gtins = {r.get("gtin") for r in family if r.get("gtin")}
        if row.get("gtin") and known_gtins and row.get("gtin") not in known_gtins:
            skip("gtin_conflict")
            continue
        keeper = brand_rows[0]
        from_group = groups.get(member_key(row))
        to_group = groups.get(member_key(keeper))
        if not from_group or not to_group:
            skip("no_group_membership")
            continue
        if from_group == to_group:
            skip("already_in_group")
            continue
        out.append({"listing": row, "keeper": keeper, "from_group": from_group, "to_group": to_group,
                    "to_content_key": keeper.get("content_key") or stripped})
    return out, counts


async def load_and_build(conn) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Read the live listings, their brand families and memberships (asyncpg), then build_proposals."""
    listings = [dict(r) for r in await conn.fetch(LISTINGS_SQL)]
    stripped = sorted({k for k in (_brand_stripped_content_key(r.get("brand"), r.get("title"))
                                    for r in listings) if k})
    families: Dict[str, List[Dict[str, Any]]] = {}
    if stripped:
        for r in await conn.fetch(FAMILIES_SQL, stripped):
            families.setdefault(r["content_key"], []).append(dict(r))
    spids = sorted(({str(r.get("source_product_id") or "") for r in listings}
                    | {str(r.get("source_product_id") or "") for fam in families.values() for r in fam}) - {""})
    groups: Dict[MemberKey, str] = {}
    if spids:
        for r in await conn.fetch(MEMBERSHIPS_SQL, spids):
            groups[member_key(dict(r))] = r["product_group_id"]
    from_cks = sorted({str(r.get("content_key")) for r in listings if r.get("content_key")})
    from_family_keys: Dict[str, List[str]] = {}
    for r in await conn.fetch(FROM_FAMILIES_SQL, from_cks):
        from_family_keys.setdefault(r["content_key"], []).append(r["product_key"])
    group_ids = sorted(set(groups.values()))
    group_sizes = {r["product_group_id"]: int(r["n"]) for r in await conn.fetch(GROUP_SIZES_SQL, group_ids)}
    return build_proposals(listings, families, groups, from_family_keys, group_sizes)


async def refresh_after_move(details: Iterable[Mapping[str, Any]], *, source: str,
                             db: Optional[Any] = None) -> Dict[str, int]:
    """Rebuild the served views a move (or its revert) touched, and both content_keys' serving
    eligibility."""
    from services.agent_pdp_view_assembler import (delete_agent_pdp_view_if_orphaned,
                                                    refresh_agent_pdp_view_for_content_key)
    from services.index_pipeline_state_service import recompute_serving_eligibility

    out = {"refreshed": 0, "reaped": 0, "recomputed": 0, "errors": 0}
    for d in details:
        if d.get("reverted") is False:  # a revert that left this listing alone touched neither view
            continue
        # Both sides, after an apply AND after a revert: each content_key gained or lost a member. A
        # content_key left with no catalog row builds nothing and has its view reaped.
        # The side that LOST the listing first: its view may still carry the listing's signature, which
        # agent_pdp_view indexes uniquely, so the gaining side's rebuild must come after it is reaped/rebuilt.
        lost_first = ((d.get("to_content_key"), d.get("from_content_key")) if d.get("reverted") else
                      (d.get("from_content_key"), d.get("to_content_key")))
        for ck in dict.fromkeys(k for k in lost_first if k):
            try:
                out["refreshed"] += int(bool(await refresh_agent_pdp_view_for_content_key(
                    ck, refresh_source=source, db=db)))
                out["reaped"] += int(bool(await delete_agent_pdp_view_if_orphaned(ck, db=db)))
                await recompute_serving_eligibility(ck, reason=source, db=db)
                out["recomputed"] += 1
            except Exception:  # noqa: BLE001 -- a cache rebuild must not undo a committed move; counted
                out["errors"] += 1
    return out
