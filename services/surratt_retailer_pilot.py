"""Reviewed US listing and native shade identities for the Surratt retailer pilot."""

from dataclasses import dataclass
import os


PILOT_FLAG = "SURRATT_EXACT_RETAILER_SHADES_ENABLED"
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

    @property
    def family_url(self) -> str:
        return f"https://{self.host}/products/surratt-lipslique"


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


def pilot_enabled() -> bool:
    return os.getenv(PILOT_FLAG) == "1"
