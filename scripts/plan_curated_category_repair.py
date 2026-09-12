"""Produce a review-only category repair manifest from an existing catalog snapshot.

Input JSON array: {product_key,title,product_type?,category_path}. Output includes
expected old values for a later guarded writer. This command has no database,
network, --apply mode or runtime mutation. Use the same taxonomy as fresh feeds.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from services.curated_brand_feed import product_category_path


def plan_category_repair(rows: list[dict[str, Any]]) -> dict[str, Any]:
    changes = []
    seen = set()
    for row in rows:
        key = str(row.get("product_key") or "").strip()
        if not key:
            raise ValueError("every snapshot row must include product_key")
        if key in seen:
            raise ValueError(f"duplicate product_key in snapshot: {key}")
        seen.add(key)
        before = row.get("category_path")
        if not isinstance(before, str):
            continue  # scope is a misclassified curated beauty cohort, not unknown verticals
        after = product_category_path(title=row.get("title"), product_type=row.get("product_type"), fallback=before)
        if after != before:
            changes.append({"product_key": key, "expected_category_path": before,
                            "proposed_category_path": after, "title": row.get("title"),
                            "product_type": row.get("product_type"),
                            "reason": "shared_taxonomy_product_evidence"})
    return {"mode": "review_only", "scanned": len(rows), "proposed_changes": len(changes),
            "changes": sorted(changes, key=lambda r: r["product_key"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path, help="read-only catalog snapshot JSON array")
    args = parser.parse_args()
    rows = json.loads(args.file.read_text())
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("snapshot must be a JSON array of row objects")
    print(json.dumps(plan_category_repair(rows), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
