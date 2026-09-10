"""Read the ONE category vocabulary — the `category_taxonomy` table — with a process cache.

WHY A TABLE. `catalog_products.category_path` is written and read by two repositories over one
database: this repo's `CATEGORY_PATTERNS` and PIVOTA-Agent's `src/services/beautyTaxonomy.js`.
Each kept its own vocabulary and neither imported the other, so on 2026-09-10 they disagreed about
where a toner lives — the gateway wrote 315 rows to `beauty/skincare/tone/toner` on purpose, this
repo's taxonomy named `treat/toner` and could not reach them, and this repo's invariant reported
them as corruption. Two dictionaries over one column turn every disagreement into a data defect.

WHAT THIS IS AND IS NOT. This is the shared RUNTIME vocabulary. It is not a replacement for
`CATEGORY_PATTERNS`, which maps product TEXT to a path — that is classification, it is regex, and
it is this repo's business. The taxonomy is the set of legal paths, and it is everyone's.

Code and table WILL drift, because both services can be deployed independently of a row change.
That is not designed away, it is DETECTED: `taxonomy_code_vs_table_drift` in
services/catalog_invariant_checks.py reports any path the code knows and the table does not, or
the reverse. An undetected disagreement is what produced this table; a detected one is a ticket.

FAILURE MODE IS EXPLICIT. `load()` raises if the table is missing or empty rather than falling
back to the in-code constants. A silent fallback would make a service that cannot see the shared
vocabulary behave exactly like one that agrees with it — the precise shape of defect this whole
line of work exists to remove.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_CACHE: Optional[Dict[str, Any]] = None


class TaxonomyUnavailable(RuntimeError):
    """The shared vocabulary could not be read. Never swallowed into a default."""


async def load(db: Any, *, refresh: bool = False) -> Dict[str, Any]:
    """Return {'leaves': frozenset, 'interior': frozenset, 'aliases': dict, 'labels': dict}."""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    rows = await db.fetch_all(
        "SELECT path, label, is_leaf, alias_of FROM category_taxonomy ORDER BY path"
    )
    records = [dict(r) for r in rows or []]
    if not records:
        raise TaxonomyUnavailable(
            "category_taxonomy is empty or absent — run scripts/seed_category_taxonomy.py. "
            "Refusing to fall back to in-code constants: a service that cannot see the shared "
            "vocabulary must not look like one that agrees with it."
        )
    leaves, interior, aliases, labels = set(), set(), {}, {}
    for record in records:
        path = record["path"]
        labels[path] = record["label"]
        if record["alias_of"]:
            aliases[path] = record["alias_of"]
        elif record["is_leaf"]:
            leaves.add(path)
        else:
            interior.add(path)
    # ONE HOP. An alias whose target is itself an alias would need a recursive resolve on a read
    # path and can cycle; the seeder refuses to write one, and this refuses to serve one.
    bad = sorted(a for a, t in aliases.items() if t in aliases)
    if bad:
        raise TaxonomyUnavailable("alias chain in category_taxonomy (one hop only): %s" % bad)
    orphan = sorted(t for t in aliases.values() if t not in leaves and t not in interior)
    if orphan:
        raise TaxonomyUnavailable("alias points at a path that is not canonical: %s" % orphan)
    _CACHE = {
        "leaves": frozenset(leaves),
        "interior": frozenset(interior),
        "aliases": dict(aliases),
        "labels": dict(labels),
    }
    return _CACHE


def reset_cache() -> None:
    """Test helper, and the hook a future row-change notification would call."""
    global _CACHE
    _CACHE = None
