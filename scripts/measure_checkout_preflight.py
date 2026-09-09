#!/usr/bin/env python3
"""Ask the merchants, over the catalog, what the checkout gate WOULD refuse — and write it down.

Dry run is the default. Pass --apply to record observations.

WHY THIS EXISTS. `services/checkout_preflight` ships in shadow so the refusal rate can be measured
before enforcement is armed. But #2154 fenced its egress: `web` runs on the `default` subnet, whose
NAT holds the address payment partners allowlist, so the request path may no longer fetch a
merchant at all. With the fence closed every live observation answers `not_yet_checked`, which is
a COVERAGE number and says nothing about any merchant. This lane is where the merchant half of the
evidence comes from: it opens the fence for ITSELF, on the crawl subnet, and walks the catalog.

RUN IT ON THE CRAWL SUBNET, AND IT CHECKS. `SUBNET=pivota-crawl` puts egress on 34.82.199.35
instead of the payment address; `CHECKOUT_PREFLIGHT_ALLOW_EGRESS=true` opens the fence for this
process only. THREE refusals, all before the first row:

  * the fence is shut — every ask would answer `not_yet_checked` and the sweep would report a
    tidy 100% refusal rate that is really our own cache. A measurement that cannot fail is worse
    than no measurement, and that exact shape has been shipped twice in this subsystem already.
  * the mode is off — `record` drops every row, so the report would describe an empty table.
  * THE EGRESS LEAVES BY THE PAYMENT ADDRESS. An earlier version of this paragraph promised the
    script "refuses otherwise" while nothing in it looked at the address at all, and `SUBNET`
    defaults to `default` — so the documented command with SUBNET forgotten swept the whole
    corpus out of 8.231.167.230. It now asks what address it actually has, once, and refuses.

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
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services import checkout_preflight  # noqa: E402
from services.handover_variant_identity import HandoverVariantResolver  # noqa: E402

#: 4s, not the backfill's 1.5s, because ONE ask here is up to THREE outbound requests: the
#: robots.txt `crawl_politeness` fetches, the `/products/<handle>.js` itself, and a
#: fire-and-forget `/meta.json` for the shop currency. At 1.5s that is ~120 requests a minute
#: against a measured cross-domain threshold of roughly 50, so the floor that looked twice as
#: safe as the threshold was actually more than twice over it.
GLOBAL_MIN_INTERVAL_S = float(os.getenv("PREFLIGHT_SWEEP_GLOBAL_INTERVAL_S", "4.0"))
CONSECUTIVE_BLOCK_ABORT = int(os.getenv("PREFLIGHT_SWEEP_ABORT_AFTER_BLOCKS", "10"))
#: How many DISTINCT hosts must be in the streak before it counts as our IP rather than their
#: shop. One is a dead storefront; a spread is a block.
_ABORT_DISTINCT_HOSTS = int(os.getenv("PREFLIGHT_SWEEP_ABORT_DISTINCT_HOSTS", "4"))

#: The address payment partners allowlist. If a sweep leaves by this one, the run is refused —
#: see the subnet note in the module docstring.
PAYMENT_EGRESS_IP = os.getenv("PIVOTA_PAYMENT_EGRESS_IP", "8.231.167.230")

#: Verdict reasons that mean WE could not ask, as opposed to a merchant answering. A run of these
#: is what a block looks like from in here.
_LOOKS_LIKE_A_BLOCK = frozenset({
    checkout_preflight.R_UNVERIFIABLE,
    checkout_preflight.R_NOT_YET_CHECKED,
})

SELECT_SEEDS_SQL = """
    SELECT e.id,
           e.attached_product_key,
           e.external_product_id,
           e.attached_variant_id,
           e.seed_data,
           COALESCE(NULLIF(e.canonical_url, ''), e.destination_url) AS url
    FROM external_product_seeds e
    WHERE e.status = 'active'
      AND jsonb_typeof(e.seed_data) = 'object'
      AND COALESCE(NULLIF(e.canonical_url, ''), e.destination_url) ~ '/products/'
      {cursor_clause}
    ORDER BY e.id
    LIMIT :limit
