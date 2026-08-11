"""Stage 3a-ii backfill — (re)materialize agent_pdp_view rows.

Stage 3a-i (migration 085) added the denormalized agent_pdp_view table; this
script seeds and re-seeds it from catalog_products × catalog_skus ×
catalog_offers × product_group_members × external_product_seeds, plus the
evidence / enrichment / seller-trust overlays.

🚨 THIS SCRIPT USED TO DESTROY THE OVERLAYS IT WAS POINTED AT.

It inlined its own fetch→assemble→upsert, calling `assemble_row` with neither
`evidence=` nor `enrichment=` nor `seller_trust_by_id=`. Those default to None,
and UPSERT_SQL assigns EVERY column from EXCLUDED on conflict — including
`description`, `bullet_points`, `usage_scenarios`, `evidence_profile` and
`required_disclaimers`. So an `--apply` run did not merely fail to add
enrichment: it NULLed the enrichment, substantiated claims and disclaimers on
every row it touched that already had them. Pointing it at the ~138 enriched
rows stranded before the SERVE_PDP_ENRICHMENT_ON_WRITE flip would have wiped
the ~217 that were already serving.

It now delegates to `refresh_agent_pdp_view_for_content_key` /
`build_agent_pdp_view_row` — the canonical path that fetches all three overlays
— and no longer owns any assembly of its own. Do not reintroduce a local
`assemble_row` call here; see the warning on `build_agent_pdp_view_row`.

The grouping model and tiebreak ladder are documented in
services/agent_pdp_view_assembler.py.

Mock/synthetic boundary (memory: feedback_mock_data_never_to_merchant): the
assembler never synthesizes description prose; this script never backfills rows
with fabricated content. Every field originates in a primary catalog table or
external_product_seeds.seed_data (employee-authored bootstrap data — memory:
project_pivota_external_seed_bootstrap).

Usage
-----
Dry-run over the enriched cohort (what workstream 1 wants):
  python3 scripts/backfill_agent_pdp_view.py --scope enriched --limit 0

Apply it:
  python3 scripts/backfill_agent_pdp_view.py --scope enriched --limit 0 --apply

Dry-run / apply over the whole corpus, paginated:
  python3 scripts/backfill_agent_pdp_view.py --limit 200 --offset 0
  python3 scripts/backfill_agent_pdp_view.py --apply --limit 200 --offset 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.agent_pdp_view_assembler import (  # noqa: E402
    BACKFILL_REFRESH_SOURCE,
    UPSERT_SQL,
    build_agent_pdp_view_row,
    row_to_upsert_params,
)

logger = logging.getLogger("backfill_agent_pdp_view")

# Distinct refresh_source for the enriched-cohort pass so the audit trail can
# tell "propagated the stranded enrichment" from a generic re-seed.
ENRICHED_REFRESH_SOURCE = "backfill_enrichment_propagation"

# Every content_key, ordered so each --limit/--offset window is a disjoint slice.
_ALL_KEYS_SQL = """
    SELECT DISTINCT content_key
    FROM catalog_products
    WHERE content_key IS NOT NULL
    ORDER BY content_key ASC
"""

# content_keys carrying a product_enrichment overlay. product_enrichment is keyed
# by the enrichment domain's spelling of the catalog identity triple —
# platform_product_id there IS source_product_id on catalog_products. Getting
# that mapping wrong is what killed the on-write publish bridge; see the comment
# in refresh_agent_pdp_view_for_enrichment_write.
_ENRICHED_KEYS_SQL = """
    SELECT DISTINCT cp.content_key
    FROM product_enrichment pe
    JOIN catalog_products cp
      ON cp.merchant_id = pe.merchant_id
     AND cp.platform = pe.platform
     AND cp.source_product_id = pe.platform_product_id
    WHERE cp.content_key IS NOT NULL
    ORDER BY cp.content_key ASC
