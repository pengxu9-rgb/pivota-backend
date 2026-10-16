"""`cross_merchant_same_size`: PROPOSE joining a retailer listing to the same product at another
merchant (phase 1 of the cross-merchant matching design, PR "M4"). It never approves or applies.

WHY. Census v2 (2026-10-16): every one of 12,477 US products with retailer offers has exactly ONE
retailer host -- each retailer URL mints its own listing and content_key, and only an exact key ever
joins an existing product. This strategy finds the listings that are provably the SAME buyable item
as a product already in the catalog and proposes moving each onto it (its content_key and product
group), through the identity engine (services.identity_resolution): attach_membership for a listing
alone on its content_key, merge_content_key when several live rows share it and must move together.

IDENTITY (Peng, 2026-10-16). A product is brand + line + formulation; size and shade are variants.
PHASE 1 MERGES SAME-SIZE ONLY, in EVERY market: the product joined has exactly one size, and every
offer and SKU of every row that moves is that size (offers.resolve, agent_pdp_view and the gateway
sibling path have no size awareness yet: they aggregate price ranges and variants per content_key).

STAR TOPOLOGY. Listings of the run's market are grouped by the deterministic tiers
(services.product_match_tiers: T0a GTIN+core+size, T0b exact title, T0c ordered core+size), each tier
key partitioned by the veto signature (conflict tokens, shade), and the groups are joined into
components. Each component has ONE anchor:
  - the brand store's product: a row that is not an ext:retailer listing and is brand-direct (a
    brand_direct offer in the market, or seed_kind 'self'), when exactly ONE content_key holds such
    rows; two or more such content_keys -> every listing of the component is held (`multi_anchor`;
    a listing that matches two of them directly is `multi_anchor_listing`);
  - else the component's oldest signed live product (`oldest_listing`).
Every other listing is judged against the anchor ONLY -- never retailer-to-retailer.

SIZE UNITS. A row's sizes are read per SKU ("unit"): each SKU of the row, in every market, with the
texts its own variant states (SKU title, visible option labels, sku_payload option fields) and the
conflict tokens those texts carry. A unit's size is the ONE size its texts state; two -> unknown.
A unit that states none takes the row title's size only when its texts carry no conflict token the
title lacks AND it is a product-level SKU ('::canonical' / mirror) or the row's only per-variant SKU.

THE JUDGEMENT (listing vs anchor), in order:
  1. a tier key equal (T0a > T0b > T0c; the best one is the proposal's tier);
  2. vetoes: veto_reasons' conflict tokens (EDT/EDP, SPF/PA, plus, new/renewal/version, refill,
     tester, mini, set/duo, multipack ...) and shade; differing valid GTINs (`gtin_differs`); a
     title size that is not the anchor's (`size`); a conflict token on any of the listing's own
     variants (Refill, Mini, Travel Size, Value Size, Tester, Gift Set, 2-Pack, Jumbo, "Size: Mini")
     that the anchor (its title and the keeper's variants) lacks (`offer_conflict:<token>`);
  3. the ANCHOR's size, DEFERRED to phase 2 unless single: the KEEPER ROW itself must state exactly
     one size (its title or its own variants) and every row on the anchor key, in every market,
     states that size or none (`anchor_multi_size`); a keeper that states none, or a priced row on
     the key that states none, is `anchor_size_unknown`;
  4. `offer_size_unpinned`: no offer of the listing in the market is on a unit of the anchor's size;
  5. `listing_multi_size`: a unit of the listing, in ANY market, priced or not, of another or an
     unknown size (moving the listing would put it on the anchor's single-size PDP);
  6. `offer_not_variant_pinned`: the matched size is only on a mirror offer (one per product, its
     price rewritten on every seed sync) or a '::canonical' SKU -- nothing names the variant
     (identity_resolution.is_variant_sku_key). The dry run also counts how many of these a
     read-time re-checked mirror pin would admit (`mirror_recheck_additional`), for Peng;
  7. same-size price: the cheapest matched variant offer in the market (in-stock first) against the
     KEEPER ROW's own same-size price in the market; none (`price_unknown`) or two currencies
     (`price_currency_mismatch`) -> held; outside the EXCLUSIVE window 0.6 < ratio < 1.67 -> vetoed
     (`price_ratio`: sets, minis, testers).
Then the WHOLE-MOVE rules: every live row on the listing's content_key must be in the same component
and pass against the same anchor with the same tier (else `key_shared_with_unmatched`); every row
that moves must be one the crawl door's resolver keeps (product_key 'ext:', else
`unguarded_writer`) and hold a product_group_members row, as must the anchor (`no_group_membership`);
each group a moving row leaves must hold only moving rows (`old_group_has_other_members`); and ONE
SIZE AND ONE KEEPER PER KEY across runs and markets: an applied, not reverted proposal of this
strategy onto the target key with another verdict size is `key_size_conflict` (also within one run);
any proposed, approved or applied one on the source or target key with another keeper, size or
target is `pending_or_applied_conflict`.

EVIDENCE CONTRACT v3 (read by be#2635's resolved-group read and #2637's approval): attach_membership
carries evidence.listing_product_key + keeper_product_key; merge_content_key carries
evidence.from_product_keys + keeper_product_key; both carry evidence.proposer, evidence.proposal_run_id
(stamped at write time) and evidence.matcher_verdict {strategy, rule_version, tier, same_size, size,
matched_offers [{offer_id, sku_key, price, currency, size, market}], matched_seeds [{seed_id,
variant_id}], from_key_single_size (merge only)}, checked by the ENGINE's
identity_resolution.validate_matcher_verdict / module_proposal_problems -- one shared rule, #2637.

Pure: no I/O here. scripts/ops/propose_cross_merchant_matches.py exports the snapshot (one read-only
transaction, two phases) and writes proposals through the engine's writer.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, FrozenSet, Hashable, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from services.identity_resolution import (
    CROSS_MERCHANT_SAME_SIZE,
    is_variant_sku_key,
    member_fingerprint,
    merge_content_key_proposal,
    module_proposal_problems,
    new_proposal,
    proposal_fingerprint,
    validate_matcher_verdict,
    verdict_size_key,
)
from services.product_match_normalizer import _UNIT_ALTS, _fold, canonical_brand, title_core, veto_reasons
from services.product_match_tiers import (
    TIERS,
    ListingSignals,
    SizeKey,
    fold_host,
    gtin_pollution,
    text_sizes,
    tier_keys,
)

STRATEGY = CROSS_MERCHANT_SAME_SIZE  # the engine's name for it (#2637); only this module produces it
RULE_VERSION = "cmss.v3"
RETAILER_LISTING_PREFIX = "ext:retailer:"
#: Rows the crawl door's resolver keeps on their stored content_key once the engine moved them
#: (identity_resolution.RESOLVER_GUARDED_PREFIX). A first-party row is re-keyed by its own sync.
MOVABLE_PREFIX = "ext:"
#: The mirror lane's one-per-product SKU ("<product_key>::canonical", ingestion.SKU_SUFFIX) and its
#: offer id ("offer:external_seed:<hash>", external_offer_dual_write.OFFER_ID_PREFIX /
#: derive_mirror_offer_id). Mirrored so this module imports no database module; a test pins both.
CANONICAL_SKU_SUFFIX = "::canonical"
MIRROR_OFFER_ID_PREFIX = "offer:external_seed:"
#: Same-size price window, EXCLUSIVE: a listing is vetoed unless 0.6 < listing / keeper < 1.67.
PRICE_RATIO_MIN = 0.6
PRICE_RATIO_MAX = 1.67
CONFIDENCE = {"T0a": 0.97, "T0b": 0.95, "T0c": 0.9}
IN_STOCK = frozenset({"in_stock", "instock", "limited_stock", "low_stock", "available"})
LIVE_PROPOSAL_STATUSES = ("proposed", "approved", "applied")
VETOES_CHECKED: Tuple[str, ...] = (
    "conflict_tokens", "shade", "gtin_differs", "size", "offer_conflict_tokens", "anchor_single_size",
    "offer_size_pinned", "listing_single_size_all_markets", "offer_variant_pinned", "key_single_size",
    "pending_or_applied_conflict", "price_ratio")
#: sku_payload fields that can name a variant's size (the export reads only these).
SKU_PAYLOAD_SIZE_FIELDS: Tuple[str, ...] = ("variant_title", "title", "size", "option1", "option2", "option3",
                                            "options", "selected_options")
#: Seed variant fields that can name its size (external_seed_audit.normalize_seed_variants' title
#: chain plus the option fields).
SEED_VARIANT_SIZE_FIELDS: Tuple[str, ...] = ("title", "name", "option_value", "option1", "option2", "option3",
                                             "options", "size", "selected_options")

# Outcome reasons. HELD goes to a human; DEFERRED waits for phase 2 (size-aware serving); VETOED is a
# different buyable item. None of them is ever proposed.
HELD_REASONS = ("multi_anchor", "multi_anchor_listing", "no_direct_anchor_match", "size_fraction_count",
                "offer_size_unpinned",
                "listing_multi_size", "offer_not_variant_pinned", "key_size_conflict",
                "pending_or_applied_conflict", "price_unknown", "price_currency_mismatch",
                "key_shared_with_unmatched", "unguarded_writer", "no_group_membership",
                "old_group_has_other_members", "no_content_key", "old_content_key_not_read")
DEFERRED_REASONS = ("anchor_multi_size", "anchor_size_unknown", "anchor_unit_conflict")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _flatten_text(value: Any) -> List[str]:
    """Every string inside a JSON value (labels arrive as a list, a dict or a JSON string)."""
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "[{":
            try:
                return _flatten_text(json.loads(s))
            except ValueError:
                pass
        return [s] if s else []
    if isinstance(value, Mapping):
        return [t for v in value.values() for t in _flatten_text(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _flatten_text(v)]
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return [str(value)]
    return []


def _variant_texts(*values: Any) -> List[str]:
    return [t for v in values for t in _flatten_text(v) if t.strip().lower() != "default title"]


def _texts_sizes(texts: Iterable[str]) -> Set[SizeKey]:
    out: Set[SizeKey] = set()
    for t in texts:
        out |= text_sizes(t)
    return out


def _texts_conflicts(texts: Iterable[str], brand: Any = None) -> FrozenSet[str]:
    """Conflict tokens in a variant's texts, read with the row's brand removed first (a brand's own
    "+", Dr.Jart+, is not a "+" edition)."""
    out: Set[str] = set()
    for t in texts:
        out |= title_core(brand, t).conflicts
    return frozenset(out)


# A count that is not a positive integer ("0.5 ct", "1/2 ct", "½ ct", "1.7 ct"): the normalizer reads
# counts as integers, so it would truncate these (0.5 -> 0, 1.7 -> 1). Found on the raw (folded) text,
# with the normalizer's own count units.
_COUNT_UNITS = dict(_UNIT_ALTS)["count"]
_FRACTION_COUNT_RE = re.compile(
    rf"(?<![\w.])(?:(?P<dec>\d+[.,]\d+)|(?P<num>\d+)\s*[/\u2044]\s*(?P<den>\d+)|(?P<vulgar>[\u00bc-\u00be\u2150-\u215e]))"
    rf"\s*(?:{_COUNT_UNITS})(?![a-z])")


def _not_whole(m: "re.Match[str]") -> bool:
    if m.group("vulgar"):
        return True
    if m.group("dec"):
        return not float(m.group("dec").replace(",", ".")).is_integer()
    den = int(m.group("den"))
    return den == 0 or int(m.group("num")) % den != 0


def has_fraction_count(text: Any) -> bool:
    """Does the text state a count that is not a positive integer ("0.5 ct", "1/2 ct", "1.7 ct")?"""
    if not text:
        return False
    for t in {str(text).lower(), _fold(text)}:
        if any(_not_whole(m) for m in _FRACTION_COUNT_RE.finditer(t)):
            return True
    return False


def offer_text_sizes(offer: Mapping[str, Any]) -> Set[SizeKey]:
    """The sizes an offer's own variant states: its SKU title, visible option labels and the
    sku_payload option fields."""
    return _texts_sizes(_variant_texts(offer.get("sku_title"), offer.get("sku_labels"), offer.get("sku_size_fields")))


def _price(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def _size_json(sk: Optional[SizeKey]) -> Optional[Dict[str, Any]]:
    if sk is None:
        return None
    value, unit, pack = sk
    return {"value": float(value), "unit": unit, "pack": int(pack)}


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

@dataclass
class Offer:
    offer_id: str
    product_key: str
    sku_key: str
    market: str
    host: str
    brand_direct: bool
    in_stock: bool
    price: Optional[float]
    currency: str

    @property
    def is_mirror(self) -> bool:
        return self.offer_id.startswith(MIRROR_OFFER_ID_PREFIX)

    @property
    def is_variant(self) -> bool:
        """On a per-variant SKU (identity_resolution.is_variant_sku_key), never a mirror offer."""
        return is_variant_sku_key(self.sku_key) and not self.is_mirror


@dataclass
class Unit:
    """One SKU of a row (in every market): what its own variant states, and its resolved size."""

    sku_key: str
    texts: List[str]
    stated: Set[SizeKey]
    conflicts: FrozenSet[str]
    product_level: bool
    offers: List[Offer] = field(default_factory=list)
    size: Optional[SizeKey] = None


@dataclass
class Snapshot:
    """Everything the planner reads (the script exports it in two phases; tests build it).

    products:  the run market's signed live catalog_products with a live offer there (product_key,
               merchant_id, platform, source_product_id, source_ref, content_key, pivota_signature_id,
               brand, title, gtin, source_system, seed_kind, created_at, image_url, canonical_url).
    hosts:     the market's (product_key, market, host, brand_direct) rows; derived from `offers` when
               empty.
    offers:    EVERY market's live offers of the rows on the candidate keys (offer_id, product_key,
               sku_key, market, offer_type, availability, currency, price, host, sku_title,
               sku_labels, sku_size_fields).
    skus:      the live SKUs of those rows ({product_key, sku_key, title, labels}).
    ck_rows:   content_key -> every NON-SUPPRESSED row on it: a product_key, or a mapping with
               product_key, title, merchant_id, platform, source_product_id, source_ref.
    memberships: (merchant_id, platform, source_product_id) -> product_group_id.
    group_sizes: product_group_id -> member count.
    seeds:     ACTIVE external_product_seeds linked to those rows (id, attached_product_key,
               external_product_id, title, seed_variants = seed_data.variants, the MAIN list only).
    proposals: this strategy's proposed / approved / applied proposals, every market (proposal_id,
               status, kind, content_key, keeper_product_key, evidence).
    gtin_rows: EVERY live row, any brand, any market, carrying one of the candidates' GTINs
               (product_key, merchant_id, source_domain, brand, title, gtin, content_key): GTIN
               pollution is computed over these too, so a brand-scoped run and a full run agree.
    """

    products: Sequence[Mapping[str, Any]]
    offers: Sequence[Mapping[str, Any]]
    skus: Sequence[Mapping[str, Any]]
    ck_rows: Mapping[str, Sequence[Any]]
    memberships: Mapping[Tuple[str, str, str], str]
    group_sizes: Mapping[str, int]
    seeds: Sequence[Mapping[str, Any]] = ()
    proposals: Sequence[Mapping[str, Any]] = ()
    hosts: Sequence[Mapping[str, Any]] = ()
    gtin_rows: Sequence[Mapping[str, Any]] = ()


@dataclass
class Listing:
    """A signed live product of the run's market (its tier signals)."""

    sig: ListingSignals
    row: Mapping[str, Any]
    keys: Dict[str, Optional[Hashable]] = field(default_factory=dict)

    @property
    def product_key(self) -> str:
        return self.sig.product_key

    @property
    def content_key(self) -> Optional[str]:
        return self.sig.content_key

    @property
    def is_retailer(self) -> bool:
        return self.product_key.startswith(RETAILER_LISTING_PREFIX)

    @property
    def is_brand_store(self) -> bool:
        return not self.is_retailer and (self.sig.brand_direct or str(self.row.get("seed_kind") or "") == "self")

    @property
    def member(self) -> Tuple[str, str, str]:
        return (str(self.row.get("merchant_id") or ""), str(self.row.get("platform") or ""),
                str(self.row.get("source_product_id") or ""))


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

