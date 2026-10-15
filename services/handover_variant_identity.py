"""Name the MERCHANT's own variant at hand-over, out of `catalog_skus`, or refuse.

WHY THIS EXISTS. The gateway's cart prefill and the checkout preflight both key on one value,
`cart_variant_id`, and until this module the only producer of that value for a crawl seed was
`shopify_variant_identity.sole_stamped_variant_id`, which reads
`seed_data.snapshot.variants[].shopify_variant_id`. Measured on prod 2026-09-08: **0 of 11,834
active seeds carry that key**, and 0 carry `snapshot.storefront_platform` either — the producer
(`scripts/backfill_shopify_variant_ids.py`) has never run at scale. So the prefill fired on zero
offers, the preflight gate behind it fired on zero offers, and the shadow report it feeds had an
empty denominator BY CONSTRUCTION. A gate that applies to nothing measures nothing.

The identity is not missing. It is in `catalog_skus.source_variant_id`, put there by
`scripts/backfill_variant_identity_skus.py` (6,087 SKU+offer pairs across 3,279 products on
2026-09-08) and by `catalog_enrichment_agent/ingestion.py`. One layer holds the evidence and a
different layer reads a field nobody writes. This module closes that seam and nothing else.

WHAT IT WILL AND WILL NOT SAY.

  * It accepts a row ONLY when `services.variant_identity.variant_id_provenance` calls the id
    MERCHANT_ISSUED. PRODUCT_DERIVED and UNVERIFIABLE are refused — they are exactly the shapes
    the safety kernel's `isRestatedProductId` throws away, and 39.4% + 13.3% of the SKU table is
    made of them.
  * The `sku_payload.variant_id_provenance` stamp may only VETO, never authorise. Read the order
    carefully: the classifier is run on every row, and the stamp is then required to agree.
    "Prefer the stamp" would let a stamp written by a future careless writer promote an id the
    classifier refuses, and this column is the one checkout reads as identity. (Measured on prod
    2026-09-08 over 20,898 live external SKUs: the stamp is present on 7,174 and disagrees with
    the classifier on **0**, so requiring agreement costs nothing today and bounds tomorrow.)
  * When a product has MORE THAN ONE live merchant-issued SKU it refuses, unless the hand-over
    itself names one of them EXACTLY. The redirect is built at product grain — the buyer has not
    chosen a variant — so picking one would be the `defaultVariant` guess that
    [[reap_variant_ids_are_reap_side_and_availability_ordered]] records as a live wrong-size
    hazard on a real storefront. An exact string equality against a stored merchant-issued id is
    not a guess; a preference order over candidates would be.
  * The seed-stamp path is kept, but ONLY where `catalog_skus` said nothing at all. If catalog
    holds candidates and refused them, the snapshot disagreeing with catalog is a reason to stay
    silent, not a second opinion to fall back on. ("A conditional fallback is still a fallback" —
    the round-5 P0 in `agent_shop_gateway._external_seed_redirect_identity`.)
  * A lookup that fails or was never primed answers `lookup_failed` / `not_primed`, which carry
    no id. Every failure here degrades the hand-over to an honest referral; none of them deletes
    an offer.

WHAT IT DOES NOT DECIDE. Whether a Shopify cart permalink can be BUILT at all — that is
`storefront_is_shopify` plus `resolve_cart_permalink`, and it is a separate piece of evidence
about the STOREFRONT rather than about the variant. Naming the variant is necessary and not
sufficient; see the note in `shopify_variant_identity.storefront_is_shopify`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import parse_qs, urlparse

from services.outbound_links_service import extract_shopify_numeric_variant_id
from services.variant_identity import MERCHANT_ISSUED, is_merchant_issued_variant_id

logger = logging.getLogger("handover_variant_identity")

#: Where a resolved id came from. Emitted so a wrong cart can be traced to a layer.
SOURCE_CATALOG_SKU = "catalog_sku"
SOURCE_SEED_STAMP = "seed_stamp"

#: Outcomes. A CLOSED set, because they are counted per request and grouped in the coverage
#: line; a free-text reason makes that line unaggregatable (same rule as
#: `checkout_preflight`'s reason vocabulary).
R_SOLE = "sole_merchant_issued_sku"
R_EXACT = "exact_merchant_issued_sku"
R_SEED_STAMP = "seed_stamped_sole_variant"
R_AMBIGUOUS = "multiple_merchant_issued_skus"
#: The hand-over names a merchant-issued id, and NO live `catalog_skus` row carries it. Two
#: merchant-issued ids disagreeing about one hand-over is a contradiction, and naming either is
#: a guess — so this refuses even when there is exactly ONE candidate. Review of the first cut
#: found the sole-candidate path returning BEFORE any comparison with the name, so a lone
#: catalog row silently overrode an operator-attached variant: a $95 Mini prefilled for the
#: $140 Standard the offer actually named, which is the wrong-size hazard this module exists
#: to refuse, reached through the one door it had left open.
R_CONTRADICTED = "hand_over_names_a_different_merchant_issued_id"
R_NO_IDENTITY = "no_merchant_issued_sku"
#: ONE live candidate, but the seed stores several variants and the offer being handed over is
#: one the candidate is not. The sole-candidate path used to hand that one id to EVERY offer of
#: the seed whose own name was a SKU (a SKU cannot contradict), so a two-variant seed with one
#: catalog row gave the sibling offer the other variant's cart, quoting one variant's price for
#: another's checkout. Found 2026-10-14 in review of #2620 / #2623 (the operator-id twin).
R_SIBLING = "sole_sku_is_another_stored_variant"
R_NO_PRODUCT_KEY = "no_attached_product_key"
R_NOT_PRIMED = "not_primed"
R_LOOKUP_FAILED = "sku_lookup_failed"

#: Why a `catalog_skus` row was not admitted as a candidate. Counted, never silent.
X_NOT_MERCHANT_ISSUED = "not_merchant_issued"
X_STAMP_VETO = "stamp_vetoed"
X_PAYLOAD_DISAGREES = "stored_id_disagrees_with_payload"

#: Distinguishes "the caller did not distinguish matching from contradicting" from "the caller
#: looked and found NO identity claim". `None` cannot carry that difference, and conflating them
#: is what let a barcode veto a stamp: the route passes the empty string when a variant entry
#: carries only a `sku`, and that must mean "nothing claims to be a variant id here", not "fall
#: back to the broad chain".
_UNSET = object()

_DEFAULT_TIMEOUT_S = 0.5
#: A resolve can touch hundreds of seed rows; the lookup is ONE statement over an indexed
#: column, but the IN-list still has to be bounded or a wide request writes an unbounded query.
#: Keys past the cap are simply never primed, and `choose` then answers `not_primed`, which
#: carries no id — the cap degrades hand-overs, it cannot publish a wrong one.
_DEFAULT_MAX_KEYS = 400

_SELECT = """
    SELECT s.sku_key, s.product_key, s.source_product_id, s.source_variant_id, s.sku_payload
      FROM catalog_skus s
      JOIN catalog_products cp ON cp.product_key = s.product_key
     WHERE s.suppressed_at IS NULL
       AND cp.suppressed_at IS NULL
       AND s.product_key IN ({placeholders})
