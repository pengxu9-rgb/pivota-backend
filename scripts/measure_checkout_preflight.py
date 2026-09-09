#!/usr/bin/env python3
"""Ask the merchants, over the catalog, what the checkout gate WOULD refuse — and write it down.

Dry run is the default. Pass --apply to record observations.

WHY THIS EXISTS. `services/checkout_preflight` ships in shadow so the refusal rate can be measured
before enforcement is armed. But #2154 fenced its egress: `web` runs on the `default` subnet, whose
NAT holds the address payment partners allowlist, so the request path may no longer fetch a
merchant at all. With the fence closed every live observation answers `not_yet_checked`, which is
a COVERAGE number and says nothing about any merchant. This lane is where the merchant half of the
evidence comes from: it opens the fence for ITSELF, on the crawl subnet, and walks the catalog.

RUN IT ON THE CRAWL SUBNET, AND IT REFUSES OTHERWISE. `SUBNET=pivota-crawl` puts egress on
34.82.199.35 instead of the payment address; `CHECKOUT_PREFLIGHT_ALLOW_EGRESS=true` opens the
fence for this process only. The script EXITS 2 if the fence is shut, rather than sweeping the
whole corpus and reporting a tidy 100% `not_yet_checked` — a measurement that cannot fail is worse
than no measurement, and that exact shape has already been shipped twice in this subsystem.

    SUBNET=pivota-crawl TASK_TIMEOUT=2400s \
    ENV_VARS="PIVOTA_ENV=production,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600,CHECKOUT_PREFLIGHT_MODE=shadow,CHECKOUT_PREFLIGHT_ALLOW_EGRESS=true,CHECKOUT_PREFLIGHT_DEADLINE_SECONDS=20" \
    scripts/ops/run_oneoff_job.sh scripts/measure_checkout_preflight.py --limit 400 --apply

`CHECKOUT_PREFLIGHT_DEADLINE_SECONDS` matters. It defaults to 4s because it bounds a BUYER's
checkout; here nobody is waiting, and a 4s cap turns a slow-but-honest merchant into a timeout
recorded as `could_not_ask_merchant` — a refusal invented by our own impatience. Raise it.

ROWS ARE TAGGED `measurement_sweep`, NOT `live`. Migration 220 exists for this: live rows are
biased toward what agents actually ask for, sweep rows are unbiased over the catalog, and one rate
over both answers neither question. `shadow_report()` splits them.

THE POPULATION IS THE GATED ONE, computed the same way the route computes it. A hand-over is gated
when a merchant-issued variant resolves — from `catalog_skus` (#2151) or from the seed stamp that
`backfill_shopify_variant_ids.py` writes. Sweeping anything else measures offers the gate is blind
to by design and drags the denominator down, which is the defect this subsystem keeps re-inventing.

PACING. Every fetch goes through `services/crawl_politeness`, and the preflight's own budget bounds
each ask. On top of that this loop keeps a global floor, because the measured hazard is
CROSS-domain and per-IP: ~50 requests over 37 Cloudflare-fronted domains in about a minute tripped
an IP-level 429 lasting ~15 minutes. It also aborts on a run of consecutive refusals that look
like a block, with the same single-host-versus-many distinction the variant backfill needed: one
dead storefront with contiguous rows is not our IP being blocked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services import checkout_preflight  # noqa: E402
from services.shopify_variant_identity import (  # noqa: E402
    sole_stamped_variant_id,
    storefront_is_shopify,
)
from services.variant_identity import variant_id_provenance  # noqa: E402

GLOBAL_MIN_INTERVAL_S = float(os.getenv("PREFLIGHT_SWEEP_GLOBAL_INTERVAL_S", "1.5"))
CONSECUTIVE_BLOCK_ABORT = int(os.getenv("PREFLIGHT_SWEEP_ABORT_AFTER_BLOCKS", "10"))

#: Verdict reasons that mean WE could not ask, as opposed to a merchant answering. A run of these
#: is what a block looks like from in here.
_LOOKS_LIKE_A_BLOCK = frozenset({
    checkout_preflight.R_UNVERIFIABLE,
    checkout_preflight.R_NOT_YET_CHECKED,
})

_SNAPSHOT_VARIANTS_SAFE = (
    "CASE WHEN jsonb_typeof(e.seed_data->'snapshot'->'variants') = 'array' "
    "THEN e.seed_data->'snapshot'->'variants' ELSE '[]'::jsonb END"
)

SELECT_SEEDS_SQL = f"""
    SELECT e.id,
           e.attached_product_key,
           e.external_product_id,
           e.seed_data,
           COALESCE(NULLIF(e.canonical_url, ''), e.destination_url) AS url,
           {_SNAPSHOT_VARIANTS_SAFE} AS variants
    FROM external_product_seeds e
    WHERE e.status = 'active'
      AND jsonb_typeof(e.seed_data) = 'object'
      AND COALESCE(NULLIF(e.canonical_url, ''), e.destination_url) ~ '/products/'
      {{cursor_clause}}
    ORDER BY e.id
    LIMIT :limit
