"""`cross_merchant_same_size`: PROPOSE joining a retailer listing to the same product at another
merchant (phase 1 of the cross-merchant matching design, PR "M4"). It never approves or applies.

WHY. Census v2 (2026-10-16): every one of 12,477 US products with retailer offers has exactly ONE
retailer host -- each retailer URL mints its own listing and content_key, and only an exact key ever
joins an existing product. This strategy finds the listings that are provably the SAME buyable item
as a product already in the catalog and proposes moving each onto it (its content_key and product
group), through the identity engine (services.identity_resolution): attach_membership for a listing
alone on its content_key, merge_content_key when several live rows share it and must move together.

IDENTITY (Peng, 2026-10-16). A product is brand + line + formulation; size and shade are variants.
PHASE 1 MERGES SAME-SIZE ONLY: the product joined has exactly one size and the listing's matched
offers are that size (offers.resolve, agent_pdp_view and the gateway sibling path have no size
awareness yet).

STAR TOPOLOGY. Listings are grouped by the deterministic tiers (services.product_match_tiers: T0a
GTIN+core+size, T0b exact title, T0c ordered core+size), each tier key partitioned by the veto
signature (conflict tokens, shade), and the groups are joined into components. Each component has
ONE anchor:
  - the brand store's product: a row that is not an ext:retailer listing and is brand-direct (a
    brand_direct offer in the market, or seed_kind 'self'), when exactly ONE content_key holds such
    rows; two or more such content_keys -> every listing of the component is held (`multi_anchor`;
    a listing that matches two of them directly is `multi_anchor_listing`);
  - else the component's oldest signed live product (`oldest_listing`).
Every other listing is judged against the anchor ONLY -- never retailer-to-retailer. A listing in
the component that does not match the anchor directly under some tier is held
(`no_direct_anchor_match`), even if it matches another listing that does.

THE JUDGEMENT (listing vs anchor), in order:
  1. a tier key equal (T0a > T0b > T0c; the best one is the proposal's tier);
  2. vetoes: veto_reasons' conflict tokens (EDT/EDP, SPF/PA, plus, new/renewal/version, refill,
     tester, mini, set/duo, multipack ...) and shade; differing valid GTINs (`gtin_differs`); a
     title size that is not the anchor's (`size`);
  3. the anchor's content_key states exactly ONE size across its rows' titles, SKUs and offers,
     else DEFERRED to phase 2 (`anchor_multi_size`, `anchor_size_unknown`);
  4. the listing's offers whose own size (SKU title / visible option labels / sku_payload option
     fields; the listing title only when the listing has exactly one priced offer in the market)
     equals the anchor's size: `listing_offer_ids`. None -> held (`offer_size_unpinned`). PHASE 1
     moves a listing only when EVERY live priced offer it has in the market is pinned to that size: a
     listing that also sells other (or unreadable) sizes is held (`listing_multi_size`), because
     moving it whole would put those offers on the anchor's single-size PDP, and agent_pdp_view
     aggregates price ranges and variants with no size awareness;
  5. same-size price: the cheapest matched offer (in-stock ones first) against the anchor's cheapest
     offer in the market; no price (`price_unknown`) or two currencies (`price_currency_mismatch`)
     -> held; a ratio above 2.0 or below 0.5 -> vetoed (`price_ratio`: sets, minis, testers).
Then the WHOLE-MOVE rules: every live row on the listing's content_key must be in the same component
and pass against the same anchor with the same tier (else `key_shared_with_unmatched`); every row
that moves must be one the crawl door's resolver keeps (product_key 'ext:', else
`unguarded_writer`) and hold a product_group_members row, as must the anchor (`no_group_membership`);
and each group a moving row leaves must hold only moving rows (`old_group_has_other_members`).

EVIDENCE CONTRACT (read by be#2635's resolved-group read and the engine): attach_membership carries
evidence.listing_product_key + keeper_product_key; merge_content_key carries
evidence.from_product_keys + keeper_product_key; both carry evidence.proposer and
evidence.matcher_verdict (built by `matcher_verdict`; checked by the ENGINE's
identity_resolution.validate_matcher_verdict / module_proposal_problems -- one shared rule, #2637).
The strategy is one of the engine's MODULE_ONLY_STRATEGIES: only this module produces its proposals,
and identity_engine approves one whole proposal run of them (approve --strategy --run --expect-digest).

Pure: no I/O here. scripts/ops/propose_cross_merchant_matches.py exports the snapshot (one read-only
transaction) and writes proposals through the engine's writer.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Hashable, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from services.identity_resolution import (
    member_fingerprint,
    merge_content_key_proposal,
    module_proposal_problems,
    new_proposal,
    proposal_fingerprint,
    validate_matcher_verdict,
)
from services.product_match_normalizer import canonical_brand, veto_reasons
from services.product_match_tiers import (
    TIERS,
    ListingSignals,
    SizeKey,
    fold_host,
    gtin_pollution,
    text_sizes,
    tier_keys,
)

STRATEGY = "cross_merchant_same_size"
RULE_VERSION = "cmss.v1"
RETAILER_LISTING_PREFIX = "ext:retailer:"
#: Rows the crawl door's resolver keeps on their stored content_key once the engine moved them
#: (identity_resolution.RESOLVER_GUARDED_PREFIX). A first-party row is re-keyed by its own sync.
MOVABLE_PREFIX = "ext:"
PRICE_RATIO_MAX = 2.0
PRICE_RATIO_MIN = 0.5
CONFIDENCE = {"T0a": 0.97, "T0b": 0.95, "T0c": 0.9}
IN_STOCK = frozenset({"in_stock", "instock", "limited_stock", "low_stock", "available"})
VETOES_CHECKED: Tuple[str, ...] = ("conflict_tokens", "shade", "gtin_differs", "size", "anchor_single_size",
                                   "offer_size_pinned", "listing_single_size", "price_ratio")
#: sku_payload fields that can name a variant's size (the export reads only these).
SKU_PAYLOAD_SIZE_FIELDS: Tuple[str, ...] = ("variant_title", "title", "size", "option1", "option2", "option3",
                                            "options", "selected_options")

# Outcome reasons. HELD goes to a human; DEFERRED waits for phase 2 (size-aware serving); VETOED is a
# different buyable item. None of them is ever proposed.
HELD_REASONS = ("multi_anchor", "multi_anchor_listing", "no_direct_anchor_match", "offer_size_unpinned",
                "listing_multi_size",
                "price_unknown", "price_currency_mismatch", "key_shared_with_unmatched", "unguarded_writer",
                "no_group_membership", "old_group_has_other_members", "no_content_key",
                "old_content_key_not_read")
DEFERRED_REASONS = ("anchor_multi_size", "anchor_size_unknown")


# ---------------------------------------------------------------------------
# Snapshot
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


def offer_text_sizes(offer: Mapping[str, Any]) -> Set[SizeKey]:
    """The sizes an offer's own variant states: its SKU title, visible option labels and the
    sku_payload option fields."""
    sizes: Set[SizeKey] = set()
    for text in [offer.get("sku_title"), *_flatten_text(offer.get("sku_labels")),
                 *_flatten_text(offer.get("sku_size_fields"))]:
        sizes |= text_sizes(text)
    return sizes


def _price(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


@dataclass
class Offer:
    offer_id: str
    product_key: str
    host: str
    brand_direct: bool
    in_stock: bool
    price: Optional[float]
    currency: str
    stated_sizes: Set[SizeKey]
    size: Optional[SizeKey] = None  # pinned: the one size this offer is, else None


@dataclass
class Listing:
    """A signed live product in the market with its offers and SKU sizes."""

    sig: ListingSignals
    row: Mapping[str, Any]
    offers: List[Offer]
    sku_sizes: Set[SizeKey]
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

    def title_sizes(self) -> Set[SizeKey]:
        return text_sizes(self.sig.title)

    def all_sizes(self) -> Set[SizeKey]:
        out = set(self.title_sizes()) | set(self.sku_sizes)
        for o in self.offers:
            out |= o.stated_sizes
        return out


@dataclass
class Snapshot:
    """Everything the planner reads, exported in one read-only transaction (or built by a test).

    products:  signed live catalog_products with a live offer in the market (product_key,
               merchant_id, platform, source_product_id, content_key, pivota_signature_id, brand,
               title, gtin, source_system, seed_kind, created_at, image_url, canonical_url).
    offers:    the market's live offers of those products (offer_id, product_key, offer_type,
               availability, currency, price, host, sku_title, sku_labels, sku_size_fields).
    skus:      every live SKU of those products ({product_key, title, labels}), for sizes.
    ck_rows:   content_key -> every NON-SUPPRESSED row on it (product_key), whatever its sync status:
               what the engine's left-behind post-check counts.
    memberships: (merchant_id, platform, source_product_id) -> product_group_id.
    group_sizes: product_group_id -> member count.
    """

    products: Sequence[Mapping[str, Any]]
    offers: Sequence[Mapping[str, Any]]
    skus: Sequence[Mapping[str, Any]]
    ck_rows: Mapping[str, Sequence[str]]
    memberships: Mapping[Tuple[str, str, str], str]
    group_sizes: Mapping[str, int]


def build_market_listings(snap: Snapshot, market: str) -> List[Listing]:
    offers_by_pk: Dict[str, List[Offer]] = defaultdict(list)
    for o in snap.offers:
        if o.get("market") not in (None, market):
            continue
        offers_by_pk[str(o["product_key"])].append(Offer(
            offer_id=str(o["offer_id"]), product_key=str(o["product_key"]), host=fold_host(o.get("host")),
            brand_direct=str(o.get("offer_type") or "") == "brand_direct",
            in_stock=str(o.get("availability") or "").strip().lower() in IN_STOCK,
            price=_price(o.get("price")), currency=str(o.get("currency") or "").strip().upper(),
            stated_sizes=offer_text_sizes(o)))
    sku_sizes: Dict[str, Set[SizeKey]] = defaultdict(set)
    sku_titles: Dict[str, List[str]] = defaultdict(list)
    for s in snap.skus:
        pk = str(s["product_key"])
        for text in [s.get("title"), *_flatten_text(s.get("labels"))]:
            sku_sizes[pk] |= text_sizes(text)
        if s.get("title"):
            sku_titles[pk].append(str(s["title"]))
    out: List[Listing] = []
    for p in snap.products:
        pk = str(p["product_key"])
        offers = sorted(offers_by_pk.get(pk, []), key=lambda o: o.offer_id)
        if not offers:
            continue
        hosts = {o.host for o in offers if o.host}
        sig = ListingSignals(p, hosts, any(o.brand_direct for o in offers), sku_titles.get(pk, ()))
        listing = Listing(sig=sig, row=p, offers=offers, sku_sizes=sku_sizes.get(pk, set()))
        _pin_offer_sizes(listing)
        out.append(listing)
    return out


def _pin_offer_sizes(listing: Listing) -> None:
    """Each offer's size: the one its own variant states; else, for a listing with exactly ONE priced
    offer in the market, the listing title's size. Two stated sizes, or none, leave it unpinned."""
    priced = [o for o in listing.offers if o.price is not None]
    title_size = listing.sig.size_key
    for o in listing.offers:
        if len(o.stated_sizes) == 1:
            o.size = next(iter(o.stated_sizes))
        elif not o.stated_sizes and len(priced) == 1 and priced[0] is o and title_size is not None:
            o.size = title_size


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
    offer_ids: List[str] = field(default_factory=list)
    price: Dict[str, Any] = field(default_factory=dict)