"""


@dataclass(frozen=True)
class Candidate:
    """One live `catalog_skus` row whose id we can positively place as the merchant's.

    TWO SPELLINGS, and only ONE of them decides anything. `variant_id` is the CANONICAL form
    every consumer gets and the only one `answers_to` compares. `stored_variant_id` is the
    column's own value and is read by NO production code — an earlier note claimed you could
    grep a wrong cart back to it, which was false, because neither it nor `sku_key` is emitted
    anywhere. It is kept because a debugger stepping through a wrong hand-over needs to see
    what the row actually said, and for nothing else. They differ only for
    `gid://shopify/ProductVariant/<n>`, which `services.variant_identity` admits as identity and
    which every other reader in this lane has always normalised away
    (`outbound_links_service.extract_shopify_numeric_variant_id`, `shopify_variant_identity._numeric_id`).
    Review of the first cut found the raw gid reaching `offer_spec["variant_id"]` — documented
    one line above itself as "the NUMERIC storefront variant id" — and reaching the preflight,
    where `live_offer_verification._check_one` string-compares it against `parse_product_js`'s
    bare-numeric ids and so can NEVER match. Normalising once, here, is the only place that
    cannot drift.
    """

    sku_key: str
    product_key: str
    variant_id: str
    stamped: bool
    stored_variant_id: str = ""
    #: The OTHER parent `variant_id_provenance` compares against. Carried because the
    #: contradiction check has to ask exactly the question admission asked, and admission passes
    #: both — `ingestion.py` restates `product_key` while `onboard_external_brand_from_crawl`
    #: restates `source_product_id`, so a predicate holding only one of them calls half the
    #: forgeries identity.
    source_product_id: str = ""

    def answers_to(self, name: str) -> bool:
        """True when `name` is this variant, in EITHER spelling. Equality, never a preference.

        One comparison, not two. A first version also checked `name in (self.variant_id,
        self.stored_variant_id)`; review showed that clause is subsumed — `variant_id` is
        already canonical and `stored_variant_id` is only ever the gid whose canonical form IS
        `variant_id` — and removing it left every test green, which is the definition of dead
        code dressed as a guard. `stored_variant_id` is kept as the column's own value for
        diagnostics (`sku_key` and it are what you grep for), not as a matching input.
        """
        return bool(name) and canonical_variant_id(name) == self.variant_id


@dataclass(frozen=True)
class HandoverVariant:
    """The answer, with its reason attached so a refusal is never indistinguishable from a miss."""

    variant_id: Optional[str]
    reason: str
    source: Optional[str] = None
    sku_key: Optional[str] = None
    candidates: int = 0

    @property
    def resolved(self) -> bool:
        return bool(self.variant_id)


def _norm(value: Any) -> str:
    return str(value or "").strip()


def names_a_merchant_issued_variant(
    value: Any,
    *,
    product_key: Optional[Any] = None,
    product_id: Optional[Any] = None,
    handle: Optional[Any] = None,
) -> bool:
    """THE question "is this string a variant id the MERCHANT issued?", asked in ONE place.

    Round 2 of review found it being asked in three, with three different predicates, in a
    module whose whole argument is that there should be one — and the divergences were real
    defects, not tidiness:

      * admission classified the id PARENT-AWARE (`product_key`/`product_id` passed), while the
        contradiction guard classified the hand-over's NAME parent-free. `variant_identity`
        checks derivation BEFORE shape, so a numeric product id restated as a variant name read
        as merchant-issued to one and product-derived to the other, and a good candidate was
        refused as "contradicted" by a name the module itself calls a forgery;
      * the route's attach branch asked `extract_shopify_numeric_variant_id`, which accepts ANY
        digit string, so a 5-digit operator SKU could veto a real catalog id — under a comment
        claiming the two branches applied the same rule.

    The id is canonicalised BEFORE classification, so `gid://shopify/ProductVariant/<n>` and
    `<n>` cannot get different answers about the same variant.
    """
    canonical = canonical_variant_id(value)
    if not canonical:
        return False
    return is_merchant_issued_variant_id(
        canonical, product_key=product_key, product_id=product_id, handle=handle
    )


def canonical_variant_id(value: Any) -> str:
    """ONE spelling for a variant id that two writers may spell two ways.

    `gid://shopify/ProductVariant/41234567890123` and `41234567890123` are the same variant, and
    `services.variant_identity` calls both MERCHANT_ISSUED. Everything downstream of this module
    — the cart permalink builder, the preflight's storefront comparison, the `variant_id` we
    publish on the offer spec — wants the bare numeric, so the fold happens once here rather
    than at each of them.
    """
    raw = _norm(value)
    if not raw:
        return ""
    return extract_shopify_numeric_variant_id(raw) or raw


def _payload(raw: Any) -> Dict[str, Any]:
    """`sku_payload` reaches this reader as a JSON STRING on BOTH dialects — accept either.

    Measured, because the obvious assumption is wrong: the column is `jsonb` on Postgres, but a
    raw-SQL read through `databases` + asyncpg does not apply the SQLAlchemy type, so the value
    arrives as text there exactly as it does on SQLite
    (`tests/test_handover_variant_identity_postgres.py` pins both facts). A reader written for
    a dict would find no `variant_id_provenance` in production, and a veto that cannot find its
    key does not veto — a fail-OPEN no SQLite test could see. The dict branch is kept because
    an ORM-typed caller does get one.

    Anything that is not an object reads as an empty one: a payload we cannot parse must not
    be able to veto (it carries no stamp) NOR to authorise (it carries no id to agree with).
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def candidate_from_row(row: Any, rejects: Optional[Dict[str, int]] = None) -> Optional[Candidate]:
    """Admit one `catalog_skus` row as a hand-over candidate, or say why not.

    THE ORDER IS THE POLICY, so it is spelled out rather than left to the reader:

      1. the CLASSIFIER runs first and is necessary — an id it does not call MERCHANT_ISSUED is
         out, whatever any stamp says;
      2. the stamp may then VETO — a row explicitly marked as something else is out even if the
         classifier would have admitted it;
      3. `sku_payload.variant_id` (the FULL id, which #2148 keeps beside the 128-bounded
         `source_variant_id`) must AGREE with the stored id. They differ exactly when the stored
         one was truncated at the column bound, and a truncated variant id names a variant the
         merchant does not have. Refusing is the only safe reading; the classifier structurally
         cannot see this, because it is handed one string.
    """
    d = dict(row) if not isinstance(row, dict) else row
    svid = _norm(d.get("source_variant_id"))
    if not svid:
        if rejects is not None:
            rejects[X_NOT_MERCHANT_ISSUED] = rejects.get(X_NOT_MERCHANT_ISSUED, 0) + 1
        return None

    product_key = _norm(d.get("product_key"))
    if not names_a_merchant_issued_variant(
        svid, product_key=product_key, product_id=d.get("source_product_id")
    ):
        if rejects is not None:
            rejects[X_NOT_MERCHANT_ISSUED] = rejects.get(X_NOT_MERCHANT_ISSUED, 0) + 1
        return None

    payload = _payload(d.get("sku_payload"))
    stamp = _norm(payload.get("variant_id_provenance"))
    if stamp and stamp != MERCHANT_ISSUED:
        if rejects is not None:
            rejects[X_STAMP_VETO] = rejects.get(X_STAMP_VETO, 0) + 1
        return None

    full = _norm(payload.get("variant_id"))
    if full and full != svid:
        if rejects is not None:
            rejects[X_PAYLOAD_DISAGREES] = rejects.get(X_PAYLOAD_DISAGREES, 0) + 1
        return None

    return Candidate(
        sku_key=_norm(d.get("sku_key")),
        product_key=product_key,
        variant_id=canonical_variant_id(svid),
        stamped=bool(stamp),
        stored_variant_id=svid,
        source_product_id=_norm(d.get("source_product_id")),
    )


