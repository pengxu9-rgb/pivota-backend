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
    audit_serving_contract_violation_page,
    audit_serving_contract_violations,
    fail_close_index_pipeline_state,
    recompute_serving_eligibility,
    upsert_classified_index_pipeline_state,
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


def _public_violation(violation: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in violation.items()
        if not str(key).startswith("_")
    }


def _emit_progress(payload: Dict[str, Any]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, default=_json_default),
        file=sys.stderr,
        flush=True,
    )


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
    expected_state = violation.get("_expected_state")
    if isinstance(expected_state, dict) and expected_state:
        await upsert_classified_index_pipeline_state(content_key, expected_state)
        return "applied_audit_state"
    await recompute_serving_eligibility(content_key, reason=RECOMPUTE_REASON)
    return "recomputed"


async def _fetch_and_apply_stream_page(
    args: argparse.Namespace,
    *,
    db: Any,
    cursor: str,
    remaining_limit: int,
) -> Dict[str, Any]:
    attempts = max(int(args.page_retries or 0), 0) + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        was_connected = await _connect_if_needed(db)
        try:
            page = await audit_serving_contract_violation_page(
                cursor=cursor,
                batch_size=args.batch_size,
                content_key=args.content_key or None,
            )
            page_violations = list(page.get("violations") or [])
            selected_violations = (
                page_violations[:remaining_limit]
                if remaining_limit > 0
                else page_violations
            )
            applied = Counter()
            if args.apply:
                for violation in selected_violations:
                    outcome = await _apply_violation(violation)
                    applied[outcome] += 1
            return {
                "page": page,
                "selected_violations": selected_violations,
                "applied": applied,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _emit_progress(
                {
                    "event": "serving_recompute_stream_page_error",
                    "cursor": cursor or "",
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "error": str(exc),
                }
            )
            if attempt >= attempts:
                raise
        finally:
            await _disconnect_if_needed(db, was_connected)
    raise RuntimeError(str(last_error or "stream page failed"))


async def _drive_stream_pages(
    args: argparse.Namespace,
    *,
    db: Any = database,
) -> Dict[str, Any]:
    max_results = int(args.limit or 0)
    cursor = ""
    report: Dict[str, Any] = {
        "apply": bool(args.apply),
        "stream_pages": True,
        "limit": args.limit,
        "batch_size": args.batch_size,
        "content_key": args.content_key or None,
        "pages_scanned": 0,
        "rows_scanned": 0,
        "last_cursor": "",
        "completed_scan": False,
        "violations_found": 0,
        "expected_blocker_counts": {},
        "samples": [],
        "applied": {
            "applied_audit_state": 0,
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
    blocker_counts: Counter[str] = Counter()
    samples = []

    while True:
        remaining_limit = (
            max_results - int(report["violations_found"])
            if max_results > 0
            else 0
        )
        if max_results > 0 and remaining_limit <= 0:
            break

        result = await _fetch_and_apply_stream_page(
            args,
            db=db,
            cursor=cursor,
            remaining_limit=remaining_limit,
        )
        page = result["page"]
        selected_violations = result["selected_violations"]
        page_rows = int(page.get("rows_scanned") or 0)
        report["pages_scanned"] += 1
        report["rows_scanned"] += page_rows
        report["last_cursor"] = str(page.get("next_cursor") or "")

        for violation in selected_violations:
            blocker_code = str(
                violation.get("expected_blocker_code") or "unknown"
            )
            blocker_counts[blocker_code] += 1
            if len(samples) < args.sample_limit:
                samples.append(_public_violation(violation))

        report["violations_found"] += len(selected_violations)
        for outcome, count in result["applied"].items():
            report["applied"][outcome] = report["applied"].get(outcome, 0) + count
        report["expected_blocker_counts"] = dict(blocker_counts)
        report["samples"] = samples

        _emit_progress(
            {
                "event": "serving_recompute_stream_page",
                "apply": bool(args.apply),
                "page": report["pages_scanned"],
                "rows_scanned_total": report["rows_scanned"],
                "page_rows": page_rows,
                "page_violations": len(page.get("violations") or []),
                "selected_violations_total": report["violations_found"],
                "applied": report["applied"],
                "next_cursor": report["last_cursor"],
                "done": bool(page.get("done")),
            }
        )

        if page.get("done") or page_rows <= 0:
            report["completed_scan"] = True
            break
        cursor = report["last_cursor"]

    if args.apply and args.postcheck:
        postcheck_args = argparse.Namespace(**vars(args))
        postcheck_args.apply = False
        postcheck_args.limit = args.postcheck_limit
        postcheck_args.postcheck = False
        postcheck = await _drive_stream_pages(postcheck_args, db=db)
        report["postcheck"] = {
            "remaining_violations": postcheck["violations_found"],
            "remaining_samples": postcheck["samples"],
            "completed_scan": postcheck["completed_scan"],
        }
    return report


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    if args.apply and args.confirm != CONFIRM_TOKEN:
        raise SystemExit(f"--apply requires --confirm {CONFIRM_TOKEN}")

    if args.stream_pages:
        return await _drive_stream_pages(args, db=db)

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
            "samples": [
                _public_violation(row)
                for row in violations[: args.sample_limit]
            ],
            "applied": {
                "applied_audit_state": 0,
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
                    "remaining_samples": [
                        _public_violation(row)
                        for row in remaining[: args.sample_limit]
                    ],
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
    parser.add_argument(
        "--stream-pages",
        action="store_true",
        help=(
            "Scan one audit page per DB connection and apply each page before "
            "advancing the cursor. Useful over flaky Railway public proxy links."
        ),
    )
    parser.add_argument(
        "--page-retries",
        type=int,
        default=2,
        help="Retries per streamed audit page before failing. Default 2.",
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
