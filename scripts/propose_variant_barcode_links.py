#!/usr/bin/env python3
"""Propose relinking existing listings that share a barcode with another seller's product.

See services/identity_barcode_link.py for the rule (F2, 2026-10-10). PROPOSE-ONLY: this script
never approves and never applies. Proposals land in identity_resolution_proposals with strategy
`variant_barcode_match`, status 'proposed', for the identity engine's review -> approve -> apply
(services.identity_resolution.approve_proposals / apply_approved(strategies=[...]) / revert_run).

  Dry run (default): print a JSON summary -- cross-seller pairs, what would be proposed, examples.
    python -m scripts.propose_variant_barcode_links
  Write the proposals (status 'proposed'; re-running dedupes on proposal_key):
    python -m scripts.propose_variant_barcode_links --propose

Reads every live catalog_products.gtin and catalog_skus.barcode once: one job at a time on the
2-vCPU primary. Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.identity_barcode_link import STRATEGY, load_and_build  # noqa: E402
from services.identity_resolution import upsert_proposals  # noqa: E402
from services.intake_identity import seller_key  # noqa: E402

_EXAMPLES = 40


def _side(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"product_key": row.get("product_key"), "seller": seller_key(row),
            "title": row.get("title"), "content_key": row.get("content_key")}


def summarize(proposals: List[Dict[str, Any]], counts: Dict[str, int],
              pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The dry-run summary: counts, then pair and proposal examples a reviewer can spot-check."""
    return {
        "strategy": STRATEGY,
        "counts": dict(sorted(counts.items())),
        "pair_examples": [{"barcode": p["barcode"], "a": _side(p["a"]), "b": _side(p["b"])}
                          for p in pairs[:_EXAMPLES]],
        "proposal_examples": [
            {k: p["evidence"].get(k) for k in (
                "listing_title", "keeper_title", "brand", "barcodes", "listing_merchant_id",
                "keeper_merchant_id", "anchored_on_brand_row", "same_seller_products_on_family",
                "listing_product_key")}
            | {"joins": p["keeper_product_key"], "to_content_key": p["content_key"]}
            for p in proposals[:_EXAMPLES]
        ],
    }


async def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--propose", action="store_true", help="write the proposals (never applies)")
    args = ap.parse_args(argv)

    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=30, command_timeout=600)
    try:
        proposals, counts, pairs = await load_and_build(conn)
        result = summarize(proposals, counts, pairs)
        result["mode"] = "propose" if args.propose else "dry_run"
        if args.propose:
            result["written"] = await upsert_proposals(conn, proposals)
        print("RESULT " + json.dumps(result, default=str), flush=True)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
