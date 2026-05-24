#!/usr/bin/env python3
"""Review-gated PDP identity graph repair.

Dry-run is read-only. Apply mode writes only high-confidence deterministic
identity edges: product_group_members, external_product_seeds.attached_product_key,
and catalog_products.pdp_scope. It never writes external_product_seeds.seed_data
or catalog_products.product_payload.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from services.pdp_identity_recovery import (  # noqa: E402
    DEFAULT_PROPOSER,
    apply_recovery_proposals,
    build_recovery_proposals,
)

CONFIRM_TOKEN = "APPLY_PDP_IDENTITY_RECOVERY"


def _parse_allowlist(values: Iterable[str] | None) -> list[str]:
    parsed: set[str] = set()
    for value in values or []:
        for item in str(value).split(","):
            item = item.strip()
            if item:
                parsed.add(item)
    return sorted(parsed)


def _proposal_sort_key(proposal: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(proposal.get("reason") or ""),
        str(proposal.get("product_group_id") or ""),
        str(proposal.get("product_key") or ""),
        str(proposal.get("source_product_id") or ""),
        str(proposal.get("seed_id") or ""),
    )


def _count_by(proposals: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for proposal in proposals:
        value = str(proposal.get(key) or "")
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def select_recovery_proposals(
    proposals: Iterable[dict[str, Any]],
    *,
    reason_allowlist: Iterable[str] | None = None,
    action_allowlist: Iterable[str] | None = None,
    max_apply: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a deterministic, reviewable subset of high-confidence proposals."""
    reasons = set(_parse_allowlist(reason_allowlist))
    actions = set(_parse_allowlist(action_allowlist))
    high_confidence = sorted(
        [proposal for proposal in proposals if proposal.get("high_confidence")],
        key=_proposal_sort_key,
    )
    selected: list[dict[str, Any]] = []
    unselected: list[dict[str, Any]] = []
    for proposal in high_confidence:
        if reasons and proposal.get("reason") not in reasons:
            unselected.append({**proposal, "selection_skip_reason": "reason_not_allowed"})
            continue
        if actions and proposal.get("action") not in actions:
            unselected.append({**proposal, "selection_skip_reason": "action_not_allowed"})
            continue
        selected.append(proposal)

    selected_before_truncation = len(selected)
    if max_apply is not None:
        if max_apply < 0:
            raise ValueError("--max-apply must be >= 0")
        if len(selected) > max_apply:
            unselected.extend(
                {**proposal, "selection_skip_reason": "max_apply_cap"}
                for proposal in selected[max_apply:]
            )
            selected = selected[:max_apply]

    selection = {
        "reason_allowlist": sorted(reasons),
        "action_allowlist": sorted(actions),
        "max_apply": max_apply,
        "high_confidence_count": len(high_confidence),
        "selected_count": len(selected),
        "selected_before_truncation": selected_before_truncation,
        "selection_truncated": selected_before_truncation != len(selected),
        "unselected_high_confidence_count": len(unselected),
        "selected_counts": _count_by(selected, "action"),
        "selected_reason_counts": _count_by(selected, "reason"),
        "unselected_reason_counts": _count_by(unselected, "reason"),
        "unselected_skip_counts": _count_by(unselected, "selection_skip_reason"),
        "selected_proposals": selected,
        "unselected_high_confidence": unselected,
    }
    return selected, selection


async def _run(args: argparse.Namespace) -> int:
    if args.apply and args.dry_run:
        raise SystemExit("--apply and --dry-run are mutually exclusive")
    if not args.apply and not args.dry_run:
        raise SystemExit("Choose --dry-run or --apply")
    if args.apply and args.confirm != CONFIRM_TOKEN:
        raise SystemExit(f"--apply requires --confirm {CONFIRM_TOKEN}")

    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        report = await build_recovery_proposals(limit=args.limit, offset=args.offset)
        selected, selection = select_recovery_proposals(
            report.get("proposals", []),
            reason_allowlist=args.reason_allowlist,
            action_allowlist=args.action_allowlist,
            max_apply=args.max_apply,
        )
        report["dry_run"] = bool(args.dry_run)
        report["apply"] = bool(args.apply)
        report["proposer"] = args.proposer
        report["high_confidence_count"] = selection["high_confidence_count"]
        report["selection"] = selection
        if args.apply:
            report["apply_result"] = await apply_recovery_proposals(
                selected,
                proposer=args.proposer,
            )
        encoded = json.dumps(report, indent=2, sort_keys=True, default=str)
        if args.export_path:
            export_path = Path(args.export_path)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            export_path.write_text(f"{encoded}\n", encoding="utf-8")
        print(encoded)
        return 0
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--proposer", default=DEFAULT_PROPOSER)
    parser.add_argument("--reason-allowlist", action="append", default=[])
    parser.add_argument("--action-allowlist", action="append", default=[])
    parser.add_argument("--max-apply", type=int)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--export-path")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