"""


def build_content_key_query(
    *, scope: str, limit: int, offset: int
) -> Tuple[str, Dict[str, Any]]:
    """(sql, params) for the content_key window. A pure builder so the driven
    PREPARE gate can plan every shape it emits against real Postgres — the
    assembled string lives in a function local, which the static sweep in
    tests/test_repo_sql_prepare_postgres.py cannot follow."""
    sql = _ENRICHED_KEYS_SQL if scope == "enriched" else _ALL_KEYS_SQL
    params: Dict[str, Any] = {}
    if limit > 0:
        sql += "\n        LIMIT :limit"
        params["limit"] = int(limit)
    if offset > 0:
        sql += "\n        OFFSET :offset"
        params["offset"] = int(offset)
    return sql, params


async def _fetch_content_keys(*, scope: str, limit: int, offset: int) -> List[str]:
    """Stable content_key window. Paged by content_key ASC so each chunk is a
    disjoint slice — no double-writes, safe to resume on partial failures.
    """
    sql, params = build_content_key_query(scope=scope, limit=limit, offset=offset)
    rows = await database.fetch_all(sql, params)
    return [r["content_key"] for r in rows or []]


def _overlay_flags(row: Dict[str, Any]) -> Dict[str, bool]:
    """Which overlays the freshly-assembled row carries. Reported in dry-run so
    the operator can see the propagation BEFORE writing, and counted on apply."""
    return {
        "bullet_points": bool(row.get("bullet_points")),
        "usage_scenarios": bool(row.get("usage_scenarios")),
        "evidence_profile": bool(row.get("evidence_profile")),
        "required_disclaimers": bool(row.get("required_disclaimers")),
    }


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------

async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    refresh_source = (
        ENRICHED_REFRESH_SOURCE if args.scope == "enriched" else BACKFILL_REFRESH_SOURCE
    )
    content_keys = await _fetch_content_keys(
        scope=args.scope, limit=args.limit, offset=args.offset
    )
    logger.info(
        "loaded %d content_keys (scope=%s limit=%d offset=%d)",
        len(content_keys), args.scope, args.limit, args.offset,
    )

    outcomes: Dict[str, int] = {
        "content_keys_considered": len(content_keys),
        "rows_assembled": 0,
        "rows_skipped_nothing_to_build": 0,
        "rows_upserted": 0,
        "rows_skipped_no_op_in_dry_run": 0,
        "with_bullet_points": 0,
        "with_usage_scenarios": 0,
        "with_evidence_profile": 0,
        "with_required_disclaimers": 0,
    }
    samples: List[Dict[str, Any]] = []

    for ck in content_keys:
        # Assemble through the canonical read path so the evidence, enrichment
        # and seller-trust overlays are attached. Dry-run stops here; apply
        # persists exactly this row.
        row = await build_agent_pdp_view_row(ck, refresh_source=refresh_source)
        if row is None:
            outcomes["rows_skipped_nothing_to_build"] += 1
            continue
        outcomes["rows_assembled"] += 1

        flags = _overlay_flags(row)
        for name, present in flags.items():
            if present:
                outcomes[f"with_{name}"] += 1

        if len(samples) < 5:
            samples.append({
                "content_key": ck,
                "title": row["title"],
                "brand": row["brand"],
                "offer_count": row["offer_count"],
                "variants_count": row["variants_count"],
                "primary_merchant_id": row["primary_merchant_id"],
                **flags,
            })

        if not args.apply:
            outcomes["rows_skipped_no_op_in_dry_run"] += 1
            continue
        await database.execute(UPSERT_SQL, row_to_upsert_params(row))
        outcomes["rows_upserted"] += 1

    return {
        "scope": args.scope,
        "refresh_source": refresh_source,
        "applied": bool(args.apply),
        "outcome_counts": outcomes,
        "samples": samples,
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually UPSERT agent_pdp_view rows. Default: dry-run.",
    )
    p.add_argument(
        "--scope", choices=("all", "enriched"), default="all",
        help=(
            "'all' = every content_key (default). 'enriched' = only the "
            "content_keys carrying a product_enrichment overlay — the cohort "
            "stranded before the SERVE_PDP_ENRICHMENT_ON_WRITE flip."
        ),
    )
    p.add_argument(
        "--limit", type=int, default=200,
        help="Max content_keys to process this run (0 = all). Default 200.",
    )
    p.add_argument(
        "--offset", type=int, default=0,
        help="Skip the first N content_keys. Use to paginate across chunks.",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