#: Every key a stored seed variant may carry its own identity under. ONE tuple for every matcher
#: in this lane (the route imports it): the offer may be named by its SKU while the same entry
#: carries the Shopify id a catalog row or an operator attachment holds.
STORED_VARIANT_ID_KEYS = (
    "variant_id", "variantId", "id", "sku", "sku_id", "shopify_variant_id", "source_variant_id",
)
#: The keys that CLAIM to be a variant id, so may contradict one. Never `sku`/`sku_id`: on this
#: corpus a SKU is routinely a barcode, which may match a variant but not refuse one.
CLAIM_VARIANT_ID_KEYS = ("variant_id", "variantId", "id", "shopify_variant_id", "source_variant_id")


def url_named_variant_id(value: Any) -> str:
    """The Shopify variant a URL-SHAPED id names through `variant=N`, or "".

    Measured 2026-10-15 (census oneoff-sibling-entries-census): stored entries routinely carry the
    JSON-LD offer `@id` as their `variant_id` — `/products/<handle>?variant=45253610242298#offer` —
    and `canonical_variant_id` reads only a gid or bare digits, so the entry that IS the candidate
    never answered to it and #2624 refused its offer (211 lost carts). The parse is strict, as in
    `shopify_variant_identity._url_named_variant_ids`: every `variant=` value must be ASCII digits
    and there must be exactly one distinct value, or the URL names nothing.
    """
    raw = _norm(value)
    if "variant=" not in raw:
        return ""
    try:
        query = parse_qs(urlparse(raw).query, keep_blank_values=True)
    except ValueError:
        return ""
    # A SUBSCRIPTION offer of the variant, not the variant: `?variant=N&selling_plan=P` is priced
    # at the plan's price, so a cart for N would charge the one-time price against it. Review
    # of #2627 (controller, 2026-10-15).
    if "selling_plan" in query:
        return ""
    values = query.get("variant", [])
    if not values or any(not (v.isascii() and v.isdigit()) for v in values):
        return ""
    distinct = set(values)
    return distinct.pop() if len(distinct) == 1 else ""


