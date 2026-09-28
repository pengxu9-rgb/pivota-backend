"""Relabel EXISTING retailer rows from a retired brand spelling to the family's canonical one.

WHY. A retailer row is keyed by its URL (`ext:retailer:<sha(url)>`) and the catalog upsert never updates a
stored brand (services/catalog_enrichment_agent/apply.py: `brand` is INSERT-only), so a spelling family added
to curated_brand_feed.RETAILER_BRAND_SPELLINGS fixes every FUTURE crawl but none of the rows already written.
Those keep the old brand -- and a content_key built from it, so the same product at two retailers never
groups as one product with two offers. Measured 2026-09-28: ETUDE's retailer rows split 293 "ETUDE HOUSE" /
174 "ETUDE" / 10 "Etude" across 20 stores.

WHAT A RELABEL MOVES, per row, all drift-guarded on the value read at plan time:
  * catalog_products.brand                  -> the canonical spelling
  * catalog_products.content_key            -> the target key (below)
  * product_group_members.product_group_id  -> the target key's group
  * external_product_seeds.seed_data.brand  -> the canonical spelling (the mirror a re-crawl rewrites anyway)
then rebuilds both content_keys' served views and serving eligibility and the row's trust (after the commit).

THE TARGET KEY, in order:
  1. the row's CURRENT content_key already holds a live row under the canonical spelling (intake identity
     attached it there earlier): keep it -- a brand-only change;
  2. a live canonical row shares this row's GTIN on exactly one content_key: join it;
  3. make_content_key(canonical, title) -- the key intake mints (intake_identity, GTIN-less) -- joining the
     canonical rows already on it, if any.
A row is HELD, never moved, when:
  * its GTIN matches canonical rows on several content_keys (which product is it?),
  * the target content_key's live rows sit in more than one product group,
  * or its CURRENT content_key has other live rows that this relabel does not move to the SAME target (moving
    one member would split a product; a row of an unrelated brand on it is left alone with it).
Rows whose spelling only differs in case ("Etude" vs "ETUDE": normalize_brand lowercases) keep their key.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.catalog_identity import make_content_key, normalize_brand
from services.product_group_autogrouper import derive_product_group_id

RETAILER_PREFIX = "ext:retailer:"

# Live retailer rows under any spelling of the family (matched case-insensitively on the alnum key in code).
CANDIDATES_SQL = """
SELECT cp.product_key, cp.brand, cp.title, cp.gtin, cp.content_key, cp.merchant_id, cp.platform,
       cp.source_product_id, cp.source_domain, pgm.product_group_id
FROM catalog_products cp
LEFT JOIN product_group_members pgm ON pgm.merchant_id = cp.merchant_id AND pgm.platform = cp.platform
     AND pgm.platform_product_id = cp.source_product_id
WHERE cp.suppression_reason IS NULL AND cp.product_key LIKE 'ext:retailer:%'
  AND lower(regexp_replace(cp.brand, '[^[:alnum:]]', '', 'g')) = ANY(:keys)
"""

# Every live row on the content_keys involved (current and target), with its group: the neighbours a move
# must not split, and the canonical rows it may join.
NEIGHBOURS_SQL = """
SELECT cp.product_key, cp.brand, cp.gtin, cp.content_key, pgm.product_group_id
FROM catalog_products cp
LEFT JOIN product_group_members pgm ON pgm.merchant_id = cp.merchant_id AND pgm.platform = cp.platform
     AND pgm.platform_product_id = cp.source_product_id
WHERE cp.suppression_reason IS NULL AND (cp.content_key = ANY(:cks) OR cp.gtin = ANY(:gtins))
"""

MOVE_ROW_SQL = """
UPDATE catalog_products SET brand = :to_brand, content_key = :to_ck, updated_at = NOW()
WHERE product_key = :pk AND brand = :from_brand AND content_key IS NOT DISTINCT FROM :from_ck
  AND suppression_reason IS NULL
RETURNING product_key
"""

MOVE_GROUP_SQL = """
UPDATE product_group_members SET product_group_id = :to_pg, updated_at = NOW()
WHERE merchant_id = :merchant_id AND platform = :platform AND platform_product_id = :spid
  AND product_group_id = :from_pg
RETURNING product_group_id
"""

INSERT_GROUP_SQL = """
INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id, is_primary,
                                   updated_at)
VALUES (:to_pg, :merchant_id, :platform, :spid, TRUE, NOW())
ON CONFLICT (merchant_id, platform, platform_product_id) DO NOTHING
RETURNING product_group_id
"""

DELETE_GROUP_SQL = """
DELETE FROM product_group_members
WHERE merchant_id = :merchant_id AND platform = :platform AND platform_product_id = :spid
  AND product_group_id = :pg
RETURNING product_group_id
"""

SEED_BRAND_SQL = """
UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{brand}', to_jsonb(CAST(:to_brand AS text))),
       updated_at = NOW()
