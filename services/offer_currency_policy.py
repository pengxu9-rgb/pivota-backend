"""Currency-defect exclusion — the ONE serving-side predicate for "this row's
currency LABEL cannot be trusted".

Why this exists: on 2026-07-28 the public unauthenticated ACP feed
(`GET /acp/feed` carrying a JSON body) served seven Mintree rows priced
847-3927.70 and labelled ``"USD"``. Mintree is an Indian store
(``mintree.us``, canonical ``vmintree.in``, ``country=IN``, ``currency=INR``):
those were rupee prices published as US dollars. Rs1,058 presented as $1,058.

The block was never broken. It was applied to the INDEX/serving path --
``catalog_offers.suppressed_at`` + ``source_currency_or_channel_defect``,
``index_pipeline_state.serving_eligible=false``, and the trust/renderability
gates -- and every consumer that reads those is clean. Measured the same day:

    canonical index feed (all 5,887 rows paged) ... 0 Mintree rows
    public sitemap ............................... 0
    PDP render ................................... 404s correctly
    ACP connected lane (find_products) ........... 20   <- leaking

This lane reads none of those gates. ``find_products`` routes to the
``connected_catalog`` / external-seed lane, gated only by
``_is_product_sellable`` (status + orderable). Suppression, currency defect,
serving eligibility and trust are all invisible to it. It is the fifth
storefront on the same catalog, and it started ungated by default -- the
ADR-012 thesis exactly: a derived-state gate enforced per-consumer rather than
at a chokepoint, where each new consumer is born ungated and nothing detects it.

WHY A DOMAIN SET AND NOT A PER-ROW CURRENCY CHECK. An INR price labelled
``"USD"`` is indistinguishable from a genuine USD price by inspection -- the
label IS the defect, so no amount of looking at the row detects it. The only
thing that knows is the suppression state recorded against the offer, keyed by
storefront. A store has one base currency, so the storefront is the right grain.

WHY THE SEED ROWS ARE STILL WRONG. ``scripts/backfill_offer_market_currency.py``
corrected ``catalog_offers.currency`` to INR/ZAR for these stores. It is scoped,
deliberately, to offers -- ``external_product_seeds.price_currency`` was never in
its scope, and ``_external_seed_to_shop_product`` reads the SEED. Worse, that
projection ends ``or "USD"``: a seed with no currency at all is stamped USD by a
fallback rather than being treated as unknown. So correcting the offers did not
and could not clean this lane.

READS FROM THE SHARED SOURCE, does not restate the rule. The predicate lives in
``catalog_offers`` and this module queries it. It is deliberately NOT a hand-kept
denylist of domains: a fourth copy of a rule is how the first three drifted.
Mirrors ``services/test_merchant_policy.py`` in shape -- resolve, cache, filter,
fail soft -- because that module is the proven pattern for exactly this problem.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, List, Optional, Set
from urllib.parse import urlsplit

# The suppression reason that means "this offer's currency/channel labelling is
# not to be trusted". Set by the currency-defect workstream; read here.
CURRENCY_DEFECT_SUPPRESSION_REASON = "source_currency_or_channel_defect"

# Same window as test_merchant_policy: at most one catalog_offers scan per TTL,
# not one per request.
_CACHE_TTL_SECONDS = 300.0

_cache_value: Optional[Set[str]] = None
_cache_at: float = 0.0


def normalize_domain(value: Any) -> str:
    """Bare lowercase host: no scheme, no ``www.``, no port, no path.

    Accepts either a bare host or a full URL, because the two sides of this
    comparison come from different places -- ``catalog_offers.source_domain``
    holds a host, while a seed row holds a destination URL.
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


def domain_matches(host: str, defect_domains: Set[str]) -> bool:
    """True when ``host`` IS a defect domain or a subdomain of one.

    Label-boundary matching only (``shop.mintree.us`` matches ``mintree.us``;
    ``notmintree.us`` does not). A plain substring test would over-match, and
    over-matching here silently deletes a real merchant's products from search.
    """
    if not host or not defect_domains:
        return False
    if host in defect_domains:
        return True
    return any(host.endswith("." + d) for d in defect_domains)


async def _resolve_currency_defect_domains(database) -> Set[str]:
    """Storefronts with at least one currency-defect-suppressed offer.

    Fail-soft: on any DB error return the empty set. A resolver hiccup must
    never crash search -- but see the caller, which logs when that happens,
    because failing open here means publishing prices again.
    """
    try:
        rows = await database.fetch_all(
            """
            SELECT DISTINCT lower(trim(source_domain)) AS domain
            FROM catalog_offers
            WHERE suppressed_at IS NOT NULL
              AND suppression_reason = :reason
              AND coalesce(trim(source_domain), '') <> ''
            """,
            {"reason": CURRENCY_DEFECT_SUPPRESSION_REASON},
        )
    except Exception:
        return set()
    out: Set[str] = set()
    for r in rows or []:
        try:
            value = r["domain"]
        except Exception:
            value = (dict(r).get("domain") if hasattr(r, "keys") else None)
        host = normalize_domain(value)
        if host:
            out.add(host)
    return out


async def get_currency_defect_domains(
    database=None,
    *,
    now: Optional[float] = None,
) -> Set[str]:
    """The effective defect-domain set, cached for ``_CACHE_TTL_SECONDS``.

    Returns the empty set when ``database`` is None (pure/sync callers).
    """
    global _cache_value, _cache_at
    if database is None:
        return set()
    ts = time.monotonic() if now is None else now
    if _cache_value is not None and (ts - _cache_at) < _CACHE_TTL_SECONDS:
        return set(_cache_value)
    resolved = await _resolve_currency_defect_domains(database)
    _cache_value = resolved
    _cache_at = ts
    return set(resolved)


def reset_cache() -> None:
    """Test hook — drop the memoized defect-domain set."""
    global _cache_value, _cache_at
    _cache_value = None
    _cache_at = 0.0


def _field(product: Any, name: str) -> Any:
    if isinstance(product, dict):
        return product.get(name)
    return getattr(product, name, None)


def product_domains(product: Any) -> Set[str]:
    """Every storefront host this row could be attributed to.

    Checks several fields rather than one because the lane's own projection
    populates different ones on different paths, and a gate that reads a field
    the row happens not to carry is a gate that silently passes everything.
    ``external_destination_url`` is the load-bearing one for external seeds --
    ``merchant_id`` is explicitly ``None`` on those rows, which is why the
    sibling rig filter cannot see them at all.
    """
    hosts: Set[str] = set()
    for name in (
        "external_destination_url",
        "canonical_url",
        "destination_url",
        "source_domain",
        "domain",
        "online_store_url",
        "product_url",
        "url",
    ):
        host = normalize_domain(_field(product, name))
        if host:
            hosts.add(host)
    return hosts


def is_currency_defect_row(product: Any, defect_domains: Set[str]) -> bool:
    if not defect_domains:
        return False
    return any(domain_matches(h, defect_domains) for h in product_domains(product))


def filter_out_currency_defect_rows(
    products: Optional[Iterable[Any]],
    defect_domains: Set[str],
) -> List[Any]:
    """Drop rows from storefronts whose currency labelling is suppressed.

    A row with no resolvable domain is KEPT. This gate exists to stop a known
    mislabelled storefront from publishing, not to require every row to prove
    its provenance -- failing closed on an unknown domain would empty the lane.
    """
    if not products:
        return list(products or [])
    if not defect_domains:
        return list(products)
    return [p for p in products if not is_currency_defect_row(p, defect_domains)]