"""

#: Only LIVE rows on LIVE products, and only ids the classifier calls merchant-issued — the same
#: admission rule `services/handover_variant_identity` applies. A suppressed row is not a
#: hand-over candidate, so counting it would measure a cart nobody can be given.
SELECT_SKUS_SQL = """
    SELECT s.product_key, s.source_product_id, s.source_variant_id
    FROM catalog_skus s
    JOIN catalog_products p ON p.product_key = s.product_key
    WHERE s.product_key = ANY(:keys)
      AND s.suppressed_at IS NULL
      AND p.suppressed_at IS NULL
"""


def _seed_data_of(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = row.get("seed_data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def _sole_merchant_issued_by_product(keys: List[str]) -> Dict[str, str]:
    """product_key -> the ONE merchant-issued variant id, for products that have exactly one.

    More than one candidate is not a tie to break. The hand-over is at PRODUCT grain — the buyer
    has not chosen a variant — so two live merchant-issued SKUs mean we cannot know which cart to
    build, and the resolver refuses. Measuring a guess here would report a gate that does not
    exist.
    """
    if not keys:
        return {}
    rows = await database.fetch_all(SELECT_SKUS_SQL, {"keys": list(keys)})
    found: Dict[str, List[str]] = {}
    for r in rows or []:
        d = dict(r)
        vid = str(d.get("source_variant_id") or "")
        cls = variant_id_provenance(
            vid, product_key=d.get("product_key"), product_id=d.get("source_product_id")
        )
        if cls == "merchant_issued":
            found.setdefault(str(d["product_key"]), []).append(vid)
    return {k: v[0] for k, v in found.items() if len(v) == 1}


def _offer_for(row: Dict[str, Any], variant_id: str, seed_data: Dict[str, Any]) -> Dict[str, Any]:
    """The offer shape `checkout_preflight` reads, built from the seed the route would serve.

    `source.seed_data` is load-bearing and not decoration: `_check_one` refuses to conclude `gone`
    without it, because a 404 from a storefront we cannot prove is Shopify means "we could not
    ask", not "it is gone". Omitting it would turn every dead handle into `not_a_known_shopify
    _storefront` and understate the refusal rate.
    """
    return {
        "offer_id": f"sweep:{row['id']}",
        "product_key": row.get("attached_product_key") or None,
        "source_product_id": row.get("external_product_id") or None,
        "sku_key": None,
        "merchant_id": (urlparse(row.get("url") or "").hostname or "")[:64] or None,
        "currency": None,
        "merchant_effective_price": None,
        "execution_spec": {"pdp_url": row.get("url"), "variant_id": variant_id},
        "source": {"seed_data": seed_data},
    }


def gated_variant_id(
    row: Dict[str, Any], seed_data: Dict[str, Any], catalog: Dict[str, str]
) -> Tuple[Optional[str], str]:
    """(variant_id, lane) for a seed the gate would apply to, or (None, why-not).

    The two lanes in the order the route prefers them, so this measures the id a buyer would
    actually be handed rather than whichever we happen to find first.
    """
    key = str(row.get("attached_product_key") or "")
    from_catalog = catalog.get(key)
    if from_catalog:
        return from_catalog, "catalog"
    if storefront_is_shopify(seed_data):
        stamped = sole_stamped_variant_id(seed_data)
        if stamped:
            return stamped, "seed_stamp"
        return None, "shopify_but_no_sole_stamp"
    return None, "no_identity"


async def run(limit: int, after: Optional[str], apply: bool) -> Dict[str, Any]:
    cursor_clause = "AND e.id > :after" if after else ""
    values: Dict[str, Any] = {"limit": max(1, int(limit))}
    if after:
        values["after"] = after
    rows = [dict(r) for r in
            await database.fetch_all(SELECT_SEEDS_SQL.format(cursor_clause=cursor_clause), values)
            or []]

    catalog = await _sole_merchant_issued_by_product(
        [str(r.get("attached_product_key")) for r in rows if r.get("attached_product_key")]
    )

    outcomes: Counter = Counter()
    reasons: Counter = Counter()
    lanes: Counter = Counter()
    skipped: Counter = Counter()
    per_host_blocks: Counter = Counter()
    consecutive_blocks = 0
    aborted = False
    asked = 0
    last_call = 0.0

    for row in rows:
        seed_data = _seed_data_of(row)
        if seed_data is None:
            skipped["unreadable_seed_data"] += 1
            continue
        variant_id, lane = gated_variant_id(row, seed_data, catalog)
        if not variant_id:
            # NOT an outcome. These are the hand-overs the gate is blind to by design, and folding
            # them into the refusal rate is the denominator mistake this subsystem keeps making.
            skipped[lane] += 1
            continue
        lanes[lane] += 1

        gap = GLOBAL_MIN_INTERVAL_S - (time.monotonic() - last_call)
        if gap > 0:
            await asyncio.sleep(gap)
        last_call = time.monotonic()

        offer = _offer_for(row, variant_id, seed_data)
        host = str(offer.get("merchant_id") or "unknown")
        if apply:
            verdict = await checkout_preflight.preflight_and_record(
                offer, source=checkout_preflight.SOURCE_SWEEP
            )
        else:
            verdict = await checkout_preflight.preflight(offer)
        asked += 1
        outcomes[verdict.outcome] += 1
        reasons[verdict.reason] += 1

        if verdict.reason in _LOOKS_LIKE_A_BLOCK:
            consecutive_blocks += 1
            per_host_blocks[host] += 1
            if consecutive_blocks >= CONSECUTIVE_BLOCK_ABORT:
                aborted = True
                break
        else:
            consecutive_blocks = 0

    blocked = sum(n for r, n in reasons.items() if r != checkout_preflight.R_OK)
    return {
        "mode": "apply" if apply else "dry_run",
        "preflight_mode": checkout_preflight.mode(),
        "egress_allowed": checkout_preflight.egress_allowed(),
        "aborted_on_block": aborted,
        "next_cursor": (rows[-1]["id"] if rows and not aborted else None),
        "seeds_seen": len(rows),
        "asked": asked,
        "would_block": blocked,
        # Over ASKED, never over seeds_seen: dividing by the second mixes in the hand-overs the
        # gate is blind to and reports our own coverage as the merchants' verdict.
        "would_block_rate": (round(blocked / asked, 4) if asked else None),
        "by_outcome": dict(outcomes),
        "by_reason": dict(reasons),
        "by_lane": dict(lanes),
        "not_gated": dict(skipped),
        "most_blocked_hosts": dict(per_host_blocks.most_common(10)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--after", type=str, default=None, help="resume past this seed id")
    parser.add_argument("--apply", action="store_true", help="record observations; omit for a dry run")
    args = parser.parse_args()

    # REFUSE RATHER THAN MEASURE NOTHING. With the fence shut every ask answers `not_yet_checked`
    # and the sweep would produce a clean, complete, worthless report — and someone would read its
    # 100% refusal rate as a fact about merchants. Exit before the first row, not after the last.
    if not checkout_preflight.egress_allowed():
        print(json.dumps({
            "error": "egress_fence_closed",
            "detail": (
                "CHECKOUT_PREFLIGHT_ALLOW_EGRESS is not set, so this process may not fetch a "
                "merchant and every ask would answer not_yet_checked. Set it, and run with "
                "SUBNET=pivota-crawl so the egress does not leave by the payment address."
            ),
        }, indent=2), flush=True)
        return 2
    if not checkout_preflight.is_enabled():
        print(json.dumps({
            "error": "preflight_mode_off",
            "detail": "CHECKOUT_PREFLIGHT_MODE is off; set it to shadow to record observations.",
        }, indent=2), flush=True)
        return 2

    async def _main() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run(limit=args.limit, after=args.after, apply=args.apply)
        finally:
            await database.disconnect()

    summary = asyncio.run(_main())
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return 1 if summary.get("aborted_on_block") else 0


if __name__ == "__main__":
    raise SystemExit(main())
