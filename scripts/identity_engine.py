#!/usr/bin/env python3
"""Operator CLI for the identity engine's governed writes (M3): batch apply with a digest,
per-proposal revert, merge_content_key proposals. DRY RUN UNLESS --execute.

Nothing here runs on its own: no scheduler, startup hook or CI job calls it. Structural data fixes go
through the coordinator session -- dry run, Peng's go, then the --execute run with the dry run's
digest -- one prod one-off at a time, under the CPU gate, outside drains and 05:15-06:15Z /
10:30-11:20Z. Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.

  Plan a batch (read-only): which approved proposals of a strategy would apply, which the pre-flight
  blocks, which the cap defers, and the digest that pins exactly that set and its before-state:
    python -m scripts.identity_engine apply --strategy S --max-proposals 150
  Apply exactly that batch (refused, nothing written, if anything changed since the dry run), then
  rebuild the touched views (both content_keys, the losing one first):
    python -m scripts.identity_engine apply --strategy S --max-proposals 150 --expect-digest ird_... --execute
  Undo ONE applied proposal (refused if anything it moved has moved since), then rebuild the views:
    python -m scripts.identity_engine revert-proposal irp_...            # dry run
    python -m scripts.identity_engine revert-proposal irp_... --execute
  Propose merging every live row on one content_key into another product (status 'proposed'):
    python -m scripts.identity_engine propose-merge --from-ck ck_A --to-ck ck_B --keeper PK --strategy S
    python -m scripts.identity_engine propose-merge ... --write
  Approve named proposals (dry run lists them; --execute writes the approval):
    python -m scripts.identity_engine approve irp_... irp_... --by "<who>" --execute

Strategy names other paths auto-apply are RESERVED here (RESERVED_STRATEGIES): the reconcile sweep
and the link scripts apply every approved proposal of their strategy with no cap or digest, so a
proposal written or approved here under one of their names would bypass this CLI's governance.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.identity_resolution import (  # noqa: E402
    FROM_KEY_ROWS_SQL,
    MEMBER_GROUP_SQL,
    ATTACH_ROW_SQL,
    RUN_EVENTS_SQL,
    apply_approved,
    approve_proposals,
    merge_content_key_proposal,
    revert_proposal,
    upsert_proposals,
)


#: Strategies some other path approves or applies by strategy name alone: the reconcile sweep's
#: allowlist and every strategy its proposers write (services.identity_resolution_strategies), and the
#: link scripts' (identity_brand_link, identity_barcode_link). Pinned against those modules by tests.
RESERVED_STRATEGIES = frozenset({
    "same_url_dup", "junk_url", "campaign_clone", "campaign_clone_ambiguous", "seed_first_party_twin",
    "multi_seller_observation", "tier3_judge", "retailer_listing_brand_prefix", "variant_barcode_match",
})

#: Strategies only their own proposer may write (their evidence carries a matcher contract the
#: engine's readers depend on): propose-merge refuses them. Applied only here, with a digest
#: (identity_resolution.DIGEST_REQUIRED_STRATEGIES) and at most this many proposals per batch.
PROPOSER_OWNED_STRATEGIES = {
    "cross_merchant_same_size": "scripts/ops/propose_cross_merchant_matches.py",
}
STRATEGY_MAX_PROPOSALS = {
    "cross_merchant_same_size": 200,
}

PROPOSAL_STRATEGIES_SQL = """
SELECT proposal_id, strategy, kind, status FROM identity_resolution_proposals
WHERE proposal_id = ANY($1::text[])
"""


def reserved(strategy: str) -> bool:
    from services.identity_reconcile_sweep import AUTO_APPROVE_STRATEGIES

    return strategy in RESERVED_STRATEGIES or strategy in AUTO_APPROVE_STRATEGIES


def moved_details(details: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The apply/revert details whose two content_keys need their served views rebuilt."""
    return [d for d in details if (d.get("attached") or d.get("merged")) and d.get("reverted") is not False]


async def _refresh(details: List[Dict[str, Any]], source: str) -> Dict[str, int]:
    from db.database import database
    from services.identity_brand_link import refresh_after_move

    if not details:
        return {"refreshed": 0, "reaped": 0, "recomputed": 0, "errors": 0}
    await database.connect()
    try:
        return await refresh_after_move(details, source=source)
    finally:
        await database.disconnect()


def _detail(raw: Any) -> Dict[str, Any]:
    return json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})


