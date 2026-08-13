"""Re-attribute orphaned product_enrichment rows to their products' CURRENT identities.

THE POPULATION. 248 product_enrichment rows are keyed to identities that no
longer exist anywhere (join diagnosis, prod 2026-08-13): all are
id_missing_within_merchant_platform — the merchant and platform exist, the
specific product id does not, in catalog_products OR products_cache. Three
groups: external_seed slugs left behind by the A9-4/ADR-009 id-space migration
(~125), and one merchant's shopify (~103) + wix (~20) products that were
deleted and recreated under new ids.

WHY IT BECAME URGENT. Those rows' curated copy WAS still serving, frozen, on
~113 agent_pdp_view rows the publish bridge could never refresh. The 2026-08-13
backlog rescore refreshed those rows through the safe path, the enrichment
fetch correctly found nothing (genuinely absent, not fetch-failed), and the
tri-state cleared them: bullet_points went 211 -> 43 on the served surface.
The content is NOT lost — it sits in these orphaned rows — but the served
copies are gone, and with them the content fingerprint that would have been
the easiest mapping key. Recovery must therefore come from identity-side
evidence, hypothesis by hypothesis, never by guessing.

THE HYPOTHESES, each measured separately in the dry run so the mapping's
evidence is visible before anything moves:

  H1 seed external id   eps.external_product_id = pe.platform_product_id and
                        the seed is attached to a live catalog row.
  H2 seed URL slug      the dead id appears as a path segment of an attached
                        seed's destination/canonical URL (old slugs were URL
                        handles).
  H3 title override     pe.title_override matches a live catalog title in the
                        same merchant+platform.
  H4 surviving APV      pe.bullet_points equals the bullets on one of the 43
                        surviving served rows, mapped back to a live catalog
                        row in the same merchant+platform.

APPLY RULES (the conservative core — a wrong re-attribution publishes one
product's curated claims onto another, which is worse than the gap):
  * BILATERAL UNIQUENESS: an orphan is applied only if it maps to exactly one
    live identity AND no other orphan maps to that same identity, across ALL
    hypotheses' accepted matches.
  * TARGET VACANCY: skipped if ANY enrichment row already exists at the target
    identity (any geo) — never clobber; ADR-001 precedence is not re-litigated
    here.
  * SAME MERCHANT+PLATFORM only: re-keying across either is out of scope.
  * Re-key is an UPDATE of platform_product_id (preserves authorship metadata
    and created_at), then the row is republished through the canonical refresh
    and serving eligibility is recomputed — the same sequence the (now-working)
    on-write bridge performs.

Dry-run (default) prints per-hypothesis coverage, the accepted mapping table,
and every rejection with its reason. --apply executes accepted matches only.

Usage
-----
  python3 scripts/reattribute_orphaned_enrichment.py
  python3 scripts/reattribute_orphaned_enrichment.py --apply
  python3 scripts/reattribute_orphaned_enrichment.py --apply --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

REFRESH_SOURCE = "enrichment_reattribution"

# The orphan definition MUST match the join diagnosis that measured the
# population: no identity match in catalog_products AND none in
# products_cache. The cache leg matters because the enrichment worker can
# still mint overlays for cache-only products that were never catalog-synced;
# re-keying one of those would strip a LIVE overlay.
_ORPHAN_GUARD = """NOT EXISTS (
        SELECT 1 FROM catalog_products c2
        WHERE c2.merchant_id = pe.merchant_id
          AND c2.platform = pe.platform
          AND c2.source_product_id = pe.platform_product_id
    )
      AND NOT EXISTS (
        SELECT 1 FROM products_cache pc
        WHERE pc.merchant_id = pe.merchant_id
          AND pc.platform = pe.platform
          AND pc.platform_product_id = pe.platform_product_id
    )"""

# Orphans: enrichment rows whose identity resolves to no catalog row.
ORPHANS_SQL = f"""
    SELECT pe.merchant_id, pe.platform, pe.platform_product_id, pe.geo_code,
           pe.title_override,
           CAST(pe.bullet_points AS TEXT) AS bullets_fp,
           (pe.bullet_points IS NOT NULL) AS has_bullets,
           (pe.description_markdown IS NOT NULL
            AND btrim(pe.description_markdown) <> '') AS has_description
    FROM product_enrichment pe
    WHERE {_ORPHAN_GUARD}
    ORDER BY pe.merchant_id, pe.platform, pe.platform_product_id
