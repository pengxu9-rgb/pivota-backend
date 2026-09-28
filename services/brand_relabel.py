"""Relabel EXISTING retailer rows from a retired brand spelling to one canonical spelling.

WHY. A retailer row is keyed by its URL (`ext:retailer:<sha(url)>`) and the catalog upsert never updates a
stored brand (services/catalog_enrichment_agent/apply.py: `brand` is INSERT-only), so the rows already written
keep the old spelling -- and a content_key built from it, so the same product at two retailers never groups as
one product with two offers. Measured 2026-09-28: ETUDE's retailer rows split 293 "ETUDE HOUSE" / 174 "ETUDE"
/ 10 "Etude" across 20 stores.

ORDER, and why this module changes no ingest behaviour (review of #2434): a spelling family in
curated_brand_feed.RETAILER_BRAND_SPELLINGS makes the drain write the canonical spelling, and intake identity
then ATTACHES a re-crawled row to make_content_key(canonical, title) before its own canonical_url self-match.
For a row still on the old spelling's key that means a new key but the old group, which apply.py's
_ensure_primary_retailer_group refuses -- failing that store's whole job on every re-crawl. So:
  1. relabel (this module), 2. resolve every HELD row, 3. only then add the family (a separate change).

WHAT A RELABEL MOVES, per row, all drift-guarded on the value read at plan time, in ONE transaction:
  * catalog_products.brand                  -> the canonical spelling
  * catalog_products.content_key            -> the target key (below)
  * product_group_members.product_group_id  -> the target key's group
  * external_product_seeds.seed_data.brand  -> the canonical spelling, on exactly the seed ids read at plan time
then, after the commit, rebuilds both keys' served views and serving eligibility, and the trust of every live
row on a touched key. Any drift aborts the whole batch; `revert` is the same all-or-nothing write, backwards.

THE TARGET KEY, in order:
  1. the row's CURRENT key already holds a live canonical row (intake attached it there): keep the key, and take
     THAT key's group -- a row intake half-moved (new key, old group) is repaired here, not certified;
  2. a live canonical row shares this row's GTIN on exactly one key: join it;
  3. make_content_key(canonical, title) -- the key intake mints (GTIN-less) -- joining whatever lives there.
A row is HELD, never moved, when:
  * its GTIN matches canonical rows on several keys (which product is it?),
  * a title-join target carries GTINs and none is this row's (intake would flag brand_title_collision),
  * the target key's live rows sit in more than one product group,
  * or its CURRENT key has other live rows that this relabel does not move to the SAME target (moving one member
    would split a product; a row of an unrelated brand on it is left alone with it).
Rows whose spelling only differs in case ("Etude" vs "ETUDE": normalize_brand lowercases) keep their key.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.catalog_identity import make_content_key, normalize_brand
from services.product_group_autogrouper import derive_product_group_id

# Live retailer rows under any spelling of the family (matched on the lowercased alnum key: PG keeps É as alnum,
# as Python's casefold does, so an accented spelling must be listed to match).
CANDIDATES_SQL = """
SELECT cp.product_key, cp.brand, cp.title, cp.gtin, cp.content_key, cp.merchant_id, cp.platform,
       cp.source_product_id, cp.source_domain, pgm.product_group_id
FROM catalog_products cp
LEFT JOIN product_group_members pgm ON pgm.merchant_id = cp.merchant_id AND pgm.platform = cp.platform
     AND pgm.platform_product_id = cp.source_product_id
WHERE cp.suppression_reason IS NULL AND cp.product_key LIKE 'ext:retailer:%'
  AND lower(regexp_replace(cp.brand, '[^[:alnum:]]', '', 'g')) = ANY(:keys)
"""

# Every live row on the keys involved (current and target) or sharing a GTIN, with its group.
NEIGHBOURS_SQL = """
SELECT cp.product_key, cp.brand, cp.gtin, cp.content_key, pgm.product_group_id
FROM catalog_products cp
LEFT JOIN product_group_members pgm ON pgm.merchant_id = cp.merchant_id AND pgm.platform = cp.platform
     AND pgm.platform_product_id = cp.source_product_id
WHERE cp.suppression_reason IS NULL AND (cp.content_key = ANY(:cks) OR cp.gtin = ANY(:gtins))
"""

SEEDS_SQL = """
SELECT id, attached_product_key, seed_data ->> 'brand' AS brand FROM external_product_seeds
WHERE attached_product_key = ANY(:pks)
"""

LIVE_ON_KEYS_SQL = """
SELECT product_key FROM catalog_products WHERE content_key = ANY(:cks) AND suppression_reason IS NULL
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

# Exactly the seed ids the plan read under the old spelling -- a seed already canonical is never touched, forward
# or back (review of #2434: an id-less revert rewrote an untouched "ETUDE" seed to "ETUDE HOUSE").
SEED_BRAND_SQL = """
UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{brand}', to_jsonb(CAST(:to_brand AS text))),
       updated_at = NOW()
WHERE id = ANY(:ids) AND attached_product_key = :pk AND seed_data ->> 'brand' = :from_brand
RETURNING id
"""


