#!/usr/bin/env python3
"""
Retro-join already-paid orders back to their originating decision.

Closes the outcome loop for orders that were paid BEFORE the live webhook
join (services/agent_decision_event_store.record_funnel_link wired into
routes/webhook_routes.py) shipped. Going forward, the webhook writes the
link at the paid transition; this script backfills the history.

What it does (idempotent, INSERT-only):
  - finds paid orders whose metadata carries a decision_layer.decision_id or
    .checkout_decision_id, and whose funnel_events row(s) for that order are
    not yet linked in agent_decision_funnel_links;
  - inserts one agent_decision_funnel_links row per (funnel_event_id) with
    ON CONFLICT (funnel_event_id) DO NOTHING — so re-running is a no-op and
    it never races the live writer.

Linkage source is the ORDER ROW metadata (authoritative), not Stripe event
metadata. decision_id may be absent on older orders; checkout_decision_id is
present for agent orders and itself joins to a decision via checkout_decisions.

Approximation: the live writer links the funnel_event created by the paid
log_order_event. Retroactively we cannot perfectly reconstruct which event
that was, so we link ALL still-unlinked funnel_events for the order (they all
belong to that order's decision). Dashboards filter by funnel stage.

Default is dry-run (counts + a sample). Pass --apply to INSERT.

Requires prod-DB authorization. Production is Cloud Run (pivota-prod/us-west1) on
Cloud SQL; the Postgres-xMr6 public proxy this used to name is the ROLLBACK's
database, so a backfill applied there writes edges nobody is served from. Run it
inside production instead — the helper mounts the DATABASE_URL secret (a job
inherits NO env and NO secrets) and takes its verdict from the exit code:

  scripts/ops/run_oneoff_job.sh scripts/backfill_decision_funnel_links.py           # dry run
  scripts/ops/run_oneoff_job.sh scripts/backfill_decision_funnel_links.py --apply

Full pattern: docs/runbooks/operating_on_gcp_production.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database


# Candidate (order, funnel_event) pairs that have decision linkage and are not
# yet linked. We read decision_id + checkout_decision_id from the order's
# decision_layer metadata block. EVERY foreign-key reference is validated
# against its parent table (agent_decision_funnel_links has ON DELETE RESTRICT
# FKs on decision_id/checkout_decision_id/content_key/catalog_offer_id), so a
# dangling id is nulled out here rather than aborting the INSERT. A row is only
# returned if it still has at least one valid decision link (decision_id or
# checkout_decision_id) after validation.
SELECT_CANDIDATES_SQL = """
WITH raw AS (
    SELECT
        o.order_id,
        o.merchant_id,
        fe.event_id AS funnel_event_id,
        NULLIF(o.metadata #>> '{decision_layer,decision_id}', '')          AS decision_id,
        NULLIF(o.metadata #>> '{decision_layer,checkout_decision_id}', '') AS checkout_decision_id,
        NULLIF(o.metadata #>> '{decision_layer,content_key}', '')          AS content_key,
        NULLIF(o.metadata #>> '{decision_layer,catalog_offer_id}', '')     AS catalog_offer_id,
        NULLIF(o.metadata #>> '{decision_layer,protocol}', '')             AS protocol
    FROM orders o
    JOIN funnel_events fe
      ON fe.attribution_jsonb ->> 'order_id' = o.order_id
    LEFT JOIN agent_decision_funnel_links link
      ON link.funnel_event_id = fe.event_id
    WHERE o.payment_status = 'paid'
      AND link.funnel_event_id IS NULL
      AND (
            NULLIF(o.metadata #>> '{decision_layer,decision_id}', '') IS NOT NULL
         OR NULLIF(o.metadata #>> '{decision_layer,checkout_decision_id}', '') IS NOT NULL
      )
)
SELECT
    order_id,
    merchant_id,
    funnel_event_id,
    CASE WHEN EXISTS (SELECT 1 FROM agent_decision_events ade WHERE ade.decision_id = raw.decision_id)
         THEN raw.decision_id END AS decision_id,
    CASE WHEN EXISTS (SELECT 1 FROM checkout_decisions cd
                      WHERE cd.checkout_decision_id::text = raw.checkout_decision_id)
         THEN raw.checkout_decision_id END AS checkout_decision_id,
    CASE WHEN EXISTS (SELECT 1 FROM agent_pdp_view apv WHERE apv.content_key = raw.content_key)
         THEN raw.content_key END AS content_key,
    CASE WHEN EXISTS (SELECT 1 FROM catalog_offers co WHERE co.offer_id = raw.catalog_offer_id)
         THEN raw.catalog_offer_id END AS catalog_offer_id,
    raw.protocol
FROM raw
WHERE EXISTS (SELECT 1 FROM agent_decision_events ade WHERE ade.decision_id = raw.decision_id)
   OR EXISTS (SELECT 1 FROM checkout_decisions cd
              WHERE cd.checkout_decision_id::text = raw.checkout_decision_id)
ORDER BY order_id
{limit_clause}
"""


INSERT_SQL = """
INSERT INTO agent_decision_funnel_links
    (link_id, decision_id, funnel_event_id, checkout_decision_id,
     content_key, catalog_offer_id, merchant_id, protocol,
     attribution_model, attribution_window_seconds)
VALUES
    (:link_id, :decision_id, :funnel_event_id, :checkout_decision_id,
     :content_key, :catalog_offer_id, :merchant_id, :protocol,
     :attribution_model, :attribution_window_seconds)
ON CONFLICT (funnel_event_id) DO NOTHING
"""

DEFAULT_PROTOCOL = "pdp_direct"
DEFAULT_ATTRIBUTION_MODEL = "last_agent_decision_v1"
DEFAULT_ATTRIBUTION_WINDOW = 2592000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,    formatter_class=argparse.RawDescriptionHelpFormatter,)
    parser.add_argument("--apply", action="store_true", help="INSERT rows. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=None, help="Cap rows processed (testing).")
    parser.add_argument("--sample", type=int, default=10, help="Sample rows to print in dry-run.")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    limit_clause = f"LIMIT {int(args.limit)}" if args.limit else ""
    # NB: the SQL contains literal `{decision_layer,decision_id}` JSONB-path braces,
    # so str.format() would choke on them — substitute the one placeholder directly.
    sql = SELECT_CANDIDATES_SQL.replace("{limit_clause}", limit_clause)

    await database.connect()
    try:
        rows = await database.fetch_all(sql)
        candidates: List[Dict[str, Any]] = [dict(r) for r in rows]
        total = len(candidates)
        with_decision = sum(1 for c in candidates if c.get("decision_id"))
        checkout_only = total - with_decision

        print(f"[backfill] candidate (order,funnel_event) pairs to link: {total}")
        print(f"[backfill]   with decision_id: {with_decision}")
        print(f"[backfill]   checkout_decision_id only (no decision_id): {checkout_only}")

        if not args.apply:
            print("[backfill] DRY-RUN — no writes. Sample:")
            for c in candidates[: args.sample]:
                print("  " + json.dumps({k: c.get(k) for k in (
                    "order_id", "funnel_event_id", "decision_id",
                    "checkout_decision_id", "merchant_id")}, default=str))
            print("[backfill] re-run with --apply to INSERT.")
            return 0

        inserted = 0
        failed = 0
        for c in candidates:
            params = {
                "link_id": str(uuid.uuid4()),
                "decision_id": c.get("decision_id"),
                "funnel_event_id": c["funnel_event_id"],
                "checkout_decision_id": c.get("checkout_decision_id"),
                "content_key": c.get("content_key"),
                "catalog_offer_id": c.get("catalog_offer_id"),
                "merchant_id": c.get("merchant_id"),
                "protocol": c.get("protocol") or DEFAULT_PROTOCOL,
                "attribution_model": DEFAULT_ATTRIBUTION_MODEL,
                "attribution_window_seconds": DEFAULT_ATTRIBUTION_WINDOW,
            }
            try:
                await database.execute(INSERT_SQL, params)
                inserted += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not abort the batch
                failed += 1
                print(f"  [skip] order={c.get('order_id')} event={c.get('funnel_event_id')} err={str(exc)[:160]}")
        print(f"[backfill] APPLIED — inserted up to {inserted} links "
              f"(ON CONFLICT no-ops excluded; {failed} skipped on error).")
        return 0
    finally:
        await database.disconnect()


def main() -> None:
    args = _parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