"""

# H1: the dead id is a seed's external_product_id; the seed is attached to a
# live catalog row in the same merchant+platform.
H1_SQL = f"""
    SELECT pe.merchant_id, pe.platform, pe.platform_product_id AS old_id,
           cp.source_product_id AS new_id, cp.content_key, cp.title
    FROM product_enrichment pe
    JOIN external_product_seeds eps
      ON eps.status = 'active'
     AND eps.external_product_id = pe.platform_product_id
    JOIN catalog_products cp
      ON cp.product_key = eps.attached_product_key
     AND cp.merchant_id = pe.merchant_id
     AND cp.platform = pe.platform
    WHERE {_ORPHAN_GUARD}
"""

# H2: the dead id appears as the FINAL URL path segment of an attached seed
# (slug ids were URL handles). The operand is LIKE-escaped — `_` is common in
# product ids and would otherwise match any character, pairing e.g. `mug_1`
# with a seed for `mug-1`. Only terminal positions count (bare end, `?query`,
# trailing `/`): a mid-path match would let a collection segment claim a
# different product's URL.
_H2_ESCAPED_ID = (
    r"""replace(replace(replace(pe.platform_product_id,"""
    r""" '\', '\\'), '%', '\%'), '_', '\_')"""
)
H2_SQL = f"""
    SELECT pe.merchant_id, pe.platform, pe.platform_product_id AS old_id,
           cp.source_product_id AS new_id, cp.content_key, cp.title
    FROM product_enrichment pe
    JOIN external_product_seeds eps
      ON eps.status = 'active'
     AND (eps.destination_url LIKE '%/' || {_H2_ESCAPED_ID}
          OR eps.destination_url LIKE '%/' || {_H2_ESCAPED_ID} || '?%'
          OR eps.destination_url LIKE '%/' || {_H2_ESCAPED_ID} || '/'
          OR eps.canonical_url  LIKE '%/' || {_H2_ESCAPED_ID}
          OR eps.canonical_url  LIKE '%/' || {_H2_ESCAPED_ID} || '?%'
          OR eps.canonical_url  LIKE '%/' || {_H2_ESCAPED_ID} || '/')
    JOIN catalog_products cp
      ON cp.product_key = eps.attached_product_key
     AND cp.merchant_id = pe.merchant_id
     AND cp.platform = pe.platform
    WHERE {_ORPHAN_GUARD}
"""

# H3: exact (case/space-normalized) title match via title_override.
H3_SQL = f"""
    SELECT pe.merchant_id, pe.platform, pe.platform_product_id AS old_id,
           cp.source_product_id AS new_id, cp.content_key, cp.title
    FROM product_enrichment pe
    JOIN catalog_products cp
      ON cp.merchant_id = pe.merchant_id
     AND cp.platform = pe.platform
     AND lower(btrim(cp.title)) = lower(btrim(pe.title_override))
    WHERE pe.title_override IS NOT NULL AND btrim(pe.title_override) <> ''
      AND {_ORPHAN_GUARD}
"""

# H4: content fingerprint against the SURVIVING served rows (the refresh
# erased most, so this recovers only what still serves).
H4_SQL = f"""
    SELECT pe.merchant_id, pe.platform, pe.platform_product_id AS old_id,
           cp.source_product_id AS new_id, cp.content_key, cp.title
    FROM product_enrichment pe
    JOIN agent_pdp_view av
      ON CAST(av.bullet_points AS TEXT) = CAST(pe.bullet_points AS TEXT)
    JOIN catalog_products cp
      ON cp.content_key = av.content_key
     AND cp.merchant_id = pe.merchant_id
     AND cp.platform = pe.platform
    WHERE pe.bullet_points IS NOT NULL
      AND {_ORPHAN_GUARD}
"""

# Target vacancy: any enrichment row already at the candidate new identity.
TARGET_OCCUPIED_SQL = """
    SELECT 1 FROM product_enrichment
    WHERE merchant_id = :merchant_id
      AND platform = :platform
      AND platform_product_id = :new_id
    LIMIT 1
"""

# RETURNING is the success signal: a post-hoc "is the target occupied now?"
# probe is truthy in BOTH outcomes (our rows landed, or a competitor's row
# blocked the guard), so only the UPDATE's own rowcount can tell them apart.
REKEY_SQL = """
    UPDATE product_enrichment
    SET platform_product_id = :new_id, updated_at = NOW()
    WHERE merchant_id = :merchant_id
      AND platform = :platform
      AND platform_product_id = :old_id
      AND NOT EXISTS (
        SELECT 1 FROM product_enrichment t
        WHERE t.merchant_id = :merchant_id
          AND t.platform = :platform
          AND t.platform_product_id = :new_id
      )
    RETURNING geo_code
"""

_HYPOTHESES: List[Tuple[str, str]] = [
    ("H1_seed_external_id", H1_SQL),
    ("H2_seed_url_slug", H2_SQL),
    ("H3_title_override", H3_SQL),
    ("H4_surviving_apv_bullets", H4_SQL),
]

# Priority = list position, NOT name order — lexicographic comparison would
# silently misrank an H10 between H1 and H2. Unknown names sort last.
_HYP_RANK: Dict[str, int] = {name: i for i, (name, _) in enumerate(_HYPOTHESES)}


def pick_matches(
    candidates: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Reduce raw hypothesis hits to APPLYABLE matches.

    Pure so it is testable and mutant-checkable. Rules:
      * hypothesis priority: the _HYPOTHESES-list-order strongest hypothesis
        that names an old_id wins; weaker hypotheses for the same old_id are
        ignored (they often re-derive the same pair, and when they disagree
        the higher-evidence one should win, not a vote).
      * bilateral uniqueness AFTER priority resolution: an old_id mapping to
        two new_ids within its winning hypothesis is ambiguous; two old_ids
        mapping to one new_id are both rejected — publishing two products'
        curated claims onto one row is exactly the failure this rule blocks.

    Returns (accepted, rejected-with-reason).
    """
    by_old: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for c in candidates:
        key = (c["merchant_id"], c["platform"], c["old_id"])
        by_old.setdefault(key, []).append(c)

    provisional: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for key, hits in by_old.items():
        best = min(_HYP_RANK.get(h["hypothesis"], len(_HYP_RANK)) for h in hits)
        winners = [h for h in hits
                   if _HYP_RANK.get(h["hypothesis"], len(_HYP_RANK)) == best]
        winning_hyp = winners[0]["hypothesis"]
        new_ids = {w["new_id"] for w in winners}
        if len(new_ids) > 1:
            rejected.append({**winners[0], "reason":
                             f"ambiguous: {len(new_ids)} targets within {winning_hyp}"})
            continue
        provisional.append(winners[0])

    target_counts: Dict[Tuple[str, str, str], int] = {}
    for m in provisional:
        t = (m["merchant_id"], m["platform"], m["new_id"])
        target_counts[t] = target_counts.get(t, 0) + 1

    accepted: List[Dict[str, Any]] = []
    for m in provisional:
        t = (m["merchant_id"], m["platform"], m["new_id"])
        if target_counts[t] > 1:
            rejected.append({**m, "reason":
                             f"collision: {target_counts[t]} orphans map to this target"})
            continue
        accepted.append(m)
    return accepted, rejected


async def collect() -> Dict[str, Any]:
    orphans = [dict(r) for r in await database.fetch_all(ORPHANS_SQL)]
    candidates: List[Dict[str, Any]] = []
    per_hypothesis: Dict[str, int] = {}
    for name, sql in _HYPOTHESES:
        rows = [dict(r) for r in await database.fetch_all(sql)]
        per_hypothesis[name] = len(rows)
        for r in rows:
            candidates.append({**r, "hypothesis": name})

    accepted, rejected = pick_matches(candidates)

    # Target vacancy is checked per accepted match (data can change between
    # dry run and apply; REKEY_SQL re-checks atomically regardless).
    vacant: List[Dict[str, Any]] = []
    occupied: List[Dict[str, Any]] = []
    for m in accepted:
        row = await database.fetch_one(TARGET_OCCUPIED_SQL, {
            "merchant_id": m["merchant_id"], "platform": m["platform"],
            "new_id": m["new_id"],
        })
        (occupied if row else vacant).append(m)

    identities = {(o["merchant_id"], o["platform"], o["platform_product_id"])
                  for o in orphans}
    matched_old = {(m["merchant_id"], m["platform"], m["old_id"])
                   for m in accepted}
    unmatched_identities = identities - matched_old
    unmatched = [
        o for o in orphans
        if (o["merchant_id"], o["platform"], o["platform_product_id"])
        in unmatched_identities
    ]
    return {
        "orphan_rows": len(orphans),
        "orphan_identities": len(identities),
        "per_hypothesis_hits": per_hypothesis,
        "accepted": vacant,
        "target_occupied": occupied,
        "rejected": rejected,
        "unmatched_count": len(unmatched_identities),
        "unmatched_by_group": _group(unmatched),
    }


def _group(orphans: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for o in orphans:
        k = f"{o['merchant_id']}/{o['platform']}"
        out[k] = out.get(k, 0) + 1
    return out


async def apply_matches(matches: List[Dict[str, Any]], limit: Optional[int]) -> Dict[str, Any]:
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
    from services.index_pipeline_state_service import recompute_serving_eligibility

    if limit is not None:
        matches = matches[:limit]
    rekeyed = republished = recomputed = skipped = 0
    republish_failed: List[str] = []
    for m in matches:
        # The NOT EXISTS inside REKEY_SQL makes vacancy atomic with the write;
        # RETURNING rows are the proof the update landed (0 rows = the guard
        # blocked it, or the orphan row disappeared since the dry run).
        moved_rows = await database.fetch_all(REKEY_SQL, {
            "merchant_id": m["merchant_id"], "platform": m["platform"],
            "old_id": m["old_id"], "new_id": m["new_id"],
        })
        if not moved_rows:
            skipped += 1
            print(f"  [SKIP] {m['old_id']} -> {m['new_id']}: rekey blocked "
                  f"(target occupied since dry run, or orphan row gone)",
                  flush=True)
            continue
        rekeyed += 1
        try:
            if await refresh_agent_pdp_view_for_content_key(
                m["content_key"], refresh_source=REFRESH_SOURCE
            ):
                republished += 1
            await recompute_serving_eligibility(m["content_key"], reason=REFRESH_SOURCE)
            recomputed += 1
        except Exception as exc:  # noqa: BLE001 — recorded for a manual retry
            republish_failed.append(m["content_key"])
            print(f"  [WARN] republish failed for {m['content_key']}: "
                  f"{type(exc).__name__}: {str(exc)[:80]}", flush=True)
    return {"rekeyed": rekeyed, "republished": republished,
            "recomputed": recomputed, "skipped": skipped,
            "republish_failed": republish_failed}


async def _drive(apply: bool, limit: Optional[int]) -> int:
    await database.connect()
    try:
        report = await collect()
        print(f"orphan rows={report['orphan_rows']} "
              f"(identities={report['orphan_identities']})  "
              f"hypothesis hits={report['per_hypothesis_hits']}")
        print(f"accepted={len(report['accepted'])}  "
              f"target_occupied={len(report['target_occupied'])}  "
              f"rejected={len(report['rejected'])}  "
              f"unmatched identities={report['unmatched_count']} "
              f"{report['unmatched_by_group']}")
        # The FULL mapping, not a preview: this printout is what the operator
        # approves before --apply, so it must show everything that would move.
        for m in report["accepted"]:
            print(f"  {m['hypothesis'][:4]}  {m['old_id'][:38]:<40} -> "
                  f"{m['new_id'][:28]:<30} {str(m['title'])[:30]}")
        for r in report["rejected"]:
            print(f"  REJ  {r['old_id'][:38]:<40} {r['reason']}")
        for o in report["target_occupied"]:
            print(f"  OCC  {o['old_id'][:38]:<40} target already enriched")
        if not apply:
            print("DRY-RUN — pass --apply to re-key the accepted matches.")
            return 0
        summary = await apply_matches(report["accepted"], limit)
        print(f"DONE: {json.dumps(summary)}")
        return 1 if (summary["skipped"] or summary["republish_failed"]) else 0
    finally:
        await database.disconnect()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Re-key accepted matches and republish. Default: dry run.")
    p.add_argument("--limit", type=int, default=None,
                   help="Apply at most N matches this run.")
    args = p.parse_args()
    if args.limit is not None and args.limit <= 0:
        p.error("--limit must be a positive integer")
    return asyncio.run(_drive(args.apply, args.limit))


if __name__ == "__main__":
    sys.exit(main())