# The manifest's durable home, written BEFORE the write (one statement, committed on its own). Not the job log: a
# 303-move manifest is ~140 KB and Cloud Logging cuts a line at 100 KB (measured 2026-09-28, run
# relabel_d64104859b24 -- the printed manifest was unparseable). identity_resolution_events.action is free text.
MANIFEST_ACTION = "brand_relabel_manifest"
STORE_MANIFEST_SQL = """
INSERT INTO identity_resolution_events (proposal_id, action, run_id, detail)
VALUES (NULL, :action, :run_id, CAST(:detail AS jsonb))
RETURNING id
"""
LOAD_MANIFEST_SQL = """
SELECT detail FROM identity_resolution_events WHERE action = :action AND run_id = :run_id
ORDER BY id DESC LIMIT 1
"""


async def store_manifest(db: Any, manifest: Mapping[str, Any]) -> None:
    row = await db.fetch_one(STORE_MANIFEST_SQL, {"action": MANIFEST_ACTION, "run_id": manifest["run_id"],
                                                  "detail": json.dumps(manifest, default=str)})
    if not row:
        raise RuntimeError(f"the manifest for {manifest['run_id']} was not stored: nothing written")


async def load_manifest(db: Any, run_id: str) -> Dict[str, Any]:
    row = await db.fetch_one(LOAD_MANIFEST_SQL, {"action": MANIFEST_ACTION, "run_id": run_id})
    detail = row and row["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    if not detail:
        raise SystemExit(f"no stored manifest for {run_id}")
    return detail


def brand_alnum(value: Any) -> str:
    return "".join(c for c in str(value or "").casefold() if c.isalnum())


def plan_relabel(rows: Sequence[Mapping[str, Any]], neighbours: Sequence[Mapping[str, Any]],
                 canonical: str, seeds: Optional[Mapping[str, Sequence[Any]]] = None) -> Dict[str, Any]:
    """Pure: one move (or hold) per candidate row. `rows` are the family's live retailer rows NOT already spelt
    `canonical`; `neighbours` every live row on their current and target keys and GTINs; `seeds` the seed ids
    per product_key that carry the row's current spelling."""
    seeds = seeds or {}
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
        # A TARGET key's group, for a row moving onto it: EVERY live row on the key, moving ones included (a
        # case-only "Etude" row being relabelled already sits on the canonical key in its own group).
        groups = {n.get("product_group_id") for n in by_ck.get(ck, [])}
        groups.discard(None)
        if len(groups) > 1:
            return None, False
        return (next(iter(groups)) if groups else derive_product_group_id(ck)), True

    def own_key_group(r: Mapping[str, Any]) -> Optional[str]:
        """The group a row that KEEPS its key belongs in, or None (hold). Read from the rows on the key that are
        NOT being relabelled -- a moving row's group is itself a candidate for repair, and reading it let two
        rows swap groups (re-review of #2434). With only moving rows on the key: the key's own group when any
        of them already has it, else the single group they share. A row alone on its key keeps its group."""
        cur = r["content_key"]
        on_key = by_ck.get(cur, [])
        fixed = {n.get("product_group_id") for n in on_key
                 if n["product_key"] not in moving and n["product_key"] != r["product_key"]} - {None}
        if fixed:
            return next(iter(fixed)) if len(fixed) == 1 else None
        if not [n for n in on_key if n["product_key"] != r["product_key"]]:
            return r.get("product_group_id") or derive_product_group_id(cur)
        groups = {n.get("product_group_id") for n in on_key} - {None}
        own = derive_product_group_id(cur)
        if own in groups or not groups:
            return own
        return next(iter(groups)) if len(groups) == 1 else None

    decided: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        base = {"product_key": r["product_key"], "from_brand": r["brand"], "to_brand": canonical,
                "from_ck": r.get("content_key"), "from_pg": r.get("product_group_id"),
                "merchant_id": r["merchant_id"], "platform": r["platform"], "spid": r["source_product_id"],
                "source_domain": r.get("source_domain"), "title": r.get("title"),
                "seed_ids": [str(s) for s in seeds.get(r["product_key"], [])]}
        cur = r.get("content_key")
        stays = cur and (any(is_canonical(n) for n in by_ck.get(cur, []))
                         or normalize_brand(r["brand"]) == canon_norm)
        if stays:
            # The key stays; the GROUP must be the key's own. A row intake half-moved (this key, an old group)
            # takes the key's group here -- never certified as it stands (review of #2434).
            to_pg = own_key_group(r)
            if to_pg is None:
                decided[r["product_key"]] = {**base, "hold": "own_key_in_several_groups"}
                continue
            reason = ("case_only" if normalize_brand(r["brand"]) == canon_norm else "brand_only_already_grouped")
            if to_pg != r.get("product_group_id"):
                reason += "_regrouped"
            decided[r["product_key"]] = {**base, "to_ck": cur, "to_pg": to_pg, "reason": reason}
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
            there = by_ck.get(to_ck, [])
            target_gtins = {str(n["gtin"]) for n in there if n.get("gtin")}
            if gtin and target_gtins and gtin not in target_gtins:
                # Same brand + title, different barcode: intake flags brand_title_collision for review.
                decided[r["product_key"]] = {**base, "hold": "gtin_conflicts_with_title_match"}
                continue
            reason = "joined_by_title" if there else "minted"
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


async def load(db: Any, spellings: Sequence[str], canonical: str
               ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, List[str]]]:
    """The candidates (the family's live retailer rows not already spelt `canonical`), every live row on their
    current keys, their title-minted target keys and their GTINs -- and, second pass, on the keys those GTINs
    point at, so a GTIN-joined target's whole group is known -- and the candidates' old-spelling seed ids."""
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
    brand_of = {r["product_key"]: r["brand"] for r in rows}
    seeds: Dict[str, List[str]] = {}
    if rows:
        for s in await db.fetch_all(SEEDS_SQL, {"pks": sorted(brand_of)}):
            if s["brand"] == brand_of.get(s["attached_product_key"]):
                seeds.setdefault(s["attached_product_key"], []).append(str(s["id"]))
    return rows, neighbours, seeds


