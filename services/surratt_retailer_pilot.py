"""Reviewed US retailer listings and their native shade identities."""

from dataclasses import dataclass, replace
import os


PILOT_FLAG = "SURRATT_EXACT_RETAILER_SHADES_ENABLED"
KIOKII_FLAG = "MEITU_US_KIOKII_EXACT_ENABLED"
KIOKII_EXPANSION_FLAG = "KIOKII_US_RETAILER_EXPANSION_ENABLED"
PROOF_KEY = "exact_retailer_shade_v1"
CANONICAL_MERCHANT_ID = "merch_obs_30f9ce3e44b7fcb4"
CANONICAL_PRODUCT_KEY = "::".join(
    ("prod", CANONICAL_MERCHANT_ID, "external_seed", "ext_2ef8d743c054c55cef2cf95e")
)
CANONICAL_GROUP_ID = "pg_ceb91ba4d550214bae12c9c91645c593"
CANONICAL_CONTENT_KEY = "ck_ceb91ba4d550214bae12c9c91645c593"


@dataclass(frozen=True)
class RetailerListing:
    merchant_id: str
    host: str
    native_product_id: str
    variants: dict[str, tuple[str, str]]
    product_path: str = "/products/surratt-lipslique"

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


RETAILERS = {
    "ext:retailer:6548393ec7fb8f0f556da26b1469b3c0": RetailerListing(
        "merch_obs_a2e07b1e8a08148b",
        "bluemercury.com",
        "4586445307979",
        {
            "32360830435403": ("31365263163459", "00617037660642"),
            "32360830533707": ("31365263196227", "00617037660635"),
        },
    ),
    "ext:retailer:076846155d508678762f333db90f3069": RetailerListing(
        "merch_obs_1602ce9889534d7e",
        "frendsbeauty.com",
        "8542558028068",
        {
            "47230401151268": ("31365263163459", "00617037660642"),
            "47230401249572": ("31365263196227", "00617037660635"),
        },
    ),
}

SURRATT_RETAILER_COHORT = ExactRetailerCohort(
    PILOT_FLAG, CANONICAL_PRODUCT_KEY, CANONICAL_MERCHANT_ID, CANONICAL_GROUP_ID, CANONICAL_CONTENT_KEY, RETAILERS,
)

_KIOKII_INITIAL_VARIANTS = {
    "51629126123840": ("52547428942138", "08800299133494"),
    "51896079974720": ("52547429007674", "08800299133517"),
}
KIOKII_INITIAL_OFFER_IDS = frozenset({
    "offer:catalog_enrichment_agent_v1:fc5ef310e498e089",
    "offer:catalog_enrichment_agent_v1:ef54321432063313",
})

KIOKII_RETAILER_COHORT = ExactRetailerCohort(
    flag=KIOKII_FLAG,
    canonical_product_key="::".join(
        ("prod", "merch_obs_1c4bc54448d534ca", "external_seed", "ext_51577f22556c1f3aeb091391")
    ),
    canonical_merchant_id="merch_obs_1c4bc54448d534ca",
    canonical_group_id="pg_d6608eb0dcd14006495d2676df6972d8",
    canonical_content_key="ck_d6608eb0dcd14006495d2676df6972d8",
    listings={
        "ext:retailer:e18722a4e9371490caee4b67526d208b": RetailerListing(
            "merch_obs_9e4d0e3a645af39b",
            "kiokii.com",
            "10073908707648",
            {
                **_KIOKII_INITIAL_VARIANTS,
                "51629126189376": ("52547428909370", "08800299133487"),
                "51629126222144": ("52547428974906", "08800299133500"),
                "51629126156608": ("52547429040442", "08800299133524"),
                "51896079876416": ("52547429073210", "08800299133531"),
            },
            product_path="/en-us/products/amuse-banana-lip-oil-4-5ml",
        ),
    },
)
_kiokii_key, _kiokii_listing = next(iter(KIOKII_RETAILER_COHORT.listings.items()))
KIOKII_INITIAL_RETAILER_COHORT = replace(
    KIOKII_RETAILER_COHORT, listings={_kiokii_key: replace(_kiokii_listing, variants=_KIOKII_INITIAL_VARIANTS)},
)

EXACT_RETAILER_LISTINGS = {**RETAILERS, **KIOKII_RETAILER_COHORT.listings}
# The ordinary Shopify mapper constructs an unlocalized /products path for this family.
# Reserve that URL's identity without admitting it to the US reviewed writer.
KIOKII_BARE_FAMILY_URL = "https://kiokii.com/products/amuse-banana-lip-oil-4-5ml"
KIOKII_BARE_LISTING_KEY = "ext:retailer:581319a8b5513cdd0a9ff47ac6a6e027"
KIOKII_PROTECTED_LISTING_KEYS = frozenset((*KIOKII_RETAILER_COHORT.listings, KIOKII_BARE_LISTING_KEY))
KIOKII_PROTECTED_FAMILY_URLS = tuple(
    listing.family_url for listing in KIOKII_RETAILER_COHORT.listings.values()
) + (KIOKII_BARE_FAMILY_URL,)
PROTECTED_RETAILER_LISTING_KEYS = frozenset(RETAILERS) | KIOKII_PROTECTED_LISTING_KEYS


def pilot_enabled() -> bool:
    return SURRATT_RETAILER_COHORT.enabled()
