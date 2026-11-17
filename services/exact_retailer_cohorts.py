"""Load reviewed US retailer cohorts and an optional private supplemental manifest."""

from dataclasses import dataclass, replace
from hashlib import sha256
from hmac import compare_digest
import json
import os
from pathlib import Path
import re

from services.catalog_identity import validated_source_gtin


COHORTS_PATH = Path(__file__).resolve().parents[1] / "config" / "reviewed_retailer_cohorts.json"
_CONFIG = json.loads(COHORTS_PATH.read_text(encoding="utf-8"))
COHORT_A_CONFIG = _CONFIG["cohort_a"]
COHORT_B_CONFIG = _CONFIG["cohort_b"]
COHORT_C_FLAG = "REVIEWED_RETAILER_EXACT_ENABLED"
COHORT_C_WRITE_FLAG = "REVIEWED_RETAILER_REFRESH_WRITE_ENABLED"
COHORT_C_MANIFEST = "REVIEWED_RETAILER_MANIFEST_PATH"
COHORT_C_MANIFEST_SHA256 = "REVIEWED_RETAILER_MANIFEST_SHA256"
COHORT_C_REVIEW_LABEL = "reviewed_retailer_c_referrals_v1"
COHORT_C_SKIP_REASON = "reviewed_retailer_c_scope"

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
    canonical_signature_id: str | None = None

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


def _matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _manifest_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate private retailer manifest field")
    return result


