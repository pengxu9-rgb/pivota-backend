"""Quarantined-source exclusion for the `find_products` connected/seed lane.

Why this exists: on 2026-07-28 the public UNAUTHENTICATED ACP feed served seven
Mintree rows priced 847-3927.70 and labelled ``"USD"``. Mintree is an Indian
store (``mintree.us``, canonical ``vmintree.in``, ``country=IN``,
``currency=INR``): those were rupee prices published as US dollars.

The containment was never broken. Measured the same day: canonical index feed
0 rows, sitemap 0, PDP 404s correctly, **connected lane 20**. The block lives on
the index/serving path, and ``find_products`` routes to the
``connected_catalog`` / external-seed lane, which is gated only by
``_is_product_sellable`` (status + orderable). Suppression, quarantine, serving
eligibility and trust are all invisible to it -- a fifth storefront on the same
catalog, born ungated. The ADR-012 thesis exactly.

WHICH SOURCE, AND WHY NOT THE OBVIOUS ONE
-----------------------------------------
The first version of this module read
``catalog_offers.suppression_reason = 'source_currency_or_channel_defect'``.
Review killed it, and the reasoning is worth keeping because it is the
difference between a fix and a no-op wearing a fix's clothes:

  * **No writer.** ``git log -S`` over all refs finds only readers and docs for
    that reason value. The rows carrying it were set by out-of-band data ops.
    A gate keyed on a value nothing maintains decays silently.
  * **The key is NULL on exactly the rows that matter.**
    ``services/external_offer_dual_write`` -- the external-seed mirror writer --
    does not populate ``source_domain`` at all; the domain rides in
    ``offer_payload`` JSON. ~4,705 domain-less mirror offers are a known open
    item, and Mintree's are among them. A resolver filtering
    ``source_domain <> ''`` would have returned the empty set, and
    "no quarantines" is runtime-indistinguishable from "resolver found nothing".

``catalog_source_quarantine`` is the mechanism that actually blocks these
stores on the doors that are clean. It has a real writer
(``scripts/audit_offer_currency.py`` -> ``create_quarantine``), a revoke path,
expiry, and it is what ``catalog_row_trust_upserter`` already reads to set
``serving_decision``. Reading it makes this lane agree with the others by
construction rather than by a restated rule.

NOT A FOURTH COPY OF THE PREDICATE. Matching is delegated to
``source_quarantine.quarantine_matches_source`` -- including its state and
expiry handling, and its EXACT (not suffix) domain comparison. This module adds
exactly one thing that module cannot do: pull a host out of a row that carries
URLs rather than a bare domain. Deliberately no local match rule, no local
denylist, no second normalisation of the match value.

FAILURE IS NOT EMPTINESS. A resolver error must not be cached as "nothing is
quarantined" -- that would disable the gate for a full TTL after one transient
blip, silently. See ``get_quarantined_sources``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

from services.source_quarantine import (
    Quarantine,
    load_active_quarantines,
    quarantine_matches_source,
)

logger = logging.getLogger(__name__)

# Same window as services/test_merchant_policy: at most one quarantine scan per
# TTL, not one per request.
_CACHE_TTL_SECONDS = 300.0

_cache_value: Optional[List[Quarantine]] = None
_cache_at: float = 0.0

# Fields that may carry the storefront. Several, because the lane's projections
# populate different ones: `_external_seed_to_shop_product` emits
# `external_destination_url`, the pivot lane emits `canonical_url`, and
# `_standard_to_shop_product` emits `online_store_url` only when the platform
# sync captured one. A gate that reads a single field is a gate that silently
# passes every row that happens not to carry it.
_URL_FIELDS: Sequence[str] = (
    "external_destination_url",
    "canonical_url",
    "destination_url",
    "online_store_url",
    "product_url",
    "url",
    "source_domain",
    "domain",
)


def url_host(value: Any) -> str:
    """Bare lowercase host from a URL or a bare domain.

    The ONLY normalisation this module owns, and only because
    ``quarantine_matches_source`` compares bare domains while these rows carry
    URLs. ``www.`` is stripped so a row whose URL includes it still matches a
    quarantine recorded without it.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "//" in raw:
        raw = urlsplit(raw).netloc or raw
    elif "/" in raw:
        raw = raw.split("/", 1)[0]
    if "@" in raw:
        raw = raw.rsplit("@", 1)[-1]
    raw = raw.split(":", 1)[0].strip().strip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