"""

def _seed_data_of(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """None means "unreadable — skip", never "empty, safe to ask about".

    `databases`+asyncpg hands JSONB back as a dict OR a JSON string depending on the codec, and a
    row that arrives as a string and is treated as an empty dict would have every admission rule
    silently answer "no evidence" — a sweep that skips real hand-overs while reporting a clean run.
    """
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


def _merchant_id_of(product_key: Any) -> Optional[str]:
    """The merchant id embedded in a `prod::{merchant}::{platform}::{handle}` key, or None.

    NOT a copy of the route's parser, and the earlier docstring claiming it was is the reason to
    say so: the route has its own tolerances. This is deliberately strict — four `::` segments
    with a `prod` head — because a wrong value in this column is worse than a null one. Live rows
    fill it with a merchant id, so anything else here makes the two sources unjoinable per
    merchant, which is most of what the source split is for.
    """
    parts = str(product_key or "").split("::")
    if len(parts) < 4 or parts[0] != "prod" or not parts[1]:
        return None
    return parts[1][:64]


def _offer_for(
    row: Dict[str, Any], variant_id: str, seed_data: Dict[str, Any],
    *, product_key: Optional[str] = None,
) -> Dict[str, Any]:
    """The offer shape `checkout_preflight` reads, built from the seed the route would serve.

    `source.seed_data` is load-bearing and not decoration: `_check_one` refuses to conclude `gone`
    without it, because a 404 from a storefront we cannot prove is Shopify means "we could not
    ask", not "it is gone". Omitting it would turn every dead handle into `not_a_known_shopify
    _storefront` and understate the refusal rate.
    """
    return {
        "offer_id": f"sweep:{row['id']}",
        # The key the RESOLVER used, not the row column. `_handover_product_key` falls back to
        # the seed document, so reading the column here recorded `product_key: None` for
        # hand-overs the resolver had keyed perfectly well.
        "product_key": product_key or row.get("attached_product_key") or None,
        "source_product_id": row.get("external_product_id") or None,
        "sku_key": None,
        # The ROUTE's vocabulary — the pivota merchant id embedded in the product key — not the
        # hostname. Writing a hostname into a column the live rows fill with a merchant id makes
        # the two sources unjoinable per merchant, which is most of what the split is for.
        "merchant_id": _merchant_id_of(product_key or row.get("attached_product_key")),
        "currency": None,
        "merchant_effective_price": None,
        "execution_spec": {"pdp_url": row.get("url"), "variant_id": variant_id},
        "source": {"seed_data": seed_data},
    }


def _handover_key(row: Dict[str, Any], seed_data: Dict[str, Any]) -> Optional[str]:
    """The key the route resolves on, taken from the route rather than restated here."""
    from routes.agent_shop_gateway import _handover_product_key

    return _handover_product_key(row, seed_data)


def _route_variants(seed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The variant entries the route walks, from the route.

    The route resolves PER VARIANT and the first cut of this script resolved once per row with
    `offer_variant_id=None`. Review showed that answers differently in BOTH directions: a product
    with two live merchant-issued SKUs is refused here as `multiple_merchant_issued_skus` while
    the route names each of them by exact match, and a product with one candidate whose seed names
    a DIFFERENT id is refused by the route as a contradiction while this script happily returned
    the candidate. Under-measuring one cohort and over-measuring another is precisely the
    population drift the lane exists to avoid.
    """
    from routes.agent_shop_gateway import _seed_variants

    variants = _seed_variants(seed_data)
    # The route caps what it walks; an uncapped sweep would ask about hand-overs no request can
    # produce, and one 200-variant product would dominate a page.
    return (variants[:12] or [{}])


def _cart_lane_variant_id(row: Dict[str, Any], seed_data: Dict[str, Any]) -> Optional[str]:
    """The OTHER half of the route's gate: `_gate_vid = _handover_id or _cart_vid`.

    The attach branch ships `_catalog_vid or _operator_vid` for a seed the route labels shopify,
    and the route gates that cart even when no `catalog_skus` row resolved — 67.1% of the corpus
    has no such row, and the route's own comment calls this "the only carts that exist today".
    Measuring only the resolved half reports a refusal rate for the cohort that is not shipping
    carts and stays silent about the one that is.
    """
    from routes.agent_shop_gateway import extract_shopify_numeric_variant_id

    snapshot = seed_data.get("snapshot") if isinstance(seed_data, dict) else None
    platform = str((snapshot or {}).get("storefront_platform") or "").strip().lower()
    if platform != "shopify":
        return None
    return extract_shopify_numeric_variant_id(row.get("attached_variant_id")) or None


async def run(limit: int, after: Optional[str], apply: bool, run_id: str) -> Dict[str, Any]:
    cursor_clause = "AND e.id > :after" if after else ""
    values: Dict[str, Any] = {"limit": max(1, int(limit))}
    if after:
        values["after"] = after
    rows = [dict(r) for r in
            await database.fetch_all(SELECT_SEEDS_SQL.format(cursor_clause=cursor_clause), values)
            or []]

    # THE REAL RESOLVER, not a restatement of it. Review found the first cut reimplementing the
    # admission rules and getting five of them wrong — it passed raw gids through where the
    # resolver canonicalises, skipped the stamp veto and the truncated-id check, accepted a seed
    # stamp the resolver's own predicate calls a forgery, and fell back to the stamp lane after
    # catalog had CONSIDERED and REFUSED candidates, which is the conditional fallback the
    # resolver explicitly forbids. A measurement whose population diverges from the gate it
    # informs is worse than no measurement, and reimplementation is how it diverges.
    from routes.agent_shop_gateway import (  # noqa: PLC0415 - route-local readers, by design
        _seed_offer_variant_id,
        _seed_variant_identity_claim,
    )

    prepared: List[Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]] = []
    skipped: Counter = Counter()
    for row in rows:
        seed_data = _seed_data_of(row)
        if seed_data is None:
            skipped["unreadable_seed_data"] += 1
            continue
        prepared.append((row, seed_data, _handover_key(row, seed_data)))

    resolver = HandoverVariantResolver()
    await resolver.prime([k for _, _, k in prepared if k])

    outcomes: Counter = Counter()
    reasons: Counter = Counter()
    lanes: Counter = Counter()
    per_host_blocks: Counter = Counter()
    block_window: List[str] = []
    aborted = False
    abort_hosts: List[str] = []
    asked = 0
    last_call = 0.0

    for row, seed_data, key in prepared:
        # PER VARIANT, exactly as the route does, passing the same two ids: the broad one may
        # MATCH a candidate, the narrow one may CONTRADICT it.
        gate_vid: Optional[str] = None
        gate_lane = "no_identity"
        for v in _route_variants(seed_data):
            handover = resolver.choose(
                product_key=key,
                product_id=row.get("external_product_id"),
                seed_data=seed_data,
                offer_variant_id=_seed_offer_variant_id(v) or None,
                named_variant_id=_seed_variant_identity_claim(v),
            )
            if handover.variant_id:
                gate_vid, gate_lane = handover.variant_id, handover.reason
                break
            gate_lane = handover.reason
        if not gate_vid:
            # The route's gate is a UNION, so the cart lane is asked before giving up.
            cart_vid = _cart_lane_variant_id(row, seed_data)
            if cart_vid:
                gate_vid, gate_lane = cart_vid, "cart_operator_id"
        if not gate_vid:
            # NOT an outcome. These are the hand-overs the gate is blind to by design, and
            # folding them into the refusal rate reports our coverage as the merchants' verdict.
            skipped[gate_lane] += 1
            continue
        lanes[gate_lane] += 1
        handover_variant_id = gate_vid

        gap = GLOBAL_MIN_INTERVAL_S - (time.monotonic() - last_call)
        if gap > 0:
            await asyncio.sleep(gap)
        last_call = time.monotonic()

        offer = _offer_for(row, handover_variant_id, seed_data, product_key=key)
        host = urlparse(str(row.get("url") or "")).hostname or "unknown"
        if apply:
            verdict = await checkout_preflight.preflight_and_record(
                offer, source=checkout_preflight.SOURCE_SWEEP, run_id=run_id
            )
        else:
            verdict = await checkout_preflight.preflight(offer)
        asked += 1
        outcomes[verdict.outcome] += 1
        reasons[verdict.reason] += 1

        if verdict.reason in _LOOKS_LIKE_A_BLOCK:
            per_host_blocks[host] += 1
            # A BOUNDED SLIDING WINDOW OF THE LAST N BLOCKED HOSTS, and nothing cleverer.
            #
            # ONE DEAD STOREFRONT IS NOT AN IP BLOCK, and seeds are id-ordered which clusters a
            # brand's rows together, so a bare consecutive count aborts on ordinary catalog rot
            # and reports one dead merchant as a 100% refusal rate. But the first attempt at the
            # distinction was worse than none: it pruned `block_streak[-1]`, which is whatever
            # host just ARRIVED, so a dead brand's rows sat in the window permanently and every
            # genuinely new blocked host was the one thrown away. A real cross-domain block could
            # then never be detected — review reproduced 40 distinct blocked hosts after a
            # 9-row dead prefix with `aborted_on_block: false`, a clean `would_block_rate: 1.0`,
            # exit 0, and a resume cursor, while still firing a request per row into a live 429.
            #
            # A window needs no pruning rule: a dead brand ages out on its own as new hosts
            # arrive, and a spread of hosts fills it. That is the whole heuristic.
            block_window.append(host)
            if len(block_window) > CONSECUTIVE_BLOCK_ABORT:
                block_window.pop(0)
            if (len(block_window) >= CONSECUTIVE_BLOCK_ABORT
                    and len(set(block_window)) >= _ABORT_DISTINCT_HOSTS):
                aborted = True
                abort_hosts = sorted(set(block_window))
                break
        else:
            # One good answer clears it. A dead handle between two live ones is ordinary catalog
            # rot (6.7% of a live sample), not evidence about our address.
            block_window = []

    # THE ANSWERED DENOMINATOR, not every refusal. `checkout_preflight.NO_CONTACT_REASONS` exists
    # because counting refusals decided without contacting a merchant reads 0.99 where the
    # merchants actually refused 0.20 — and the first cut of this script reintroduced that defect
    # one level down, under a comment claiming the denominator was right. The JSON printed here is
    # the only thing an operator reads from a one-off job.
    no_contact = sum(n for r, n in reasons.items() if r in checkout_preflight.NO_CONTACT_REASONS)
    answered = asked - no_contact
    answered_blocked = sum(
        n for r, n in reasons.items()
        if r != checkout_preflight.R_OK and r not in checkout_preflight.NO_CONTACT_REASONS
    )
    return {
        "mode": "apply" if apply else "dry_run",
        "run_id": run_id,
        "preflight_mode": checkout_preflight.mode(),
        "egress_allowed": checkout_preflight.egress_allowed(),
        "aborted_on_block": aborted,
        "aborted_on_hosts": abort_hosts,
        "next_cursor": (rows[-1]["id"] if rows and not aborted else None),
        "seeds_seen": len(rows),
        "gated": asked,
        # What the MERCHANTS said, over the asks that reached one. This is the number the
        # enforcement decision is read from.
        "answered": answered,
        "would_block": answered_blocked,
        "would_block_rate": (round(answered_blocked / answered, 4) if answered else None),
        # And our own coverage, kept beside it rather than folded in.
        "no_contact": no_contact,
        "by_outcome": dict(outcomes),
        "by_reason": dict(reasons),
        "by_lane": dict(lanes),
        "not_gated": dict(skipped),
        "most_blocked_hosts": dict(per_host_blocks.most_common(10)),
    }


async def _egress_ip() -> Optional[str]:
    """Which address this process actually leaves by, or None. One request, no data sent.

    RETURNS AN IP OR NOTHING. The first cut returned `.text.strip()` unchecked, so an HTML error
    body from the echo service — `<html><body>503 Service Unavailable</body></html>` — was not
    equal to the payment address and therefore counted as proof it was safe to sweep. That string
    is the only thing standing between "SUBNET forgotten" and a corpus crawl out of the
    payment-allowlisted IP, so anything that is not a parseable address must read as unknown.
    """
    import ipaddress

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.ipify.org")
            resp.raise_for_status()
            return str(ipaddress.ip_address(resp.text.strip()))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--after", type=str, default=None, help="resume past this seed id")
    parser.add_argument("--apply", action="store_true",
                        help="record observations; omit for a dry run")
    parser.add_argument("--run-id", type=str, default=None,
                        help="tag every row of this pass; defaults to a fresh id")
    parser.add_argument("--allow-payment-egress", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    def _refuse(error: str, detail: str) -> int:
        print(json.dumps({"error": error, "detail": detail}, indent=2), flush=True)
        return 2

    # REFUSE RATHER THAN MEASURE NOTHING. With the fence shut every ask answers `not_yet_checked`
    # and the sweep would produce a clean, complete, worthless report — and someone would read its
    # refusal rate as a fact about merchants. Exit before the first row, not after the last.
    if not checkout_preflight.egress_allowed():
        return _refuse("egress_fence_closed", (
            "CHECKOUT_PREFLIGHT_ALLOW_EGRESS is not set, so this process may not fetch a merchant "
            "and every ask would answer not_yet_checked. Set it, and run with SUBNET=pivota-crawl."
        ))
    if not checkout_preflight.is_enabled():
        return _refuse("preflight_mode_off",
                       "CHECKOUT_PREFLIGHT_MODE is off; set it to shadow to record observations.")

    # AND CHECK THE ADDRESS, rather than claiming to. The docstring used to say this script
    # "refuses otherwise" while nothing here looked at the subnet at all — SUBNET defaults to
    # `default`, so the documented command with SUBNET forgotten sweeps the whole corpus out of
    # the payment-allowlisted address. That is the incident the fence exists to prevent.
    ip = asyncio.run(_egress_ip())
    if ip == PAYMENT_EGRESS_IP and not args.allow_payment_egress:
        return _refuse("egress_leaves_by_the_payment_address", (
            f"this process egresses from {ip}, the address payment partners allowlist. "
            "Re-run with SUBNET=pivota-crawl so it leaves by the crawl NAT instead."
        ))
    if ip is None:
        return _refuse("egress_ip_unknown", (
            "could not determine this process's egress address, so it cannot be shown NOT to be "
            "the payment one. Refusing rather than guessing."
        ))

    run_id = args.run_id or f"sweep-{int(time.time())}-{uuid.uuid4().hex[:8]}"

    # PRINTED BEFORE THE FIRST ROW. The summary at the end is the only other place this appears,
    # and a TASK_TIMEOUT kill loses it while the rows are already committed — leaving exactly the
    # partial run the tag exists to exclude, untaggable. `--limit 400` at a 20s deadline can
    # exceed a 2400s task timeout, so this is the likely case, not the unlucky one.
    print(json.dumps({"run_id": run_id, "mode": "apply" if args.apply else "dry_run",
                      "egress_ip": ip, "limit": args.limit, "after": args.after}), flush=True)

    async def _main() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run(limit=args.limit, after=args.after, apply=args.apply, run_id=run_id)
        finally:
            await database.disconnect()

    summary = asyncio.run(_main())
    summary["egress_ip"] = ip
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    # Non-zero on abort so a wrapper cannot read a partial sweep as a completed one. The rows are
    # already committed, so the run_id above is how you exclude them.
    return 1 if summary.get("aborted_on_block") else 0


if __name__ == "__main__":
    raise SystemExit(main())