def _seed_variants(seed: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The seed's MAIN variant list (seed_data.variants). The snapshot list is never read."""
    raw = seed.get("seed_variants")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return [v for v in raw if isinstance(v, Mapping)] if isinstance(raw, list) else []


def _seed_variant_sizes(variant: Mapping[str, Any]) -> Set[SizeKey]:
    return _texts_sizes(_variant_texts(*(variant.get(f) for f in SEED_VARIANT_SIZE_FIELDS)))


def seed_variant_id(variant: Mapping[str, Any]) -> str:
    """The id the seed lane's READER resolves for a variant -- routes/agent_shop_gateway.
    _seed_offer_variant_id's chain, spelled exactly as it returns it (variant_id | variantId | sku |
    sku_id | id, stripped). Mirrored here (that module is the HTTP router); a test pins the two."""
    raw = (variant.get("variant_id") or variant.get("variantId") or variant.get("sku")
           or variant.get("sku_id") or variant.get("id") or "")
    return str(raw).strip()


def _seed_variant_conflicts(seed: Mapping[str, Any], variant: Mapping[str, Any], brand: Any) -> FrozenSet[str]:
    texts = _variant_texts(*(variant.get(f) for f in SEED_VARIANT_SIZE_FIELDS))
    return _texts_conflicts(texts, brand) | _texts_conflicts(_variant_texts(seed.get("title")), brand)


def seed_matches(seed: Mapping[str, Any], size: SizeKey, conflicts: FrozenSet[str] = frozenset(),
                 brand: Any = None) -> List[Dict[str, Any]]:
    """matched_seeds entries for one seed at the verdict size and the matched unit's conflict words.

    A seed or variant is listed ONLY when its own size is exactly `size` AND its conflict words (the
    variant's text plus the seed title, the MAIN variant list only) EQUAL `conflicts` -- a "Refill",
    "Tester" or "Mini" seed is never the matched item. `variant_id: null` ONLY for a seed with EXACTLY
    ONE variant (its size: the variant's, else the seed title's). A multi-variant seed names each such
    variant by seed_variant_id (the reader's own spelling) and is otherwise not listed. A seed with no variants, of another
    size, or whose size cannot be read is never listed."""
    seed_id = str(seed.get("id") or "")
    if not seed_id:
        return []
    variants = _seed_variants(seed)
    if len(variants) == 1:
        sizes = _seed_variant_sizes(variants[0]) or text_sizes(seed.get("title"))
        if sizes == {size} and _seed_variant_conflicts(seed, variants[0], brand) == conflicts:
            return [{"seed_id": seed_id, "variant_id": None}]
        return []
    out = []
    for v in variants:
        vid = seed_variant_id(v)
        if not vid or _seed_variant_sizes(v) != {size}:
            continue
        if _seed_variant_conflicts(seed, v, brand) != conflicts:
            continue
        out.append({"seed_id": seed_id, "variant_id": vid})
    return out


# ---------------------------------------------------------------------------
# Judgement
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    status: str  # pass | vetoed | held | deferred
    reasons: List[str]
    tier: Optional[str] = None
    tiers_matched: List[str] = field(default_factory=list)
    anchor_size: Optional[SizeKey] = None
    matched_offers: List[Dict[str, Any]] = field(default_factory=list)
    price: Dict[str, Any] = field(default_factory=dict)
    mirror_clear: bool = False
    words: FrozenSet[str] = frozenset()


def _side_price(offers: Iterable[Offer]) -> Tuple[Optional[float], str, str]:
    """(cheapest price, its currency, basis): in-stock offers first, else every priced offer."""
    priced = [o for o in offers if o.price is not None]
    stock = [o for o in priced if o.in_stock]
    pool, basis = (stock, "in_stock") if stock else (priced, "any")
    if not pool:
        return None, "", "none"
    best = min(pool, key=lambda o: (o.price, o.offer_id))
    return best.price, best.currency, basis


def _row_info(entry: Any) -> Dict[str, Any]:
    return {"product_key": entry} if isinstance(entry, str) else dict(entry)


class Planner:
    """Plans one market's proposals from a Snapshot. Pure."""

    def __init__(self, snap: Snapshot, market: str, *, brands: Optional[Sequence[str]] = None,
                 limit_groups: Optional[int] = None):
        self.snap = snap
        self.market = market
        self.offers_by_pk: Dict[str, List[Offer]] = defaultdict(list)
        for o in snap.offers:
            self.offers_by_pk[str(o["product_key"])].append(Offer(
                offer_id=str(o["offer_id"]), product_key=str(o["product_key"]), sku_key=str(o.get("sku_key") or ""),
                market=str(o.get("market") or market), host=fold_host(o.get("host")),
                brand_direct=str(o.get("offer_type") or "") == "brand_direct",
                in_stock=str(o.get("availability") or "").strip().lower() in IN_STOCK,
                price=_price(o.get("price")), currency=str(o.get("currency") or "").strip().upper()))
        self._offer_rows = {str(o["offer_id"]): o for o in snap.offers}
        for pk in self.offers_by_pk:
            self.offers_by_pk[pk].sort(key=lambda o: o.offer_id)
        self.skus_by_pk: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
        for s in snap.skus:
            self.skus_by_pk[str(s["product_key"])].append(s)
        # Rows: the market's products, plus every other row on a candidate key.
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.ck_members: Dict[str, List[str]] = {}
        for ck, entries in snap.ck_rows.items():
            members = []
            for e in entries:
                info = _row_info(e)
                pk = str(info["product_key"])
                members.append(pk)
                self.rows.setdefault(pk, {**info, "content_key": ck})
            self.ck_members[ck] = members
        for p in snap.products:
            self.rows[str(p["product_key"])] = dict(p)
        hosts = list(snap.hosts) or [
            {"product_key": o.product_key, "market": o.market, "host": o.host, "brand_direct": o.brand_direct}
            for offers in self.offers_by_pk.values() for o in offers]
        market_hosts: Dict[str, Set[str]] = defaultdict(set)
        brand_direct: Dict[str, bool] = defaultdict(bool)
        has_market: Set[str] = set()
        for h in hosts:
            if h.get("market") != market:
                continue
            pk = str(h["product_key"])
            has_market.add(pk)
            if fold_host(h.get("host")):
                market_hosts[pk].add(fold_host(h.get("host")))
            brand_direct[pk] = brand_direct[pk] or bool(h.get("brand_direct"))
        self.all_listings = [Listing(sig=ListingSignals(p, market_hosts[str(p["product_key"])],
                                                        brand_direct[str(p["product_key"])], ()), row=p)
                             for p in snap.products if str(p["product_key"]) in has_market]
        self.by_pk: Dict[str, Listing] = {li.product_key: li for li in self.all_listings}
        extra_gtin_rows = [ListingSignals(r, set(), False, ()) for r in snap.gtin_rows
                           if str(r["product_key"]) not in self.by_pk]
        self.pollution = gtin_pollution([*(li.sig for li in self.all_listings), *extra_gtin_rows])
        for li in self.all_listings:
            li.keys = tier_keys(li.sig, self.pollution.polluted)
        wanted = {canonical_brand(b) for b in brands or ()} - {""}
        self.brands = sorted(wanted)
        self.listings = [li for li in self.all_listings if not wanted or li.sig.cb in wanted]
        self.limit_groups = limit_groups
        self.by_ck: Dict[str, List[Listing]] = defaultdict(list)
        for li in self.all_listings:
            if li.content_key:
                self.by_ck[li.content_key].append(li)
        self._units: Dict[str, List[Unit]] = {}
        self._anchor: Dict[Tuple[str, str], Tuple[Optional[SizeKey], Optional[str], FrozenSet[str]]] = {}
        self._index_seeds()
        self._index_proposals()

    # -- seeds and existing proposals ---------------------------------------------------------------
    def _index_seeds(self) -> None:
        by_ref: Dict[str, Set[str]] = defaultdict(set)
        by_spid: Dict[str, Set[str]] = defaultdict(set)
        for pk, r in self.rows.items():
            if r.get("source_ref"):
                by_ref[str(r["source_ref"])].add(pk)
            if r.get("source_product_id"):
                by_spid[str(r["source_product_id"])].add(pk)
        self.seeds_by_pk: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
        for seed in sorted(self.snap.seeds, key=lambda x: str(x.get("id") or "")):
            pks = set(by_ref.get(str(seed.get("id") or ""), ()))
            if seed.get("external_product_id"):
                pks |= by_spid.get(str(seed["external_product_id"]), set())
            if seed.get("attached_product_key"):
                pks.add(str(seed["attached_product_key"]))
            for pk in sorted(pks):
                self.seeds_by_pk[pk].append(seed)

    def _index_proposals(self) -> None:
        self.as_target: Dict[str, List[Tuple[str, Optional[SizeKey], str]]] = defaultdict(list)
        self.as_source: Dict[str, List[Tuple[str, str, Optional[SizeKey], str]]] = defaultdict(list)
        for p in self.snap.proposals:
            status = str(p.get("status") or "")
            if status not in LIVE_PROPOSAL_STATUSES:
                continue
            ev = p.get("evidence") or {}
            if isinstance(ev, str):
                ev = json.loads(ev or "{}")
            size = verdict_size_key(ev)
            keeper, target = str(p.get("keeper_product_key") or ""), str(p.get("content_key") or "")
            self.as_target[target].append((keeper, size, status))
            if ev.get("from_content_key"):
                self.as_source[str(ev["from_content_key"])].append((target, keeper, size, status))

    def existing_conflict(self, from_ck: str, target_ck: str, keeper: str, size: SizeKey) -> Optional[str]:
        """key_size_conflict / pending_or_applied_conflict against this strategy's proposals of every
        market and run, or None."""
        targets = self.as_target.get(target_ck, ())
        if any(st == "applied" and s != size for _k, s, st in targets):
            return "key_size_conflict"
        if any(k != keeper or s != size for k, s, _st in targets):
            return "pending_or_applied_conflict"
        if any(st != "applied" for *_x, st in self.as_source.get(target_ck, ())):
            return "pending_or_applied_conflict"  # the target key is itself moving away
        if any(st != "applied" and (t, k, s) != (target_ck, keeper, size)
               for t, k, s, st in self.as_source.get(from_ck, ())):
            return "pending_or_applied_conflict"
        if self.as_target.get(from_ck):
            return "pending_or_applied_conflict"  # others were proposed onto the key this would vacate
        return None

    def matched_seeds(self, pks: Iterable[str], size: SizeKey, conflicts: FrozenSet[str] = frozenset(),
                      brand: Any = None) -> List[Dict[str, Any]]:
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for pk in pks:
            for seed in self.seeds_by_pk.get(pk, ()):
                for m in seed_matches(seed, size, conflicts, brand):
                    out[(m["seed_id"], m["variant_id"] or "")] = m
        return [out[k] for k in sorted(out)]

    # -- size units ---------------------------------------------------------------------------------
    def _title(self, pk: str) -> Any:
        return self.rows.get(pk, {}).get("title")

    def units(self, pk: str) -> List[Unit]:
        """The row's SKUs in every market, each with its own variant texts and resolved size."""
        cached = self._units.get(pk)
        if cached is not None:
            return cached
        by_key: Dict[str, Unit] = {}

        def unit(key: str) -> Unit:
            if key not in by_key:
                by_key[key] = Unit(sku_key=key, texts=[], stated=set(), conflicts=frozenset(),
                                   product_level=key.endswith(CANONICAL_SKU_SUFFIX))
            return by_key[key]

        for s in self.skus_by_pk.get(pk, ()):
            u = unit(str(s.get("sku_key") or f"sku:{pk}:{s.get('title')}"))
            u.texts += _variant_texts(s.get("title"), s.get("labels"))
        for o in self.offers_by_pk.get(pk, ()):
            raw = self._offer_rows.get(o.offer_id, {})
            u = unit(o.sku_key or f"offer:{o.offer_id}")
            u.offers.append(o)
            u.product_level = u.product_level or o.is_mirror
            u.texts += _variant_texts(raw.get("sku_title"), raw.get("sku_labels"), raw.get("sku_size_fields"))
        title = self._title(pk)
        brand = self.rows.get(pk, {}).get("brand")
        title_sizes = text_sizes(title)
        title_size = next(iter(title_sizes)) if len(title_sizes) == 1 else None
        title_conflicts = title_core(brand, title).conflicts if title else frozenset()
        out = sorted(by_key.values(), key=lambda u: u.sku_key)
        variant_units = [u for u in out if not u.product_level]
        for u in out:
            u.stated = _texts_sizes(u.texts)
            u.conflicts = _texts_conflicts(u.texts, brand)
            if len(u.stated) == 1:
                u.size = next(iter(u.stated))
            elif (not u.stated and title_size is not None and u.conflicts <= title_conflicts
                  and (u.product_level or variant_units == [u])):
                u.size = title_size
        self._units[pk] = out
        return out

    def row_has_fraction_count(self, pk: str) -> bool:
        return has_fraction_count(self._title(pk)) or any(has_fraction_count(t) for u in self.units(pk) for t in u.texts)

    def row_stated_sizes(self, pk: str) -> Set[SizeKey]:
        """What the row ITSELF states: its title's sizes and its units' resolved sizes."""
        out = set(text_sizes(self._title(pk)))
        out |= {u.size for u in self.units(pk) if u.size is not None}
        out |= {s for u in self.units(pk) for s in u.stated}
        return out

    def unit_words(self, li: Listing, u: Unit) -> FrozenSet[str]:
        """A unit's conflict words: its own variant text's plus its row title's."""
        return frozenset(u.conflicts) | frozenset(li.sig.tc.conflicts)

    def anchor_size(self, anchor: Listing) -> Tuple[Optional[SizeKey], Optional[str], FrozenSet[str]]:
        """(size, None, the keeper unit's conflict words) or (None, deferral reason, ...).

        The KEEPER ROW must state exactly one size; every row on the key, in every market, states that
        size or none; a PRICED row stating none is unknown. Then UNIT TO UNIT: every unit of the keeper
        must be at that size (a keeper unit of unknown size -- "Refill" with no size beside "Jar 50ml" --
        defers) and carry the SAME conflict words; keeper units at the size with different words (a
        jar and a refill) defer too (`anchor_unit_conflict`). Those words are what a listing's units
        must equal."""
        ck = anchor.content_key or ""
        cache_key = (ck, anchor.product_key)
        if cache_key in self._anchor:
            return self._anchor[cache_key]
        keeper_units = self.units(anchor.product_key)
        word_sets = {self.unit_words(anchor, u) for u in keeper_units}
        tokens = next(iter(word_sets)) if len(word_sets) == 1 else frozenset(anchor.sig.tc.conflicts)
        keeper = self.row_stated_sizes(anchor.product_key)
        result: Tuple[Optional[SizeKey], Optional[str], FrozenSet[str]]
        if not keeper:
            result = (None, "anchor_size_unknown", tokens)
        else:
            size = min(keeper)
            result = (size, None, tokens)
            # The keeper itself is checked first: a keeper stating two sizes is multi-size.
            for pk in [anchor.product_key, *(k for k in self.ck_members.get(ck, ()) if k != anchor.product_key)]:
                stated = self.row_stated_sizes(pk)
                if stated - {size}:
                    result = (None, "anchor_multi_size", tokens)
                    break
                if not stated and any(o.price is not None for o in self.offers_by_pk.get(pk, ())):
                    result = (None, "anchor_size_unknown", tokens)
                    break
            if result[1] is None and (any(u.size != size for u in keeper_units) or len(word_sets) > 1):
                result = (None, "anchor_unit_conflict", tokens)
        self._anchor[cache_key] = result
        return result

    def row_single_size(self, pk: str, size: SizeKey) -> bool:
        """Every unit of the row, in every market, priced or not, is `size`."""
        return all(u.size == size for u in self.units(pk))

    def from_key_single_size(self, from_ck: str, size: SizeKey) -> bool:
        """matcher_verdict.from_key_single_size, COMPUTED: every unit of every live row on the losing key,
        every market, priced or not, is `size` (and the key's rows were read)."""
        rows = self.ck_members.get(from_ck)
        return bool(rows) and all(self.row_single_size(pk, size) for pk in rows)

    def keeper_price_offers(self, anchor: Listing, size: SizeKey, words: FrozenSet[str],
                            market: Optional[str] = None) -> List[Offer]:
        """The keeper row's own offers in `market` (default: the run's) on units EXACTLY at `size` with
        EXACTLY the matched conflict words -- never a unit of unknown size, never a refill or a tester."""
        market = market or self.market
        return [o for u in self.units(anchor.product_key)
                if u.size == size and self.unit_words(anchor, u) == words
                for o in u.offers if o.market == market]

    # -- the judgement ------------------------------------------------------------------------------
    def judge(self, li: Listing, anchor: Listing) -> Verdict:
        matched = [t for t in TIERS if li.keys.get(t) is not None and li.keys.get(t) == anchor.keys.get(t)]
        if not matched:
            return Verdict("held", ["no_direct_anchor_match"])
        vetoes = [r for r in veto_reasons(li.sig.tc, anchor.sig.tc) if r not in ("size", "size_unknown")]
        ga, gb = li.sig.gtin, anchor.sig.gtin
        polluted = self.pollution.polluted
        if (ga and gb and ga != gb and not li.sig.gtin_restriction and not anchor.sig.gtin_restriction
                and ga not in polluted and gb not in polluted):
            vetoes.append("gtin_differs")
        anchor_size, deferral, keeper_words = self.anchor_size(anchor)
        if li.sig.size_key is not None and anchor_size is not None and li.sig.size_key != anchor_size:
            vetoes.append("size")
        units = self.units(li.product_key)
        # UNIT TO UNIT: each of the listing's units (its variant text plus its title) must carry exactly
        # the keeper unit's conflict words -- a "Refill" variant is not the keeper's jar, nor the reverse.
        # (Only against a decided anchor: one whose own units disagree defers every listing instead.)
        if deferral is None:
            extra = sorted({t for u in units for t in (self.unit_words(li, u) ^ keeper_words)})
            vetoes += [f"offer_conflict:{t}" for t in extra]
        if vetoes:
            return Verdict("vetoed", vetoes, tiers_matched=matched)
        if (self.row_has_fraction_count(li.product_key) or self.row_has_fraction_count(anchor.product_key)
                or (anchor_size is not None and anchor_size[1] == "count"
                    and not (anchor_size[0] >= 1 and float(anchor_size[0]).is_integer()))):
            # A fractional count would be truncated by the size reader: never a proven size.
            return Verdict("held", ["size_fraction_count"], tiers_matched=matched)
        if deferral:
            return Verdict("deferred", [deferral], tiers_matched=matched)
        held = dict(tiers_matched=matched, anchor_size=anchor_size)
        market_units = [u for u in units if any(o.market == self.market for o in u.offers)]
        if not any(u.size == anchor_size for u in market_units):
            return Verdict("held", ["offer_size_unpinned"], **held)
        if not self.row_single_size(li.product_key, anchor_size):
            return Verdict("held", ["listing_multi_size"], **held)
        variant_all = [o for u in units for o in u.offers if o.is_variant]
        variant_market = [o for o in variant_all if o.market == self.market]
        if not variant_market:
            mirror_clear = any(o.price is not None and o.market == self.market
                               for u in units if u.product_level and u.size == anchor_size for o in u.offers)
            return Verdict("held", ["offer_not_variant_pinned"], mirror_clear=mirror_clear, **held)
        # The price window, PER MARKET: each market's matched offers against the keeper's same-market,
        # same-size, same-words unit. The run's market must have both sides; another market where the
        # keeper sells nothing at that size is skipped, and recorded.
        by_market: Dict[str, Dict[str, Any]] = {}
        price: Dict[str, Any] = {}
        for mkt in sorted({o.market for o in variant_all} | {self.market}):
            lp, lc, lbasis = _side_price(o for o in variant_all if o.market == mkt)
            ap, ac, abasis = _side_price(self.keeper_price_offers(anchor, anchor_size, keeper_words, mkt))
            entry = {"listing": lp, "anchor": ap, "listing_currency": lc, "anchor_currency": ac,
                     "listing_basis": lbasis, "anchor_basis": abasis, "ratio": None}
            by_market[mkt] = entry
            if mkt == self.market:
                price = entry
            if lp is None or ap is None:
                if mkt == self.market:
                    return Verdict("held", ["price_unknown"], price={**price, "by_market": by_market}, **held)
                entry["skipped"] = "keeper_has_no_same_size_offer_here" if ap is None else "no_listing_price_here"
                continue
            if lc != ac:
                return Verdict("held", ["price_currency_mismatch"], price={**price, "by_market": by_market}, **held)
            entry["ratio"] = round(lp / ap, 4)
            if not (PRICE_RATIO_MIN < entry["ratio"] < PRICE_RATIO_MAX):
                return Verdict("vetoed", ["price_ratio"], price={**price, "by_market": by_market}, **held)
        price = {**price, "by_market": by_market}
        offers = [{"offer_id": o.offer_id, "sku_key": o.sku_key, "price": o.price, "currency": o.currency,
                   "size": _size_json(anchor_size), "market": o.market} for o in variant_all]
        return Verdict("pass", [], tier=matched[0], tiers_matched=matched, anchor_size=anchor_size,
                       price=price, matched_offers=sorted(offers, key=lambda m: m["offer_id"]),
                       words=keeper_words)

    # -- components (star groups) ---------------------------------------------------------------
    def components(self) -> List[List[Listing]]:
        parent: Dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        by_node: Dict[Hashable, List[str]] = defaultdict(list)
        for li in self.listings:
            for t in TIERS:
                k = li.keys.get(t)
                if k is not None:
                    by_node[(t, k, li.sig.signature)].append(li.product_key)
        for members in by_node.values():
            if len(members) < 2:
                continue
            root = find(members[0])
            for m in members[1:]:
                r = find(m)
                if r != root:
                    parent[r] = root
        comps: Dict[str, List[Listing]] = defaultdict(list)
        for pk in parent:
            comps[find(pk)].append(self.by_pk[pk])
        out = []
        for members in comps.values():
            members.sort(key=lambda li: li.product_key)
            if len({li.content_key or f"pk:{li.product_key}" for li in members}) >= 2:
                out.append(members)
        return out

    @staticmethod
    def _age(li: Listing) -> Tuple[str, str]:
        return (str(li.row.get("created_at") or "9999"), li.product_key)

    def choose_anchor(self, comp: List[Listing]) -> Tuple[Optional[Listing], str, List[Listing]]:
        """(anchor row, reason, candidate brand-store anchors). None when two brand-store content_keys
        compete (multi_anchor)."""
        stores: Dict[str, List[Listing]] = defaultdict(list)
        for li in comp:
            if li.is_brand_store and li.content_key:
                stores[li.content_key].append(li)
        if len(stores) > 1:
            return None, "multi_anchor", [min(rows, key=self._age) for _ck, rows in sorted(stores.items())]
        if len(stores) == 1:
            rows = next(iter(stores.values()))
            return min(rows, key=self._age), "brand_store", []
        with_ck = [li for li in comp if li.content_key]
        if not with_ck:
            return None, "no_content_key", []
        return min(with_ck, key=self._age), "oldest_listing", []

    def groups(self) -> List[Tuple[Optional[Listing], str, List[Listing], List[Listing]]]:
        comps = []
        for comp in self.components():
            anchor, reason, candidates = self.choose_anchor(comp)
            comps.append((anchor, reason, candidates, comp))
        comps.sort(key=lambda c: (c[0].product_key if c[0] else c[3][0].product_key))
        if self.limit_groups is not None:
            comps = comps[: self.limit_groups]
        return comps

    def scope(self) -> Tuple[List[str], List[str]]:
        """Phase 1 of the export: the product_keys and content_keys of every group this run judges, so
        phase 2 reads offers, SKUs, rows, memberships and seeds for those keys only."""
        pks: Set[str] = set()
        cks: Set[str] = set()
        for _a, _r, _c, comp in self.groups():
            for li in comp:
                pks.add(li.product_key)
                if li.content_key:
                    cks.add(li.content_key)
        return sorted(pks), sorted(cks)

    # -- the plan ---------------------------------------------------------------------------------
    def plan(self) -> "Plan":
        comps = self.groups()
        plan = Plan(market=self.market, brands=self.brands)
        candidates_moves: List[Dict[str, Any]] = []
        for anchor, reason, candidates, comp in comps:
            plan.groups += 1
            if anchor is None:
                for li in comp:
                    if reason == "multi_anchor":
                        passing = [a for a in candidates if a.content_key != li.content_key
                                   and self.judge(li, a).status == "pass"]
                        plan.hold(li, "multi_anchor_listing" if len(passing) > 1 else "multi_anchor", None)
                    else:
                        plan.hold(li, reason, None)
                continue
            plan.anchor_reasons[reason] += 1
            verdicts: Dict[str, Verdict] = {}
            for li in comp:
                if li.content_key == anchor.content_key:
                    continue
                verdicts[li.product_key] = self.judge(li, anchor)
            # One decision per moving content_key: every live row on it must pass against this anchor
            # with the same tier.
            by_from_ck: Dict[str, List[Listing]] = defaultdict(list)
            for li in comp:
                if li.content_key == anchor.content_key:
                    plan.already_on_anchor += 1
                    continue
                if not li.content_key:
                    plan.hold(li, "no_content_key", anchor)
                    continue
                by_from_ck[li.content_key].append(li)
            for from_ck, rows in sorted(by_from_ck.items()):
                own = [verdicts[li.product_key] for li in rows]
                failing = [(li, v) for li, v in zip(rows, own) if v.status != "pass"]
                for li, v in failing:
                    plan.record(li, v, anchor)
                if failing:
                    for li, v in zip(rows, own):
                        if v.status == "pass":
                            plan.hold(li, "key_shared_with_unmatched", anchor)
                    continue
                on_key = self.ck_members.get(from_ck)
                if on_key is None:
                    for li in rows:
                        plan.hold(li, "old_content_key_not_read", anchor)
                    continue
                rows_pks = {li.product_key for li in rows}
                if set(on_key) != rows_pks or len({v.tier for v in own}) != 1:
                    for li in rows:
                        plan.hold(li, "key_shared_with_unmatched", anchor)
                    continue
                if any(not li.product_key.startswith(MOVABLE_PREFIX) or li.is_brand_store for li in rows):
                    for li in rows:
                        plan.hold(li, "unguarded_writer", anchor)
                    continue
                groups = {li.product_key: self.snap.memberships.get(li.member) for li in rows}
                to_group = self.snap.memberships.get(anchor.member)
                if not to_group or not all(groups.values()):
                    for li in rows:
                        plan.hold(li, "no_group_membership", anchor)
                    continue
                size = own[0].anchor_size
                conflict = self.existing_conflict(from_ck, anchor.content_key or "", anchor.product_key, size)
                if conflict:
                    for li in rows:
                        plan.hold(li, conflict, anchor)
                    continue
                candidates_moves.append({"anchor": anchor, "anchor_reason": reason, "rows": rows,
                                         "verdicts": {li.product_key: verdicts[li.product_key] for li in rows},
                                         "from_ck": from_ck, "from_groups": groups, "to_group": to_group,
                                         "size": size})
        # Whole moves: a group a moving row leaves must hold only moving rows (else the next crawl
        # resolves the listing back to the member left behind). To a fixpoint: dropping one move can
        # strand another's group.
        moves = candidates_moves
        while True:
            moving_by_group: Counter = Counter()
            for m in moves:
                for g in m["from_groups"].values():
                    moving_by_group[g] += 1
            bad = {g for g, n in moving_by_group.items() if self.snap.group_sizes.get(g, 0) != n}
            keep = [m for m in moves if not (set(m["from_groups"].values()) & bad)]
            for m in moves:
                if m not in keep:
                    for li in m["rows"]:
                        plan.hold(li, "old_group_has_other_members", m["anchor"])
            if len(keep) == len(moves):
                break
            moves = keep
        # One size per target key within the run too.
        conflicted = run_size_conflicts((m["anchor"].content_key, m["size"]) for m in moves)
        for m in moves:
            if m["anchor"].content_key in conflicted:
                for li in m["rows"]:
                    plan.hold(li, "key_size_conflict", m["anchor"])
                continue
            plan.add_proposal(self, m)
        self._report_partition_vetoes(plan, [c[0] for c in comps if c[0] is not None])
        plan.finish(self)
        return plan

    def _report_partition_vetoes(self, plan: "Plan", anchors: List[Listing]) -> None:
        """Components are keyed by (tier key, conflict tokens, shade), so a listing whose tier key
        equals an anchor's but whose conflicts or shade differ never meets it: that IS the veto. Report
        it (reporting only) against the first anchor-eligible row -- a chosen anchor or any brand-store
        row -- with the same tier key on another content_key."""
        eligible: Dict[Tuple[str, Hashable], List[Listing]] = defaultdict(list)
        seen: Set[str] = set()
        for a in [*anchors, *(li for li in self.listings if li.is_brand_store)]:
            if a.product_key in seen:
                continue
            seen.add(a.product_key)
            for t in TIERS:
                if a.keys.get(t) is not None:
                    eligible[(t, a.keys[t])].append(a)
        for li in self.listings:
            if li.product_key in plan._decided or li.product_key in seen:
                continue
            for t in TIERS:
                k = li.keys.get(t)
                hit = next((a for a in eligible.get((t, k), ()) if k is not None and a.content_key != li.content_key
                            and a.sig.signature != li.sig.signature), None)
                if hit is not None:
                    reasons = [r for r in veto_reasons(li.sig.tc, hit.sig.tc) if r not in ("size", "size_unknown")]
                    plan.record(li, Verdict("vetoed", reasons or ["signature"], tiers_matched=[t]), hit)
                    break


def run_size_conflicts(moves: Iterable[Tuple[Optional[str], Optional[SizeKey]]]) -> Set[str]:
    """The target content_keys one run would give two verdict sizes (each move: (target key, size)).
    Unreachable through Planner today -- an anchor key's size is one size by construction -- and kept
    as the run-level half of "one size per content_key"."""
    sizes: Dict[str, Set[Optional[SizeKey]]] = defaultdict(set)
    for ck, size in moves:
        sizes[str(ck)].add(size)
    return {ck for ck, ss in sizes.items() if len(ss) > 1}


# ---------------------------------------------------------------------------
# Proposals + evidence
# ---------------------------------------------------------------------------

def matcher_verdict(*, tier: str, size: SizeKey, matched_offers: Sequence[Mapping[str, Any]],
                    matched_seeds: Sequence[Mapping[str, Any]], merge: bool,
                    from_key_single_size: bool = False) -> Dict[str, Any]:
    """evidence.matcher_verdict, contract v3 (identity_resolution.validate_matcher_verdict checks it).

    matched_offers: the moved row(s)' per-variant offers, every market, all at the verdict size, each
                    {offer_id, sku_key, price, currency, size, market}.
    matched_seeds:  the moved and keeper rows' seeds of exactly that size (seed_matches).
    from_key_single_size: merge only -- COMPUTED by the caller over every unit of every row on the
                    losing key (Planner.row_single_size), never assumed."""
    offers = sorted((dict(o) for o in matched_offers), key=lambda o: str(o["offer_id"]))
    v: Dict[str, Any] = {"strategy": STRATEGY, "rule_version": RULE_VERSION, "tier": tier, "same_size": True,
                         "size": _size_json(size), "matched_offers": offers,
                         "matched_seeds": [{"seed_id": str(m["seed_id"]), "variant_id": m["variant_id"]}
                                           for m in matched_seeds]}
    if merge:
        v["from_key_single_size"] = bool(from_key_single_size)
    return v


def evidence_problems(p: Mapping[str, Any]) -> List[str]:
    """Everything the writer checks before a proposal may be inserted: the engine's module rule
    (strategy, evidence.proposer, evidence.proposal_run_id, identity_resolution.validate_matcher_verdict
    -- #2637's one shared rule), the keys be#2635 reads (listing_product_key or from_product_keys,
    keeper_product_key), and that no matched offer is a mirror offer."""
    ev = p.get("evidence") or {}
    kind = p.get("kind") or ""
    problems = list(validate_matcher_verdict(kind, dict(ev), STRATEGY))
    problems += [x for x in module_proposal_problems(dict(p), STRATEGY) if x not in problems]
    v = ev.get("matcher_verdict") if isinstance(ev.get("matcher_verdict"), Mapping) else {}
    for o in v.get("matched_offers") or []:
        if isinstance(o, Mapping) and str(o.get("offer_id") or "").startswith(MIRROR_OFFER_ID_PREFIX):
            problems.append(f"matched offer {o.get('offer_id')} is a mirror offer")
    if ev.get("keeper_product_key") != p.get("keeper_product_key") or not ev.get("keeper_product_key"):
        problems.append("evidence.keeper_product_key is not the proposal's keeper")
    if kind == "attach_membership" and not ev.get("listing_product_key"):
        problems.append("evidence.listing_product_key missing")
    if kind == "merge_content_key" and not ev.get("from_product_keys"):
        problems.append("evidence.from_product_keys missing")
    if kind not in ("attach_membership", "merge_content_key"):
        problems.append(f"kind {kind!r}")
    return problems


def _key_json(k: Optional[Hashable]) -> Any:
    if k is None:
        return None
    return json.loads(json.dumps(k, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o)))


