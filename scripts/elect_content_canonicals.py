#!/usr/bin/env python3
"""Elect ONE canonical PDP URL per content_key, and keep it elected.

474 content_keys serve the same product under more than one sitemap-eligible
URL (551 redundant URLs; groups of 2, 3, 4, 5 and 7). Every sibling returns HTTP
200 with the same title, the same Product JSON-LD, and a SELF-referential
``<link rel="canonical">``. This sweep decides which sibling holds the URL and
writes the answer to ``content_canonical_election``, from where the sitemap and
the gateway both READ it — see services/content_canonical_election for why one
stored value beats two computed rules.

TWO MODES.

1. SEEDING (once, at rollout). ``--seed-from-sitemap <path-or-url>`` imports the
   winner from the URLs pivota-agent-ui is ALREADY advertising, so no indexed
   URL moves on the day this ships. That import is exact rather than
   approximate: all 474 duplicate groups have exactly one member in the live
   sitemap. Run this BEFORE the first plain sweep — a plain sweep on an empty
   table would elect lexicographically and swap 183 already-indexed URLs, which
   is precisely the regression pivota-agent-ui#280 was written to prevent.

2. STEADY STATE (scheduled). No flags. Elects newly-arrived content_keys and
   re-elects only where the stored winner has stopped being a candidate. A
   steady-state run over an unchanged corpus writes ZERO rows; if it reports
   many replacements, something upstream is flapping ``renderable`` and that is
   the bug to chase, not this.

Dry-run is the default and writes nothing:

    DATABASE_URL=... python scripts/elect_content_canonicals.py
    DATABASE_URL=... python scripts/elect_content_canonicals.py \
        --seed-from-sitemap https://agent.pivota.cc/sitemap-products.xml
    DATABASE_URL=... python scripts/elect_content_canonicals.py --apply

Every replacement of an existing winner is printed individually, never summed:
each one is a live URL moving, and the whole point of the stickiness rule is
that this should be rare enough to read line by line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import urllib.request
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.content_canonical_election import (  # noqa: E402
    KEEPER_SIGS_SQL,
    UPSERT_ELECTION_SQL,
    candidates_query,
    plan_elections,
)

logger = logging.getLogger("elect_content_canonicals")

STORED_ELECTIONS_SQL = "SELECT content_key, canonical_sig_id FROM content_canonical_election"

_SITEMAP_PRODUCT_PREFIX = "https://agent.pivota.cc/products/"
_LOC_RE = re.compile(r"<loc>([^<]*)</loc>")
_XML_ENTITIES = (
    ("&apos;", "'"),
    ("&quot;", '"'),
    ("&gt;", ">"),
    ("&lt;", "<"),
    # LAST, always: unescaping &amp; first would turn "&amp;lt;" into "<".
    ("&amp;", "&"),
)


def parse_sitemap_product_ids(xml: str) -> Set[str]:
    """The ids the live sitemap advertises — the incumbents.

    Undoes both writers in the order pivota-agent-ui applies them
    (``productUrlEntries`` percent-encodes the id, then ``buildSitemapUrlsetXml``
    escapes the ``<loc>``), mirroring ``parseSitemapProductIds`` in
    scripts/sitemap_lib.mjs. In practice only the apostrophe needs the entity
    pass — encodeURIComponent already consumes & < > " — but sig ids are hex, so
    for THIS population both passes are no-ops. They are here so the two parsers
    stay recognisably the same function.
    """
    ids: Set[str] = set()
    for match in _LOC_RE.finditer(xml or ""):
        loc = match.group(1).strip()
        for entity, char in _XML_ENTITIES:
            loc = loc.replace(entity, char)
        if not loc.startswith(_SITEMAP_PRODUCT_PREFIX):
            continue
        raw = loc[len(_SITEMAP_PRODUCT_PREFIX):]
        if not raw:
            continue
        ids.add(unquote(raw))
    return ids


def load_sitemap(source: str) -> Set[str]:
    if source.startswith("http://") or source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=60) as response:
            xml = response.read().decode("utf-8", errors="replace")
    else:
        with open(source, "r", encoding="utf-8") as handle:
            xml = handle.read()
    ids = parse_sitemap_product_ids(xml)
    if not ids:
        # Refuse to seed off an empty read. An unreachable URL or a wrong path
        # would otherwise look like "no incumbents" and silently degrade the
        # seed run into the lexicographic sweep it exists to prevent.
        raise SystemExit(
            f"--seed-from-sitemap read ZERO product URLs from {source!r}. "
            "Seeding from an empty incumbent set would elect lexicographically "
            "and swap already-indexed URLs; refusing."
        )
    return ids


async def run_election(
    *,
    apply: bool,
    incumbents: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    candidate_rows = await database.fetch_all(candidates_query())
    candidates_by_content_key: Dict[str, List[str]] = defaultdict(list)
    for row in candidate_rows:
        candidates_by_content_key[row["content_key"]].append(row["pivota_signature_id"])

    stored_rows = await database.fetch_all(STORED_ELECTIONS_SQL)
    stored = {r["content_key"]: r["canonical_sig_id"] for r in stored_rows}

    keeper_rows = await database.fetch_all(KEEPER_SIGS_SQL)
    keepers = {r["content_key"]: r["keeper_sig_id"] for r in keeper_rows}

    planned = plan_elections(
        candidates_by_content_key=dict(candidates_by_content_key),
        stored_by_content_key=stored,
        incumbents=incumbents,
        keeper_by_content_key=keepers,
    )
    replacements = [e for e in planned if e.replaced]
    duplicate_groups = sum(
        1 for sigs in candidates_by_content_key.values() if len(set(sigs)) > 1
    )

    # THE NUMBER THAT ACTUALLY MATTERS, and the one `replacements` cannot give.
    #
    # `replaced` is the PREVIOUSLY STORED winner, so on a seed run — the only
    # run where hundreds of URLs move at once — the table is empty and
    # `replacements` is structurally ZERO no matter how much moves. The runbook
    # called `replacements: 0` its acceptance criterion, which was therefore a
    # gate that could never fail. Measured against prod, the seed moves 340 of
    # 4,528 sitemap URLs (7.5%) and `replacements` still reports 0.
    #
    # This counts what the operator is actually deciding: elected winners that
    # differ from the URL the live sitemap advertises today. Only computable
    # when seeded, because the sitemap is where "what we advertise today" lives.
    incumbent_set = set(incumbents or ())
    moved_from_sitemap = []
    if incumbent_set:
        elected_now = {e.content_key: e.canonical_sig_id for e in planned}
        for content_key, sigs in candidates_by_content_key.items():
            advertised = [s for s in set(sigs) if s in incumbent_set]
            elected = elected_now.get(content_key, stored.get(content_key))
            if len(advertised) == 1 and elected and elected != advertised[0]:
                moved_from_sitemap.append(
                    {"content_key": content_key, "from": advertised[0], "to": elected}
                )

    report: Dict[str, Any] = {
        "apply": apply,
        "seeded_from_sitemap": bool(incumbents),
        "sitemap_incumbents": len(incumbents or ()),
        "candidate_rows": len(candidate_rows),
        "content_keys": len(candidates_by_content_key),
        "duplicate_groups": duplicate_groups,
        "redundant_urls": sum(
            len(set(sigs)) - 1 for sigs in candidates_by_content_key.values()
        ),
        "already_correct": len(candidates_by_content_key) - len(planned),
        "new_elections": len(planned) - len(replacements),
        "replacements": len(replacements),
        # Enumerated, never just counted: each is a live URL moving.
        "replaced": [
            {
                "content_key": e.content_key,
                "from": e.replaced,
                "to": e.canonical_sig_id,
                "reason": e.election_reason,
            }
            for e in replacements
        ],
        # THE acceptance number for a seed run. See the note above.
        "moved_from_sitemap": len(moved_from_sitemap),
        "moved_from_sitemap_detail": moved_from_sitemap[:50],
        "reasons": {},
    }
    for election in planned:
        report["reasons"][election.election_reason] = (
            report["reasons"].get(election.election_reason, 0) + 1
        )

    if apply and planned:
        async with database.transaction():
            for election in planned:
                await database.execute(
                    UPSERT_ELECTION_SQL,
                    {
                        "content_key": election.content_key,
                        "canonical_sig_id": election.canonical_sig_id,
                        "election_reason": election.election_reason,
                    },
                )
        report["written"] = len(planned)
    else:
        report["written"] = 0

    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the elections. Default is a dry run that writes nothing.",
    )
    parser.add_argument(
        "--seed-from-sitemap",
        metavar="PATH_OR_URL",
        help=(
            "Import incumbency from a live sitemap-products.xml. Use ONCE at "
            "rollout, before the first plain sweep."
        ),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    incumbents = load_sitemap(args.seed_from_sitemap) if args.seed_from_sitemap else None

    await database.connect()
    try:
        report = await run_election(apply=args.apply, incumbents=incumbents)
    finally:
        await database.disconnect()

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
