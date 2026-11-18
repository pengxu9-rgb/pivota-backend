"""Bounded model review for individually allowlisted US brand-direct drafts."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from hmac import compare_digest
import json
import logging
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit, urlunsplit

import httpx

from services import crawl_politeness, shopify_presentment, storefront_currency
from services.catalog_identity import validated_source_gtin
from services.external_seed_model_review import ReviewTarget, build_review_snapshot, review_seed
from services.shopify_storefront_session import MarketsRefused, ShopifyStorefrontSession, _ua
from services.exact_retailer_cohorts import RETAILER_COHORT_B

# The allowlisted drafts, their reviewed listing facts and the corroborating retailer read
# live in config/external_seed_review_listings.json.
LISTINGS_PATH = Path(__file__).resolve().parents[1] / "config" / "external_seed_review_listings.json"
_CONFIG = json.loads(LISTINGS_PATH.read_text(encoding="utf-8"))
POLICY_VERSION = _CONFIG["policy_version"]
REVIEW_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash")
EVIDENCE_MAX_AGE = timedelta(minutes=10)
SEED_B_ID = _CONFIG["listings"]["seed_b"]["seed_id"]
SEED_C_ID = _CONFIG["listings"]["seed_c"]["seed_id"]
SEED_A_ID = _CONFIG["listings"]["seed_a"]["seed_id"]
_CORROBORATION = _CONFIG["barcode_corroboration"]


@dataclass(frozen=True)
class PilotListing:
    url: str
    seller_ref: str
    price_minor: int
    brand: str
    title: str
    description: str
    variants: tuple[tuple[str, str, str, str], ...]
    native_product_id: int
    corroboration_url: str | None = None
    seller_source_ref: str | None = None
    shipping_source_url: str | None = None
    native_shop_domain: str | None = None
    policy_version: str = POLICY_VERSION
    machine_activation_allowed: bool = False

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc

    @property
    def seller_host(self) -> str:
        return self.seller_source_ref or self.host

    @property
    def listing_urls(self) -> tuple[str, ...]:
        if self.seller_host == self.host:
            return (self.url,)
        alias_url = urlunsplit(urlsplit(self.url)._replace(netloc=self.seller_host))
        return (self.url, alias_url)


_LISTING_FIELDS = frozenset(PilotListing.__dataclass_fields__)


def _listing(row: dict) -> PilotListing:
    fields = {key: value for key, value in row.items() if key in _LISTING_FIELDS}
    fields["variants"] = tuple(tuple(variant) for variant in fields["variants"])
    return PilotListing(**fields)


LISTINGS = {row["seed_id"]: _listing(row) for row in _CONFIG["listings"].values()}

# Serving may preserve the selected shade for independently reviewed additions after
# they are present in the active seed and catalog. Draft review uses LISTINGS only.
# The reviewed additions are real catalog identifiers, so they live in a private manifest
# mounted per runtime: NAMED_REFERRAL_VARIANTS_MANIFEST_PATH, pinned by
# NAMED_REFERRAL_VARIANTS_MANIFEST_SHA256. Shapes:
#   {"schema_version": 1, "listings": {"<seed_id>": {"<native variant id>": ["<GTIN-14>", "<SKU>"]}}}
#   {"schema_version": 2, "listings": {"<seed_id>": {
#       "listing": {"url": "https://<store>/products/<handle>", "seller_ref": "merch_obs_<16 hex>",
#                   "native_product_id": <int>, "variants": [["<native id>", "<GTIN-14>", "<shade>", "<SKU>"], ...]},
#       "additions": {"<native variant id>": ["<GTIN-14>", "<SKU>"]}}}}
# v2 `listing` carries the reviewed listing identity of a seed that is NOT in the public LISTINGS (and is
# refused for one that is). Serving still matches it against the live seed's seller and family URL.
# Unset: no additions. Set but unreadable, unpinned or invalid: no additions and an ERROR
# log; only this shade pinning is disabled (links fall back to the family page).
NAMED_REFERRAL_VARIANTS_MANIFEST = "NAMED_REFERRAL_VARIANTS_MANIFEST_PATH"
NAMED_REFERRAL_VARIANTS_MANIFEST_SHA256 = "NAMED_REFERRAL_VARIANTS_MANIFEST_SHA256"
# The mount is a Secret Manager secret, whose payload limit is 64 KiB, and it carries every batch's seeds at
# once (a v2 seed with its listing and ~30 shades is ~4 KiB). The old 32 KiB cap fits ~8 such seeds.
_MANIFEST_LIMIT = 65536
# A Shopify product page exactly: lower-case host, one non-empty handle, nothing else (no userinfo, port,
# params, query, fragment, escapes or whitespace).
_PRODUCT_URL = re.compile(r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"
                          r"/products/[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_SELLER_REF = re.compile(r"merch_obs_[0-9a-f]{16}\Z")
_NATIVE_VARIANT_ID = re.compile(r"[1-9][0-9]{0,23}\Z")
_SKU = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")
_logger = logging.getLogger(__name__)


def _unique_object(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate manifest key")
    return dict(pairs)


def is_product_page_url(url) -> bool:
    """The one rule for a reviewed listing url (the batch writer uses it too): an exact https product page."""
    return (isinstance(url, str) and url.isascii() and len(url) <= 300 and bool(_PRODUCT_URL.fullmatch(url))
            and urlunsplit(urlsplit(url)) == url)


def _private_listing(raw) -> PilotListing:
    if not isinstance(raw, dict) or raw.keys() != {"url", "seller_ref", "native_product_id", "variants"}:
        raise ValueError("manifest listing must be {url, seller_ref, native_product_id, variants}")
    if not is_product_page_url(raw["url"]):
        raise ValueError("manifest listing url is not an https product page")
    if not isinstance(raw["seller_ref"], str) or not _SELLER_REF.fullmatch(raw["seller_ref"]):
        raise ValueError("manifest listing seller_ref is invalid")
    if type(raw["native_product_id"]) is not int or not 0 < raw["native_product_id"] < 10 ** 20:
        raise ValueError("manifest listing native_product_id is invalid")
    variants = raw["variants"]
    if not isinstance(variants, list) or not variants:
        raise ValueError("manifest listing has no existing variants")
    checked = []
    for variant in variants:
        if (not isinstance(variant, list) or len(variant) != 4 or not all(isinstance(item, str) for item in variant)
                or not _NATIVE_VARIANT_ID.fullmatch(variant[0]) or validated_source_gtin(variant[1]) != variant[1]
                or len(variant[1]) != 14 or not 0 < len(variant[2].strip()) <= 80 or not _SKU.fullmatch(variant[3])):
            raise ValueError("manifest listing variant must be [native id, GTIN-14, shade, SKU]")
        checked.append(tuple(variant))
    if len({v[0] for v in checked}) != len(checked):
        raise ValueError("manifest listing repeats a variant")
    # Serving reads url, seller_ref and variants only; the review-only fields stay empty.
    return PilotListing(url=raw["url"], seller_ref=raw["seller_ref"], price_minor=0, brand="", title="",
                        description="", variants=tuple(checked), native_product_id=raw["native_product_id"])


def validate_named_referral_manifest(config, listings):
    """Return (additions by seed, private listings by seed), or raise ValueError naming the first problem."""
    if (not isinstance(config, dict) or config.keys() != {"schema_version", "listings"}
            or type(config["schema_version"]) is not int or config["schema_version"] not in (1, 2)):
        raise ValueError("manifest must be {schema_version: 1 or 2, listings: {...}}")
    version = config["schema_version"]
    entries = config["listings"]
    if not isinstance(entries, dict) or not entries:
        raise ValueError("manifest lists no seeds")
    result, private = {}, {}
    # Native variant ids and GTINs are global identities: one may belong to one seed only. (SKUs are per store.)
    owner_of_id, owner_of_gtin = {}, {}
    for seed_id, entry in entries.items():
        listing = listings.get(seed_id)
        if version == 1:
            additions = entry
        else:
            if not isinstance(entry, dict) or not ({"additions"} <= entry.keys() <= {"additions", "listing"}):
                raise ValueError("manifest v2 seed must be {additions, listing?}")
            additions = entry["additions"]
            if "listing" in entry:
                if listing is not None:
                    raise ValueError("manifest v2 listing repeats a public reviewed listing")
                if not isinstance(seed_id, str) or not re.fullmatch(r"eps_[0-9a-f]{24}", seed_id):
                    raise ValueError("manifest v2 seed id is invalid")
                listing = private[seed_id] = _private_listing(entry["listing"])
        if listing is None:
            raise ValueError("manifest seed is not a reviewed listing")
        if not isinstance(additions, dict) or not additions:
            raise ValueError("manifest seed has no additions")
        existing_ids = {variant_id for variant_id, *_ in listing.variants}
        seen_gtins = {validated_source_gtin(barcode) for _, barcode, *_ in listing.variants}
        seen_skus = {sku for *_, sku in listing.variants}
        checked = {}
        for native_id, identity in additions.items():
            if not isinstance(native_id, str) or not _NATIVE_VARIANT_ID.fullmatch(native_id):
                raise ValueError("manifest variant id is not a native id")
            if native_id in existing_ids:
                raise ValueError("manifest variant repeats a reviewed listing variant")
            if not isinstance(identity, list) or len(identity) != 2:
                raise ValueError("manifest variant must be [GTIN-14, SKU]")
            gtin, sku = identity
            if not isinstance(gtin, str) or len(gtin) != 14 or validated_source_gtin(gtin) != gtin:
                raise ValueError("manifest GTIN is not a check-digit valid GTIN-14")
            if not isinstance(sku, str) or not _SKU.fullmatch(sku):
                raise ValueError("manifest SKU is invalid")
            if gtin in seen_gtins or sku in seen_skus:
                raise ValueError("manifest GTIN or SKU is not unique within its listing")
            seen_gtins.add(gtin)
            seen_skus.add(sku)
            checked[native_id] = (gtin, sku)
        identities = [(variant_id, validated_source_gtin(barcode)) for variant_id, barcode, *_ in listing.variants]
        for native_id, (gtin, _sku) in checked.items():
            identities.append((native_id, gtin))
        for native_id, gtin in identities:
            if owner_of_id.setdefault(native_id, seed_id) != seed_id or (
                    gtin and owner_of_gtin.setdefault(gtin, seed_id) != seed_id):
                raise ValueError("manifest variant id or GTIN belongs to another seed")
        result[seed_id] = checked
    return result, private


def validate_named_referral_variants(config, listings) -> dict[str, dict[str, tuple[str, str]]]:
    """The reviewed additions keyed by seed (v1 and v2), or raise ValueError naming the first problem."""
    return validate_named_referral_manifest(config, listings)[0]


def load_named_referral_manifest(listings=None):
    """(additions by seed, private listings by seed). Any refusal disables only this shade pinning."""
    listings = LISTINGS if listings is None else listings
    path = os.getenv(NAMED_REFERRAL_VARIANTS_MANIFEST)
    digest = os.getenv(NAMED_REFERRAL_VARIANTS_MANIFEST_SHA256)
    if not path and not digest:
        return {}, {}
    try:
        if not path or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("manifest requires a path and a SHA-256 pin")
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise ValueError("manifest path is not a regular file")
        with Path(path).open("rb") as source:
            data = source.read(_MANIFEST_LIMIT + 1)
        if len(data) > _MANIFEST_LIMIT:
            raise ValueError("manifest is too large")
        if not compare_digest(sha256(data).hexdigest(), digest):
            raise ValueError("manifest bytes do not match the SHA-256 pin")
        return validate_named_referral_manifest(json.loads(data, object_pairs_hook=_unique_object), listings)
    except (OSError, ValueError, RecursionError) as error:
        _logger.error("named referral variants manifest refused; shade pinning disabled: %s", error)
        return {}, {}


def load_named_referral_variants(listings=None) -> dict[str, dict[str, tuple[str, str]]]:
    return load_named_referral_manifest(listings)[0]


NAMED_REFERRAL_VARIANTS, NAMED_REFERRAL_LISTINGS = load_named_referral_manifest()


SCOPED_LISTING = _listing(_CONFIG["scoped_listing"])
SCOPED_EXTERNAL_PRODUCT_ID = _CONFIG["scoped_listing"]["external_product_id"]
SCOPED_LISTING_PIN_ENV = _CONFIG["scoped_listing"]["pin_env"]


def machine_review_listing(seed_id: str) -> PilotListing | None:
    """The isolated job may pin one new draft ID without widening the existing pilot."""
    listing = LISTINGS.get(seed_id)
    if listing is not None:
        return listing
    pinned = os.getenv(SCOPED_LISTING_PIN_ENV)
    if seed_id == pinned and re.fullmatch(r"eps_[0-9a-f]{24}", seed_id):
        return SCOPED_LISTING
    return None


def _usd_minor(value) -> int | None:
    try:
        minor = Decimal(str(value)) * 100
    except InvalidOperation:
        return None
    return int(minor) if minor.is_finite() and minor == minor.to_integral_value() else None


def draft_matches_listing(row: dict, seed_data: dict, listing: PilotListing) -> bool:
    if (
        row.get("status") != "draft" or row.get("market") != "US" or row.get("tool") != "*"
        or row.get("seller_ref") != listing.seller_ref or row.get("seed_kind") != "cross"
        or row.get("domain") not in (listing.host, listing.seller_host) or row.get("destination_url") != listing.url
        or row.get("canonical_url") not in (None, listing.url)
        or not row.get("external_product_id") or seed_data.get("external_product_id") != row["external_product_id"]
        or str(seed_data.get("product_id") or "") != str(listing.native_product_id)
        or row.get("title") != listing.title or seed_data.get("title") != listing.title
        or seed_data.get("brand") != listing.brand or seed_data.get("description") != listing.description
        or seed_data.get("merchant_display_name") != listing.brand or row.get("attached_product_key")
        or row.get("attached_variant_id") or seed_data.get("revision_of")
        or "audit_reject:" in str(row.get("notes") or "")
        or row.get("destination_corroborated_dead_at") or row.get("destination_dead_404_since")
        or row.get("destination_verdict") in ("dead_404", "redirected_off_product")
        or row.get("price_currency") != "USD" or _usd_minor(row.get("price_amount")) != listing.price_minor
        or row.get("availability") != "in_stock"
    ):
        return False
    variants = seed_data.get("variants")
    if not isinstance(variants, list) or len(variants) != len(listing.variants):
        return False
    if any(not isinstance(variant, dict) for variant in variants):
        return False
    expected = {variant_id: (gtin, shade, sku) for variant_id, gtin, shade, sku in listing.variants}
    for variant in variants:
        variant_id = str(variant.get("variant_id") or variant.get("id") or "")
        if variant_id not in expected:
            return False
        gtin, shade, sku = expected.pop(variant_id)
        if (
            str(variant.get("barcode") or "") != gtin
            or variant.get("sku") != sku
            or str(variant.get("title") or "").strip().casefold() != shade.casefold()
            or variant.get("url") != f"{listing.url}?variant={variant_id}"
            or variant.get("price_currency") != "USD"
            or _usd_minor(variant.get("price_amount")) != listing.price_minor
            or variant.get("availability") != "in_stock" or variant.get("in_stock") is not True
        ):
            return False
    return not expected


def _public_variants(listing: PilotListing, detail: object) -> dict | None:
    variants = detail.get("variants") if isinstance(detail, dict) else None
    if not isinstance(variants, list) or detail.get("id") != listing.native_product_id:
        return None
    expected = {variant_id: (gtin, shade, sku) for variant_id, gtin, shade, sku in listing.variants}
    observed = {}
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        variant_id = str(variant.get("id") or "")
        if variant_id not in expected:
            continue
        gtin, shade, expected_sku = expected[variant_id]
        barcode = variant.get("barcode") or None
        sku = str(variant.get("sku") or "")
        sku_only = bool(listing.corroboration_url and not barcode and sku == gtin)
        if (
            variant_id in observed or sku != expected_sku
            or (validated_source_gtin(barcode) != validated_source_gtin(gtin) and not sku_only)
            or str(variant.get("title") or "").strip().casefold() != shade.casefold()
            or shopify_presentment.products_js_price_minor(variant.get("price"), "USD") != listing.price_minor
            or variant.get("available") is not True
        ):
            return None
        observed[variant_id] = {"variant_id": variant_id, "gtin": gtin, "shade": shade,
                                "price_minor": listing.price_minor, "in_stock": True,
                                "native_barcode": barcode, "merchant_sku": sku,
                                "gtin_provenance": "retailer_corroborated_sku" if sku_only else "native_barcode"}
    return observed if observed.keys() == expected.keys() else None


async def _barcode_corroboration(client, listing: PilotListing, observed: dict) -> dict | None:
    if not listing.corroboration_url:
        return {}
    required = {variant_id: variant for variant_id, variant in observed.items() if not variant["native_barcode"]}
    if not required:
        return {}
    url = urlsplit(listing.corroboration_url)
    session = ShopifyStorefrontSession(client, url.netloc, crawl_politeness, {}, "US")
    code, detail = await session.json(f"{url.path}?{url.query}")
    variants = detail.get("variants") if code == 200 and isinstance(detail, dict) else None
    if (
        not isinstance(variants, list) or detail.get("id") != _CORROBORATION["product_id"]
        or str(detail.get("title") or "").strip().casefold() != _CORROBORATION["title"]
    ):
        return None
    (retailer,) = RETAILER_COHORT_B.listings.values()
    expected_ids = {canonical: native for native, (canonical, _) in retailer.variants.items()}
    matched = {}
    for variant_id, brand in required.items():
        retailer_shade = _CORROBORATION["retailer_shade_titles"].get(variant_id, brand["shade"])
        matches = [
            item for item in variants if isinstance(item, dict)
            and str(item.get("id") or "") == expected_ids.get(variant_id)
            and str(item.get("title") or "").strip().casefold() == retailer_shade.casefold()
            and validated_source_gtin(item.get("barcode")) == validated_source_gtin(brand["gtin"])
        ]
        if len(matches) != 1:
            return None
        matched[variant_id] = {
            "source_url": listing.corroboration_url, "variant_id": str(matches[0]["id"]),
            "shade": brand["shade"], "native_barcode": brand["gtin"], "gtin_provenance": "identity_only",
        }
    return matched


async def _cart_confirmed_product(listing: PilotListing, path: str) -> object | None:
    """Re-read the product between two USD cart observations in the same session."""
    async with httpx.AsyncClient(
        timeout=8.0, follow_redirects=False, headers={"User-Agent": _ua(), "Accept": "application/json"},
    ) as client:
        session = ShopifyStorefrontSession(client, listing.host, crawl_politeness, {}, "US")
        before = await session.cart_currency()
        if before != "USD":
            return None
        code, detail, currency, problem = await session.json_presented(path)
        if code != 200 or (currency != "USD" and problem != "cookie_absent"):
            return None
        return detail if await session.cart_currency() == "USD" else None


async def capture_shopify_evidence(listing: PilotListing) -> dict:
    """Use the existing paced, market-pinned Shopify reads on the allowlisted host."""
    path = f"{urlsplit(listing.url).path}.js?country=US"
    shipping_url = listing.shipping_source_url or f"https://{listing.host}/meta.json"
    shipping = urlsplit(shipping_url)
    try:
        async with shopify_presentment.no_cookie_client(
            timeout=8.0, follow_redirects=False, headers={"User-Agent": _ua(), "Accept": "application/json"},
        ) as client:
            session = ShopifyStorefrontSession(client, listing.host, crawl_politeness, {}, "US")
            shipping_session = (
                session if shipping.netloc == listing.host
                else ShopifyStorefrontSession(client, shipping.netloc, crawl_politeness, {}, "US")
            )
            meta_response = await shipping_session.request("GET", shipping.path, expect_json=True)
            meta = storefront_currency.parse_meta(meta_response.text) if meta_response.status_code == 200 else None
            if not meta or "US" not in (meta.get("ships_to_countries") or []):
                return {"reason": "storefront_us_reach_unverified"}
            if listing.native_shop_domain and meta.get("myshopify_domain") != listing.native_shop_domain:
                return {"reason": "storefront_shop_identity_changed"}
            code, detail, currency, problem = await session.json_presented(path)
            currency_proof = "product_cart_currency_cookie"
            if code == 200 and problem == "cookie_absent" and meta["currency"] == "USD":
                detail = await _cart_confirmed_product(listing, path)
                currency = "USD" if detail is not None else None
                currency_proof = "store_base_and_same_session_cart"
            if code != 200 or currency != "USD":
                return {"reason": "storefront_us_price_unverified"}
            observed = _public_variants(listing, detail)
            if not observed:
                return {"reason": "storefront_variants_changed"}
            corroboration = await _barcode_corroboration(client, listing, observed)
            if corroboration is None:
                return {"reason": "variant_barcode_corroboration_unverified"}
    except (MarketsRefused, httpx.HTTPError):
        return {"reason": "storefront_unavailable"}
    captured_at = datetime.now(timezone.utc).isoformat()
    return {
        "captured_at": captured_at,
        "sources": {
            **{f"shopify_us:{variant_id}": {
                **variant,
                "source_url": f"https://{listing.host}{path}",
                "offer_url": f"{listing.url}?variant={variant_id}",
                "shipping_source_url": shipping_url,
                "shipping_scope": "store_declares_us_destination",
                "country": "US", "currency": currency, "currency_proof": currency_proof,
                "observed_at": captured_at,
            }
            for variant_id, variant in observed.items()},
            **{f"barcode:{variant_id}": {**variant, "observed_at": captured_at}
               for variant_id, variant in corroboration.items()},
        },
        "target_sources": [
            {"variant_id": variant_id, "scope": "brand_direct", "offer_id": None,
             "evidence_refs": [f"shopify_us:{variant_id}"]
             + ([f"barcode:{variant_id}"] if variant_id in corroboration else [])}
            for variant_id in observed
        ],
    }


async def adjudicate(
    seed_id: str, review_version: str, listing: PilotListing, evidence: dict,
    *, exact_retailer_targets: tuple[ReviewTarget, ...] = (),
) -> dict:
    from services.external_seed_gemini_reviewer import run_gemini_review

    snapshot = build_review_snapshot(
        seed_id=seed_id, seed_snapshot={"review_version": review_version, "policy_version": listing.policy_version},
        evidence_packet=evidence,
        expected_targets=[ReviewTarget(variant[0], "brand_direct") for variant in listing.variants]
        + list(exact_retailer_targets),
    )
    primary, secondary = await asyncio.gather(
        run_gemini_review(snapshot, evidence, stage="primary", model_name=REVIEW_MODELS[0],
                          targets=snapshot.expected_targets),
        run_gemini_review(snapshot, evidence, stage="secondary", model_name=REVIEW_MODELS[1],
                          targets=snapshot.expected_targets),
    )
    receipts = dict(primary_receipt=primary, secondary_receipt=secondary)
    result = review_seed(snapshot, expected_model_names=REVIEW_MODELS, **receipts)
    if result.state == "requires_tie_break":
        tie_break = await run_gemini_review(snapshot, evidence, stage="tie_break", model_name=REVIEW_MODELS[2],
                                            targets=result.tie_break_targets)
        receipts["tie_break_receipt"] = tie_break
        if tie_break is not None:
            result = review_seed(snapshot, expected_model_names=REVIEW_MODELS, **receipts)
    return {
        "evidence_digest": snapshot.evidence_digest,
        "result": asdict(result),
        "receipts": {stage: asdict(receipt) if receipt else None for stage, receipt in receipts.items()},
        "eligible": result.state == "completed"
        and all(item.action == "employee_review" for item in result.decisions),
    }
