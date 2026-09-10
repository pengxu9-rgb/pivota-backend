#!/usr/bin/env python3
"""Seed / reconcile `category_taxonomy` — the one vocabulary both services read.

Idempotent. Run it after a taxonomy change in either repo; it upserts and reports what moved.

WHAT GOES IN, and why it is not just this repo's constants:

  leaves    every path in CATEGORY_PATTERNS (this repo's classifier) — 72
  interior  every strict ancestor of a leaf; recall admits these via #2122 but never scores them
  aliases   services/category_path_aliases.ALIASES — spellings measured in production that mean
            one of the above

PIVOTA-Agent's `src/services/beautyTaxonomy.js` is the OTHER author. It has THREE tables, and
saying "the two disagree on nothing" after checking two of them is exactly the mistake that merged
seven path families in production. Its 25 canonical paths and 22 aliases agree with this set; its
INTENTIONALLY_DISTINCT list is the third, and services/gateway_intentionally_distinct.py is
asserted against it at import. They will drift again — that is what
`taxonomy_code_vs_table_drift` is for, and why the gateway must be pointed at this table rather
than re-seeded from it.

NOT SEEDED IN THE MIGRATION, on purpose: a migration that wrote these rows would make the table
un-editable without another migration, and the point of moving the vocabulary into data is that a
category decision stops being a code deploy in two repositories.

  --dry-run   (default) report only
  --apply     write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from services.category_path_aliases import (  # noqa: E402
    ALIASES,
    ANCESTOR_NODES,
    TAXONOMY_GAPS,
    TAXONOMY_LEAVES,
)
from services.pdp_category_classifier import CATEGORY_PATTERNS  # noqa: E402

_LABELS = {path: label for label, path, _pattern in CATEGORY_PATTERNS}

_NOTES = {
    "beauty/skincare/tone/toner": (
        "Peer of cleansers/moisturizers, NOT nested under treat/. Google Product Taxonomy 5976 "
        "and Shopify hb-3-2-9-17 both make Toners & Astringents a direct child of Skin Care. "
        "Folding into treat/ recreates the serum+mask+exfoliant bucket behind the 2026-07-31 "
        "junk recall (PIVOTA-Agent beautyTaxonomy.js). Adopted here 2026-09-10."
    ),
}


def _desired() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(TAXONOMY_LEAVES):
        rows.append({
            "path": path,
            "label": _LABELS.get(path, path.rsplit("/", 1)[-1].replace("-", " ").title()),
            "is_leaf": True,
            "alias_of": None,
            "note": _NOTES.get(path),
        })
    for path in sorted(ANCESTOR_NODES):
        rows.append({
            "path": path,
            "label": path.rsplit("/", 1)[-1].replace("-", " ").title(),
            "is_leaf": False,
            "alias_of": None,
            "note": None,
        })
    for source in sorted(ALIASES):
        rows.append({
            "path": source,
            "label": _LABELS.get(ALIASES[source], source.rsplit("/", 1)[-1].replace("-", " ").title()),
            "is_leaf": False,
            "alias_of": ALIASES[source],
            "note": None,
        })
    # The invariants the table's CHECKs cannot express, asserted before anything is written.
    seen = [r["path"] for r in rows]
    assert len(seen) == len(set(seen)), "a path is defined twice"
    canonical = {r["path"] for r in rows if r["alias_of"] is None}
    dangling = sorted({r["alias_of"] for r in rows if r["alias_of"]} - canonical)
    assert not dangling, "alias target is not canonical: %s" % dangling
    aliases = {r["path"] for r in rows if r["alias_of"]}
    chained = sorted(r["path"] for r in rows if r["alias_of"] in aliases)
    assert not chained, "alias chain (one hop only): %s" % chained
    return rows


async def run_seed(db, apply: bool) -> dict:
    """The whole job, against an INJECTED connection.

    Separated from `main()` so a test can execute the apply path without connecting or
    disconnecting the shared global `database`. The first version could only be tested by running
    `main()`, which does both — and these Postgres gate files share one database, so a test that
    disconnects the global seam breaks whichever unrelated module runs next. It also meant the
    apply path went untested, which is how a NameError in the retraction branch shipped: the
    branch is unreachable under --dry-run.
    """
    rows = _desired()
    if True:
        existing = {
            r["path"]: dict(r)
            for r in (await db.fetch_all(
                "SELECT path, label, is_leaf, alias_of FROM category_taxonomy"
            ) or [])
        }
        wanted = {r["path"]: r for r in rows}
        to_insert = sorted(set(wanted) - set(existing))
        # A merge instruction this repo has retracted: the path is now a declared GAP but the table
        # still holds it as an alias saying "merge this". Computed in BOTH modes, because a dry run
        # that describes the table differently from the apply it previews is not a preview — with a
        # foreign canonical row present the two used to disagree (dry-run "delete: 3", apply
        # "deleted 0"). Only the EXECUTION is gated on --apply.
        retracted = sorted(
            path for path in existing
            if path in TAXONOMY_GAPS and existing[path].get("alias_of")
        )
        to_delete = sorted(set(existing) - set(wanted) - set(retracted))
        to_update = sorted(
            p for p in set(wanted) & set(existing)
            if (existing[p]["is_leaf"], existing[p]["alias_of"], existing[p]["label"])
            != (wanted[p]["is_leaf"], wanted[p]["alias_of"], wanted[p]["label"])
        )
        report = {
            "mode": "apply" if apply else "dry_run",
            "retracted_merge_instructions": retracted,
            # Reported, never performed: a canonical path this repo stopped knowing may be one the
            # gateway wrote, and deleting it would orphan its rows.
            "note": (
                "extra rows are reported, not deleted — they may belong to the other service; "
                "the exception is an alias row for a path now declared a GAP, which is retracted"
            ),
            "desired_rows": len(rows),
            "existing_rows": len(existing),
            "insert": len(to_insert),
            "update": len(to_update),
            "delete": len(to_delete),
            "delete_paths": to_delete[:20],
        }

        if apply:
            # Canonical rows FIRST: an alias inserted before its target violates the self-FK.
            for record in [r for r in rows if r["alias_of"] is None] + \
                          [r for r in rows if r["alias_of"]]:
                await db.execute(
                    """
                    INSERT INTO category_taxonomy (path, label, is_leaf, alias_of, note, updated_at)
                    VALUES (:path, :label, :is_leaf, :alias_of, :note, now())
                    ON CONFLICT (path) DO UPDATE SET
                      label = EXCLUDED.label,
                      is_leaf = EXCLUDED.is_leaf,
                      alias_of = EXCLUDED.alias_of,
                      note = COALESCE(EXCLUDED.note, category_taxonomy.note),
                      updated_at = now()
                    """,
                    record,
                )
            # Deletions are REPORTED, never performed. A path this repo stopped knowing about may
            # be one the gateway still writes; removing it would make its rows orphans, which is
            # the failure this table exists to prevent.
            #
            # ONE EXCEPTION, and it is the opposite risk: an ALIAS row for a path this repo now
            # declares a GAP. That row says "merge this", it was written by this seeder, and
            # leaving it means the gateway starts merging on it the moment it reads this table —
            # which is precisely how seven INTENTIONALLY_DISTINCT paths were collapsed on
            # 2026-09-10. Retracting a merge instruction cannot orphan a row; it only stops a
            # rewrite. Canonical rows are still never deleted.
            for path in retracted:
                await db.execute(
                    "DELETE FROM category_taxonomy WHERE path = :p AND alias_of IS NOT NULL",
                    {"p": path},
                )
            report["deleted"] = len(retracted)
        return report


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    await database.connect()
    try:
        report = await run_seed(database, apply=args.apply and not args.dry_run)
    finally:
        await database.disconnect()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
