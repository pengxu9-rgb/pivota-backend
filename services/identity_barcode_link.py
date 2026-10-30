"""Propose linking EXISTING listings that share a barcode across sellers (ADR-010 attach_membership).

Intake Tier-0a now matches variant barcodes (F2, 2026-10-10; services/intake_identity.py, block
comment at gtin_spellings), but only for a NEW listing: an existing one is never moved by a crawl.
Rows already in the catalog keep the second content_key they minted while Tier-0a read
catalog_products.gtin only -- census 2026-10-10 (reports/coverage_census_2026_10_10/
matching_diagnosis.md) found 88 minority-seller keys sharing a barcode with another seller's key
(beautyencounter "Shalimar by Guerlain for Women", barcode on a SKU only, vs perfumania "Shalimar
Perfume", product gtin 03346470113541; ~64 KISS-family distributor keys). This module proposes moving
each such listing onto the family that keeps the identity; the identity engine applies approved
proposals (drift-guarded, reversible: services.identity_resolution.revert_run). It never applies.

Rules, each the intake rule or identity_brand_link's:
  - a barcode is evidence only if services.intake_identity.barcode_reuse_reason accepts it (one
    product per seller: the OPI shades sharing 00619828139641 at universalnailsupplies are refused);
  - only rows on DIFFERENT sellers (intake_identity.seller_key) and different content_keys pair up;
  - content_keys joined by trusted barcodes form one component, which converges on ONE family:
    the family holding the brand's own row (a non-`ext:retailer:` product, as Tier-0e's "brand row")
    when exactly one family does; no anchor -> the family of the oldest row (Tier-0a's "oldest is the
    original identity"); two anchored families -> nothing is proposed (two brand products sharing a
    barcode is a review question, not a move);
  - only retailer listings (`ext:retailer:`) move, as in identity_brand_link;
  - a move leaves nothing behind (identity_brand_link.whole_moves).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from services.identity_brand_link import (
    FROM_FAMILIES_SQL,
    GROUP_SIZES_SQL,
    MEMBERSHIPS_SQL,
    MemberKey,
    member_key,
    whole_moves,
)
from services.identity_resolution import new_proposal
from services.intake_identity import (
    _is_retailer_listing_key,
    barcode_reuse_reason,
    canonical_gtin,
    seller_key,
)

STRATEGY = "variant_barcode_match"

# Every live row carrying a barcode that sits on 2+ content_keys, once per way it carries it (product
# gtin or a live SKU's barcode). code14 is the zero-padded digit string; Python re-validates it with
# canonical_gtin (check digit, never all-zero), the intake rule. Rows with content_key NULL stay in:
# they are evidence for the reuse guard, though they never move. A full pass over catalog_skus and
# catalog_products: run it as a one-off, one job at a time on the 2-vCPU primary.
BARCODE_ROWS_SQL = r"""
WITH hits AS (
    SELECT product_key, gtin AS raw, 'product_gtin' AS match_source,
           CAST(NULL AS TEXT) AS sku_key, CAST(NULL AS TEXT) AS sku_title
    FROM catalog_products
    WHERE gtin IS NOT NULL AND suppression_reason IS NULL
    UNION ALL
    SELECT product_key, barcode, 'sku_barcode', sku_key, title
    FROM catalog_skus
    WHERE barcode IS NOT NULL AND suppression_reason IS NULL
),
keyed AS (
    SELECT h.*, lpad(regexp_replace(h.raw, '[\s-]+', '', 'g'), 14, '0') AS code14
    FROM hits h
    WHERE regexp_replace(h.raw, '[\s-]+', '', 'g') ~ '^[0-9]{8,14}$'
),
live AS (
    SELECT k.product_key, k.match_source, k.sku_key, k.sku_title, k.code14,
           cp.merchant_id, cp.platform, cp.source_product_id, cp.source_domain,
           cp.brand, cp.title, cp.content_key, cp.gtin, cp.created_at
    FROM keyed k
    JOIN catalog_products cp ON cp.product_key = k.product_key
    WHERE cp.suppression_reason IS NULL
),
shared AS (
    SELECT code14 FROM live GROUP BY code14 HAVING count(DISTINCT content_key) >= 2
)
SELECT live.* FROM live JOIN shared USING (code14)
ORDER BY live.code14, live.created_at, live.product_key
"""


def _bump(counts: Dict[str, int], key: str, n: int = 1) -> None:
    counts[key] = counts.get(key, 0) + n


def _order_key(row: Mapping[str, Any]) -> Tuple[bool, str, str]:
    return (row.get("created_at") is None, str(row.get("created_at") or ""), str(row.get("product_key") or ""))


def trusted_barcodes(
    rows: Iterable[Mapping[str, Any]], counts: Optional[Dict[str, int]] = None,
) -> Dict[str, List[Mapping[str, Any]]]:
    """Pure: canonical barcode -> its rows, for the barcodes that can link two sellers -- valid,
    accepted by the reuse guard, on 2+ sellers and 2+ content_keys. Skips are tallied."""
    counts = counts if counts is not None else {}
    by_code: Dict[str, List[Mapping[str, Any]]] = {}
    for r in rows:
        code = canonical_gtin(str(r.get("code14") or r.get("matched_gtin") or "") or None)
        if code:
            by_code.setdefault(code, []).append(r)
        else:
            _bump(counts, "row_invalid_barcode")
    out: Dict[str, List[Mapping[str, Any]]] = {}
    for code, matches in by_code.items():
        reason = barcode_reuse_reason(matches)
        if reason:
            _bump(counts, f"barcode_untrusted:{reason}")
            continue
        sellers = {seller_key(m) for m in matches}
        families = {m.get("content_key") for m in matches if m.get("content_key")}
        if len(sellers) < 2 or len(families) < 2:
            _bump(counts, "barcode_single_seller_or_family")
            continue
        out[code] = matches
    return out


def cross_seller_pairs(trusted: Mapping[str, List[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    """Pure: every (row, row) on different sellers and different content_keys sharing a trusted
    barcode -- the census figure, before any direction or batch rule."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for code in sorted(trusted):
        products: Dict[str, Mapping[str, Any]] = {}
        for m in trusted[code]:
            products.setdefault(str(m.get("product_key")), m)
        rows = sorted(products.values(), key=_order_key)
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if seller_key(a) == seller_key(b) or not a.get("content_key") or not b.get("content_key"):
                    continue
                if a.get("content_key") == b.get("content_key"):
                    continue
                pair = tuple(sorted((str(a["product_key"]), str(b["product_key"]))))
                if pair in seen:
                    continue
                seen.add(pair)
                out.append({"barcode": code, "a": a, "b": b})
    return out


