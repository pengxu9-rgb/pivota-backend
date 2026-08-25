#!/usr/bin/env python3
"""ADR-009 A9-3/A9-4 parity-week watch — seller_ref_missing.

Daily shadow-mode watch confirming the seller-of-record backfill has made the
seller-keyed closure path total, so the LEGACY closure fallbacks (the A9-1
raw-host mismatch compare + the `seller_ref_missing` honest-gap branch in
services.commerce_attribution_service.close_external_order_conversion) can be
removed by a separate packet WITHOUT silently dropping attribution.

Follows the repo monitor convention (scripts/stage1_daily_monitor.py): raw SQL
against prod, no service-import graph, a one-page pass/fail report, and an exit
code that marks the day clean. Run it once a day; when it reports CLEAN for
CLEAN_DAYS_REQUIRED consecutive days, the legacy paths are safe to remove.

The watch window is anchored at the seeds backfill (`--since`, default the
2026-07-07 execute run). "Recent" = strictly after that anchor, i.e. traffic
that flowed through the NOW-seller-bearing seeds.

Checks (a clean day requires all three green):
  A. CLOSURE (primary)   — no external conversion has closed since the anchor
     with metadata.seller_ref_missing=true. This is the metric the A9-3 note
     names. (0 conversions yet → vacuously green; the leading indicator below
     is what moves first.)
  B. SUPPLY  (leading)   — every recent external-seed CLICK that maps to a seed
     which HAS a seller_ref carried seller_ref into its signed-token context.
     A miss here is a live T2-1 stamping regression (caught before it can
     become a seller_ref_missing conversion).
  C. WRITE-PATH (regression) — no seed INSERTED since the anchor is left
     seller_ref-NULL while resolvable (non-empty destination), i.e. the A9-3
     new-write derivation keeps stamping.

Also reported (informational, NOT gates for seller_ref_missing — they need the
separate phase-3 catalog re-key, `--phases catalog`): the banned external_seed
bucket residue on catalog_products / commerce_attribution_edges, and the honest
seed floor (seeds with no resolvable destination stay seller_ref-NULL by design).

Usage (production is Cloud Run, pivota-prod/us-west1; Railway is the ROLLBACK, and
its database is a DIFFERENT database — a parity check run there measures the wrong
platform and reports a clean day for traffic it never saw):
    # inside production (native internal DB — most reliable). There is no
    # `railway ssh` equivalent; use a throwaway job on the production image, which
    # propagates this script's clean/dirty exit code as the job's:
    scripts/ops/run_oneoff_job.sh -m scripts.parity_watch_seller_ref
    # operator, against a database you already have a URL for:
    DATABASE_PUBLIC_URL=... python -m scripts.parity_watch_seller_ref
    # pin a day log:
    python -m scripts.parity_watch_seller_ref >> /tmp/sellerref-parity-$(date -u +%Y%m%d).log

Exit codes: 0 clean day · 1 drift (a gate is red) · 2 script error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Anchor: the 2026-07-07 seeds backfill execute run (scripts.backfill_seller_of_record).
DEFAULT_SINCE = "2026-07-07T12:39:48+00:00"
CLEAN_DAYS_REQUIRED = 5


def _dsn() -> str:
    pub = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("PUB_URL")
    if pub:
        return pub if "sslmode=" in pub else pub + ("&" if "?" in pub else "?") + "sslmode=require"
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: set DATABASE_PUBLIC_URL (operator) or DATABASE_URL (in-container).", file=sys.stderr)
        raise SystemExit(2)
    return url


async def _val(conn, sql: str, *args) -> int:
    v = await conn.fetchval(sql, *args)
    return int(v or 0)


async def run(since: str) -> int:
    import asyncpg

    since_dt = datetime.fromisoformat(since)
    conn = await asyncpg.connect(_dsn(), timeout=25)
    try:
        m: Dict[str, Any] = {}

        # ---- A. CLOSURE (primary) ----
        m["ext_converted_total"] = await _val(
            conn,
            "SELECT count(*) FROM commerce_attribution_edges "
            "WHERE source='external_redirect' AND state='converted'",
        )
        m["ext_converted_seller_missing_total"] = await _val(
            conn,
            "SELECT count(*) FROM commerce_attribution_edges "
            "WHERE source='external_redirect' AND state='converted' "
            "AND (metadata->>'seller_ref_missing')::boolean IS TRUE",
        )
        m["ext_converted_seller_missing_recent"] = await _val(
            conn,
            "SELECT count(*) FROM commerce_attribution_edges "
            "WHERE source='external_redirect' AND state='converted' "
            "AND (metadata->>'seller_ref_missing')::boolean IS TRUE "
            "AND converted_at > $1",
            since_dt,
        )

        # ---- B. SUPPLY (leading) ----
        # external-seed clicks are tagged in context (source=external_seed or an
        # external_seed_id key). A recent one whose seed HAS a seller_ref but
        # whose context lacks seller_ref is a live T2-1 stamping miss.
        m["ext_clicks_recent"] = await _val(
            conn,
            "SELECT count(*) FROM surface_click_events "
            "WHERE created_at > $1 "
            "AND (context->>'source' = 'external_seed' OR context ? 'external_seed_id')",
            since_dt,
        )
        m["ext_clicks_recent_with_seller_ref"] = await _val(
            conn,
            "SELECT count(*) FROM surface_click_events "
            "WHERE created_at > $1 "
            "AND (context->>'source' = 'external_seed' OR context ? 'external_seed_id') "
            "AND context ? 'seller_ref'",
            since_dt,
        )
        m["ext_clicks_recent_missing_but_resolvable"] = await _val(
            conn,
            "SELECT count(*) FROM surface_click_events c "
            "WHERE c.created_at > $1 "
            "AND (c.context->>'source' = 'external_seed' OR c.context ? 'external_seed_id') "
            "AND NOT (c.context ? 'seller_ref') "
            "AND EXISTS (SELECT 1 FROM external_product_seeds s "
            "            WHERE s.id = c.context->>'external_seed_id' "
            "            AND s.seller_ref IS NOT NULL AND btrim(s.seller_ref) <> '')",
            since_dt,
        )

        # ---- C. WRITE-PATH (regression) ----
        m["seeds_new_resolvable_missing"] = await _val(
            conn,
            "SELECT count(*) FROM external_product_seeds "
            "WHERE created_at > $1 AND seller_ref IS NULL "
            "AND COALESCE(NULLIF(btrim(domain),''), NULLIF(btrim(destination_url),''), "
            "            NULLIF(btrim(canonical_url),'')) IS NOT NULL",
            since_dt,
        )

        # ---- Floor + bucket (informational) ----
        m["seeds_seller_ref_null_floor"] = await _val(
            conn, "SELECT count(*) FROM external_product_seeds WHERE seller_ref IS NULL"
        )
        m["seeds_seller_ref_present"] = await _val(
            conn,
            "SELECT count(*) FROM external_product_seeds "
            "WHERE seller_ref IS NOT NULL AND btrim(seller_ref) <> ''",
        )
        # Banned external_seed bucket residue (seller_identity.BANNED_BUCKET_MERCHANT_ID
        # = 'external_seed'). Drained by the separate phase-3 catalog re-key — NOT a
        # gate for seller_ref_missing; reported so the operator sees the D4 progress.
        m["catalog_external_seed_bucket"] = await _val(
            conn, "SELECT count(*) FROM catalog_products WHERE merchant_id = 'external_seed'"
        )
        m["edges_external_seed_bucket"] = await _val(
            conn, "SELECT count(*) FROM commerce_attribution_edges WHERE merchant_id = 'external_seed'"
        )

        # ---- Gates ----
        gate_closure = m["ext_converted_seller_missing_recent"] == 0
        gate_supply = m["ext_clicks_recent_missing_but_resolvable"] == 0
        gate_writepath = m["seeds_new_resolvable_missing"] == 0
        clean = gate_closure and gate_supply and gate_writepath

        report = {
            "watch": "adr009_seller_ref_missing_parity",
            "run_at": datetime.now(timezone.utc).isoformat(),
            "since": since,
            "clean_days_required": CLEAN_DAYS_REQUIRED,
            "metrics": m,
            "gates": {
                "A_closure_no_recent_seller_missing": gate_closure,
                "B_supply_all_resolvable_clicks_stamped": gate_supply,
                "C_writepath_new_seeds_stamped": gate_writepath,
            },
            "verdict": "CLEAN" if clean else "DRIFT",
        }
        print(json.dumps(report, indent=2, default=str))

        line = (
            f"[{report['run_at']}] {report['verdict']} | "
            f"conv={m['ext_converted_total']} seller_missing_recent={m['ext_converted_seller_missing_recent']} | "
            f"ext_clicks_recent={m['ext_clicks_recent']} stamped={m['ext_clicks_recent_with_seller_ref']} "
            f"miss_resolvable={m['ext_clicks_recent_missing_but_resolvable']} | "
            f"new_seed_miss={m['seeds_new_resolvable_missing']} | "
            f"seller_ref present={m['seeds_seller_ref_present']} null_floor={m['seeds_seller_ref_null_floor']}"
        )
        print(line, file=sys.stderr)
        return 0 if clean else 1
    finally:
        await conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ADR-009 seller_ref_missing parity-week watch (daily).")
    ap.add_argument("--since", default=os.getenv("SELLER_REF_PARITY_SINCE", DEFAULT_SINCE),
                    help="Watch anchor (ISO ts). Default: the 2026-07-07 seeds backfill execute.")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args.since))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: parity watch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