def _row_brief(li: Listing, *, offer_ids: Sequence[str] = ()) -> Dict[str, Any]:
    return {"product_key": li.product_key, "title": li.sig.title, "brand": li.sig.brand,
            "hosts": sorted(li.sig.hosts), "title_size": _size_json(li.sig.size_key),
            "offer_ids": sorted(offer_ids)}


@dataclass
class Plan:
    market: str
    brands: List[str]
    proposals: List[Dict[str, Any]] = field(default_factory=list)
    outcomes: List[Dict[str, Any]] = field(default_factory=list)  # held / deferred / vetoed, per listing
    anchor_reasons: Counter = field(default_factory=Counter)
    groups: int = 0
    already_on_anchor: int = 0
    _decided: Set[str] = field(default_factory=set)
    pairs: List[Dict[str, Any]] = field(default_factory=list)  # (moved row, anchor) for audit + stats
    new_multi_merchant_groups: int = 0

    def _outcome(self, li: Listing, status: str, reason: str, anchor: Optional[Listing],
                 reasons: Optional[List[str]] = None, tiers: Optional[List[str]] = None,
                 mirror_clear: bool = False) -> None:
        if li.product_key in self._decided:
            return
        self._decided.add(li.product_key)
        self.outcomes.append({"product_key": li.product_key, "status": status, "reason": reason,
                              "reasons": reasons or [reason], "tiers_matched": tiers or [],
                              "anchor_product_key": anchor.product_key if anchor else None,
                              "brand": li.sig.cb, "title": li.sig.title, "mirror_clear": mirror_clear})

    def hold(self, li: Listing, reason: str, anchor: Optional[Listing]) -> None:
        status = "deferred" if reason in DEFERRED_REASONS else "held"
        self._outcome(li, status, reason, anchor)

    def record(self, li: Listing, v: Verdict, anchor: Listing) -> None:
        self._outcome(li, v.status, v.reasons[0], anchor, v.reasons, v.tiers_matched, v.mirror_clear)

    def add_proposal(self, planner: Planner, m: Dict[str, Any]) -> None:
        anchor: Listing = m["anchor"]
        rows: List[Listing] = m["rows"]
        verdicts: Dict[str, Verdict] = m["verdicts"]
        tier = verdicts[rows[0].product_key].tier or ""
        size = m["size"]
        matched_offers = [mo for li in rows for mo in verdicts[li.product_key].matched_offers]
        merge = len(rows) > 1
        single = planner.from_key_single_size(m["from_ck"], size)
        mv = matcher_verdict(tier=tier, size=size, matched_offers=matched_offers,
                             matched_seeds=planner.matched_seeds([*(li.product_key for li in rows),
                                                                  anchor.product_key], size,
                                                                 verdicts[rows[0].product_key].words,
                                                                 anchor.sig.brand),
                             merge=merge, from_key_single_size=single)
        anchor_hosts = sorted({h for r in planner.by_ck.get(anchor.content_key or "", ()) for h in r.sig.hosts})
        row_detail = []
        for li in rows:
            v = verdicts[li.product_key]
            row_detail.append({**_row_brief(li, offer_ids=[o["offer_id"] for o in v.matched_offers]),
                               "tiers_matched": v.tiers_matched, "price": v.price, "price_ratio": v.price["ratio"]})
        ratios = [verdicts[li.product_key].price["ratio"] for li in rows]
        evidence: Dict[str, Any] = {
            "proposer": STRATEGY, "rule_version": RULE_VERSION, "tier": tier, "market": planner.market,
            "keeper_product_key": anchor.product_key, "anchor_reason": m["anchor_reason"],
            "anchor": _row_brief(anchor),
            "normalized_keys": {
                "brand": anchor.sig.cb, "core": anchor.sig.tc.core,
                "conflicts": sorted(anchor.sig.tc.conflicts), "shade": sorted(anchor.sig.tc.shade_tokens),
                "listing": {t: _key_json(rows[0].keys.get(t)) for t in TIERS},
                "anchor": {t: _key_json(anchor.keys.get(t)) for t in TIERS},
            },
            "sizes": {"anchor": _size_json(size),
                      "listing": {li.product_key: _size_json(li.sig.size_key) for li in rows},
                      "matched": _size_json(size)},
            "vetoes_checked": list(VETOES_CHECKED), "vetoes": [],
            "price_ratio": max(ratios, key=lambda r: abs(1 - r)) if ratios else None,
            "hosts": {"listing": sorted({h for li in rows for h in li.sig.hosts}), "anchor": anchor_hosts},
            "listings": row_detail,
            "matcher_verdict": mv,
        }
        if merge:
            p = merge_content_key_proposal(
                strategy=STRATEGY, from_content_key=m["from_ck"], to_content_key=anchor.content_key,
                from_product_keys=[li.product_key for li in rows], from_groups=m["from_groups"],
                keeper_product_key=anchor.product_key, to_product_group_id=m["to_group"],
                confidence=CONFIDENCE.get(tier), evidence=evidence)
        else:
            li = rows[0]
            evidence.update({"listing_product_key": li.product_key, "listing_title": li.sig.title,
                             "brand": li.sig.brand, "from_content_key": li.content_key,
                             "from_product_group_id": m["from_groups"][li.product_key],
                             "to_product_group_id": m["to_group"]})
            p = new_proposal(kind="attach_membership", strategy=STRATEGY,
                             subject_product_keys=[li.product_key, anchor.product_key],
                             keeper_product_key=anchor.product_key, merchant_id=li.row.get("merchant_id"),
                             content_key=anchor.content_key, confidence=CONFIDENCE.get(tier), evidence=evidence)
        self.proposals.append(p)
        for li in rows:
            self._decided.add(li.product_key)
            v = verdicts[li.product_key]
            self.pairs.append({"proposal_id": p["proposal_id"], "kind": p["kind"], "tier": tier,
                               "listing": li, "anchor": anchor, "verdict": v})

    def finish(self, planner: Planner) -> None:
        self.proposals.sort(key=lambda p: p["proposal_id"])
        self.outcomes.sort(key=lambda o: o["product_key"])
        self.pairs.sort(key=lambda x: (x["proposal_id"], x["listing"].product_key))
        gained: Dict[str, Set[str]] = defaultdict(set)
        for pr in self.pairs:
            gained[pr["anchor"].content_key].update(pr["listing"].sig.hosts)
        for ck, hosts in gained.items():
            before = {h for r in planner.by_ck.get(ck, ()) for h in r.sig.hosts}
            if len(before) < 2 and len(before | hosts) >= 2:
                self.new_multi_merchant_groups += 1

    # -- reporting --------------------------------------------------------------------------------
    def digest(self) -> str:
        return proposal_set_digest(self.proposals)

    def summary(self, *, top: int = 30, samples_per_tier: int = 20) -> List[Dict[str, Any]]:
        by_tier = Counter(p["evidence"]["tier"] for p in self.proposals)
        by_kind = Counter(p["kind"] for p in self.proposals)
        status_reason = Counter((o["status"], o["reason"]) for o in self.outcomes)
        veto_tokens = Counter(r for o in self.outcomes if o["status"] == "vetoed" for r in o["reasons"])
        brands = Counter(pr["listing"].sig.cb for pr in self.pairs)
        host_pairs = Counter(f"{'|'.join(sorted(pr['listing'].sig.hosts)) or '?'} -> "
                             f"{'|'.join(sorted(pr['anchor'].sig.hosts)) or '?'}" for pr in self.pairs)
        mirror_extra = sum(1 for o in self.outcomes if o["reason"] == "offer_not_variant_pinned" and o["mirror_clear"])
        lines: List[Dict[str, Any]] = [
            {"section": "proposals", "market": self.market, "brands": self.brands, "groups": self.groups,
             "proposals": len(self.proposals), "listings_moving": len(self.pairs),
             "by_tier": dict(sorted(by_tier.items())), "by_kind": dict(sorted(by_kind.items())),
             "anchor_reasons": dict(sorted(self.anchor_reasons.items())),
             "already_on_anchor": self.already_on_anchor,
             "expected_new_multi_merchant_groups": self.new_multi_merchant_groups,
             "digest": self.digest(), "rule_version": RULE_VERSION},
            {"section": "yield_both_ways", "market": self.market,
             "per_variant_only_listings": len(self.pairs),
             "mirror_recheck_additional": mirror_extra,
             "note": ("listings held offer_not_variant_pinned whose mirror / ::canonical offer is priced in the "
                      "market at the anchor's size: proposable only if mirror offers were pinned with a "
                      "read-time price + size re-check (Peng decides)")},
            {"section": "outcomes", "market": self.market,
             "held": {r: n for (s, r), n in sorted(status_reason.items()) if s == "held"},
             "deferred": {r: n for (s, r), n in sorted(status_reason.items()) if s == "deferred"},
             "vetoed": {r: n for (s, r), n in sorted(status_reason.items()) if s == "vetoed"},
             "veto_tokens": dict(veto_tokens.most_common())},
            {"section": "top_markets", "markets": {self.market: len(self.pairs)}},
            {"section": "top_brands", "market": self.market,
             "brands": [{"brand": b, "listings": n} for b, n in sorted(brands.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]},
            {"section": "host_pairs", "market": self.market,
             "pairs": [{"pair": k, "listings": n} for k, n in sorted(host_pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]},
        ]
        for tier in TIERS:
            sample = [p for p in self.proposals if p["evidence"]["tier"] == tier][:samples_per_tier]
            lines.append({"section": "samples", "market": self.market, "tier": tier, "proposals": [
                {"proposal_id": p["proposal_id"], "kind": p["kind"], "keeper": p["keeper_product_key"],
                 "anchor_title": p["evidence"]["anchor"]["title"],
                 "listings": [{"title": d["title"], "hosts": d["hosts"], "ratio": d["price_ratio"]}
                              for d in p["evidence"]["listings"]],
                 "size": p["evidence"]["matcher_verdict"]["size"]} for p in sample]})
        return lines

    def audit_rows(self, *, size: int = 600, seed: int = 20261016) -> List[Dict[str, Any]]:
        """A seeded RANDOM sample of (moved listing, anchor) pairs for Peng's side-by-side review.
        Catalog fields only: no personal data."""
        pool = list(self.pairs)
        rng = random.Random(seed)
        picked = pool if len(pool) <= size else rng.sample(pool, size)
        picked.sort(key=lambda x: (x["proposal_id"], x["listing"].product_key))
        out = []
        for pr in picked:
            li, an, v = pr["listing"], pr["anchor"], pr["verdict"]
            out.append({
                "proposal_id": pr["proposal_id"], "kind": pr["kind"], "tier": pr["tier"], "market": self.market,
                "size": _size_json(v.anchor_size), "price_ratio": v.price.get("ratio"),
                "listing": _audit_side(li, v.price.get("listing"), v.price.get("listing_currency"),
                                       [o["offer_id"] for o in v.matched_offers]),
                "anchor": _audit_side(an, v.price.get("anchor"), v.price.get("anchor_currency"), ()),
            })
        return out


def _audit_side(li: Listing, price: Any, currency: Any, offer_ids: Sequence[str]) -> Dict[str, Any]:
    return {"product_key": li.product_key, "title": li.sig.title, "brand": li.sig.brand,
            "size": _size_json(li.sig.size_key), "price": price, "currency": currency,
            "image_url": li.row.get("image_url"), "canonical_url": li.row.get("canonical_url"),
            "hosts": sorted(li.sig.hosts), "offer_ids": sorted(offer_ids)}


def proposal_set_digest(proposals: Sequence[Mapping[str, Any]]) -> str:
    """Deterministic digest of a proposal set: each proposal's id and what it says it will do
    (identity_resolution.proposal_fingerprint: kind, keeper, target, subjects, evidence), sorted."""
    lines = sorted(f"{p['proposal_id']}\t{proposal_fingerprint(dict(p))}" for p in proposals)
    return "cmd_" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:32]


def stamp_run(proposals: Sequence[Mapping[str, Any]], run_id: str) -> List[Dict[str, Any]]:
    """Copies of `proposals` whose evidence carries the proposal run id (one per --write-proposals):
    the run identity_resolution.approve_module_run approves as a whole. proposal_id / proposal_key do
    not depend on it, so a re-run dedupes onto the rows already there (still in the earlier run)."""
    return [{**p, "evidence": {**(p.get("evidence") or {}), "proposal_run_id": run_id}} for p in proposals]


def plan_market(snap: Snapshot, market: str, *, brands: Optional[Sequence[str]] = None,
                limit_groups: Optional[int] = None) -> Plan:
    return Planner(snap, market, brands=brands, limit_groups=limit_groups).plan()


def gtin_lookup_values(raw_gtins: Iterable[Any]) -> List[str]:
    """Every stored spelling of the candidates' valid GTINs (14 digits and its 13/12/8-digit forms) for
    an equality lookup on catalog_products.gtin (idx_catalog_products_gtin)."""
    from services.product_match_normalizer import gtin_canonical

    out: Set[str] = set()
    for raw in raw_gtins:
        g = gtin_canonical(raw)
        if not g:
            continue
        out.add(str(raw).strip())
        for n in (14, 13, 12, 8):
            if g[: 14 - n].strip("0") == "":
                out.add(g[14 - n:])
    return sorted(out)


def brand_sql_regex(brands: Sequence[str]) -> Optional[str]:
    """A POSIX regex over lower(coalesce(brand, '')) that selects a SUPERSET of the rows whose
    canonical_brand is one of `brands`: an alternation of literal tokens -- each requested spelling
    (raw and accent-folded), its words of 3+ characters, its canonical key, and every measured spelling
    key of its family. Literal tokens of 3+ characters let the trigram GIN index
    idx_catalog_products_brand_trgm (migration 059, on lower(coalesce(brand, ''))) serve the filter.
    The exact canonical_brand filter then runs in Python. None for no brands."""
    from services.brand_spelling import RETAILER_BRAND_SPELLINGS

    wanted = {canonical_brand(b) for b in brands} - {""}
    if not wanted:
        return None
    tokens: Set[str] = set(wanted)
    tokens |= {k for k, family in RETAILER_BRAND_SPELLINGS.items() if family in wanted}
    for b in brands:
        for form in (str(b).strip().lower(), _fold(b).strip()):
            tokens.add(form)
            tokens |= {w for w in re.split(r"[^\w]+", form) if len(w) >= 3}
    tokens = {t for t in tokens if len(t) >= 3}
    return "(" + "|".join(re.escape(t) for t in sorted(tokens)) + ")"


__all__ = [
    "CONFIDENCE", "PRICE_RATIO_MAX", "PRICE_RATIO_MIN", "RULE_VERSION", "STRATEGY", "Plan", "Planner",
    "Snapshot", "brand_sql_regex", "evidence_problems", "matcher_verdict", "member_fingerprint",
    "plan_market", "proposal_set_digest", "seed_matches", "stamp_run",
]
