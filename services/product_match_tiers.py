"""The deterministic cross-merchant tiers (T0a / T0b / T0c) over ONE market's listings. Pure.

Shared by the read-only yield census (scripts/ops/product_match_yield_census.py, which sized the
tiers) and the `cross_merchant_same_size` strategy (services/identity_cross_merchant_match.py,
which proposes the matches): both read a listing's signals, the GTIN pollution set and the tier
keys from here, so the census's numbers are the strategy's candidates.

  T0a  valid (check digit, not all zeros: catalog_identity.validated_source_gtin), non-restricted,
       non-polluted GTIN + canonical brand + parsed size + ORDERED title core. A GTIN is POLLUTED
       when intake_identity.barcode_reuse_reason says one seller reuses it (two titles or two
       content_keys at one seller: OPI shades on one barcode), or the market's listings carry it
       on >1 size or >1 stated shade.
  T0b  canonical brand + catalog_identity.normalize_title (exact title, digits kept).
  T0c  match_key_t0c: canonical brand + ORDERED title core + (size, unit, pack count).

No key carries the conflict tokens or the shade: a caller MUST then apply the vetoes
(product_match_normalizer.veto_reasons, or partition by `signature`).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Hashable, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from services.intake_identity import barcode_reuse_reason
from services.product_match_normalizer import (
    canonical_brand,
    exact_title_key,
    gtin_canonical,
    gtin_restriction,
    parse_size,
    size_key,
    title_core,
)

SizeKey = Tuple[float, str, int]
TIERS: Tuple[str, ...] = ("T0a", "T0b", "T0c")


def fold_host(host: Optional[str]) -> str:
    """Lowercase, leading "www." dropped (www/apex are one store)."""
    h = (host or "").strip().lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def text_sizes(text: Any) -> Set[SizeKey]:
    """Every size a title or variant label states: its primary size, or -- when it states two
    different sizes ("30ml / 50ml") -- each of them (so a two-size text reads as multi-size, never
    as unknown). Empty for no size and for "Default Title"."""
    if not text or str(text).strip().lower() == "default title":
        return set()
    parsed = parse_size(text)
    sk = size_key(parsed)
    if sk is not None:
        return {sk}
    if len(parsed.sizes) > 1:
        return {(q.value, q.unit, parsed.pack_count) for q in parsed.sizes}
    return set()


class ListingSignals:
    """One product as seen in one market, with every normalizer signal precomputed."""

    __slots__ = (
        "product_key", "brand", "title", "content_key", "ck", "hosts", "brand_direct", "cb", "tc",
        "size_key", "gtin", "gtin_restriction", "exact_key", "t0c_key", "t0c_set_key", "signature",
        "variant_sizes", "merchant_id", "source_domain", "core", "row",
    )

    def __init__(self, row: Mapping[str, Any], hosts: Set[str], brand_direct: bool,
                 sku_titles: Sequence[str]) -> None:
        self.row = row
        self.product_key = str(row["product_key"])
        self.merchant_id = row.get("merchant_id")
        self.source_domain = row.get("source_domain")
        self.brand = row.get("brand")
        self.title = row.get("title")
        self.content_key = row.get("content_key")
        self.ck = self.content_key or f"pk:{self.product_key}"
        self.hosts = hosts
        self.brand_direct = brand_direct
        self.cb = canonical_brand(self.brand)
        self.tc = title_core(self.brand, self.title)
        self.size_key = size_key(self.tc.size)
        self.gtin = gtin_canonical(row.get("gtin"))
        self.gtin_restriction = gtin_restriction(self.gtin) if self.gtin else None
        self.exact_key = exact_title_key(self.brand, self.title)
        self.core = tuple(self.tc.core.split())
        keyable = bool(self.cb and self.core and self.size_key is not None)
        self.t0c_key = (self.cb, self.core, self.size_key) if keyable else None
        self.t0c_set_key = (self.cb, self.tc.tokens, self.size_key) if keyable else None
        self.signature = (self.tc.conflicts, self.tc.shade_tokens)
        sizes = {self.size_key} if self.size_key else set()
        for t in sku_titles or ():
            if not t or str(t).strip().lower() == "default title":
                continue
            sk = size_key(parse_size(t))
            if sk:
                sizes.add(sk)
        self.variant_sizes = sizes


def build_listings(products: Sequence[Mapping[str, Any]], hosts: Sequence[Mapping[str, Any]],
                   sku_titles: Mapping[str, Sequence[str]], market: str) -> List[ListingSignals]:
    """The market's listings: every product with at least one host row ({product_key, market, host,
    brand_direct}) in `market`."""
    by_pk: Dict[str, Set[str]] = defaultdict(set)
    bd: Dict[str, bool] = defaultdict(bool)
    for h in hosts:
        if h.get("market") != market:
            continue
        pk = str(h["product_key"])
        host = fold_host(h.get("host"))
        if host:
            by_pk[pk].add(host)
        else:
            by_pk.setdefault(pk, set())
        bd[pk] = bd[pk] or bool(h.get("brand_direct"))
    return [
        ListingSignals(p, by_pk[str(p["product_key"])], bd[str(p["product_key"])],
                       sku_titles.get(str(p["product_key"]), ()))
        for p in products if str(p["product_key"]) in by_pk
    ]


class GtinPollution:
    """Which of a market's GTINs cannot identify one product, and why."""

    def __init__(self, polluted: Set[str], by_reason: Counter, gtin_brands: Mapping[str, Set[str]]):
        self.polluted = polluted
        self.by_reason = by_reason
        self.gtin_brands = gtin_brands