def _side_price(offers: Iterable[Offer]) -> Tuple[Optional[float], str, str]:
    """(cheapest price, its currency, basis): in-stock offers first, else every priced offer."""
    priced = [o for o in offers if o.price is not None]
    stock = [o for o in priced if o.in_stock]
    pool, basis = (stock, "in_stock") if stock else (priced, "any")
    if not pool:
        return None, "", "none"
    best = min(pool, key=lambda o: (o.price, o.offer_id))
    return best.price, best.currency, basis


class Planner:
    """Plans one market's proposals from a Snapshot. Pure."""

    def __init__(self, snap: Snapshot, market: str, *, brands: Optional[Sequence[str]] = None,
                 limit_groups: Optional[int] = None):
        self.snap = snap
        self.market = market
        self.all_listings = build_market_listings(snap, market)
        self.by_pk: Dict[str, Listing] = {li.product_key: li for li in self.all_listings}
        self.pollution = gtin_pollution(li.sig for li in self.all_listings)
        for li in self.all_listings:
            li.keys = tier_keys(li.sig, self.pollution.polluted)
        wanted = {canonical_brand(b) for b in brands or ()} - {""}
        self.brands = sorted(wanted)
        self.listings = [li for li in self.all_listings if not wanted or li.sig.cb in wanted]
        self.limit_groups = limit_groups
        self._anchor_sizes: Dict[str, Set[SizeKey]] = {}
        self.by_ck: Dict[str, List[Listing]] = defaultdict(list)
        for li in self.all_listings:
            if li.content_key:
                self.by_ck[li.content_key].append(li)

    # -- the anchor's size: every size its content_key's rows state --------------------------------
    def anchor_sizes(self, anchor_ck: str) -> Set[SizeKey]:
        cached = self._anchor_sizes.get(anchor_ck)
        if cached is None:
            cached = set()
            for li in self.by_ck.get(anchor_ck, ()):
                cached |= li.all_sizes()
            self._anchor_sizes[anchor_ck] = cached
        return set(cached)

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
        sizes = self.anchor_sizes(anchor.content_key or "")
        anchor_size = next(iter(sizes)) if len(sizes) == 1 else None
        listing_title_size = li.sig.size_key
        if listing_title_size is not None and anchor_size is not None and listing_title_size != anchor_size:
            vetoes.append("size")
        if vetoes:
            return Verdict("vetoed", vetoes, tiers_matched=matched)
        if not sizes:
            return Verdict("deferred", ["anchor_size_unknown"], tiers_matched=matched)
        if anchor_size is None:
            return Verdict("deferred", ["anchor_multi_size"], tiers_matched=matched)
        pinned = [o for o in li.offers if o.size == anchor_size]
        if not pinned:
            return Verdict("held", ["offer_size_unpinned"], tiers_matched=matched, anchor_size=anchor_size)
        if any(o.price is not None and o.size != anchor_size for o in li.offers):
            # Phase 1: a priced offer of another (or an unreadable) size would ride along onto the
            # anchor's single-size PDP. Move the listing only when ALL its priced offers are this size.
            return Verdict("held", ["listing_multi_size"], tiers_matched=matched, anchor_size=anchor_size)
        lp, lc, lbasis = _side_price(pinned)
        ap, ac, abasis = _side_price(anchor.offers)  # the anchor row's own price (single-size product)
        price = {"listing": lp, "anchor": ap, "listing_currency": lc, "anchor_currency": ac,
                 "listing_basis": lbasis, "anchor_basis": abasis, "ratio": None}
        if lp is None or ap is None:
            return Verdict("held", ["price_unknown"], tiers_matched=matched, anchor_size=anchor_size, price=price)
        if lc != ac:
            return Verdict("held", ["price_currency_mismatch"], tiers_matched=matched, anchor_size=anchor_size,
                           price=price)
        ratio = round(lp / ap, 4)
        price["ratio"] = ratio
        if ratio > PRICE_RATIO_MAX or ratio < PRICE_RATIO_MIN:
            return Verdict("vetoed", ["price_ratio"], tiers_matched=matched, anchor_size=anchor_size, price=price)
        return Verdict("pass", [], tier=matched[0], tiers_matched=matched, anchor_size=anchor_size,
                       offer_ids=sorted(o.offer_id for o in pinned), price=price)

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

    # -- the plan ---------------------------------------------------------------------------------
    def plan(self) -> "Plan":
        comps = []
        for comp in self.components():
            anchor, reason, candidates = self.choose_anchor(comp)
            comps.append((anchor, reason, candidates, comp))
        comps.sort(key=lambda c: (c[0].product_key if c[0] else c[3][0].product_key))
        if self.limit_groups is not None:
            comps = comps[: self.limit_groups]
        plan = Plan(market=self.market, brands=self.brands)
        comp_of: Dict[str, int] = {}
        for i, (_a, _r, _c, comp) in enumerate(comps):
            for li in comp:
                comp_of[li.product_key] = i
        candidates_moves: List[Dict[str, Any]] = []
        for i, (anchor, reason, candidates, comp) in enumerate(comps):
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
                on_key = self.snap.ck_rows.get(from_ck)
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
                candidates_moves.append({"anchor": anchor, "anchor_reason": reason, "rows": rows,
                                         "verdicts": {li.product_key: verdicts[li.product_key] for li in rows},
                                         "from_ck": from_ck, "from_groups": groups, "to_group": to_group})
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
        for m in moves:
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


