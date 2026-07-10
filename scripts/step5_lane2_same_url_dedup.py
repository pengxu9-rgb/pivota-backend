#!/usr/bin/env python3
"""Step-5 Lane 2 — same-merchant same-URL duplicate collapse (keep-one).

Second apply cut of docs/plans/adr011_step5_catalog_identity_reconciliation.md:
same-merchant dup groups whose every row shares one normalized canonical_url —
the same PDP seeded N times as distinct source records. Keep exactly one row
per group; tombstone the rest (reversible suppression, no hard deletes) and
deactivate the losing rows' seeds on BOTH mirrors (see
scripts/step5_sweep_orphan_mirrors.py for the bidirectional linkage rule).

KEEPER POLICY — serving-aligned by construction: the keeper is whatever
services.agent_pdp_view_assembler.pick_canonical already selects (audit rows
last, then is_primary, then signature-bearing, then lowest product_key). The
served card, its signature, and anything publicly citing it are untouched;
the suppressed rows are precisely the ones serving already ignores. This
matters because in prod EVERY row of every lane-2 group carries its own
signature (the legacy per-URL N-sig population), so "prefer signed" would
discriminate nothing.

PROPOSE -> REVIEW -> APPLY:
  Dry-run (default) derives the groups and writes a proposal file:
    python3 scripts/step5_lane2_same_url_dedup.py --output-json reports/step5/lane2_proposal.json
  Apply ONLY takes a reviewed proposal file; it re-derives the groups fresh
  and applies exactly the groups whose keeper+losers still match the
  proposal, skipping (and reporting) any that drifted since review:
    python3 scripts/step5_lane2_same_url_dedup.py --apply --proposal reports/step5/lane2_proposal.json

Revert (by run):
  UPDATE catalog_products
  SET suppression_reason = NULL, suppression_metadata = NULL, updated_at = NOW()
  WHERE suppression_reason = 'step5_same_merchant_same_url_dup'
    AND suppression_metadata->>'run_id' = '<run_id printed at apply time>';
  (Seed reactivation, if wanted, via the seed ids listed in the apply output.)

Access notes as in step5_working_set.py (single retried asyncpg connection).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.step5_working_set import (  # noqa: E402
    ORPHAN_MIRRORS_SQL,
    WORKING_ROWS_SQL,
    build_report,
)
from services.agent_pdp_view_assembler import pick_canonical  # noqa: E402

SUPPRESSION_REASON = "step5_same_merchant_same_url_dup"

# Detail the keeper decision needs, for exactly the lane-2 rows. group_is_
# primary joins the same way agent_pdp_view_assembler does.
DETAIL_SQL = """
SELECT cp.product_key, cp.merchant_id, cp.content_key, cp.platform,
       cp.canonical_url, cp.source_ref, cp.pivota_signature_id,
       cp.title, cp.created_at,
       length(coalesce(cp.product_payload::text, '')) AS payload_bytes,
       pgm.is_primary AS group_is_primary
FROM catalog_products cp
LEFT JOIN product_group_members pgm
  ON pgm.merchant_id = cp.merchant_id
 AND pgm.platform = cp.platform
 AND pgm.platform_product_id = cp.source_product_id
WHERE cp.product_key = ANY($1::text[])
"""

SUPPRESS_SQL = """
UPDATE catalog_products cp
SET suppression_reason = $2,
    suppression_metadata = $3::jsonb,
    updated_at = NOW()
WHERE cp.product_key = ANY($1::text[])
  AND cp.product_key <> $4
  AND cp.suppression_reason IS NULL
"""

# Deactivate the losers' seeds via BOTH linkage directions — EXCEPT any seed
# that also backs the keeper. Seed<->row linkage is many-to-many within a
# group: the first prod apply hit 411 seeds whose id was a loser's source_ref
# while their attached_product_key pointed at the KEEPER; deactivating them
# orphaned the kept card (EXTERNAL_SEED_INACTIVE serving block) until a
# targeted reactivation repaired it. The keeper's linkage is excluded
# in-statement so that cannot recur.
DEACTIVATE_SEEDS_SQL = """
UPDATE external_product_seeds
SET status = 'inactive', updated_at = NOW()
WHERE (id = ANY($1::text[]) OR attached_product_key = ANY($2::text[]))
  AND lower(coalesce(status, '')) = 'active'
  AND (attached_product_key IS NULL OR attached_product_key <> $3)
  AND id IS DISTINCT FROM $4