def _components(trusted: Mapping[str, List[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    """Content_keys joined by trusted barcodes (union-find), each with its rows and barcodes."""
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for matches in trusted.values():
        cks = sorted({m["content_key"] for m in matches if m.get("content_key")})
        for ck in cks[1:]:
            ra, rb = find(cks[0]), find(ck)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    comps: Dict[str, Dict[str, Any]] = {}
    for code, matches in trusted.items():
        for m in matches:
            ck = m.get("content_key")
            if not ck:
                continue
            comp = comps.setdefault(find(ck), {"rows": {}, "codes": {}})
            comp["rows"].setdefault(str(m["product_key"]), m)
            comp["codes"].setdefault(str(m["product_key"]), set()).add(code)
    return list(comps.values())


def build_proposals(
    rows: Iterable[Mapping[str, Any]],
    groups: Mapping[MemberKey, str],
    from_family_keys: Mapping[str, List[str]],
    group_sizes: Mapping[str, int],
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Dict[str, Any]]]:
    """Pure: (attach_membership proposals, counts, cross-seller pairs) from BARCODE_ROWS_SQL's rows.

    `groups` maps a member to its product group, `from_family_keys` each mover's CURRENT content_key
    to every live product_key on it, `group_sizes` each group to its member count (as
    identity_brand_link.build_proposals)."""
    counts: Dict[str, int] = {}
    trusted = trusted_barcodes(rows, counts)
    pairs = cross_seller_pairs(trusted)
    counts["cross_seller_pairs"] = len(pairs)
    candidates: List[Dict[str, Any]] = []
    for comp in _components(trusted):
        members: Dict[str, Mapping[str, Any]] = comp["rows"]
        by_ck: Dict[str, List[Mapping[str, Any]]] = {}
        for r in sorted(members.values(), key=_order_key):
            by_ck.setdefault(r["content_key"], []).append(r)
        if len(by_ck) < 2:
            continue
        anchored = sorted(ck for ck, rs in by_ck.items()
                          if any(not _is_retailer_listing_key(r.get("product_key")) for r in rs))
        if len(anchored) > 1:
            _bump(counts, "component_multiple_anchor_families")
            continue
        if anchored:
            target = anchored[0]
            keeper = next(r for r in by_ck[target] if not _is_retailer_listing_key(r.get("product_key")))
        else:
            keeper = min(members.values(), key=_order_key)
            target = keeper["content_key"]
        on_target: Dict[str, int] = {}
        for r in by_ck[target]:
            on_target[seller_key(r)] = on_target.get(seller_key(r), 0) + 1
        for ck, rs in by_ck.items():
            if ck == target:
                continue
            # Every row here is a retailer listing: a family holding a brand row is an anchor, and a
            # component with two anchors was skipped above.
            for r in rs:
                from_group = groups.get(member_key(r))
                to_group = groups.get(member_key(keeper))
                if not from_group or not to_group:
                    _bump(counts, "no_group_membership")
                    continue
                if from_group == to_group:
                    _bump(counts, "already_in_group")
                    continue
                candidates.append({
                    "listing": r, "keeper": keeper, "from_group": from_group, "to_group": to_group,
                    "to_content_key": target, "barcodes": sorted(comp["codes"].get(str(r["product_key"]), ())),
                    "on_target": on_target,
                    "anchored": bool(anchored),
                })
    # How many of ONE seller's products sit on the target family once every candidate moves: 2+ is a
    # seller's per-shade/per-size rows landing on another seller's family row -- the open product
    # question of report §5 (a family PDP carrying single-shade offers). Surfaced, not refused: the
    # proposals are reviewed before anything applies.
    per_seller: Dict[Tuple[str, str], int] = {}
    for c in candidates:
        key = (c["to_content_key"], seller_key(c["listing"]))
        per_seller[key] = per_seller.get(key, 0) + 1
    for c in candidates:
        key = (c["to_content_key"], seller_key(c["listing"]))
        c["same_seller_products_on_family"] = per_seller[key] + c["on_target"].get(key[1], 0)
    proposals: List[Dict[str, Any]] = []
    for c in whole_moves(candidates, from_family_keys, group_sizes, counts):
        row, keeper = c["listing"], c["keeper"]
        if c["same_seller_products_on_family"] > 1:
            _bump(counts, "proposed_with_same_seller_products_on_family")
        proposals.append(new_proposal(
            kind="attach_membership", strategy=STRATEGY,
            subject_product_keys=[row["product_key"], keeper["product_key"]],
            keeper_product_key=keeper["product_key"], merchant_id=row.get("merchant_id"),
            content_key=c["to_content_key"], confidence=0.95,
            evidence={"listing_product_key": row["product_key"], "listing_title": row.get("title"),
                      "keeper_title": keeper.get("title"), "brand": row.get("brand"),
                      "listing_merchant_id": row.get("merchant_id"),
                      "keeper_merchant_id": keeper.get("merchant_id"),
                      "barcodes": c["barcodes"], "anchored_on_brand_row": c["anchored"],
                      "same_seller_products_on_family": c["same_seller_products_on_family"],
                      "from_content_key": row.get("content_key"),
                      "from_product_group_id": c["from_group"], "to_product_group_id": c["to_group"],
                      "rule": "shared GS1 barcode (product gtin or SKU barcode), reuse-guarded"},
        ))
        _bump(counts, "proposed")
    return proposals, counts, pairs


async def load_and_build(conn) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Dict[str, Any]]]:
    """Read the shared-barcode rows and the movers' memberships (asyncpg), then build_proposals."""
    rows = [dict(r) for r in await conn.fetch(BARCODE_ROWS_SQL)]
    spids = sorted({str(r.get("source_product_id") or "") for r in rows} - {""})
    groups: Dict[MemberKey, str] = {}
    if spids:
        for r in await conn.fetch(MEMBERSHIPS_SQL, spids):
            groups[member_key(dict(r))] = r["product_group_id"]
    cks = sorted({str(r.get("content_key")) for r in rows if r.get("content_key")})
    from_family_keys: Dict[str, List[str]] = {}
    if cks:
        for r in await conn.fetch(FROM_FAMILIES_SQL, cks):
            from_family_keys.setdefault(r["content_key"], []).append(r["product_key"])
    group_ids = sorted(set(groups.values()))
    group_sizes = {r["product_group_id"]: int(r["n"]) for r in await conn.fetch(GROUP_SIZES_SQL, group_ids)}
    return build_proposals(rows, groups, from_family_keys, group_sizes)