def _entry_ids(entry: Dict[str, Any], keys: Sequence[str]) -> Set[str]:
    out: Set[str] = set()
    for key in keys:
        canonical = canonical_variant_id(entry.get(key))
        if canonical:
            out.add(canonical)
        named = url_named_variant_id(entry.get(key))
        if named:
            out.add(named)
    return out


def stored_entry_variant_ids(entry: Any) -> Set[str]:
    """Every variant id ONE stored entry names: the canonical form of each identity key, the
    `variant=` a URL-shaped one names, and the `variant=` on the entry's own `url`. The ONE reader
    every matcher in this lane uses, so they cannot drift apart again (#2624 review, P3)."""
    if not isinstance(entry, dict):
        return set()
    out = _entry_ids(entry, STORED_VARIANT_ID_KEYS)
    named = url_named_variant_id(entry.get("url"))
    if named:
        out.add(named)
    return out


def stored_entry_claimed_variant_ids(entry: Any) -> Set[str]:
    """The ids an entry CLAIMS (no `sku`), URL-read too — the only ones that may contradict."""
    if not isinstance(entry, dict):
        return set()
    return _entry_ids(entry, CLAIM_VARIANT_ID_KEYS)


def stored_entry_answers_to(entry: Any, variant_id: Any) -> bool:
    target = canonical_variant_id(variant_id)
    return bool(target) and target in stored_entry_variant_ids(entry)


def _entry_price(entry: Any) -> Optional[float]:
    if not isinstance(entry, dict):
        return None
    for key in ("price_amount", "price"):
        value = entry.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None
    return None


