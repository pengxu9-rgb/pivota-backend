#!/usr/bin/env python3
"""Step-5 Lane 3 — same-domain campaign-clone collapse (keep-one, reviewed).

Third apply cut of docs/plans/adr011_step5_catalog_identity_reconciliation.md:
same-merchant dup groups on ONE domain with several distinct normalized URLs —
the campaign-PDP pattern (dozens of ad-specific slugs like
`biodance.com/products/0627_cm_2544_pp_aloneimg_jhp1` for one product).

Unlike Lane 2 (same literal PDP; mechanical), distinct URLs can in principle
be distinct products sharing brand+title, so THIS lane's proposal is meant to
be reviewed group-by-group and is REVIEWER-EDITABLE:

  - deleting a group from the reviewed file leaves that group untouched;
  - editing a group's "keeper" to a different member is honored at apply
    (the drift fingerprint covers merchant + content_key + the member set,
    NOT the keeper, and the reviewed keeper is validated to be a live member).

The DEFAULT keeper is still serving-aligned (pick_canonical), but each row
carries slug evidence for the review: the slug's trigram similarity to the
slugified title, and whether it matches campaign-marker patterns (date-code
prefixes, tracking suffixes, split-test tokens). Groups whose default keeper
looks like a campaign URL while a cleaner member exists are flagged
`keeper_url_looks_campaign` — those are the ones worth editing.

Apply mechanics (suppression + seed deactivation + post-checks incl. the
keeper-backing guard) are shared with Lane 2 — see
scripts/step5_lane2_same_url_dedup.py.

  Dry-run:
    python3 scripts/step5_lane3_campaign_clone_dedup.py --output-json reports/step5/lane3_proposal.json
  Apply the reviewed (possibly edited) file:
    python3 scripts/step5_lane3_campaign_clone_dedup.py --apply --proposal reports/step5/lane3_proposal.json

Revert (by run): same recipe as Lane 2, reason 'step5_campaign_clone_dup'.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.step5_lane2_same_url_dedup import (  # noqa: E402
    DEACTIVATE_SEEDS_SQL,
    DETAIL_SQL,
    ORPHANED_KEEPERS_SQL,
    SUPPRESS_SQL,
    _connect_with_retry,
    choose_keeper,
)
from scripts.step5_working_set import (  # noqa: E402
    ORPHAN_MIRRORS_SQL,
    WORKING_ROWS_SQL,
    build_report,
)
from services.pdp_matcher.deterministic import (  # noqa: E402
    normalize_canonical_url,
    trigram_similarity,
)

SUPPRESSION_REASON = "step5_campaign_clone_dup"

# Campaign-URL tells, matched against the slug (last path segment):
#   0627_cm_..., 0704_cm_a_...     date-code + campaign prefixes
#   ..._jhp1, ..._ahj1, ..._ppd    creator/split-test suffixes
#   ...-99, ...-100                numbered clone suffixes
#   utm handled upstream by normalize_canonical_url (querystring stripped)
CAMPAIGN_MARKER_RE = re.compile(
    r"(^\d{3,4}_)|(_cm_)|(_pp[a-z]?\d*$)|(_[a-z]{2,4}\d+$)|(-\d{1,3}$)|(_ttest)|(_test\b)",
    re.IGNORECASE,
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify_title(title: Optional[str]) -> str:
    return _NON_ALNUM.sub("-", str(title or "").lower()).strip("-")


def url_slug(url: Optional[str]) -> str:
    normalized = normalize_canonical_url(url)
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1]


def slug_evidence(row: Dict[str, Any]) -> Dict[str, Any]:
    slug = url_slug(row.get("canonical_url"))
    return {
        "slug": slug,
        "title_slug_similarity": round(
            trigram_similarity(slug, slugify_title(row.get("title"))), 3
        ),
        "campaign_marker": bool(CAMPAIGN_MARKER_RE.search(slug)),
    }


def _row_out(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "product_key": row.get("product_key"),
        "platform": row.get("platform"),
        "canonical_url": row.get("canonical_url"),
        "has_signature": bool(row.get("pivota_signature_id")),
        "group_is_primary": bool(row.get("group_is_primary")),
        "payload_bytes": int(row.get("payload_bytes") or 0),
        "created_at": str(row.get("created_at") or ""),
        "source_ref": row.get("source_ref"),
    }
    out.update(slug_evidence(row))
    return out


def cleanest_member(details: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The member whose URL looks most like the product's canonical PDP:
    no campaign marker first, then highest slug/title similarity, then the
    stable product_key order."""
    def key(d: Dict[str, Any]) -> Tuple[int, float, str]:
        ev = slug_evidence(d)
        return (
            1 if ev["campaign_marker"] else 0,
            -ev["title_slug_similarity"],
            d.get("product_key") or "",
        )

    return sorted(details, key=key)[0]