def gtin_pollution(listings: Iterable[ListingSignals]) -> GtinPollution:
    """A valid, non-restricted GTIN is polluted when barcode_reuse_reason says a seller reuses it,
    or the market's listings carry it on >1 size or >1 stated shade."""
    listings = list(listings)
    gtin_sizes: Dict[str, Set[Any]] = defaultdict(set)
    gtin_shades: Dict[str, Set[Any]] = defaultdict(set)
    gtin_brands: Dict[str, Set[str]] = defaultdict(set)
    gtin_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for li in listings:
        if li.gtin and not li.gtin_restriction:
            if li.size_key:
                gtin_sizes[li.gtin].add(li.size_key)
            if li.tc.shade_tokens:
                gtin_shades[li.gtin].add(li.tc.shade_tokens)
            gtin_brands[li.gtin].add(li.cb)
            gtin_rows[li.gtin].append({"product_key": li.product_key, "merchant_id": li.merchant_id,
                                       "source_domain": li.source_domain, "title": li.title,
                                       "content_key": li.content_key})
    by_reason: Counter = Counter()
    polluted: Set[str] = set()
    for g in gtin_brands:
        reason = barcode_reuse_reason(gtin_rows[g]) or (
            "sizes_differ" if len(gtin_sizes[g]) > 1 else
            "shades_differ" if len(gtin_shades[g]) > 1 else None)
        if reason:
            polluted.add(g)
            by_reason[reason] += 1
    return GtinPollution(polluted, by_reason, gtin_brands)


def t0a_key(li: ListingSignals, polluted: Set[str]) -> Optional[Hashable]:
    """Phase 1: the GTIN AND the T0c core agree (a GTIN alone never joins two titles)."""
    if (not li.gtin or li.gtin_restriction or li.gtin in polluted or not li.cb or li.size_key is None
            or not li.core):
        return None
    return (li.gtin, li.cb, li.size_key, li.core)


def t0b_key(li: ListingSignals) -> Optional[Hashable]:
    return li.exact_key


def t0c_key(li: ListingSignals) -> Optional[Hashable]:
    return li.t0c_key


def tier_keys(li: ListingSignals, polluted: Set[str]) -> Dict[str, Optional[Hashable]]:
    return {"T0a": t0a_key(li, polluted), "T0b": t0b_key(li), "T0c": t0c_key(li)}