def _entry_currency(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    return _norm(entry.get("price_currency") or entry.get("currency")).upper()


def _price_key(entry: Any) -> tuple:
    return (_entry_price(entry), _entry_currency(entry))


def namers_agree_on_price(stored: Sequence[Dict[str, Any]], variant_id: Any) -> bool:
    """True when the stored entries naming `variant_id` cannot disagree about its price: at most
    one names it, or every one of them states the SAME known price (and currency). Two entries
    naming one variant at $19 and $29 — or a one-time offer and its subscription plans — make
    the price ambiguous, and an ambiguous price gets no cart (review of #2627, 2026-10-15)."""
    namers = [v for v in stored if stored_entry_answers_to(v, variant_id)]
    if len(namers) <= 1:
        return True
    keys = {_price_key(v) for v in namers}
    return len(keys) == 1 and next(iter(keys))[0] is not None


def snapshot_names_one_variant(seed_data: Any, variant_id: Any) -> bool:
    """True when the seed's snapshot holds EXACTLY ONE variant and it is `variant_id` — the
    products.js-backed statement that the product has a single variant."""
    snapshot = seed_data.get("snapshot") if isinstance(seed_data, dict) else None
    variants = snapshot.get("variants") if isinstance(snapshot, dict) else None
    if not isinstance(variants, list) or len(variants) != 1:
        return False
    return stored_entry_answers_to(variants[0], variant_id)


def entry_is_the_sole_variant_read_again(
    entry: Dict[str, Any],
    *,
    variant_id: Any,
    stored: Sequence[Dict[str, Any]],
    seed_data: Any,
    product_key: Any = None,
    product_id: Any = None,
) -> bool:
    """May an entry that does not name `variant_id` still be folded into it — the same single
    variant read twice (JSON-LD offer + a SKU/barcode entry: census 2026-10-15)?

    ONLY WITH CORROBORATION (controller ruling, 2026-10-15): the snapshot holds exactly one
    variant and it is `variant_id`; the entry claims no OTHER variant id (a URL-named one, or a
    claim the classifier calls merchant-issued relative to the product — a 6-digit product or SKU
    number like jurlique's "114807" is neither); and its price (and
    currency, where both say one) equals that of the one stored entry naming `variant_id`, or the
    seed's own price when no entry names it. Equal price alone never folds: a same-price wrong
    shade is still a wrong-variant cart.
    """
    target = canonical_variant_id(variant_id)
    if not target or not snapshot_names_one_variant(seed_data, target):
        return False
    for claimed in stored_entry_claimed_variant_ids(entry) - {target}:
        if url_named_variant_id(claimed) or claimed.isdigit() and names_a_merchant_issued_variant(
            claimed, product_key=product_key, product_id=product_id
        ):
            return False
    for key in CLAIM_VARIANT_ID_KEYS:
        named = url_named_variant_id(entry.get(key))
        if named and named != target:
            return False
    namers = [v for v in stored if stored_entry_answers_to(v, target)]
    # ONE VARIANT, SEVERAL PRICES among the entries that do not name it: those are selling plans
    # or sale/compare prices, not the variant read again, and picking the one priced like the
    # reference would hand a subscription offer the one-time cart (review of #2627: murad, seed
    # price 16.38, the 1-month plan folded and the one-time entry did not). Fold none.
    others = {
        _entry_price(v) for v in stored
        if all(v is not n for n in namers) and _entry_price(v) is not None
    }
    if len(others) > 1:
        return False
    if len(namers) == 1:
        reference_price, reference_currency = _entry_price(namers[0]), _entry_currency(namers[0])
    elif not namers:
        reference_price = _entry_price(seed_data) if isinstance(seed_data, dict) else None
        reference_currency = _entry_currency(seed_data)
    else:
        return False
    price = _entry_price(entry)
    if price is None or reference_price is None or price != reference_price:
        return False
    currency = _entry_currency(entry)
    return not (currency and reference_currency and currency != reference_currency)


def _offer_may_take_the_sole_candidate(one: Candidate, *, seed_data: Any, wanted: str) -> bool:
    """May the lone candidate be handed over for the offer named `wanted`?

    Yes when there is nothing to choose between: a product-grain hand-over (no name) or a seed
    storing at most one entry — the case R_SOLE was written for. Otherwise EXACTLY ONE stored
    entry must be the offer, and it must name the candidate (`stored_entry_answers_to`, which
    reads a URL-shaped id's `variant=`), or be the snapshot-corroborated sole variant read again
    (`entry_is_the_sole_variant_read_again`). An offer the stored entries do not know is refused.
    """
    if not wanted:
        return True
    stored = seed_data.get("variants") if isinstance(seed_data, dict) else None
    stored = [v for v in stored if isinstance(v, dict)] if isinstance(stored, list) else []
    if len(stored) <= 1:
        return True
    mine = [v for v in stored if any(_norm(v.get(k)) == wanted for k in STORED_VARIANT_ID_KEYS)]
    # EXACTLY ONE entry may be this offer. Two entries answering to its name (both `sku: X`, or
    # every `id` restating the product id) cannot say which one it is, so neither lends it the
    # candidate. Review of #2624 (controller, 2026-10-14).
    if len(mine) != 1:
        return False
    if stored_entry_answers_to(mine[0], one.variant_id):
        return namers_agree_on_price(stored, one.variant_id)
    return entry_is_the_sole_variant_read_again(
        mine[0], variant_id=one.variant_id, stored=stored, seed_data=seed_data,
        product_key=one.product_key, product_id=one.source_product_id or None,
    )


def choose_handover_variant(
    candidates: Optional[Sequence[Candidate]],
    *,
    seed_data: Any = None,
    offer_variant_id: Any = None,
    named_variant_id: Any = _UNSET,
    product_key: Any = None,
    product_id: Any = None,
    primed: bool = True,
    lookup_ok: bool = True,
) -> HandoverVariant:
    """The whole decision, pure — so it can be tested without a database.

    `candidates` are the rows `candidate_from_row` already admitted for ONE product.
    """
    if not lookup_ok:
        return HandoverVariant(None, R_LOOKUP_FAILED)
    if not primed:
        return HandoverVariant(None, R_NOT_PRIMED)

    live = list(candidates or [])
    #: What the hand-over may be MATCHED against — the broad chain, `sku` included, because a
    #: SKU equal to a stored `source_variant_id` IS that variant.
    wanted = _norm(offer_variant_id)
    #: What the hand-over may CONTRADICT with — a value that claims to be a variant id.
    #: Defaults to `wanted` so a caller that does not distinguish the two keeps one behaviour,
    #: but the gateway passes the narrower `_seed_variant_identity_claim`: on this corpus an
    #: 8+-digit `sku` is routinely a barcode, and a barcode refusing a variant we DID resolve
    #: is a refusal built out of an absence of information. See round-3 P2-8.
    claimed = wanted if named_variant_id is _UNSET else _norm(named_variant_id)
    # PARENT-AWARE ON BOTH PATHS, and computed ONCE. `variant_identity` checks derivation
    # before shape, so this predicate needs the products it is relative to or it calls a
    # restated product id "merchant-issued" — round 2 found exactly that, refusing a good
    # candidate as contradicted by a name the module itself calls a forgery. Round 3 found the
    # ZERO-candidate path still half-blind, passing `product_key` and no `product_id`, so one
    # string got two verdicts depending on whether catalog happened to hold a row: the parents
    # now come from the candidate when there is one and from the CALLER when there is not.
    named_identity = names_a_merchant_issued_variant(
        claimed,
        product_key=(live[0].product_key if live else _norm(product_key)),
        product_id=(live[0].source_product_id if live else _norm(product_id)),
    )

    if live:
        # THE NAME IS CHECKED BEFORE THE COUNT, and that order is the fix for the first cut's
        # one real hole. A `len(live) == 1` shortcut placed above this returned the lone
        # candidate without ever comparing it to the name the hand-over carries, so a single
        # catalog row overrode a DIFFERENT merchant-issued id the caller had explicitly
        # attached. Only a name that is ITSELF merchant-issued can contradict: the seed lane
        # routinely passes a SKU string here (`_seed_offer_variant_id` reads
        # variant_id | variantId | sku | sku_id | id), and a SKU naming no variant is an
        # absence of information, not a disagreement.
        hits = [c for c in live if c.answers_to(wanted)]
        if len(hits) == 1:
            return HandoverVariant(
                hits[0].variant_id,
                R_EXACT if len(live) > 1 else R_SOLE,
                SOURCE_CATALOG_SKU, hits[0].sku_key, len(live),
            )
        if not hits and named_identity:
            return HandoverVariant(None, R_CONTRADICTED, None, None, len(live))

    if len(live) == 1:
        one = live[0]
        if not _offer_may_take_the_sole_candidate(one, seed_data=seed_data, wanted=wanted):
            return HandoverVariant(None, R_SIBLING, None, None, 1)
        return HandoverVariant(one.variant_id, R_SOLE, SOURCE_CATALOG_SKU, one.sku_key, 1)

    if len(live) > 1:
        # Two or more live merchant-issued SKUs and the hand-over names none of them. There is
        # no ordering over these that is anything but a guess — see the module docstring.
        return HandoverVariant(None, R_AMBIGUOUS, None, None, len(live))

    # ZERO candidates. Only here may the seed's own stamp speak: nothing in catalog contradicts
    # it. `sole_stamped_variant_id` carries its own refusal (it declines on any product with
    # more than one snapshot variant entry) and is imported lazily so this module stays free of
    # the serving-path import graph for callers that only want the pure decision.
    from services.shopify_variant_identity import sole_stamped_variant_id

    stamped = _norm(sole_stamped_variant_id(seed_data))
    # THE SAME BAR THE CATALOG ROWS CLEAR. `sole_stamped_variant_id` gates on
    # `shopify_variant_identity._numeric_id`, which accepts ANY digit string, while
    # `variant_identity` requires 8+ digits — so a snapshot stamped `"12345"` used to become a
    # hand-over id that `checkout_preflight` would then refuse as
    # `no_merchant_issued_variant_id`, a refusal this resolver had caused. One rule for both
    # sources, or the module's own guarantee is only true of half its answers.
    if not (
        stamped
        and names_a_merchant_issued_variant(
            stamped, product_key=product_key, product_id=product_id
        )
    ):
        return HandoverVariant(None, R_NO_IDENTITY, None, None, 0)

    canonical_stamp = canonical_variant_id(stamped)
    # AND THE SAME CONTRADICTION RULE. Round 2 found it guarding the catalog path only, so the
    # disagreement refused one branch up was resolved in the stamp's favour on the other —
    # `sole_stamped_variant_id` reads `shopify_variant_id` while `_seed_offer_variant_id` reads
    # `variant_id | variantId | sku | sku_id | id`, so ONE snapshot entry can carry two
    # merchant-issued ids that disagree with no data corruption at all, and the buyer got a
    # cart for the one they did not name. Nothing takes this path on today's corpus (0 of
    # 11,834 seeds are stamped); it goes live the moment `backfill_shopify_variant_ids.py` runs,
    # which is the stated plan, so the rule belongs here now rather than after the incident.
    if named_identity and canonical_variant_id(claimed) != canonical_stamp:
        return HandoverVariant(None, R_CONTRADICTED, None, None, 0)
    return HandoverVariant(canonical_stamp, R_SEED_STAMP, SOURCE_SEED_STAMP, None, 0)


class HandoverVariantResolver:
    """ONE lookup and ONE memo for a whole request.

    Built per request, exactly like `_PreflightBudget`: the four hand-over lanes each walk a
    list of seed rows, and a per-row query would be a query per card. `prime` takes every
    product key a lane is about to hand over and loads them in one statement; `choose` is then
    pure and can be called inside the per-variant loop for free.

    Every failure mode answers with an EMPTY id: a timeout, a driver error, an unprimed key and
    an over-cap key are all "we could not name the variant", which degrades the hand-over to a
    referral. None of them can publish an id.
    """

    def __init__(self, *, timeout_s: Optional[float] = None, max_keys: Optional[int] = None) -> None:
        self._by_key: Dict[str, List[Candidate]] = {}
        self._primed: set = set()
        #: Keys whose batch RAISED. Per key rather than one latch, because a failure about one
        #: product key is not evidence about another: three lanes prime this resolver, and a
        #: latch let one slow statement refuse every ATTACHED seed the later lanes loaded fine.
        #: (An earlier version of this note named standalone seeds as the cohort a latch would
        #: silence. That was wrong twice over — review falsified it — because `choose` takes the
        #: no-product-key branch before it consults `_failed` at all, so a standalone seed is
        #: unaffected under either design. The real cohort is a keyed seed in a later batch.)
        self._failed: set = set()
        self.stats: Dict[str, int] = {
            "handover_considered": 0,
            "handover_resolved": 0,
            "handover_resolved_catalog": 0,
            "handover_resolved_seed_stamp": 0,
            "handover_refused_ambiguous": 0,
            "handover_refused_sibling": 0,
            "handover_contradicted": 0,
            "handover_no_identity": 0,
            "handover_no_product_key": 0,
            "handover_not_primed": 0,
            "handover_lookup_failed": 0,
            "handover_rows_scanned": 0,
            "handover_rows_rejected": 0,
        }
        if timeout_s is None:
            timeout_s = _env_float("HANDOVER_VARIANT_LOOKUP_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_S)
        if max_keys is None:
            max_keys = _env_int("HANDOVER_VARIANT_LOOKUP_MAX_KEYS", _DEFAULT_MAX_KEYS)
        # A CONFIGURED ZERO IS NOT "no limit". `asyncio.wait_for(..., timeout=None)` waits
        # forever, so `self._timeout_s or None` turned the most natural spelling of "do not
        # wait" into an unbounded await on a serving path whose comment promises a bound. Zero
        # or negative falls back to the default rather than removing the guard.
        self._timeout_s = float(timeout_s) if float(timeout_s) > 0 else _DEFAULT_TIMEOUT_S
        self._max_keys = max(0, int(max_keys))

    async def prime(self, product_keys: Iterable[Any]) -> None:
        """Load every not-yet-loaded key, in one statement, under one timeout.

        Fail-soft on the DB call ONLY, and the softness is a refusal, not a pass: the keys in
        the failed batch are recorded in `_failed`, and `choose` answers `sku_lookup_failed`
        with no id for each of them until a later `prime` loads them. PER KEY, never one latch —
        see the note on `_failed` for the cohort that actually costs. An earlier shape of this
        in the same route caught the DERIVATION too, which turned a programming error into a
        feature that silently never ran.
        """
        wanted: List[str] = []
        for key in product_keys or ():
            k = _norm(key)
            if not k or k in self._primed or k in wanted:
                continue
            if len(self._primed) + len(wanted) >= self._max_keys:
                break
            wanted.append(k)
        if not wanted:
            return

        try:
            rows = await asyncio.wait_for(self._fetch(wanted), timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001
            self._failed.update(wanted)
            logger.warning(
                "handover variant lookup failed for %d product key(s): %s",
                len(wanted), repr(exc)[:200],
            )
            return

        # Mark primed BEFORE filling, so a key with no rows at all is "primed and empty"
        # (-> no_merchant_issued_sku) rather than "never asked" (-> not_primed). The two are
        # different facts and the coverage line has to be able to tell them apart.
        self._primed.update(wanted)
        # AND CLEAR THE FAILURE. A key that timed out in one lane and loaded in the next kept
        # answering `sku_lookup_failed` with its candidates sitting in `_by_key` — fail-closed,
        # so never a wrong cart, but it threw away a hand-over the second lookup had paid for.
        self._failed.difference_update(wanted)
        rejects: Dict[str, int] = {}
        for row in rows or ():
            self.stats["handover_rows_scanned"] += 1
            cand = candidate_from_row(row, rejects)
            if cand is None:
                continue
            self._by_key.setdefault(cand.product_key, []).append(cand)
        self.stats["handover_rows_rejected"] += sum(rejects.values())
        for reason, n in rejects.items():
            self.stats["handover_reject_" + reason] = self.stats.get(
                "handover_reject_" + reason, 0
            ) + n

    async def _fetch(self, keys: Sequence[str]) -> Any:
        """Separated so a test can drive the decision without a database.

        The IN-list is built from GENERATED parameter names, never from the keys themselves —
        a product key is caller-influenced data (`external_product_seeds.attached_product_key`)
        and interpolating it would be an injection site on a serving path.
        """
        from db.database import database

        names = [f"pk{i}" for i in range(len(keys))]
        sql = _SELECT.format(placeholders=", ".join(":" + n for n in names))
        return await database.fetch_all(sql, dict(zip(names, keys)))

    def candidates_for(self, product_key: Any) -> List[Candidate]:
        """Read the memo. TEST-ONLY today, and deliberately so rather than deleted.

        `tests/test_handover_variant_identity_postgres.py::test_another_products_row_is_never_borrowed`
        asserts on the buckets directly, because the alternative — asserting only through
        `choose` — cannot tell "this product's row was never loaded" from "it was loaded and
        refused", which is exactly the confusion the `not_primed` / `no_merchant_issued_sku`
        split exists to prevent.
        """
        return list(self._by_key.get(_norm(product_key)) or [])

    def choose(
        self,
        *,
        product_key: Any,
        seed_data: Any = None,
        offer_variant_id: Any = None,
        named_variant_id: Any = _UNSET,
        product_id: Any = None,
    ) -> HandoverVariant:
        """The per-hand-over answer. Pure — `prime` already did every await."""
        self.stats["handover_considered"] += 1
        key = _norm(product_key)
        if not key:
            # A STANDALONE seed — no attached product key, so there is no `catalog_skus` row to
            # look up and nothing that could contradict the seed's own stamp. It takes the
            # zero-candidate path deliberately: refusing here instead would have silently
            # removed the seed-stamp hand-over that already worked for this cohort, which is a
            # regression dressed as caution. Only the REASON is relabelled, and only when
            # nothing resolved, so the coverage line can still tell this apart from a product
            # that simply has no merchant-issued SKU.
            answer = choose_handover_variant(
                (), seed_data=seed_data, offer_variant_id=offer_variant_id,
                named_variant_id=named_variant_id, product_id=product_id,
            )
            if not answer.resolved:
                answer = HandoverVariant(None, R_NO_PRODUCT_KEY)
                self.stats["handover_no_product_key"] += 1
                return answer
            self._count(answer)
            return answer

        answer = choose_handover_variant(
            self._by_key.get(key),
            seed_data=seed_data,
            offer_variant_id=offer_variant_id,
            named_variant_id=named_variant_id,
            product_key=key,
            product_id=product_id,
            primed=key in self._primed,
            lookup_ok=key not in self._failed,
        )
        self._count(answer)
        return answer

    def _count(self, answer: HandoverVariant) -> None:
        if answer.resolved:
            self.stats["handover_resolved"] += 1
            if answer.source == SOURCE_CATALOG_SKU:
                self.stats["handover_resolved_catalog"] += 1
            else:
                self.stats["handover_resolved_seed_stamp"] += 1
            return
        if answer.reason == R_AMBIGUOUS:
            self.stats["handover_refused_ambiguous"] += 1
        elif answer.reason == R_SIBLING:
            self.stats["handover_refused_sibling"] += 1
        elif answer.reason == R_CONTRADICTED:
            self.stats["handover_contradicted"] += 1
        elif answer.reason == R_LOOKUP_FAILED:
            self.stats["handover_lookup_failed"] += 1
        elif answer.reason == R_NOT_PRIMED:
            self.stats["handover_not_primed"] += 1
        else:
            self.stats["handover_no_identity"] += 1


def handover_coverage_fields(stats: Optional[Dict[str, int]]) -> Dict[str, Any]:
    """The counters, or {} when no hand-over was considered at all.

    DELIBERATELY NOT keyed on `resolved > 0`, which is the trap
    `preflight_coverage_fields` had to be corrected for in the other direction: the whole point
    of this lane is to see the REFUSALS, and a request where every hand-over was refused is the
    single most informative one to look at. It disappears from the log only when the request
    handed nothing over.
    """
    if not stats or not stats.get("handover_considered"):
        return {}
    considered = stats["handover_considered"]
    out: Dict[str, Any] = {
        "handover_considered": considered,
        # READ, not merely incremented. Review found these written in `prime` and present in no
        # emitted field — the defect the neighbouring comment in `_handle_offers_resolve` was
        # written about ("a counter nobody reads is indistinguishable from one that is always
        # zero"). They also carry the only production evidence for the two guards justified as
        # bounds on future writers: a stamp veto or a payload disagreement showing up in prod is
        # how anyone would ever learn a writer had started emitting one.
        "handover_rows_scanned": stats.get("handover_rows_scanned", 0),
        "handover_rows_rejected": stats.get("handover_rows_rejected", 0),
        "handover_rows_stamp_vetoed": stats.get("handover_reject_" + X_STAMP_VETO, 0),
        "handover_rows_payload_disagreed": stats.get("handover_reject_" + X_PAYLOAD_DISAGREES, 0),
        "handover_contradicted": stats.get("handover_contradicted", 0),
        "handover_resolved": stats.get("handover_resolved", 0),
        "handover_resolved_catalog": stats.get("handover_resolved_catalog", 0),
        "handover_resolved_seed_stamp": stats.get("handover_resolved_seed_stamp", 0),
        "handover_refused_ambiguous": stats.get("handover_refused_ambiguous", 0),
        "handover_refused_sibling": stats.get("handover_refused_sibling", 0),
        "handover_no_identity": stats.get("handover_no_identity", 0),
        "handover_no_product_key": stats.get("handover_no_product_key", 0),
        "handover_lookup_failed": stats.get("handover_lookup_failed", 0),
        "handover_not_primed": stats.get("handover_not_primed", 0),
        "handover_resolved_fraction": round(stats.get("handover_resolved", 0) / considered, 3),
    }
    return out


def handover_coverage_message(fields: Dict[str, Any]) -> str:
    """The same numbers as MESSAGE TEXT.

    No formatter in this repo renders `extra`, and in production the root logger sits at
    WARNING with `setup_structured_logging()` never called from main — so a counter that lives
    only in `extra` is not merely unformatted, it is not emitted. Anything worth counting here
    has to be in the string.
    """
    if not fields:
        return ""
    return (
        " handover considered=%d resolved=%d catalog=%d seed_stamp=%d ambiguous=%d"
        " contradicted=%d no_identity=%d no_product_key=%d lookup_failed=%d not_primed=%d"
        " rows_scanned=%d rows_rejected=%d stamp_vetoed=%d payload_disagreed=%d"
        " sibling=%d resolved_fraction=%.3f"
    ) % (
        fields["handover_considered"], fields["handover_resolved"],
        fields["handover_resolved_catalog"], fields["handover_resolved_seed_stamp"],
        fields["handover_refused_ambiguous"], fields["handover_contradicted"],
        fields["handover_no_identity"], fields["handover_no_product_key"],
        fields["handover_lookup_failed"], fields["handover_not_primed"],
        fields["handover_rows_scanned"], fields["handover_rows_rejected"],
        fields["handover_rows_stamp_vetoed"], fields["handover_rows_payload_disagreed"],
        fields["handover_refused_sibling"], fields["handover_resolved_fraction"],
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default