def build_proposal(
    lane3_groups: List[Dict[str, Any]],
    detail_by_key: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Pure: lane-3 groups + per-row detail -> reviewer-editable proposal."""
    groups_out: List[Dict[str, Any]] = []
    flagged = 0
    skipped = 0
    for g in lane3_groups:
        details = [
            detail_by_key[r["product_key"]]
            for r in g["rows"]
            if r["product_key"] in detail_by_key
        ]
        if len(details) != len(g["rows"]) or len(details) < 2:
            skipped += 1
            continue
        keeper = choose_keeper(details)
        keeper_ev = slug_evidence(keeper)
        clean = cleanest_member(details)
        looks_campaign = bool(
            keeper_ev["campaign_marker"]
            and clean["product_key"] != keeper["product_key"]
        )
        if looks_campaign:
            flagged += 1
        losers = [d for d in details if d["product_key"] != keeper["product_key"]]
        groups_out.append(
            {
                "merchant_id": g["merchant_id"],
                "content_key": g["content_key"],
                "keeper": _row_out(keeper),
                "keeper_url_looks_campaign": looks_campaign,
                "cleanest_member_product_key": clean["product_key"],
                "losers": [_row_out(d) for d in losers],
            }
        )
    return {
        "policy": (
            "default keeper = pick_canonical (serving-aligned); reviewer may "
            "edit 'keeper' to any member or delete groups before apply"
        ),
        "suppression_reason": SUPPRESSION_REASON,
        "summary": {
            "groups": len(groups_out),
            "losers": sum(len(g["losers"]) for g in groups_out),
            "keeper_url_looks_campaign": flagged,
            "skipped_inconsistent": skipped,
        },
        "groups": groups_out,
    }


def _member_fingerprint(g: Dict[str, Any]) -> Tuple[str, str, Tuple[str, ...]]:
    members = sorted(
        [g["keeper"]["product_key"], *(l["product_key"] for l in g["losers"])]
    )
    return (g["merchant_id"], g["content_key"], tuple(members))


def match_proposal(
    fresh: Dict[str, Any], reviewed: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """Pure: honor reviewer edits. A reviewed group applies when its member
    set still matches the freshly derived group (keeper NOT part of the
    fingerprint) AND its keeper — possibly reviewer-overridden — is one of
    those members; the applied group uses the REVIEWED keeper. Everything
    else drifts."""
    fresh_by_fp = {_member_fingerprint(g): g for g in fresh["groups"]}
    to_apply: List[Dict[str, Any]] = []
    drifted: List[Tuple[str, str]] = []
    for g in reviewed["groups"]:
        fp = _member_fingerprint(g)
        fresh_g = fresh_by_fp.get(fp)
        keeper_key = g["keeper"]["product_key"]
        if fresh_g is None or keeper_key not in fp[2]:
            drifted.append((g["merchant_id"], g["content_key"]))
            continue
        details = {
            d["product_key"]: d
            for d in [fresh_g["keeper"], *fresh_g["losers"]]
        }
        keeper = details[keeper_key]
        to_apply.append(
            {
                "merchant_id": g["merchant_id"],
                "content_key": g["content_key"],
                "keeper": keeper,
                "losers": [d for k, d in sorted(details.items()) if k != keeper_key],
            }
        )
    return to_apply, drifted


def build_metadata(run_id: str, group: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "script": "step5_lane3_campaign_clone_dedup",
            "run_id": run_id,
            "plan": "docs/plans/adr011_step5_catalog_identity_reconciliation.md",
            "keeper_product_key": group["keeper"]["product_key"],
            "content_key": group["content_key"],
        }
    )


async def _derive_fresh_proposal(conn) -> Dict[str, Any]:
    working = [dict(r) for r in await conn.fetch(WORKING_ROWS_SQL)]
    orphans = [dict(r) for r in await conn.fetch(ORPHAN_MIRRORS_SQL)]
    report = build_report(working, orphans)
    lane3 = report["lanes"].get("lane3_campaign_clones", [])
    keys = sorted({r["product_key"] for g in lane3 for r in g["rows"]})
    detail = [dict(r) for r in await conn.fetch(DETAIL_SQL, keys)] if keys else []
    return build_proposal(lane3, {d["product_key"]: d for d in detail})


async def _run(args: argparse.Namespace) -> int:
    conn = await _connect_with_retry(os.environ["DATABASE_URL"])
    try:
        fresh = await _derive_fresh_proposal(conn)
        print(json.dumps({"summary": fresh["summary"]}, indent=2))

        if not args.apply:
            if args.output_json:
                os.makedirs(os.path.dirname(args.output_json) or ".", exist_ok=True)
                with open(args.output_json, "w") as fh:
                    json.dump(fresh, fh, indent=2, default=str)
                print(f"proposal -> {args.output_json}", file=sys.stderr)
            print("DRY-RUN — no changes written. Review (and optionally edit "
                  "keepers / delete groups), then re-run with --apply --proposal <file>.")
            return 0

        with open(args.proposal) as fh:
            reviewed = json.load(fh)
        to_apply, drifted = match_proposal(fresh, reviewed)
        print(
            f"apply: {len(to_apply)} group(s) match; "
            f"{len(drifted)} drifted and are SKIPPED: {drifted[:10]}"
        )
        if not to_apply:
            print("Nothing to apply.")
            return 0

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suppressed = 0
        seed_ids: List[str] = []
        async with conn.transaction():
            for g in to_apply:
                loser_keys = [l["product_key"] for l in g["losers"]]
                loser_refs = [
                    l["source_ref"] for l in g["losers"] if l.get("source_ref")
                ]
                result = await conn.execute(
                    SUPPRESS_SQL,
                    loser_keys,
                    SUPPRESSION_REASON,
                    build_metadata(run_id, g),
                    g["keeper"]["product_key"],
                )
                suppressed += int(str(result).split()[-1] or 0)
                deactivated = await conn.fetch(
                    DEACTIVATE_SEEDS_SQL,
                    loser_refs,
                    loser_keys,
                    g["keeper"]["product_key"],
                    g["keeper"].get("source_ref"),
                )
                seed_ids.extend(str(r["id"]) for r in deactivated)

        orphaned_keepers = await conn.fetchval(
            ORPHANED_KEEPERS_SQL, [g["keeper"]["product_key"] for g in to_apply]
        )
        empty = await conn.fetchval(
            """
            SELECT COUNT(*) FROM (
              SELECT 1 FROM catalog_products
              WHERE content_key = ANY($1::text[])
              GROUP BY merchant_id, content_key
              HAVING COUNT(*) FILTER (WHERE suppression_reason IS NULL) = 0
            ) t
            """,
            [g["content_key"] for g in to_apply],
        )
        print(
            json.dumps(
                {
                    "applied_groups": len(to_apply),
                    "rows_suppressed": suppressed,
                    "seeds_deactivated": len(seed_ids),
                    "run_id": run_id,
                    "reason": SUPPRESSION_REASON,
                    "groups_left_empty_post_check": empty,
                    "keepers_orphaned_post_check": orphaned_keepers,
                    "deactivated_seed_ids": seed_ids,
                },
                indent=2,
            )
        )
        return 0 if (empty == 0 and orphaned_keepers == 0) else 1
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Apply a reviewed proposal (default is dry-run)")
    parser.add_argument("--proposal",
                        help="Reviewed proposal file (required with --apply)")
    parser.add_argument("--output-json",
                        help="Dry-run: write the full proposal to this path")
    args = parser.parse_args()
    if args.apply and not args.proposal:
        parser.error("--apply requires --proposal <reviewed proposal file>")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