RETURNING id
"""

# Post-apply guard: a keeper must still have an active seed in at least one
# linkage direction (external_seed keepers only; others have no seed to lose).
ORPHANED_KEEPERS_SQL = """
SELECT COUNT(*)
FROM catalog_products cp
WHERE cp.product_key = ANY($1::text[])
  AND cp.platform = 'external_seed'
  AND cp.suppression_reason IS NULL
  AND NOT EXISTS (
        SELECT 1 FROM external_product_seeds e
        WHERE (e.id = cp.source_ref
               OR e.attached_product_key = cp.product_key)
          AND lower(coalesce(e.status, '')) = 'active')
"""


def choose_keeper(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Serving-aligned keeper: exactly agent_pdp_view's canonical pick."""
    return pick_canonical(rows)


def _row_out(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "product_key": row.get("product_key"),
        "platform": row.get("platform"),
        "canonical_url": row.get("canonical_url"),
        "has_signature": bool(row.get("pivota_signature_id")),
        "group_is_primary": bool(row.get("group_is_primary")),
        "payload_bytes": int(row.get("payload_bytes") or 0),
        "created_at": str(row.get("created_at") or ""),
        "source_ref": row.get("source_ref"),
    }


def build_proposal(
    lane2_groups: List[Dict[str, Any]],
    detail_by_key: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Pure: lane-2 groups (from the working-set report) + per-row detail ->
    keeper/loser proposal. Groups missing detail rows are skipped defensively
    (they changed between the two queries)."""
    groups_out: List[Dict[str, Any]] = []
    skipped = 0
    for g in lane2_groups:
        detail = [
            detail_by_key[r["product_key"]]
            for r in g["rows"]
            if r["product_key"] in detail_by_key
        ]
        if len(detail) != len(g["rows"]) or len(detail) < 2:
            skipped += 1
            continue
        keeper = choose_keeper(detail)
        losers = [d for d in detail if d["product_key"] != keeper["product_key"]]
        groups_out.append(
            {
                "merchant_id": g["merchant_id"],
                "content_key": g["content_key"],
                "keeper": _row_out(keeper),
                "losers": [_row_out(d) for d in losers],
            }
        )
    return {
        "policy": "keeper = agent_pdp_view_assembler.pick_canonical (serving-aligned)",
        "suppression_reason": SUPPRESSION_REASON,
        "summary": {
            "groups": len(groups_out),
            "keepers": len(groups_out),
            "losers": sum(len(g["losers"]) for g in groups_out),
            "skipped_inconsistent": skipped,
        },
        "groups": groups_out,
    }


def _group_fingerprint(g: Dict[str, Any]) -> Tuple[str, str, str, Tuple[str, ...]]:
    return (
        g["merchant_id"],
        g["content_key"],
        g["keeper"]["product_key"],
        tuple(sorted(l["product_key"] for l in g["losers"])),
    )


def match_proposal(
    fresh: Dict[str, Any], reviewed: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """Pure: intersect the freshly derived proposal with the reviewed one.
    Returns (groups safe to apply, drifted (merchant_id, content_key) pairs).
    A group applies only if merchant, content_key, keeper AND the exact loser
    set are unchanged since review."""
    fresh_by_fp = {_group_fingerprint(g): g for g in fresh["groups"]}
    to_apply: List[Dict[str, Any]] = []
    drifted: List[Tuple[str, str]] = []
    for g in reviewed["groups"]:
        fp = _group_fingerprint(g)
        if fp in fresh_by_fp:
            to_apply.append(fresh_by_fp[fp])
        else:
            drifted.append((g["merchant_id"], g["content_key"]))
    return to_apply, drifted


def build_metadata(run_id: str, group: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "script": "step5_lane2_same_url_dedup",
            "run_id": run_id,
            "plan": "docs/plans/adr011_step5_catalog_identity_reconciliation.md",
            "keeper_product_key": group["keeper"]["product_key"],
            "content_key": group["content_key"],
        }
    )


async def _connect_with_retry(dsn: str, attempts: int = 6):
    import asyncpg

    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await asyncpg.connect(dsn, timeout=30, command_timeout=180)
        except Exception as e:  # public proxy flakes intermittently
            last = e
            await asyncio.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


async def _derive_fresh_proposal(conn) -> Dict[str, Any]:
    working = [dict(r) for r in await conn.fetch(WORKING_ROWS_SQL)]
    orphans = [dict(r) for r in await conn.fetch(ORPHAN_MIRRORS_SQL)]
    report = build_report(working, orphans)
    lane2 = report["lanes"].get("lane2_same_url", [])
    keys = sorted({r["product_key"] for g in lane2 for r in g["rows"]})
    detail = [dict(r) for r in await conn.fetch(DETAIL_SQL, keys)] if keys else []
    return build_proposal(lane2, {d["product_key"]: d for d in detail})


async def _run(args: argparse.Namespace) -> int:
    conn = await _connect_with_retry(os.environ["DATABASE_URL"])
    try:
        fresh = await _derive_fresh_proposal(conn)
        print(json.dumps({"summary": fresh["summary"]}, indent=2))

        if not args.apply:
            if args.output_json:
                os.makedirs(os.path.dirname(args.output_json) or ".", exist_ok=True)
                with open(args.output_json, "w") as fh:
                    json.dump(fresh, fh, indent=2, default=str)
                print(f"proposal -> {args.output_json}", file=sys.stderr)
            print("DRY-RUN — no changes written. Review the proposal, then re-run "
                  "with --apply --proposal <file>.")
            return 0

        with open(args.proposal) as fh:
            reviewed = json.load(fh)
        to_apply, drifted = match_proposal(fresh, reviewed)
        print(
            f"apply: {len(to_apply)} group(s) match the reviewed proposal; "
            f"{len(drifted)} drifted and are SKIPPED: {drifted[:10]}"
        )
        if not to_apply:
            print("Nothing to apply.")
            return 0

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suppressed = 0
        seed_ids: List[str] = []
        async with conn.transaction():
            for g in to_apply:
                loser_keys = [l["product_key"] for l in g["losers"]]
                loser_refs = [
                    l["source_ref"] for l in g["losers"] if l.get("source_ref")
                ]
                result = await conn.execute(
                    SUPPRESS_SQL,
                    loser_keys,
                    SUPPRESSION_REASON,
                    build_metadata(run_id, g),
                    g["keeper"]["product_key"],
                )
                suppressed += int(str(result).split()[-1] or 0)
                deactivated = await conn.fetch(
                    DEACTIVATE_SEEDS_SQL,
                    loser_refs,
                    loser_keys,
                    g["keeper"]["product_key"],
                    g["keeper"].get("source_ref"),
                )
                seed_ids.extend(str(r["id"]) for r in deactivated)

        # Post-checks: every applied group keeps >=1 unsuppressed row, and no
        # keeper lost its seed backing (would be an EXTERNAL_SEED_INACTIVE
        # serving block on the card we meant to keep).
        orphaned_keepers = await conn.fetchval(
            ORPHANED_KEEPERS_SQL, [g["keeper"]["product_key"] for g in to_apply]
        )
        empty = await conn.fetchval(
            """
            SELECT COUNT(*) FROM (
              SELECT 1 FROM catalog_products
              WHERE content_key = ANY($1::text[])
              GROUP BY merchant_id, content_key
              HAVING COUNT(*) FILTER (WHERE suppression_reason IS NULL) = 0
            ) t
            """,
            [g["content_key"] for g in to_apply],
        )
        print(
            json.dumps(
                {
                    "applied_groups": len(to_apply),
                    "rows_suppressed": suppressed,
                    "seeds_deactivated": len(seed_ids),
                    "run_id": run_id,
                    "reason": SUPPRESSION_REASON,
                    "groups_left_empty_post_check": empty,
                    "keepers_orphaned_post_check": orphaned_keepers,
                    "deactivated_seed_ids": seed_ids,
                },
                indent=2,
            )
        )
        return 0 if (empty == 0 and orphaned_keepers == 0) else 1
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Apply a reviewed proposal (default is dry-run)")
    parser.add_argument("--proposal",
                        help="Reviewed proposal file (required with --apply)")
    parser.add_argument("--output-json",
                        help="Dry-run: write the full proposal to this path")
    args = parser.parse_args()
    if args.apply and not args.proposal:
        parser.error("--apply requires --proposal <reviewed proposal file>")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
