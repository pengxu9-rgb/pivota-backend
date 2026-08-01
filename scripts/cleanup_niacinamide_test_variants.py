#!/usr/bin/env python3
"""Deactivate the near-duplicate test-variant seeds for the Jumiso USA
"20% NIACINAMIDE High Potency Dark Spot Serum" and suppress their
catalog_products mirror rows. Keeps exactly ONE clean canonical row.

Why this exists
---------------
~22 near-duplicate external_product_seeds rows for this product were
hand/QA-cloned under the singleton synthetic merchant 'external_seed'.
Their titles carry junk suffixes — (Copy), (Copy_b)..(Copy_h),
(Copy_T1)..(Copy_T7), (Convert_a) — and each distinct suffix mints a
distinct content_key on the catalog_products mirror. The serving-side
near-dup collapse (PIVOTA-Agent #1738/#1739/#1740) demotes the ones whose
collapse key matches, but exotic-suffix copies still leak into the
visible page. Root cause = these fake rows should not exist.

No pivota-backend writer produces these suffixes (verified by grep); they
are manual/QA artifacts. No test or fixture references this product or
these suffixes, so removal is safe. They live under the Pivota-managed
synthetic 'external_seed' merchant — not a real merchant's catalog.

What this does (two mirrors — see scripts/mirror_external_seeds_to_catalog_products.py)
--------------------------------------------------------------------------------------
1. external_product_seeds: status -> 'inactive' on the junk rows.
   - Excludes them from recall (agent_shop_gateway: WHERE status='active').
   - Excludes them from future mirror passes (mirror SELECTs active only).
2. catalog_products: suppression_reason -> 'niacinamide_dup_test_variant'
   on the mirror rows (joined by source_ref = seed id). The mirror never
   tombstones dropped seeds and the stale-catalog sweep excludes
   'external_seed', so the mirror row must be suppressed explicitly. This
   matches the established tombstone pattern in
   db/migrations/139_tombstone_cross_merchant_redundant_external_seed.sql.

Safety
------
- Dry-run by DEFAULT. Pass --apply to write.
- Idempotent: guarded by status='active' / suppression_reason IS NULL.
- Deactivation, not hard-delete: reversible, and safe w.r.t. references.
- Canonical protection: the single clean (non-junk-suffix) active row is
  kept. If zero or more than one clean active row exists, the script
  ABORTS and asks for an explicit --keep-id so a human picks the winner.

Usage
-----
  DATABASE_URL=postgresql://... python3 scripts/cleanup_niacinamide_test_variants.py            # dry-run
  DATABASE_URL=postgresql://... python3 scripts/cleanup_niacinamide_test_variants.py --apply     # execute
  DATABASE_URL=postgresql://... python3 scripts/cleanup_niacinamide_test_variants.py --keep-id <seed_id> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Group scope: specific to this product. Title is the reliable
# discriminator (brand fields drift on the citable-supplement copies).
TITLE_MATCH = "%High Potency Dark Spot Serum%"

# An opening "(Copy" / "(Convert" parenthetical anywhere in the title,
# case-insensitive. Deliberately does NOT require a closing paren — prod
# has a malformed leaker titled "...Dark Spot Serum (Copy_e" (no close).
# The clean canonical title has no such marker and is NOT matched.
JUNK_SUFFIX_RE = r"\(\s*(copy|convert)"

MIRROR_SUPPRESSION_REASON = "niacinamide_dup_test_variant"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the writes. Without this flag the script is a dry-run.",
    )
    parser.add_argument(
        "--keep-id",
        default=None,
        help=(
            "Explicit seed id to keep as canonical. Every OTHER in-scope "
            "row is deactivated. Required when the clean-row heuristic is "
            "ambiguous (zero or >1 clean active rows)."
        ),
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        # 1) Enumerate the whole product group from the source of truth.
        rows = await conn.fetch(
            f"""
            SELECT
              id,
              status,
              title,
              external_product_id,
              attached_product_key,
              created_at,
              (title ~* $2) AS is_junk
            FROM external_product_seeds
            WHERE title ILIKE $1
            ORDER BY is_junk ASC, created_at ASC
            """,
            TITLE_MATCH,
            JUNK_SUFFIX_RE,
        )

        if not rows:
            print("No external_product_seeds rows match the product group. Nothing to do.")
            return 0

        active = [r for r in rows if r["status"] == "active"]
        active_junk = [r for r in active if r["is_junk"]]
        active_clean = [r for r in active if not r["is_junk"]]

        print(f"Product group: {len(rows)} rows total, {len(active)} active.")
        print(f"  active junk-suffix rows : {len(active_junk)}")
        print(f"  active clean candidates : {len(active_clean)}")
        for r in active_clean:
            print(
                f"    CLEAN  id={r['id']}  title={r['title']!r}  "
                f"attached={r['attached_product_key']}"
            )

        # 2) Decide which rows to deactivate.
        if args.keep_id:
            keep_id = args.keep_id
            if keep_id not in {r["id"] for r in rows}:
                print(
                    f"ERROR: --keep-id {keep_id!r} is not in the product group.",
                    file=sys.stderr,
                )
                return 2
            targets = [r for r in active if r["id"] != keep_id]
            print(f"\nKeeping canonical (explicit): {keep_id}")
        else:
            if len(active_clean) != 1:
                print(
                    "\nABORT: cannot auto-pick a canonical row — expected exactly "
                    f"1 clean active row, found {len(active_clean)}.\n"
                    "Re-run with --keep-id <seed_id> to choose the winner "
                    "explicitly (run scripts/ops_niacinamide_dup_seed_probe.sql "
                    "first to inspect the rows).",
                    file=sys.stderr,
                )
                return 3
            keep_id = active_clean[0]["id"]
            # Default rule: deactivate only the junk-suffix rows. The single
            # clean row stays active.
            targets = list(active_junk)
            print(f"\nKeeping canonical (auto, single clean row): {keep_id}")

        # Never deactivate an attached seed silently — flag for review.
        attached_targets = [
            r
            for r in targets
            if r["attached_product_key"] and str(r["attached_product_key"]).strip()
        ]
        if attached_targets:
            print(
                "\nWARNING: the following target rows are ATTACHED to a product "
                "key (attached_product_key set). Review before applying:"
            )
            for r in attached_targets:
                print(f"    id={r['id']}  attached={r['attached_product_key']}  title={r['title']!r}")

        target_ids = [r["id"] for r in targets]
        print(f"\nWould deactivate {len(target_ids)} seed row(s):")
        for r in targets:
            print(f"    id={r['id']}  status={r['status']}  title={r['title']!r}")

        # 3) Preview mirror impact.
        mirror_rows = await conn.fetch(
            f"""
            SELECT product_key, source_ref AS seed_id, title, suppression_reason
            FROM catalog_products
            WHERE merchant_id = 'external_seed'
              AND source_ref = ANY($1::text[])
              AND suppression_reason IS NULL
            """,
            target_ids,
        )
        print(
            f"\nWould suppress {len(mirror_rows)} catalog_products mirror row(s) "
            f"(reason={MIRROR_SUPPRESSION_REASON!r})."
        )

        if not target_ids:
            print("\nNothing to deactivate. Group is already clean.")
            return 0

        if not args.apply:
            print("\nDRY-RUN — no changes written. Re-run with --apply to execute.")
            return 0

        # 4) Apply in one transaction. Idempotent guards on both updates.
        async with conn.transaction():
            seed_result = await conn.execute(
                """
                UPDATE external_product_seeds
                SET status = 'inactive', updated_at = now()
                WHERE id = ANY($1::text[])
                  AND status = 'active'
                """,
                target_ids,
            )
            mirror_result = await conn.execute(
                """
                UPDATE catalog_products
                SET suppression_reason = $2,
                    suppressed_at = COALESCE(suppressed_at, now()),
                    updated_at = now()
                WHERE merchant_id = 'external_seed'
                  AND source_ref = ANY($1::text[])
                  AND suppression_reason IS NULL
                """,
                target_ids,
                MIRROR_SUPPRESSION_REASON,
            )
        print(f"\nAPPLIED. external_product_seeds: {seed_result}. catalog_products: {mirror_result}.")

        # 5) Post-check: exactly one active row remains in the group.
        remaining_active = await conn.fetchval(
            "SELECT COUNT(*) FROM external_product_seeds "
            "WHERE title ILIKE $1 AND status = 'active'",
            TITLE_MATCH,
        )
        print(f"Post-check: {remaining_active} active row(s) remain in the group (expected 1).")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
