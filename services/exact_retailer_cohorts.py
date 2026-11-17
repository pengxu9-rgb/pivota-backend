"""Reviewed US retailer listings and their native shade identities.

The cohorts live in `config/reviewed_retailer_cohorts.json`: the store hosts, listing keys,
native variant pairs, serving flags and refresh-job settings. This module loads that file once
at import and exposes the same frozen shapes the reader, writer and refresh jobs check against.
"""

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path


COHORTS_PATH = Path(__file__).resolve().parents[1] / "config" / "reviewed_retailer_cohorts.json"
_CONFIG = json.loads(COHORTS_PATH.read_text(encoding="utf-8"))
COHORT_A_CONFIG = _CONFIG["cohort_a"]
COHORT_B_CONFIG = _CONFIG["cohort_b"]

PILOT_FLAG = COHORT_A_CONFIG["flag"]
COHORT_B_FLAG = COHORT_B_CONFIG["flag"]
COHORT_B_EXPANSION_FLAG = COHORT_B_CONFIG["expansion_flag"]
PROOF_KEY = _CONFIG["proof_key"]
CANONICAL_MERCHANT_ID = COHORT_A_CONFIG["canonical_merchant_id"]
CANONICAL_PRODUCT_KEY = "::".join(
    ("prod", CANONICAL_MERCHANT_ID, "external_seed", COHORT_A_CONFIG["canonical_external_id"])
)
CANONICAL_GROUP_ID = COHORT_A_CONFIG["canonical_group_id"]
CANONICAL_CONTENT_KEY = COHORT_A_CONFIG["canonical_content_key"]


@dataclass(frozen=True)
class RetailerListing:
    merchant_id: str
    host: str
    native_product_id: str
    variants: dict[str, tuple[str, str]]
    product_path: str

    @property
    def family_url(self) -> str:
        return f"https://{self.host}{self.product_path}"


@dataclass(frozen=True)
class ExactRetailerCohort:
    flag: str
    canonical_product_key: str
    canonical_merchant_id: str
    canonical_group_id: str
    canonical_content_key: str
    listings: dict[str, RetailerListing]

    def enabled(self) -> bool:
        return os.getenv(self.flag) == "1"


def _listings(config: dict) -> dict[str, RetailerListing]:
    return {
        key: RetailerListing(
            row["merchant_id"], row["host"], row["native_product_id"],
            {native: tuple(pair) for native, pair in row["variants"].items()},
            product_path=row["product_path"],
        )
        for key, row in config["listings"].items()
    }


RETAILERS = _listings(COHORT_A_CONFIG)

RETAILER_COHORT_A = ExactRetailerCohort(
    PILOT_FLAG, CANONICAL_PRODUCT_KEY, CANONICAL_MERCHANT_ID, CANONICAL_GROUP_ID, CANONICAL_CONTENT_KEY, RETAILERS,
)

RETAILER_COHORT_B = ExactRetailerCohort(
    flag=COHORT_B_FLAG,
    canonical_product_key="::".join(
        ("prod", COHORT_B_CONFIG["canonical_merchant_id"], "external_seed", COHORT_B_CONFIG["canonical_external_id"])
    ),
    canonical_merchant_id=COHORT_B_CONFIG["canonical_merchant_id"],
    canonical_group_id=COHORT_B_CONFIG["canonical_group_id"],
    canonical_content_key=COHORT_B_CONFIG["canonical_content_key"],
    listings=_listings(COHORT_B_CONFIG),
)
_cohort_b_key, _cohort_b_listing = next(iter(RETAILER_COHORT_B.listings.items()))
_COHORT_B_INITIAL_VARIANTS = {
    native: _cohort_b_listing.variants[native] for native in COHORT_B_CONFIG["initial_variants"]
}
COHORT_B_INITIAL_OFFER_IDS = frozenset(COHORT_B_CONFIG["initial_offer_ids"])
RETAILER_COHORT_B_INITIAL = replace(
    RETAILER_COHORT_B, listings={_cohort_b_key: replace(_cohort_b_listing, variants=_COHORT_B_INITIAL_VARIANTS)},
)

EXACT_RETAILER_LISTINGS = {**RETAILERS, **RETAILER_COHORT_B.listings}
# The ordinary Shopify mapper constructs an unlocalized /products path for this family.
# Reserve that URL's identity without admitting it to the US reviewed writer.
COHORT_B_BARE_FAMILY_URL = COHORT_B_CONFIG["bare_family_url"]
COHORT_B_BARE_LISTING_KEY = COHORT_B_CONFIG["bare_listing_key"]
COHORT_B_PROTECTED_LISTING_KEYS = frozenset((*RETAILER_COHORT_B.listings, COHORT_B_BARE_LISTING_KEY))
COHORT_B_PROTECTED_FAMILY_URLS = tuple(
    listing.family_url for listing in RETAILER_COHORT_B.listings.values()
) + (COHORT_B_BARE_FAMILY_URL,)
PROTECTED_RETAILER_LISTING_KEYS = frozenset(RETAILERS) | COHORT_B_PROTECTED_LISTING_KEYS


def pilot_enabled() -> bool:
    return RETAILER_COHORT_A.enabled()
