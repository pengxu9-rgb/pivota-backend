"""Which URL a seed serves, and when two URLs are the same destination. No I/O, no DB.

`canonical_url` is the SERVED URL (`services.external_seed_destination_liveness.destination_of`
reads it first): the refresh, the liveness sweep and the readiness gate all judge that page,
while the buyer's click is minted from `destination_url`. Every writer that stores a canonical
beside a destination -- the seed refresh, seed creation, CSV import, preview, and the catalog
enrichment agent's seed upsert -- goes through these, so the rule is one function, not five.
Dependency-free on purpose: the enrichment agent imports it without pulling in routes or the
liveness machinery.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple
from urllib.parse import urlsplit


def destination_key(url: Optional[str]) -> Optional[Tuple[Any, ...]]:
    text = str(url or "").strip().rstrip("/")
    if not text:
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return (text,)
    host = (parts.hostname or "").lower()
    if not host:
        return (text,)
    if host.startswith("www."):
        host = host[len("www."):]
    return (parts.scheme.lower(), host, port, parts.path.rstrip("/"), parts.query, parts.fragment)


def same_destination(fetched: Optional[str], served: Optional[str]) -> bool:
    """Is `fetched` the same destination as `served` (the URL the serving lane hands out)?

    Compared after trimming and dropping a trailing slash — the two columns are written by
    different code paths and differ cosmetically far more often than they differ in substance.
    The HOST is compared case-insensitively and without a leading `www.`: the refresh writes
    `canonical_url` from the page it fetched, and a store whose canonical tag names `www.`
    turned a bare-domain `destination_url` into a permanent mismatch -- every later refresh
    was `not_read`, so the row could never be re-read again. Measured 2026-09-26: 200
    gate-fresh seeds (all 182 dodoskin rows, 8 eyurs incl. the Round Lab sunscreen whose stale
    16.0 started #2340). Everything else -- scheme, port, path, query, fragment -- is
    deliberately NOT normalised: on a storefront a differing query string can select a
    different variant, and a different path is a different product (fenty's canonical names a
    sibling shade's handle), so treating those as the same URL would reintroduce exactly the
    mis-attribution this guard exists to prevent.
    """
    a = destination_key(fetched)
    return a is not None and a == destination_key(served)


def canonical_for_destination(dest: Optional[str], *candidates: Optional[str]) -> Optional[str]:
    """The canonical_url to store beside `dest`: the first candidate that IS `dest`, else `dest`.

    For the writers that set `destination_url` in the same statement (seed creation, CSV import)
    and for the preview that shows what creation would store. `canonical_url` is the served URL
    (`destination_of` reads it first) and the click is minted from `destination_url`, so any
    stored canonical that is not the same destination (`same_destination`) makes the seed serve
    one page and send buyers to another, and locks it out of every refresh. Candidates are the
    page's canonical tag, an operator-supplied value, or the row's existing canonical -- each a
    claim to be checked, not a value to trust. Same rule the refresh applies in
    `_next_served_canonical`, minus the read requirement: these writers store `dest` itself.
    """
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and same_destination(dest, text):
            return text
    return dest