def _valid_private_manifest(config):
    identifiers = {
        "canonical_product_key": r"ext:[a-z0-9]+(?:-[a-z0-9]+)*::[0-9a-f]{8,32}",
        "canonical_merchant_id": r"merch_obs_[0-9a-f]{16}",
        "canonical_signature_id": r"sig_[0-9a-f]{30,40}",
        "canonical_group_id": r"pg_[0-9a-f]{30,40}",
        "canonical_content_key": r"ck_[0-9a-f]{30,40}",
    }
    if (not isinstance(config, dict) or config.keys() != identifiers.keys() | {"version", "listings", "refresh"}
            or type(config["version"]) is not int or config["version"] != 1
            or not all(_matches(config[field], pattern) for field, pattern in identifiers.items())):
        return False
    listings = config["listings"]
    if not isinstance(listings, dict) or len(listings) != 1:
        return False
    (key, listing), = listings.items()
    if (not _matches(key, r"ext:retailer:[0-9a-f]{32}") or not isinstance(listing, dict)
            or listing.keys() != {"merchant_id", "host", "native_product_id", "product_path", "variants"}
            or not _matches(listing["merchant_id"], r"merch_obs_[0-9a-f]{16}")
            or listing["merchant_id"] == config["canonical_merchant_id"]
            or not _matches(listing["host"], r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
            or listing["host"].startswith("www.")
            or not _matches(listing["product_path"], r"/products/[a-z0-9]+(?:-[a-z0-9]+)*")
            or not _matches(listing["native_product_id"], r"[1-9][0-9]{0,19}")):
        return False
    variants = listing["variants"]
    if not isinstance(variants, dict) or len(variants) != 8:
        return False
    if not all(
        _matches(native, r"[1-9][0-9]{0,19}") and isinstance(pair, list) and len(pair) == 2
        and _matches(pair[0], r"[1-9][0-9]{0,19}") and _matches(pair[1], r"[0-9]{14}")
        and validated_source_gtin(pair[1]) == pair[1]
        for native, pair in variants.items()
    ) or any(len({pair[i] for pair in variants.values()}) != len(variants) for i in (0, 1)):
        return False
    refresh = config["refresh"]
    return (
        isinstance(refresh, dict)
        and refresh.keys() == {"storefront", "vendor", "retailer_name", "category_path", "sku_by_variant"}
        and refresh["storefront"] in (listing["host"], f"www.{listing['host']}")
        and all(_matches(refresh[key], r"[^\x00-\x1f\x7f]{1,120}") and refresh[key] == refresh[key].strip()
                for key in ("vendor", "retailer_name"))
        and _matches(refresh["category_path"], r"[a-z0-9-]+(?:/[a-z0-9-]+)*")
        and isinstance(refresh["sku_by_variant"], dict) and refresh["sku_by_variant"].keys() == variants.keys()
        and all(_matches(sku, r"[A-Za-z0-9_.-]{1,80}") for sku in refresh["sku_by_variant"].values())
    )


def load_private_manifest():
    path = os.getenv(COHORT_C_MANIFEST)
    digest = os.getenv(COHORT_C_MANIFEST_SHA256)
    if path is None and digest is None and all(os.getenv(flag) != "1" for flag in (COHORT_C_FLAG, COHORT_C_WRITE_FLAG)):
        return None
    if not path or not _matches(digest, r"[0-9a-f]{64}"):
        raise ValueError("private retailer manifest requires a path and SHA-256 pin")
    try:
        with Path(path).open("rb") as source:
            data = source.read(32769)
        if len(data) > 32768 or not compare_digest(sha256(data).hexdigest(), digest):
            raise ValueError()
        config = json.loads(data, object_pairs_hook=_manifest_object)
        if not _valid_private_manifest(config):
            raise ValueError()
    except (OSError, ValueError, RecursionError):
        raise ValueError("private retailer manifest is unreadable or invalid") from None
    return config


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
COHORT_C_CONFIG = load_private_manifest()
RETAILER_COHORT_C = None if COHORT_C_CONFIG is None else ExactRetailerCohort(
    flag=COHORT_C_FLAG,
    canonical_product_key=COHORT_C_CONFIG["canonical_product_key"],
    canonical_merchant_id=COHORT_C_CONFIG["canonical_merchant_id"],
    canonical_group_id=COHORT_C_CONFIG["canonical_group_id"],
    canonical_content_key=COHORT_C_CONFIG["canonical_content_key"],
    listings=_listings(COHORT_C_CONFIG),
    canonical_signature_id=COHORT_C_CONFIG["canonical_signature_id"],
)
_cohort_b_key, _cohort_b_listing = next(iter(RETAILER_COHORT_B.listings.items()))
_COHORT_B_INITIAL_VARIANTS = {
    native: _cohort_b_listing.variants[native] for native in COHORT_B_CONFIG["initial_variants"]
}
COHORT_B_INITIAL_OFFER_IDS = frozenset(COHORT_B_CONFIG["initial_offer_ids"])
RETAILER_COHORT_B_INITIAL = replace(
    RETAILER_COHORT_B, listings={_cohort_b_key: replace(_cohort_b_listing, variants=_COHORT_B_INITIAL_VARIANTS)},
)

_supplemental_listings = RETAILER_COHORT_C.listings if RETAILER_COHORT_C else {}
if _supplemental_listings.keys() & (RETAILERS.keys() | RETAILER_COHORT_B.listings.keys() | {COHORT_B_CONFIG["bare_listing_key"]}):
    raise ValueError("private retailer manifest overlaps an existing cohort")
EXACT_RETAILER_LISTINGS = {**RETAILERS, **RETAILER_COHORT_B.listings, **_supplemental_listings}
# The ordinary Shopify mapper constructs an unlocalized /products path for this family.
# Reserve that URL's identity without admitting it to the US reviewed writer.
COHORT_B_BARE_FAMILY_URL = COHORT_B_CONFIG["bare_family_url"]
COHORT_B_BARE_LISTING_KEY = COHORT_B_CONFIG["bare_listing_key"]
COHORT_B_PROTECTED_LISTING_KEYS = frozenset((*RETAILER_COHORT_B.listings, COHORT_B_BARE_LISTING_KEY))
COHORT_B_PROTECTED_FAMILY_URLS = tuple(
    listing.family_url for listing in RETAILER_COHORT_B.listings.values()
) + (COHORT_B_BARE_FAMILY_URL,)
EXACT_ONLY_LISTING_KEYS = COHORT_B_PROTECTED_LISTING_KEYS | frozenset(_supplemental_listings)
EXACT_ONLY_FAMILY_URLS = COHORT_B_PROTECTED_FAMILY_URLS + tuple(
    listing.family_url for listing in _supplemental_listings.values()
)
PROTECTED_RETAILER_LISTING_KEYS = frozenset(RETAILERS) | EXACT_ONLY_LISTING_KEYS


def pilot_enabled() -> bool:
    return RETAILER_COHORT_A.enabled()