async def get_quarantined_sources(
    database=None,
    *,
    now: Optional[float] = None,
) -> List[Quarantine]:
    """Active quarantines, cached for ``_CACHE_TTL_SECONDS``.

    A FAILED resolve is never cached. Caching an error as the empty set would
    turn one transient DB blip into 300 seconds of ungated serving, with no
    exception for the caller to notice -- the same shape as the bug this gate
    exists to close. On failure we keep the previous value if we have one and
    retry on the next call.
    """
    global _cache_value, _cache_at
    if database is None:
        return []
    ts = time.monotonic() if now is None else now
    if _cache_value is not None and (ts - _cache_at) < _CACHE_TTL_SECONDS:
        return list(_cache_value)
    try:
        resolved = await load_active_quarantines(db=database)
    except Exception:
        logger.warning(
            "offer_currency_policy: quarantine resolve FAILED — serving with "
            "%s cached entries; the gate is degraded, not off",
            len(_cache_value or []),
            exc_info=True,
        )
        return list(_cache_value or [])
    _cache_value = list(resolved)
    _cache_at = ts
    return list(resolved)


def reset_cache() -> None:
    """Test hook — drop the memoized quarantine set."""
    global _cache_value, _cache_at
    _cache_value = None
    _cache_at = 0.0


def _field(product: Any, name: str) -> Any:
    if isinstance(product, dict):
        return product.get(name)
    return getattr(product, name, None)


def product_hosts(product: Any) -> List[str]:
    """Every storefront host this row could be attributed to."""
    hosts: List[str] = []
    for name in _URL_FIELDS:
        host = url_host(_field(product, name))
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def is_quarantined_row(product: Any, quarantines: Sequence[Quarantine]) -> bool:
    """True when any of the row's identities matches an active quarantine.

    Handles ``domain`` and ``merchant_platform`` quarantines.
    ``source_system_ref`` is passed through but is currently INERT on this lane:
    no card builder emits ``source_system`` / ``source_ref``, so such a
    quarantine would silently match nothing. Said plainly rather than claimed
    otherwise -- a future operator adding one would otherwise get a quarantine
    that reports success and does nothing.
    """
    if not quarantines:
        return False
    merchant_id = _field(product, "merchant_id")
    platform = _field(product, "platform")
    hosts = product_hosts(product) or [None]
    for q in quarantines:
        # A blank match_value matches EVERYTHING with no host: the delegated
        # comparison normalises both sides, so `"" == normalize(None)` is True
        # and every URL-less row (i.e. every connected-merchant card) would be
        # dropped from the public lane, logged only as a normal exclusion.
        # The migration declares `match_value TEXT NOT NULL` with no non-empty
        # CHECK, and these rows are created by direct-SQL ops, so one bad insert
        # is enough. Guard here rather than trusting the table.
        if not str(q.match_value or "").strip():
            continue
        for host in hosts:
            if quarantine_matches_source(
                q,
                domain=host,
                merchant_id=merchant_id,
                platform=platform,
                source_system=_field(product, "source_system"),
                source_ref=_field(product, "source_ref"),
            ):
                return True
    return False


def filter_out_quarantined_rows(
    products: Optional[Iterable[Any]],
    quarantines: Sequence[Quarantine],
) -> List[Any]:
    """Drop rows from quarantined sources.

    A row with no resolvable identity is KEPT. This gate stops a known-bad
    storefront from publishing; requiring every row to prove its provenance
    would empty the lane instead.
    """
    if not products:
        return list(products or [])
    if not quarantines:
        return list(products)
    return [p for p in products if not is_quarantined_row(p, quarantines)]