WHERE attached_product_key = :pk AND seed_data ->> 'brand' = :from_brand
RETURNING id
"""


def brand_alnum(value: Any) -> str:
    return "".join(c for c in str(value or "").casefold() if c.isalnum())


def plan_relabel(rows: Sequence[Mapping[str, Any]], neighbours: Sequence[Mapping[str, Any]],
                 canonical: str) -> Dict[str, Any]:
    """Pure: one move (or hold) per candidate row. `rows` are the family's live retailer rows NOT already
    spelt `canonical`; `neighbours` every live row on their current and target content_keys and GTINs."""
    canon_norm = normalize_brand(canonical)
    moving = {r["product_key"] for r in rows}

    def is_canonical(n: Mapping[str, Any]) -> bool:
        # already on the canonical key (case aside), and not a row this run is itself relabelling
        return normalize_brand(n.get("brand")) == canon_norm and n["product_key"] not in moving

    by_ck: Dict[str, List[Mapping[str, Any]]] = {}
    by_gtin: Dict[str, List[Mapping[str, Any]]] = {}
    for n in neighbours:
        if n.get("content_key"):
            by_ck.setdefault(n["content_key"], []).append(n)
        if n.get("gtin"):
            by_gtin.setdefault(str(n["gtin"]), []).append(n)

    def group_of(ck: str) -> Tuple[Optional[str], bool]:
        # EVERY live row on the target, moving ones included: a case-only "Etude" row that is itself being
        # relabelled already sits on the canonical key in its own group, and joining any other group splits it.
        groups = {n.get("product_group_id") for n in by_ck.get(ck, [])}
        groups.discard(None)
        if len(groups) > 1:
            return None, False
        return (next(iter(groups)) if groups else derive_product_group_id(ck)), True

    decided: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        base = {"product_key": r["product_key"], "from_brand": r["brand"], "to_brand": canonical,
                "from_ck": r.get("content_key"), "from_pg": r.get("product_group_id"),
                "merchant_id": r["merchant_id"], "platform": r["platform"], "spid": r["source_product_id"],
                "source_domain": r.get("source_domain"), "title": r.get("title")}
        cur = r.get("content_key")
        if cur and any(is_canonical(n) for n in by_ck.get(cur, [])):
            decided[r["product_key"]] = {**base, "to_ck": cur, "to_pg": r.get("product_group_id"),
                                         "reason": "brand_only_already_grouped"}
            continue
        if cur and normalize_brand(r["brand"]) == canon_norm:
            decided[r["product_key"]] = {**base, "to_ck": cur, "to_pg": r.get("product_group_id"),
                                         "reason": "case_only"}
            continue
        gtin = str(r.get("gtin") or "")
        gtin_cks = {n["content_key"] for n in by_gtin.get(gtin, []) if is_canonical(n) and n.get("content_key")} \
            if gtin else set()
        if len(gtin_cks) > 1:
            decided[r["product_key"]] = {**base, "hold": "gtin_matches_several_products"}
            continue
        if gtin_cks:
            to_ck, reason = next(iter(gtin_cks)), "joined_by_gtin"
        else:
            to_ck = make_content_key(canonical, r.get("title"))
            if not to_ck:
                decided[r["product_key"]] = {**base, "hold": "no_title"}
                continue
            reason = "joined_by_title" if by_ck.get(to_ck) else "minted"
        to_pg, ok = group_of(to_ck)
        if not ok:
            decided[r["product_key"]] = {**base, "hold": "target_in_several_groups"}
            continue
        decided[r["product_key"]] = {**base, "to_ck": to_ck, "to_pg": to_pg, "reason": reason}

    # Never split a product: every live row on a vacated content_key must move with it, to the same target.
    for pk, d in list(decided.items()):
        if "hold" in d or d["to_ck"] == d["from_ck"] or not d["from_ck"]:
            continue
        for n in by_ck.get(d["from_ck"], []):
            other = decided.get(n["product_key"])
            if n["product_key"] == pk:
                continue
            if other is None or "hold" in other or other.get("to_ck") != d["to_ck"]:
                decided[pk] = {**{k: v for k, v in d.items() if k not in ("to_ck", "to_pg", "reason")},
                               "hold": "would_split_its_product"}
                break
    moves = [d for d in decided.values() if "hold" not in d]
    holds = [d for d in decided.values() if "hold" in d]
    counts: Dict[str, int] = {}
    for d in decided.values():
        k = d.get("hold") or d["reason"]
        counts[k] = counts.get(k, 0) + 1
    return {"canonical": canonical, "moves": moves, "holds": holds, "counts": counts}


async def load(db: Any, spellings: Sequence[str], canonical: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The candidates (the family's live retailer rows not already spelt `canonical`) and every live row on
    their current content_keys, their title-minted target keys, their GTINs -- and, second pass, on the
    content_keys those GTINs point at, so a GTIN-joined target's whole group is known."""
    keys = sorted({brand_alnum(s) for s in spellings} | {brand_alnum(canonical)})
    rows = [dict(r) for r in await db.fetch_all(CANDIDATES_SQL, {"keys": keys})]
    rows = [r for r in rows if r["brand"] != canonical]
    cks = {r["content_key"] for r in rows if r.get("content_key")}
    cks |= {k for k in (make_content_key(canonical, r.get("title")) for r in rows) if k}
    gtins = sorted({str(r["gtin"]) for r in rows if r.get("gtin")})
    neighbours = [dict(n) for n in await db.fetch_all(NEIGHBOURS_SQL, {"cks": sorted(cks), "gtins": gtins})]
    more = {n["content_key"] for n in neighbours if n.get("content_key")} - cks
    if more:
        seen = {n["product_key"] for n in neighbours}
        neighbours += [dict(n) for n in await db.fetch_all(NEIGHBOURS_SQL, {"cks": sorted(more), "gtins": []})
                       if n["product_key"] not in seen]
    return rows, neighbours


