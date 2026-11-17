"""Bounded model review for individually allowlisted US brand-direct drafts."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
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