async def build_merge(conn, *, from_ck: str, to_ck: str, keeper: str, strategy: str) -> Dict[str, Any]:
    """A merge_content_key proposal from the live rows on `from_ck` and their memberships now."""
    rows = list(await conn.fetch(FROM_KEY_ROWS_SQL, from_ck))
    if not rows:
        raise SystemExit(f"no live rows on {from_ck}")
    groups = {r["product_key"]: await conn.fetchval(MEMBER_GROUP_SQL, r["merchant_id"], r["platform"],
                                                    r["source_product_id"]) for r in rows}
    target = await conn.fetchrow(ATTACH_ROW_SQL, keeper)
    if not target or target["content_key"] != to_ck or target["suppression_reason"] is not None:
        raise SystemExit(f"keeper {keeper} is not a live row on {to_ck}")
    to_group = await conn.fetchval(MEMBER_GROUP_SQL, target["merchant_id"], target["platform"],
                                   target["source_product_id"])
    if not to_group:
        raise SystemExit(f"keeper {keeper} has no product group")
    return merge_content_key_proposal(strategy=strategy, from_content_key=from_ck, to_content_key=to_ck,
                                      from_product_keys=list(groups), from_groups=groups,
                                      keeper_product_key=keeper, to_product_group_id=to_group)


async def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--strategy", required=True)
    a.add_argument("--max-proposals", type=int, required=True)
    a.add_argument("--expect-digest")
    a.add_argument("--execute", action="store_true")
    r = sub.add_parser("revert-proposal")
    r.add_argument("proposal_id")
    r.add_argument("--execute", action="store_true")
    m = sub.add_parser("propose-merge")
    m.add_argument("--from-ck", required=True)
    m.add_argument("--to-ck", required=True)
    m.add_argument("--keeper", required=True)
    m.add_argument("--strategy", required=True)
    m.add_argument("--write", action="store_true")
    ok = sub.add_parser("approve")
    ok.add_argument("proposal_ids", nargs="+")
    ok.add_argument("--by", required=True)
    ok.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "apply" and args.execute and not args.expect_digest:
        ap.error("--execute needs --expect-digest from a dry run")
    if args.cmd in ("apply", "propose-merge") and reserved(args.strategy):
        ap.error(f"strategy {args.strategy!r} is reserved: another path applies it with no cap or digest")
    if args.cmd == "propose-merge" and args.strategy in PROPOSER_OWNED_STRATEGIES:
        ap.error(f"strategy {args.strategy!r} is written only by {PROPOSER_OWNED_STRATEGIES[args.strategy]}")
    cap = STRATEGY_MAX_PROPOSALS.get(getattr(args, "strategy", None) or "")
    if args.cmd == "apply" and cap is not None and args.max_proposals > cap:
        ap.error(f"strategy {args.strategy!r} applies at most {cap} proposals per batch")

    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=30, command_timeout=300)
    try:
        if args.cmd == "apply":
            result = await apply_approved(conn, strategies=[args.strategy], max_proposals=args.max_proposals,
                                          expect_digest=args.expect_digest, dry_run=not args.execute)
            if args.execute:
                details = moved_details([_detail(e["detail"]) for e in await conn.fetch(RUN_EVENTS_SQL,
                                                                                        result["run_id"])])
                result["refresh"] = await _refresh(details, args.strategy)
        elif args.cmd == "revert-proposal":
            result = await revert_proposal(conn, args.proposal_id, dry_run=not args.execute)
            if args.execute and not result.get("refused"):
                result["refresh"] = await _refresh(moved_details(result.get("detached") or []),
                                                   "identity_revert_proposal")
        elif args.cmd == "propose-merge":
            p = await build_merge(conn, from_ck=args.from_ck, to_ck=args.to_ck, keeper=args.keeper,
                                  strategy=args.strategy)
            result = {"proposal": p}
            if args.write:
                result["written"] = await upsert_proposals(conn, [p])
        else:
            rows = [dict(r) for r in await conn.fetch(PROPOSAL_STRATEGIES_SQL, args.proposal_ids)]
            bad = sorted(r["proposal_id"] for r in rows if reserved(r["strategy"]))
            if bad:
                raise SystemExit(f"refusing to approve proposals of reserved strategies: {bad}")
            if args.execute:
                result = {"approved": await approve_proposals(conn, args.proposal_ids, args.by.strip())}
            else:
                result = {"dry_run": True, "would_approve": [r for r in rows if r["status"] == "proposed"]}
        print("RESULT " + json.dumps(result, default=str), flush=True)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