def manifest_for(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """The reversal record, built BEFORE the write: every move's from/to values."""
    return {"run_id": f"relabel_{uuid.uuid4().hex[:12]}", "canonical": plan["canonical"],
            "at": datetime.now(timezone.utc).isoformat(),
            "moves": [{k: d[k] for k in ("product_key", "from_brand", "to_brand", "from_ck", "to_ck", "from_pg",
                                         "to_pg", "merchant_id", "platform", "spid")} for d in plan["moves"]]}


async def write_moves(db: Any, moves: Iterable[Mapping[str, Any]], *, reverse: bool = False) -> Dict[str, int]:
    """One transaction. Forward: from -> to. Reverse: to -> from, for a manifest. Raises (rolled back) on any
    drift: a row whose brand, content_key or group is no longer what the plan read."""
    counts = {"products": 0, "groups": 0, "seeds": 0}
    async with db.transaction():
        for m in moves:
            f, t = ("to", "from") if reverse else ("from", "to")
            row = await db.fetch_one(MOVE_ROW_SQL, {"pk": m["product_key"], "from_brand": m[f"{f}_brand"],
                                                    "to_brand": m[f"{t}_brand"], "from_ck": m[f"{f}_ck"],
                                                    "to_ck": m[f"{t}_ck"]})
            if not row:
                raise RuntimeError(f"drift on {m['product_key']}: brand/content_key changed since the plan")
            counts["products"] += 1
            if m[f"{f}_pg"] != m[f"{t}_pg"]:
                key = {"merchant_id": m["merchant_id"], "platform": m["platform"], "spid": m["spid"]}
                if m[f"{f}_pg"] and m[f"{t}_pg"]:
                    moved = await db.fetch_one(MOVE_GROUP_SQL, {**key, "from_pg": m[f"{f}_pg"], "to_pg": m[f"{t}_pg"]})
                elif m[f"{t}_pg"]:  # the row had no membership: give it the target's
                    moved = await db.fetch_one(INSERT_GROUP_SQL, {**key, "to_pg": m[f"{t}_pg"]})
                else:               # a revert to "no membership"
                    moved = await db.fetch_one(DELETE_GROUP_SQL, {**key, "pg": m[f"{f}_pg"]})
                if not moved:
                    raise RuntimeError(f"drift on {m['product_key']}: group membership changed since the plan")
                counts["groups"] += 1
            seeds = await db.fetch_all(SEED_BRAND_SQL, {"pk": m["product_key"], "from_brand": m[f"{f}_brand"],
                                                        "to_brand": m[f"{t}_brand"]})
            counts["seeds"] += len(seeds)
    return counts


async def refresh_after(db: Any, moves: Iterable[Mapping[str, Any]], *, source: str,
                        reverse: bool = False) -> Dict[str, int]:
    """After the commit: rebuild both content_keys' views (the side that LOST the row first -- agent_pdp_view
    indexes a signature uniquely) and eligibility, then each moved row's trust. Never undoes the write."""
    from services.agent_pdp_view_assembler import (delete_agent_pdp_view_if_orphaned,
                                                    refresh_agent_pdp_view_for_content_key)
    from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many
    from services.index_pipeline_state_service import recompute_serving_eligibility

    moves = list(moves)
    lost = [m["to_ck"] if reverse else m["from_ck"] for m in moves]
    gained = [m["from_ck"] if reverse else m["to_ck"] for m in moves]
    order = list(dict.fromkeys(k for k in [*lost, *gained] if k))  # every losing side before any gaining one
    out = {"refreshed": 0, "reaped": 0, "recomputed": 0, "trust": 0, "errors": 0}
    for ck in order:
        try:
            out["refreshed"] += int(bool(await refresh_agent_pdp_view_for_content_key(ck, refresh_source=source, db=db)))
            out["reaped"] += int(bool(await delete_agent_pdp_view_if_orphaned(ck, db=db)))
            await recompute_serving_eligibility(ck, reason=source, db=db)
            out["recomputed"] += 1
        except Exception:  # noqa: BLE001 -- counted; a cache rebuild never undoes a committed relabel
            out["errors"] += 1
    try:
        out["trust"] = int(await upsert_catalog_row_trust_many(db=db, product_keys=[m["product_key"] for m in moves]) or 0)
    except Exception:  # noqa: BLE001
        out["errors"] += 1
    return out


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
