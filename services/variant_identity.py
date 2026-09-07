"""Is this variant id something the MERCHANT issued, or something we made up?

WHY THIS EXISTS. Five separate writers in this repo store a variant id that is *derived from the
product id*, and every one of them lands in `catalog_skus.source_variant_id` — the column the
checkout path reads as "the merchant's own name for the thing we are about to buy":

  - `catalog_enrichment_agent/ingestion.py`  `source_variant_id = product_key`   (11,811 rows)
  - `scripts/onboard_external_brand_from_crawl.py`  `f"{epid}-default"`          (1,473 ids)
  - `services/curated_brand_feed.py`  `f"{handle}:{i}"`
  - `services/beauty_external_ranking.py`  `f"{product_id}_{idx + 1}"`
  - a hex-digest-of-the-product-key writer                                        (166 ids)

None of those is a bug of *intent*. `ingestion.py`'s is a storage token that exists to satisfy
`idx_catalog_skus_source_identity` (every agent SKU shares merchant_id/platform='external_seed', so
something has to vary per PDP). `onboard_external_brand_from_crawl`'s exists because the readiness
gate treats `zero_variants` as a hard blocker, so a seed with no variants array is dropped from
every agent-search response — the synthetic default variant is what makes the row *servable* at
all. Both are load-bearing. The defect is that a token minted for storage or for recall is
indistinguishable, once written, from identity we actually got from the merchant.

The gateway already refuses these: `safety-kernel/src/protocol/buyerIntake.js:isRestatedProductId`
rejects any variant id derivable from the product id. That guard is CORRECT. This module is the
backend half of the same rule, so the two repos agree on what "identity" means instead of one
minting what the other throws away.

THE RULE IS FAIL-CLOSED, and deliberately so. `services/shopify_variant_identity.py` already states
the principle for this exact column: "A wrong variant id is worse than none. It builds a cart URL
that silently adds the wrong size and the buyer completes a purchase we mis-specified." So an id is
only `MERCHANT_ISSUED` when it carries a shape we can actually verify came from a storefront — a
bare numeric Shopify id, or the `gid://shopify/ProductVariant/<n>` form. Everything we cannot place
is `UNVERIFIABLE`, which is not an accusation, just an absence of evidence; callers that spend money
must treat it exactly as they treat a missing id.

WHAT THIS MODULE IS NOT. It touches no database, no network and no serving path — it is a pure
predicate over strings, so it can be called from a writer, a backfill, a ratchet test or a SQL-side
classification without dragging any of those into each other.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional

# An id we can positively place as the merchant's own.
MERCHANT_ISSUED = "merchant_issued"
# An id we minted ourselves out of the product's identity. Never buyable.
PRODUCT_DERIVED = "product_derived"
# Present, but nothing tells us where it came from. Treated as "no id" for money.
UNVERIFIABLE = "unverifiable"
# Nothing there at all.
ABSENT = "absent"

#: Shopify variant ids are 8+ digits in practice (they are Rails bigints and have been
#: 13 digits since ~2016). Requiring 8 keeps a stray "12345" price or count from being
#: mistaken for identity.
_RE_NUMERIC = re.compile(r"^\d{8,}$")
_RE_GID = re.compile(r"^gid://shopify/ProductVariant/\d+$")

#: Suffixes our own writers append when they synthesise a "the product is the variant" row.
_SYNTHETIC_SUFFIX_WORDS = ("default", "canonical", "single", "seed", "variant", "v")

#: Digest lengths actually used in this repo when a key is hashed into an id
#: (`[:12]`, `[:16]`, `[:32]` all appear), plus the full-length forms.
_DIGEST_PREFIX_LENGTHS = (8, 12, 16, 32, 40, 64)

#: Literals a writer emits when it has nothing at all to key on.
_KNOWN_PLACEHOLDER_IDS = frozenset({
    "seed-variant-default",
    "default",
    "default-title",
    "defaulttitle",
})


def _norm(value: object) -> str:
    return str(value or "").strip()


def _fold(value: str) -> str:
    """Case/separator-insensitive form, so `Foo_Bar-1` and `foo bar 1` compare equal.

    Only used for the *derivation* comparisons — never to decide an id is real.
    """
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _digests_of(value: str) -> Iterable[str]:
    raw = value.encode("utf-8", "surrogatepass")
    for algo in (hashlib.md5, hashlib.sha1, hashlib.sha256):
        full = algo(raw).hexdigest()
        for n in _DIGEST_PREFIX_LENGTHS:
            if n <= len(full):
                yield full[:n]


def _is_restatement_of(variant_id: str, parent: str) -> bool:
    """True when `variant_id` carries no information that `parent` did not already carry.

    Covers, in the shapes our writers actually emit:
      - exact equality                          `product_key` == `source_variant_id`
      - parent + a synthetic word               `tonymoly_us_858145855914-default`
      - parent + a small ordinal                `handle:3`, `product_id_1`
      - a hex digest of the parent              `4250d5f6e1b6`
    """
    if not variant_id or not parent:
        return False

    folded_v, folded_p = _fold(variant_id), _fold(parent)
    if not folded_v or not folded_p:
        return False
    if folded_v == folded_p:
        return True

    # A hex digest of the parent carries no independent information either. Compare against
    # the digest rather than sniffing for "looks like hex", so a genuine hex-shaped merchant
    # SKU is not condemned by its alphabet alone.
    lowered = variant_id.lower()
    if lowered in set(_digests_of(parent)) or lowered in set(_digests_of(parent.lower())):
        return True

    if not folded_v.startswith(folded_p):
        return False

    # Whatever the variant id adds beyond the parent: if it is only one of our synthetic
    # words, or a small ordinal, then the id names the product, not a variant of it.
    remainder = folded_v[len(folded_p):]
    if not remainder:
        return True
    if remainder in _SYNTHETIC_SUFFIX_WORDS:
        return True
    if remainder.isdigit() and len(remainder) <= 3:
        return True
    for word in _SYNTHETIC_SUFFIX_WORDS:
        if remainder == word or remainder.startswith(word) and remainder[len(word):].isdigit():
            return True
    return False


def variant_id_provenance(
    variant_id: object,
    *,
    product_id: Optional[object] = None,
    product_key: Optional[object] = None,
    handle: Optional[object] = None,
) -> str:
    """Classify one variant id as ABSENT / PRODUCT_DERIVED / MERCHANT_ISSUED / UNVERIFIABLE.

    Derivation is checked BEFORE shape: a product whose own id is numeric must not have that
    same number handed back as its variant identity just because numbers look real.
    """
    vid = _norm(variant_id)
    if not vid:
        return ABSENT
    if _fold(vid) in {_fold(p) for p in _KNOWN_PLACEHOLDER_IDS}:
        return PRODUCT_DERIVED

    for parent in (product_id, product_key, handle):
        if _is_restatement_of(vid, _norm(parent)):
            return PRODUCT_DERIVED

    if _RE_NUMERIC.match(vid) or _RE_GID.match(vid):
        return MERCHANT_ISSUED
    return UNVERIFIABLE


def is_merchant_issued_variant_id(
    variant_id: object,
    *,
    product_id: Optional[object] = None,
    product_key: Optional[object] = None,
    handle: Optional[object] = None,
) -> bool:
    """The money predicate. True only when we can positively place the id as the merchant's.

    Anything else — absent, derived from the product, or simply unplaceable — is False, because
    every one of those cases means the same thing at a checkout: we do not know which physical
    thing the buyer would receive.
    """
    return variant_id_provenance(
        variant_id, product_id=product_id, product_key=product_key, handle=handle
    ) == MERCHANT_ISSUED


def merchant_issued_variants(
    variants: Iterable[dict],
    *,
    product_id: Optional[object] = None,
    product_key: Optional[object] = None,
    handle: Optional[object] = None,
) -> list:
    """The subset of `variants` carrying identity we could actually transact against.

    Reads `variant_id` then `id`, which is the order every existing reader in this repo uses
    (`catalog_variant_promoter`, `beauty_external_ranking`, `agent_api._seed_variants`).
    """
    kept = []
    for v in variants or []:
        if not isinstance(v, dict):
            continue
        vid = _norm(v.get("variant_id")) or _norm(v.get("id"))
        if is_merchant_issued_variant_id(
            vid, product_id=product_id, product_key=product_key, handle=handle
        ):
            kept.append(v)
    return kept