# ---------------------------------------------------------------------------
# Proposals + evidence
# ---------------------------------------------------------------------------

def _size_json(sk: Optional[SizeKey]) -> Optional[Dict[str, Any]]:
    if sk is None:
        return None
    value, unit, pack = sk
    return {"value": float(value), "unit": unit, "pack": int(pack)}


def matcher_verdict(*, tier: str, size: SizeKey, listing_offer_ids: Sequence[str], merge: bool) -> Dict[str, Any]:
    """evidence.matcher_verdict, the contract be#2635 and the engine's approval read."""
    v: Dict[str, Any] = {"strategy": STRATEGY, "rule_version": RULE_VERSION, "tier": tier, "same_size": True,
                         "size": _size_json(size), "listing_offer_ids": sorted(set(listing_offer_ids))}
    if merge:
        v["from_key_single_size"] = True
    return v


def evidence_problems(p: Mapping[str, Any]) -> List[str]:
    """Everything the writer checks before a proposal may be inserted: the engine's module rule
    (strategy, evidence.proposer, identity_resolution.validate_matcher_verdict -- #2637's one shared
    rule) plus the keys be#2635 reads (listing_product_key or from_product_keys, keeper_product_key)."""
    ev = p.get("evidence") or {}
    kind = p.get("kind") or ""
    problems = list(validate_matcher_verdict(kind, dict(ev), STRATEGY))
    problems += [x for x in module_proposal_problems(dict(p), STRATEGY) if x not in problems]
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
                 reasons: Optional[List[str]] = None, tiers: Optional[List[str]] = None) -> None:
        if li.product_key in self._decided:
            return
        self._decided.add(li.product_key)
        self.outcomes.append({"product_key": li.product_key, "status": status, "reason": reason,
                              "reasons": reasons or [reason], "tiers_matched": tiers or [],
                              "anchor_product_key": anchor.product_key if anchor else None,
                              "brand": li.sig.cb, "title": li.sig.title})

    def hold(self, li: Listing, reason: str, anchor: Optional[Listing]) -> None:
        status = "deferred" if reason in DEFERRED_REASONS else "held"
        self._outcome(li, status, reason, anchor)

    def record(self, li: Listing, v: Verdict, anchor: Listing) -> None:
        self._outcome(li, v.status, v.reasons[0], anchor, v.reasons, v.tiers_matched)

    def add_proposal(self, planner: Planner, m: Dict[str, Any]) -> None:
        anchor: Listing = m["anchor"]
        rows: List[Listing] = m["rows"]
        verdicts: Dict[str, Verdict] = m["verdicts"]
        tier = verdicts[rows[0].product_key].tier or ""
        size = verdicts[rows[0].product_key].anchor_size
        offer_ids = sorted({oid for li in rows for oid in verdicts[li.product_key].offer_ids})
        merge = len(rows) > 1
        mv = matcher_verdict(tier=tier, size=size, listing_offer_ids=offer_ids, merge=merge)
        anchor_hosts = sorted({h for r in planner.by_ck.get(anchor.content_key or "", ()) for h in r.sig.hosts})
        row_detail = []
        for li in rows:
            v = verdicts[li.product_key]
            row_detail.append({**_row_brief(li, offer_ids=v.offer_ids), "tiers_matched": v.tiers_matched,
                               "price": v.price, "price_ratio": v.price["ratio"]})
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
        lines: List[Dict[str, Any]] = [
            {"section": "proposals", "market": self.market, "brands": self.brands, "groups": self.groups,
             "proposals": len(self.proposals), "listings_moving": len(self.pairs),
             "by_tier": dict(sorted(by_tier.items())), "by_kind": dict(sorted(by_kind.items())),
             "anchor_reasons": dict(sorted(self.anchor_reasons.items())),
             "already_on_anchor": self.already_on_anchor,
             "expected_new_multi_merchant_groups": self.new_multi_merchant_groups,
             "digest": self.digest(), "rule_version": RULE_VERSION},
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
                "listing": _audit_side(li, v.price.get("listing"), v.price.get("listing_currency"), v.offer_ids),
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


__all__ = [
    "CONFIDENCE", "PRICE_RATIO_MAX", "PRICE_RATIO_MIN", "RULE_VERSION", "STRATEGY", "Plan", "Planner",
    "Snapshot", "evidence_problems", "matcher_verdict", "member_fingerprint", "plan_market",
    "proposal_set_digest", "stamp_run",
]
