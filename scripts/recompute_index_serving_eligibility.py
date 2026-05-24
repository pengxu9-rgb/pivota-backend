#!/usr/bin/env python3
"""Dry-run-first recompute for index_pipeline_state serving contract drift.

This PR-3 lane fixes rows that are currently public-serving according to
index_pipeline_state but fail the canonical serving contract when reclassified
from catalog_products, agent_pdp_view, catalog_offers, product_group_members,
quality snapshots, seed audit status, and domain regression state.

It does not edit PDP content, catalog content, offers, prices, identity rows,
or source seeds. Apply only rewrites index_pipeline_state.

Dry-run:
  python3 scripts/recompute_index_serving_eligibility.py --limit 600

Apply:
  python3 scripts/recompute_index_serving_eligibility.py \
    --apply --confirm RECOMPUTE_INDEX_SERVING_ELIGIBILITY --limit 600
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from decimal import Decimal
from typing import Any, Dict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.database import database  # noqa: E402
from services.index_pipeline_state_service import (  # noqa: E402
    audit_serving_contract_violations,
    fail_close_index_pipeline_state,
    recompute_serving_eligibility,
)


CONFIRM_TOKEN = "RECOMPUTE_INDEX_SERVING_ELIGIBILITY"
RECOMPUTE_REASON = "serving_contract_recompute_v1"


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def _connect_if_needed(db: Any) -> bool:
    was_connected = bool(getattr(db, "is_connected", False))
    if not was_connected:
        connect = getattr(db, "connect", None)
        if callable(connect):
            await connect()
    return was_connected


async def _disconnect_if_needed(db: Any, was_connected: bool) -> None:
    if not was_connected and bool(getattr(db, "is_connected", False)):
        disconnect = getattr(db, "disconnect", None)
        if callable(disconnect):
            await disconnect()


async def _apply_violation(violation: Dict[str, Any]) -> str:
    content_key = str(violation.get("content_key") or "").strip()
    if not content_key:
        return "skipped_missing_content_key"
    if int(violation.get("input_rows") or 0) <= 0:
        await fail_close_index_pipeline_state(
            content_key,
            blocker_code=str(violation.get("expected_blocker_code") or "not_live"),
            blocker_detail=str(
                violation.get("expected_blocker_detail")
                or "no catalog_products rows found during serving contract recompute"
            ),
        )
        return "fail_closed_no_catalog_inputs"
    await recompute_serving_eligibility(content_key, reason=RECOMPUTE_REASON)
    return "recomputed"


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    if args.apply and args.confirm != CONFIRM_TOKEN:
        raise SystemExit(f"--apply requires --confirm {CONFIRM_TOKEN}")

    was_connected = await _connect_if_needed(db)
    try:
        violations = await audit_serving_contract_violations(
            limit=args.limit,
            batch_size=args.batch_size,
            content_key=args.content_key or None,
        )
        blocker_counts = Counter(
            str(row.get("expected_blocker_code") or "unknown")
            for row in violations
        )
        report: Dict[str, Any] = {
            "apply": bool(args.apply),
            "limit": args.limit,
            "batch_size": args.batch_size,
            "content_key": args.content_key or None,
            "violations_found": len(violations),
            "expected_blocker_counts": dict(blocker_counts),
            "samples": violations[: args.sample_limit],
            "applied": {
                "recomputed": 0,
                "fail_closed_no_catalog_inputs": 0,
                "skipped_missing_content_key": 0,
            },
            "safety": {
                "catalog_content_updates": 0,
                "offer_updates": 0,
                "price_or_availability_fallbacks": 0,
                "identity_updates": 0,
                "source_seed_updates": 0,
            },
        }

        if args.apply:
            for violation in violations:
                outcome = await _apply_violation(violation)
                report["applied"][outcome] = report["applied"].get(outcome, 0) + 1
            if args.postcheck:
                remaining = await audit_serving_contract_violations(
                    limit=args.postcheck_limit,
                    batch_size=args.batch_size,
                    content_key=args.content_key or None,
                )
                report["postcheck"] = {
                    "remaining_violations": len(remaining),
                    "remaining_samples": remaining[: args.sample_limit],
                }
        return report
    finally:
        await _disconnect_if_needed(db, was_connected)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply recompute/fail-close to violating IPS rows. Default: dry-run.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Required with --apply. Must equal {CONFIRM_TOKEN}.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=600,
        help="Max violations to classify and optionally repair (0 = all). Default 600.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Serving-eligible IPS rows to scan per page. Default 500.",
    )
    parser.add_argument(
        "--content-key",
        default="",
        help="Optional single content_key to audit/recompute.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=20,
        help="Max violation samples in the report. Default 20.",
    )
    parser.add_argument(
        "--no-postcheck",
        dest="postcheck",
        action="store_false",
        help="Skip post-apply violation re-scan.",
    )
    parser.add_argument(
        "--postcheck-limit",
        type=int,
        default=600,
        help="Max remaining violations to scan after apply. Default 600.",
    )
    parser.set_defaults(postcheck=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
