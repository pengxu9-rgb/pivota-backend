"""Bounded model review for individually allowlisted US brand-direct drafts."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

import httpx

from services import crawl_politeness, shopify_presentment, storefront_currency
from services.catalog_identity import validated_source_gtin
from services.external_seed_model_review import ReviewTarget, build_review_snapshot, review_seed
from services.retailer_ingest.shopify_markets import MarketsRefused, _Session, _ua

POLICY_VERSION = "olive-young-us-brand-direct-v3"
REVIEW_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash")
EVIDENCE_MAX_AGE = timedelta(minutes=10)
_AMUSE_BARCODE_SOURCE = "https://kiokii.com/en-us/products/amuse-banana-lip-oil-4-5ml.js?country=US"
SURRATT_SEED_ID = "eps_9f3368aa75d349e09914fbcb"


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


LISTINGS = {
    "eps_31f74c1ec20a4d5db0fa977f": PilotListing(
        "https://amuseseoulmakeup.com/products/banana-lip-oil", "merch_obs_1c4bc54448d534ca", 1500,
        "AMUSE", "Banana Lip Oil",
        "A 4.5 mL tinted lip oil with a glossy finish, a banana scent and a cushioned applicator. "
        "Shade 02 Banana is clear, while 04 Juicy Peach gives a peach tint.",
        (("52547428942138", "8800299133494", "02 BANANA", "8800299133494"),
         ("52547429007674", "8800299133517", "04 JUICY PEACH", "8800299133517")),
        10117885559098, _AMUSE_BARCODE_SOURCE,
    ),
    "eps_6aa6d3d1e93a424b8c64d6c5": PilotListing(
        "https://romandbeauty.com/products/glasting-melting-balm", "merch_obs_acf4a659be2fb0fb", 1400,
        "rom&nd", "Glasting Melting Balm",
        "A 3.5 g tinted lip balm with a soft, melting texture and buildable, glass-like shine. "
        "Coco Nude is a beige nude and Mauve Whip is a mauve pink.",
        (("41341731438667", "8800258082894", "COCO NUDE", "UGMB01"),
         ("41341731635275", "8800258083112", "MAUVE WHIP", "UGMB07")),
        7317311291467,
    ),
    SURRATT_SEED_ID: PilotListing(
        "https://www.surrattbeauty.com/products/lipslique", "merch_obs_30f9ce3e44b7fcb4", 3400,
        "Surratt Beauty", "Lipslique",
        "Lipslique is a twist-up lip color with a lightweight, balm-like feel and a creamy finish. "
        "The formula contains macadamia seed oil for moisture and can be layered for more color. "
        "Each tube contains 1.56 g.",
        (("31365263163459", "617037660642", "NU DE SOLEIL - PEACHY BEIGE", "SB003-10"),
         ("31365263196227", "617037660635", "PARAMOUR - PEACHY CORAL", "SB003-09")),
        4389835046979, seller_source_ref="surrattbeauty.com",
        shipping_source_url="https://surrattbeauty.com/meta.json", native_shop_domain="surrattbeauty.myshopify.com",
        policy_version="surratt-us-brand-direct-v1",
        machine_activation_allowed=True,
    ),
}


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
    session = _Session(client, url.netloc, crawl_politeness, {}, "US")
    code, detail = await session.json(f"{url.path}?{url.query}")
    variants = detail.get("variants") if code == 200 and isinstance(detail, dict) else None
    if (
        not isinstance(variants, list) or detail.get("id") != 10073908707648
        or str(detail.get("title") or "").strip().casefold() != "amuse banana lip oil 4.5ml"
    ):
        return None
    expected_ids = {"52547428942138": "51629126123840", "52547429007674": "51896079974720"}
    matched = {}
    for variant_id, brand in required.items():
        matches = [
            item for item in variants if isinstance(item, dict)
            and str(item.get("id") or "") == expected_ids[variant_id]
            and str(item.get("title") or "").strip().casefold() == brand["shade"].casefold()
            and validated_source_gtin(item.get("barcode")) == validated_source_gtin(brand["gtin"])
        ]
        if len(matches) != 1:
            return None
        matched[variant_id] = {
            "source_url": listing.corroboration_url, "variant_id": expected_ids[variant_id],
            "shade": brand["shade"], "native_barcode": brand["gtin"], "gtin_provenance": "identity_only",
        }
    return matched


async def _cart_confirmed_product(listing: PilotListing, path: str) -> object | None:
    """Re-read the product between two USD cart observations in the same session."""
    async with httpx.AsyncClient(
        timeout=8.0, follow_redirects=False, headers={"User-Agent": _ua(), "Accept": "application/json"},
    ) as client:
        session = _Session(client, listing.host, crawl_politeness, {}, "US")
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
            session = _Session(client, listing.host, crawl_politeness, {}, "US")
            shipping_session = (
                session if shipping.netloc == listing.host
                else _Session(client, shipping.netloc, crawl_politeness, {}, "US")
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


async def adjudicate(seed_id: str, review_version: str, listing: PilotListing, evidence: dict) -> dict:
    from services.external_seed_gemini_reviewer import run_gemini_review

    snapshot = build_review_snapshot(
        seed_id=seed_id, seed_snapshot={"review_version": review_version, "policy_version": listing.policy_version},
        evidence_packet=evidence,
        expected_targets=[ReviewTarget(variant[0], "brand_direct") for variant in listing.variants],
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
