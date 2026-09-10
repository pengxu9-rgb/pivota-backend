#!/usr/bin/env python3
"""Snap off-taxonomy `category_path` values onto the taxonomy leaf they meant.

WHAT IS BROKEN. 710 serving-eligible rows (6.8%, measured 2026-09-09) sit on 117 paths that are
neither a taxonomy leaf nor an ancestor of one. Such a row has NO category door: recall matches
`category_path LIKE 'beauty/skincare/treat/%'`, and #2122's ancestor admission needs the stored
path to be a strict ANCESTOR of the query prefix — a sibling typo is not an ancestor of anything.
315 toners sat on `beauty/skincare/tone/toner` against the real `beauty/skincare/treat/toner`.

WHAT THIS DOES. Rewrites `category_path` to the leaf from services.category_path_aliases, for rows
where that map has an honest answer (626 of the 710). The other 84 are TAXONOMY GAPS — nail polish,
oral care, supplements, accessories — and are deliberately LEFT ALONE, because filing a nail polish
under a blush leaf would make it findable by the wrong query, which is louder than not being
findable at all. They stay counted by `serving_eligible_off_taxonomy_path`.

WHAT IT DOES NOT TOUCH. `category_label`, `category_confidence`, and `category_label_source` are
left as they are on purpose. The source stamp is the only remaining evidence of WHICH lane wrote
the bad path (`taxonomy_reconciler_v1`, `codex_review_v1`, `reviewed_ext_seed_mirror` — none of
which exists in this repository); overwriting it would erase the audit trail for a cosmetic gain.

  --dry-run   (default) report only, no writes
  --apply     perform the update
  --limit N   cap rows considered

Read the module docstring of services/category_path_aliases.py before changing the map.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from services.category_path_aliases import (  # noqa: E402
    ALIASES,
    TAXONOMY_GAPS,
    TAXONOMY_LEAVES,
    resolve,
)


async def _candidates(limit: int | None) -> list[dict]:
    """Every serving-eligible row whose stored path is not a leaf and not an ancestor.

    Re-derived here rather than imported from catalog_invariant_checks so this script says out loud
    which rows it will touch, in the same terms the invariant counts them.
    """
    rows = await database.fetch_all(
        """
        SELECT cp.product_key, cp.category_path, cp.category_label_source
        FROM catalog_products cp
        JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
        WHERE ips.serving_eligible
          AND coalesce(btrim(cp.category_path), '') <> ''
        ORDER BY cp.product_key
        """
    )
    out = []
    for row in rows or []:
        record = dict(row)
        path = (record["category_path"] or "").strip()
        if path in TAXONOMY_LEAVES:
            continue
        target = resolve(path)
        if target is None:
            continue  # a real leaf-ancestor, or a declared gap — neither is ours to touch
        if target == path:
            continue
        record["target"] = target
        out.append(record)
        if limit and len(out) >= limit:
            break
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    await database.connect()
    try:
        rows = await _candidates(args.limit)
        by_target = Counter(r["target"] for r in rows)
        by_source = Counter(r["category_label_source"] or "(null)" for r in rows)
        report = {
            "mode": "apply" if apply else "dry_run",
            "rows_to_update": len(rows),
            "by_target": dict(by_target.most_common()),
            "by_source": dict(by_source.most_common()),
            "alias_entries": len(ALIASES),
            "declared_gaps": len(TAXONOMY_GAPS),
        }

        if apply:
            updated = 0
            for record in rows:
                # RETURNING + fetch_val: `databases.execute()` gives NO rowcount for an UPDATE, so
                # counting its return would count statements, not rows.
                got = await database.fetch_val(
                    """
                    UPDATE catalog_products
                    SET category_path = :target
                    WHERE product_key = :key AND category_path = :old
                    RETURNING 1
                    """,
                    {
                        "target": record["target"],
                        "key": record["product_key"],
                        "old": record["category_path"],
                    },
                )
                if got:
                    updated += 1
            report["rows_updated"] = updated
            # Re-measure rather than assert success: the whole point of this work is that a lane
            # reporting its own intentions is not evidence.
            remaining = await _candidates(None)
            report["repairable_remaining"] = len(remaining)

        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        await database.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