MOVE_KEYS = ("product_key", "from_brand", "to_brand", "from_ck", "to_ck", "from_pg", "to_pg", "merchant_id",
             "platform", "spid", "seed_ids")


def manifest_for(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """The reversal record, built BEFORE the write: every move's from/to values and the seed ids it rewrites."""
    return {"run_id": f"relabel_{uuid.uuid4().hex[:12]}", "canonical": plan["canonical"],
            "at": datetime.now(timezone.utc).isoformat(),
            "moves": [{k: d[k] for k in MOVE_KEYS} for d in plan["moves"]]}


async def write_moves(db: Any, moves: Iterable[Mapping[str, Any]], *, reverse: bool = False) -> Dict[str, int]:
    """One transaction, all or nothing. Forward: from -> to. Reverse: to -> from, for a manifest. Raises (rolled
    back) on any drift: a row whose brand, content_key, group or recorded seeds are no longer what was read."""
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
            ids = list(m.get("seed_ids") or [])
            if ids:
                # external_product_seeds.id is TEXT ("seed:catalog_enrichment_agent_v1:<hex>", mig 044): bound as is
                done = await db.fetch_all(SEED_BRAND_SQL, {"ids": [str(i) for i in ids], "pk": m["product_key"],
                                                           "from_brand": m[f"{f}_brand"], "to_brand": m[f"{t}_brand"]})
                if len(done) != len(ids):
                    raise RuntimeError(f"drift on {m['product_key']}: a seed's brand changed since the plan")
                counts["seeds"] += len(done)
    return counts


def touched_keys(moves: Iterable[Mapping[str, Any]], *, reverse: bool = False) -> List[str]:
    """Both sides of every move, every LOSING side first: agent_pdp_view indexes a signature uniquely, so the
    view that still carries a moved row must be rebuilt (or reaped) before the view that gains it."""
    moves = list(moves)
    lost = [m["to_ck"] if reverse else m["from_ck"] for m in moves]
    gained = [m["from_ck"] if reverse else m["to_ck"] for m in moves]
    return list(dict.fromkeys(k for k in [*lost, *gained] if k))


async def refresh_after(db: Any, moves: Iterable[Mapping[str, Any]], *, source: str,
                        reverse: bool = False) -> Dict[str, Any]:
    """After the commit: every touched key's view (reaped when it is left empty) and eligibility, then the trust
    of EVERY live row on those keys -- a joined key's own rows included. Never undoes the write; failures are
    listed so `refresh --manifest` can re-run just this."""
    from services.agent_pdp_view_assembler import (delete_agent_pdp_view_if_orphaned,
                                                    refresh_agent_pdp_view_for_content_key)
    from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many
    from services.index_pipeline_state_service import recompute_serving_eligibility

    order = touched_keys(moves, reverse=reverse)
    out: Dict[str, Any] = {"refreshed": 0, "reaped": 0, "recomputed": 0, "trust": 0, "failed_keys": []}
    for ck in order:
        try:
            out["refreshed"] += int(bool(await refresh_agent_pdp_view_for_content_key(ck, refresh_source=source, db=db)))
            out["reaped"] += int(bool(await delete_agent_pdp_view_if_orphaned(ck, db=db)))
            await recompute_serving_eligibility(ck, reason=source, db=db)
            out["recomputed"] += 1
        except Exception as exc:  # noqa: BLE001 -- listed; a cache rebuild never undoes a committed relabel
            out["failed_keys"].append({"content_key": ck, "error": f"{type(exc).__name__}: {exc}"[:200]})
    try:
        pks = [r["product_key"] for r in await db.fetch_all(LIVE_ON_KEYS_SQL, {"cks": order})] if order else []
        out["trust"] = int(await upsert_catalog_row_trust_many(db=db, product_keys=pks) or 0) if pks else 0
    except Exception as exc:  # noqa: BLE001
        out["failed_keys"].append({"content_key": None, "error": f"trust: {type(exc).__name__}: {exc}"[:200]})
    return out


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
