#!/usr/bin/env python3
"""READ-ONLY census: what the PDP live read compares, for the hosts where it says "price changed" on
nearly every read.

THE SIGNAL. The gateway's background PDP read (PIVOTA-Agent src/services/ucpLiveOfferRead.js,
createUcpLiveProductReader.readAndLog, logged as get_pdp_v2_ucp_live_read) counts a variant as
`price_changed` when

    Number(before.price.current.amount) !== roundTo(live.amount, currency)

where `before` is a variant of the get_pdp_v2 page AS BUILT (canonical module data.pdp_payload.product,
before the live overlay) and `live` is the merchant's UCP lookup_catalog answer for the same Shopify
variant number. Two properties of that test matter here:

  * A variant the page shows WITHOUT a price (b undefined -> NaN) is counted as changed whenever the
    merchant answers it. The page withholds the price of every variant of a listing whose own money is
    unavailable (own_money_gap = CURRENT_OWN_OFFER_UNAVAILABLE, page_state read_only), and of any single
    variant without own money (current_own_offer_status 'unavailable').
  * No currency is compared (an unpriced page has no currency, so the reader's re-price guard is skipped).

WHAT THE PAGE STORES, per lane (gateway at 04f819f4a):

  * Enrichment lane (catalog_products.source_system = 'catalog_enrichment_agent_v1', platform
    'external_seed'), market US / USD -- canonicalPdpOwnMoney.readCanonicalOwnMoney: each variant's price
    is coalesce(catalog_offers.merchant_effective_price, catalog_offers.list_price) of the listing's OWN
    offer (own-seller SQL below; public trust, live, US/USD, in_stock, unsuppressed, > 0), keyed by
    catalog_skus.source_variant_id -- matched to the page variant id EXACTLY (or its numeric alias). A
    variant without such a row is unpriced; a default variant without one, or a conflict, makes the whole
    listing unavailable. CANONICAL_OWN_MONEY_FRESHNESS is `shadow` in prod (10-22), so price_checked_at
    does not gate what is served.
  * Every external seed page: the page variants are the active seed's (lane 0: external_product_id =
    catalog_products.source_product_id, newest updated_at first; lane 1: attached_product_key)
    seed_data->'snapshot'->'variants' when non-empty, ELSE seed_data->'variants' (server.js
    fetchExternalSeedProductDetailFromDb; the nightly refresh reconciles seed_data.variants, so a stale
    snapshot list shadows it). id = variant_id | id | sku; price = price | price_amount |
    pricing.current.amount, through normalizeExternalSeedPrice (an integer >= 1000 in a non-zero-decimal
    currency, on a beauty-worded title/url, is divided by 100). Outside the enrichment lane this IS the
    compared stored amount.
  * A connected merchant's page (not platform external_seed, no seed): the newest products_cache row of
    (merchant_id, platform_product_id = source_product_id), its variants priced as cached.

No crawled lane stores a compare-at: every external writer sets list_price = merchant_effective_price =
estimated_best_price = the read price, and no writer puts compare_at_price on a seed variant. The census
still reads every compare-at key a variant could carry and reports how many it found, so "the stored
amount is the compare-at" is answered by data, not assumed.

OUTPUT. CENSUS_MARKER + one JSON object per line:
  * params;
  * one `host` line per host: products (signature rows), lanes, page verdicts (own money: priced /
    partially priced / unavailable by reason), live-readable variants (the reader asks a 6-20 digit
    Shopify id, gid or ?variant= URL), readable variants the page leaves UNPRICED (counted as changed on
    every answered read), offer flags (currency vs market, price_checked_at NULL / > --stale-days,
    merchant_effective_price != list_price, stored amount = a compare-at), seed flags (snapshot list
    shadowing a different variants price, minor-unit division, seed currency vs market);
  * one `product` line per signature row (capped by --max-products-per-host): verdict, counts and the first
    variants' stored amounts, to join offline with the log's per-product price_changed history.

--signatures sig_a,sig_b reads THOSE PDPs' rows (pivota_signature_id, indexed) instead of the hosts' --
for a host whose rows carry another source_domain or none (a connected merchant's); host lines then group
by the domain the rows carry.

SAFETY. One READ ONLY transaction (REPEATABLE READ, one snapshot) with SET LOCAL statement_timeout = 30s;
it writes nothing, creates nothing, calls no merchant and no agent tool. Up to four reads: the signature
rows of the hosts (one pass over catalog_products: there is no source_domain index; by signature, an index
read), their offers (by product_key, indexed), their seeds (by external_product_id /
attached_product_key, indexed; seed_data is detoasted ONCE per seed) and, for connected-merchant rows
only, their products_cache rows (by merchant_id + platform_product_id, indexed). It carries no at-sign so it can be passed inline:

    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_pdp_live_price_drift.py)"
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_pdp_live_price_drift.py)" --hosts heimish.us,tocobo.us
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_pdp_live_price_drift.py)" --signatures sig_a,sig_b

Self-contained on purpose (the gateway is another repo): the own-seller SQL, the own-money rules and the
variant id rules are COPIES of PIVOTA-Agent 04f819f4a (src/services/canonicalOwnOfferSellerSql.js,
canonicalPdpOwnMoney.js, ucpLiveOfferRead.js numericVariantId, externalSeedProducts.js
shouldTreatAsMinorUnitPrice). tests/test_census_pdp_live_price_drift.py pins the pure rules.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

if os.path.isdir("/app") and "/app" not in sys.path:
    sys.path.insert(0, "/app")

CENSUS_MARKER = "pdp_live_price_drift_census "
STATEMENT_TIMEOUT = "30s"
DEFAULT_HOSTS = ("ichibanm.com", "dodoskin.com", "heimish.us", "tocobo.us", "fentybeauty.com", "mixsoon.us")
DEFAULT_STALE_DAYS = 7.0
DEFAULT_MAX_PRODUCTS_PER_HOST = 600
# The seed variant list the gateway keeps (server.js limitedVariantsSql: ord <= 80).
MAX_SEED_VARIANTS = 80
# The variant lines printed on a product line.
SAMPLE_VARIANTS = 4
ENRICHMENT_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"
EXTERNAL_SEED_PLATFORM = "external_seed"
MARKET_CURRENCY = {"US": "USD", "SG": "SGD", "AU": "AUD", "JP": "JPY", "KR": "KRW", "GB": "GBP", "CA": "CAD"}
# COPY: externalSeedProducts.js ZERO_DECIMAL_PRICE_CURRENCIES and PRICE_MINOR_UNIT_HINT_RE.
ZERO_DECIMAL_PRICE_CURRENCIES = frozenset((
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF",
    "XPF",
))
PRICE_MINOR_UNIT_HINT_RE = re.compile(
    r"\b(fragrance|perfume|parfum|cologne|sunscreen|sun\s*screen|spf|broad\s+spectrum|sun\s+(?:serum|fluid|"
    r"cream|gel|milk|stick)|uv\s*(?:protection|shield|defen[cs]e|lock)|shampoo|conditioner|cleanser|toner|"
    r"moisturizer|cream|serum|concealer|foundation|powder|mascara|lip|brow|hair|beauty|bundle|treatment|"
    r"spot[-\s]?target(?:ing|ed)?|blemish|acne|salicylic|bha|aha|clarifying)\b",
    re.IGNORECASE,
)

# COPY of PIVOTA-Agent src/services/canonicalOwnOfferSellerSql.js (currency USD), aliases co / own_cp.
OWN_SELLER_SQL = """(
    co.merchant_id = own_cp.merchant_id
    OR (
      own_cp.platform = 'external_seed'
      AND own_cp.source_system = 'catalog_enrichment_agent_v1'
      AND co.source_system = 'catalog_enrichment_agent_v1'
      AND co.offer_type = 'brand_direct' AND co.is_first_party IS TRUE
      AND co.offer_mode = 'external_referral' AND co.catalog_track = 'external_referral'
      AND co.truth_tier = 'primary' AND co.readiness_tier = 'referral_only'
      AND lower(own_cp.brand) ~ '[a-z0-9]'
      AND own_cp.source_domain IS NOT NULL
      AND own_cp.canonical_url ~ '^https://[a-z0-9.-]+/[^[:space:]?#]+$'
      AND substring(own_cp.canonical_url from '^https://([^/]+)') = own_cp.source_domain
      AND co.source_domain = own_cp.source_domain
      AND co.source_ref = own_cp.canonical_url
      AND co.offer_payload->>'destination_url' = own_cp.canonical_url
      AND co.offer_payload->>'canonical_url' = own_cp.canonical_url
      AND EXISTS (
        SELECT 1 FROM catalog_merchants listing_merchant
        WHERE listing_merchant.merchant_id = co.merchant_id
          AND listing_merchant.source_system = 'catalog_enrichment_agent_v1'
          AND listing_merchant.status = 'active' AND listing_merchant.indexable IS TRUE
          AND regexp_replace(lower(listing_merchant.source_ref), '^www[.]', '')
              = regexp_replace(lower(own_cp.source_domain), '^www[.]', '')
          AND regexp_replace(lower(listing_merchant.metadata_json->>'domain'), '^www[.]', '')
              = regexp_replace(lower(own_cp.source_domain), '^www[.]', '')
          AND co.merchant_id = 'agent_seed::' || left(trim(both '-' from
            regexp_replace(lower(own_cp.brand), '[^a-z0-9]+', '-', 'g')), 80)
      )
      AND EXISTS (
        SELECT 1 FROM catalog_skus listing_sku
        WHERE listing_sku.sku_key = co.sku_key
          AND listing_sku.product_key = own_cp.product_key
          AND listing_sku.merchant_id = own_cp.merchant_id
          AND listing_sku.currency = 'USD'
          AND listing_sku.suppressed_at IS NULL AND listing_sku.suppression_reason IS NULL
      )
    )
  )"""

_PRODUCT_COLUMNS = """
SELECT cp.product_key, cp.merchant_id, cp.platform, cp.source_system, cp.source_product_id,
       cp.pivota_signature_id,
       coalesce(regexp_replace(lower(cp.source_domain), '^www[.]', ''), '(no source_domain)') AS host,
       cp.canonical_url, cp.title, cp.sync_status, cp.suppression_reason,
       (cp.suppressed_at IS NOT NULL) AS product_suppressed,
       t.serving_decision, cp.updated_at
FROM catalog_products cp
LEFT JOIN catalog_row_trust t ON t.subject_type = 'product' AND t.subject_key = cp.product_key"""
# The signature rows (PDP-reachable products) of the hosts. No source_domain index: one pass.
PRODUCTS_SQL = _PRODUCT_COLUMNS + """
WHERE regexp_replace(lower(cp.source_domain), '^www[.]', '') = ANY($1::text[])
  AND cp.pivota_signature_id IS NOT NULL
"""
# --signatures: the rows of the named PDPs (indexed on pivota_signature_id), whatever domain they carry --
# a connected merchant's rows can carry none.
PRODUCTS_BY_SIGNATURE_SQL = _PRODUCT_COLUMNS + """
WHERE cp.pivota_signature_id = ANY($1::text[])
"""

# Every offer of those products, with each own-money gate as its own column.
OFFERS_SQL = f"""
SELECT co.offer_id, co.product_key, co.sku_key, co.merchant_id, co.source_system,
       co.market, co.currency, co.availability,
       co.list_price, co.merchant_effective_price, co.estimated_best_price,
       co.price_checked_at, co.updated_at,
       (co.suppressed_at IS NOT NULL OR co.suppression_reason IS NOT NULL) AS offer_suppressed,
       co.offer_payload->>'price_read_source' AS price_read_source,
       co.offer_payload->>'declared_market' AS declared_market,
       s.source_variant_id, s.currency AS sku_currency,
       (s.sku_key IS NOT NULL) AS sku_joined,
       (s.suppressed_at IS NOT NULL OR s.suppression_reason IS NOT NULL) AS sku_suppressed,
       {OWN_SELLER_SQL} AS own_seller
FROM catalog_offers co
JOIN catalog_products own_cp ON own_cp.product_key = co.product_key
LEFT JOIN catalog_skus s ON s.sku_key = co.sku_key
  AND s.product_key = own_cp.product_key AND s.merchant_id = own_cp.merchant_id
WHERE co.product_key = ANY($1::text[])
"""


def _variant_list_sql(path: str) -> str:
    """The price-bearing keys of a seed variant list, the gateway's coalesce order (server.js
    limitedVariantsSql), plus every compare-at key a variant could carry."""
    return f"""(
      SELECT coalesce(jsonb_agg(jsonb_build_object(
          'variant_id', coalesce(v.value->>'variant_id', v.value->>'id'),
          'sku', coalesce(v.value->>'sku', v.value->>'sku_id', v.value->>'variant_sku'),
          'title', v.value->>'title',
          'price', coalesce(v.value->'price', v.value->'price_amount', v.value->'pricing'->'current'->'amount'),
          'currency', coalesce(v.value->>'currency', v.value->>'price_currency',
                               v.value->'pricing'->'current'->>'currency'),
          'compare_at', coalesce(v.value->'compare_at_price', v.value->'compare_at', v.value->'compareAtPrice',
                                 v.value->'original_price', v.value->'pricing'->'compare_at'->'amount',
                                 v.value->'price'->'compare_at'->'amount'),
          'list_price', v.value->'list_price'
        ) ORDER BY v.ord), '[]'::jsonb)
      FROM jsonb_array_elements(CASE WHEN jsonb_typeof({path}) = 'array' THEN {path} ELSE '[]'::jsonb END)
        WITH ORDINALITY AS v(value, ord)
      WHERE v.ord <= {MAX_SEED_VARIANTS}
    )"""


# Active seeds of those products by the gateway's two routes. `j.sd` detoasts seed_data once per seed
# (OFFSET 0 keeps the subquery from being inlined into every reference).
SEEDS_SQL = f"""
SELECT eps.id, eps.external_product_id, eps.attached_product_key, eps.market, eps.domain,
       eps.price_amount, eps.price_currency, eps.updated_at, eps.created_at,
       eps.last_crawled_at,
       j.sd->'snapshot'->>'price_amount' AS snapshot_price_amount,
       j.sd->'snapshot'->>'price_currency' AS snapshot_price_currency,
       coalesce(j.sd->'snapshot'->>'refreshed_at', j.sd->'snapshot'->>'extracted_at') AS snapshot_at,
       j.sd->'snapshot'->'variant_refresh'->>'status' AS variant_refresh_status,
       coalesce(j.sd->'snapshot'->'compare_at_price', j.sd->'compare_at_price') AS seed_compare_at,
       {_variant_list_sql("j.sd->'snapshot'->'variants'")} AS snapshot_variants,
       {_variant_list_sql("j.sd->'variants'")} AS variants
FROM external_product_seeds eps
CROSS JOIN LATERAL (SELECT coalesce(eps.seed_data, '{{}}'::jsonb) || '{{}}'::jsonb AS sd OFFSET 0) j
WHERE eps.status = 'active'
  AND (eps.external_product_id = ANY($1::text[]) OR eps.attached_product_key = ANY($2::text[]))
"""

# A connected merchant's PDP is built from its products_cache row (gateway server.js ~5240: merchant_id +
# platform_product_id, newest cached_at; the gateway also reads an expired row as a stale fallback).
# product_data is JSON: cast once per row.
CACHE_SQL = f"""
SELECT pc.merchant_id, pc.platform_product_id, pc.cached_at, pc.expires_at,
       coalesce(d.pd->>'currency', d.pd->'price'->>'currency') AS currency,
       {_variant_list_sql("d.pd->'variants'")} AS variants
FROM products_cache pc
CROSS JOIN LATERAL (SELECT pc.product_data::jsonb AS pd OFFSET 0) d
WHERE (pc.merchant_id, pc.platform_product_id) IN (
  SELECT m, p FROM unnest($1::text[], $2::text[]) AS w(m, p))
"""

READ_STATEMENTS = (PRODUCTS_SQL, OFFERS_SQL, SEEDS_SQL, CACHE_SQL)
ALL_READ_STATEMENTS = READ_STATEMENTS + (PRODUCTS_BY_SIGNATURE_SQL,)


# ---------------------------------------------------------------------------------------------- copies


def numeric_variant_id(raw: Any) -> Optional[str]:
    """COPY of ucpLiveOfferRead.js numericVariantId + urlNamedVariantId: the Shopify variant number the
    live reader asks for, or None (the reader skips the variant: never read, never counted)."""
    s = str(raw if raw is not None else "").strip()
    m = re.fullmatch(r"(?:gid://shopify/ProductVariant/)?(\d{6,20})", s)
    if m:
        return m.group(1)
    if "variant=" not in s or re.search(r"[\x00-\x1f\x7f]", s):
        return None
    try:
        params = parse_qs(urlsplit(s if "://" in s else "https://variant.invalid/" + s.lstrip("/")).query,
                          keep_blank_values=True)
    except ValueError:
        return None
    if "selling_plan" in params:
        return None
    values = params.get("variant") or []
    if not values or any(not re.fullmatch(r"[0-9]+", v) for v in values):
        return None
    distinct = set(values)
    only = next(iter(distinct))
    return only if len(distinct) == 1 and re.fullmatch(r"\d{6,20}", only) else None


def stored_numeric_variant(raw: Any, product_key: str) -> Optional[str]:
    """COPY of canonicalPdpOwnMoney.js storedNumericVariant: the numeric alias an own row's
    source_variant_id also answers to."""
    s = raw.strip() if isinstance(raw, str) else ""
    if re.fullmatch(r"[0-9]{1,20}", s):
        return s
    m = re.fullmatch(r"gid://shopify/ProductVariant/([0-9]{1,20})", s)
    if m:
        return m.group(1)
    m = re.fullmatch(r"(ext_[A-Za-z0-9]+):([0-9]{1,20})", s)
    return m.group(2) if m and m.group(1) == product_key.split("::")[-1] else None


def normalize_amount(raw: Any) -> float:
    """COPY of externalSeedProducts.js normalizeAmount (strips every non [0-9.-] char)."""
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else 0.0
    if isinstance(raw, str):
        try:
            value = float(re.sub(r"[^0-9.\-]+", "", raw))
        except ValueError:
            return 0.0
        return value if math.isfinite(value) else 0.0
    if isinstance(raw, dict):
        for key in ("amount", "current", "price_amount", "value"):
            if key in raw and raw[key] is not None:
                inner = raw[key]["amount"] if key == "current" and isinstance(raw[key], dict) else raw[key]
                return normalize_amount(inner)
    return 0.0


def minor_unit_rule_fires(raw: Any, amount: float, currency: Optional[str], surface_text: str) -> bool:
    """COPY of externalSeedProducts.js shouldTreatAsMinorUnitPrice (the census's surface text is the
    product title, variant title and URLs; the gateway also reads category and description)."""
    if not math.isfinite(amount) or amount < 1000:
        return False
    if str(currency or "").strip().upper() in ZERO_DECIMAL_PRICE_CURRENCIES:
        return False
    text = str(raw if raw is not None else "").strip()
    looks_minor = (
        (isinstance(raw, dict) and float(amount).is_integer())
        or (isinstance(raw, (int, float)) and not isinstance(raw, bool) and float(raw).is_integer())
        or bool(re.fullmatch(r"\d+(?:\.0+)?", text))
    )
    if not looks_minor or not PRICE_MINOR_UNIT_HINT_RE.search(surface_text or ""):
        return False
    return amount / 100 <= 1000


# ---------------------------------------------------------------------------------------------- rules

CANONICAL_PRODUCT = "<product-grain>"
# The own-money gates, in the order a row is refused (first failing gate names the refusal).
OFFER_GATES = (
    "not_own_seller", "market_not_us", "currency_not_usd", "not_in_stock", "offer_suppressed",
    "no_positive_amount", "no_sku_row", "sku_currency_not_usd", "sku_suppressed",
)


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def own_amount(offer: Dict[str, Any]) -> Optional[float]:
    """coalesce(merchant_effective_price, list_price): the amount readCanonicalOwnMoney serves."""
    mep = _num(offer.get("merchant_effective_price"))
    return mep if mep is not None else _num(offer.get("list_price"))


def product_gate(product: Dict[str, Any]) -> Optional[str]:
    """Why readCanonicalOwnMoney cannot use the listing at all, or None."""
    if product.get("platform") != EXTERNAL_SEED_PLATFORM or product.get("source_system") != ENRICHMENT_SOURCE_SYSTEM:
        return "not_own_money_lane"
    if product.get("serving_decision") != "public":
        return "trust_not_public"
    if product.get("sync_status") != "live":
        return "product_not_live"
    if product.get("suppression_reason") is not None:
        return "product_suppressed"
    return None


def offer_gate(offer: Dict[str, Any]) -> Optional[str]:
    """The first own-money gate an offer row fails, or None (eligible)."""
    amount = own_amount(offer)
    checks = (
        ("not_own_seller", offer.get("own_seller") is True),
        ("market_not_us", offer.get("market") == "US"),
        ("currency_not_usd", offer.get("currency") == "USD"),
        ("not_in_stock", offer.get("availability") == "in_stock"),
        ("offer_suppressed", not offer.get("offer_suppressed")),
        ("no_positive_amount", amount is not None and amount > 0),
        ("no_sku_row", bool(offer.get("sku_joined"))),
        ("sku_currency_not_usd", offer.get("sku_currency") == "USD"),
        ("sku_suppressed", not offer.get("sku_suppressed")),
    )
    for name, ok in checks:
        if not ok:
            return name
    return None


def money_from_own_rows(rows: List[Dict[str, Any]], product_key: str) -> Tuple[Dict[str, float], Optional[str]]:
    """COPY of canonicalPdpOwnMoney.js moneyFromOwnRows: (variant key -> amount, None) or ({}, reason)."""
    money: Dict[str, Tuple[float, int]] = {}
    for row in sorted(rows, key=lambda r: (str(r.get("source_variant_id") or ""), str(r.get("offer_id") or ""))):
        vid = row["source_variant_id"].strip() if isinstance(row.get("source_variant_id"), str) else ""
        if not vid:
            continue
        placeholder = vid == product_key and row.get("sku_key") == f"{product_key}::canonical"
        numeric = stored_numeric_variant(vid, product_key)
        keys = [CANONICAL_PRODUCT] if placeholder else [vid] + ([numeric] if numeric else [])
        amount = own_amount(row) or 0.0
        minor = round(amount * 100)
        if row.get("currency") != "USD" or minor <= 0 or abs(amount * 100 - minor) > 1e-6:
            return {}, "malformed_money"
        for key in keys:
            previous = money.get(key)
            if previous and previous[1] != minor:
                return {}, "conflicting_prices"
            money[key] = (amount, minor)
    if not money:
        return {}, "no_eligible_rows"
    return {k: v[0] for k, v in money.items()}, None


def pick_seed(product: Dict[str, Any], seeds_by_epid: Dict[str, List[Dict[str, Any]]],
              seeds_by_attached: Dict[str, List[Dict[str, Any]]]) -> Tuple[Optional[Dict[str, Any]], str]:
    """The seed the page is built from: lane 0 by external_product_id, else lane 1 by attachment;
    newest updated_at, then created_at, then id (the gateway's ORDER BY)."""
    def newest(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        floor = datetime.min.replace(tzinfo=timezone.utc)
        ordered = sorted(rows, key=lambda r: str(r.get("id") or ""))
        ordered.sort(key=lambda r: r.get("created_at") or floor, reverse=True)
        ordered.sort(key=lambda r: r.get("updated_at") or floor, reverse=True)
        return ordered[0]

    lane0 = seeds_by_epid.get(str(product.get("source_product_id") or ""))
    if lane0:
        return newest(lane0), "external_product_id"
    lane1 = seeds_by_attached.get(str(product.get("product_key") or ""))
    if lane1:
        return newest(lane1), "attached_product_key"
    return None, "none"


def page_variants(seed: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """snapshot.variants when non-empty, else variants: what the page renders."""
    snap = _json_list(seed.get("snapshot_variants"))
    if snap:
        return snap, "snapshot"
    return _json_list(seed.get("variants")), "variants"


def _json_list(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def variant_page_id(variant: Dict[str, Any], product_id: str, index: int) -> str:
    """normalizeSeedVariants: variant_id | id | sku; buildVariants' positional fallback."""
    for key in ("variant_id", "sku"):
        value = str(variant.get(key) or "").strip()
        if value:
            return value
    return f"{product_id}-{index + 1}"


def classify_product(product: Dict[str, Any], offers: List[Dict[str, Any]], seed: Optional[Dict[str, Any]],
                     seed_route: str, *, now: datetime, stale_days: float,
                     cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One product's verdict: what the page stores per variant and which variants the live read would
    count as changed whatever the merchant answers."""
    product_key = str(product["product_key"])
    product_id = str(product.get("source_product_id") or "")
    lane = ("enrichment" if product.get("source_system") == ENRICHMENT_SOURCE_SYSTEM
            else str(product.get("source_system") or "unknown"))
    out: Dict[str, Any] = {
        "kind": "product", "host": product.get("host"), "sig": product.get("pivota_signature_id"),
        "product_key": product_key, "lane": lane, "serving_decision": product.get("serving_decision"),
        "seed_route": seed_route, "offers": len(offers),
    }

    # --- the own-money path (enrichment lane, US)
    own_lane = product.get("platform") == EXTERNAL_SEED_PLATFORM and product.get("source_system") == ENRICHMENT_SOURCE_SYSTEM
    gate = product_gate(product)
    refusals = Counter()
    eligible: List[Dict[str, Any]] = []
    for offer in offers:
        reason = offer_gate(offer)
        if reason:
            refusals[reason] += 1
        else:
            eligible.append(offer)
    money: Dict[str, float] = {}
    money_gap: Optional[str] = None
    if own_lane:
        if gate:
            money_gap = gate
        else:
            money, money_gap = money_from_own_rows(eligible, product_key)
    out["own_money_lane"] = own_lane
    out["own_offer_refusals"] = dict(sorted(refusals.items()))
    out["own_money_rows"] = len(eligible)

    # --- the page's variants
    if seed:
        variants, variant_source = page_variants(seed)
    elif cache:
        # A connected merchant's page: the cached StandardProduct's variants, priced as stored.
        variants, variant_source = _json_list(cache.get("variants")), "products_cache"
    else:
        variants, variant_source = [], "no_seed"
    out["variant_source"] = variant_source
    out["variants"] = len(variants)
    title = str(product.get("title") or "")
    urls = " ".join(str(x or "") for x in (product.get("canonical_url"),))
    seed_currency = str((seed or {}).get("price_currency") or (cache or {}).get("currency") or "").upper() or None
    ids = [variant_page_id(v, product_id, i) for i, v in enumerate(variants)]
    product_grain = (not variants) or (len(variants) == 1 and ids[0] in (product_id, f"{product_id}::canonical"))

    page_unavailable = False
    if own_lane:
        if money_gap:
            page_unavailable = True
        else:
            if product_grain:
                selected_ok = CANONICAL_PRODUCT in money
            else:
                # pickDefaultVariant: (no line option / explicit default in a seed page) the variant whose price
                # equals the product price, else the first.
                product_price = _num((seed or {}).get("price_amount"))
                default = next((ids[i] for i, v in enumerate(variants)
                                if product_price and abs(normalize_amount(v.get("price")) - product_price) < 0.01), ids[0] if ids else None)
                selected_ok = default in money
            if not selected_ok:
                page_unavailable, money_gap = True, "default_variant_without_own_money"
    out["own_money_gap"] = money_gap if own_lane else None
    out["page_unavailable"] = page_unavailable

    # --- per variant
    by_vid = defaultdict(list)
    for offer in offers:
        if isinstance(offer.get("source_variant_id"), str):
            by_vid[offer["source_variant_id"].strip()].append(offer)
    seed_variants_by_id = {variant_page_id(v, product_id, i): v for i, v in enumerate(_json_list((seed or {}).get("variants")))}
    counts = Counter()
    sample = []
    for i, variant in enumerate(variants):
        vid = ids[i]
        readable = numeric_variant_id(vid) is not None
        raw_price = _json_value(variant.get("price"))
        seed_amount = normalize_amount(raw_price)
        currency = str(variant.get("currency") or seed_currency or "").upper() or None
        divides = seed is not None and minor_unit_rule_fires(
            raw_price, seed_amount, currency, " ".join((title, str(variant.get("title") or ""), urls)))
        seed_page_amount = seed_amount / 100 if divides else seed_amount
        if own_lane:
            stored = None if page_unavailable else (money.get(CANONICAL_PRODUCT) if product_grain else money.get(vid))
        else:
            stored = seed_page_amount if seed_page_amount > 0 and currency else None
        compare_at = normalize_amount(_json_value(variant.get("compare_at"))) or None
        counts["readable"] += readable
        counts["readable_unpriced"] += readable and stored is None
        counts["readable_priced"] += readable and stored is not None
        counts["minor_unit_divided"] += divides
        counts["compare_at_present"] += compare_at is not None
        if stored is not None and compare_at is not None and abs(stored - compare_at) < 0.005:
            counts["stored_equals_compare_at"] += 1
        if stored is not None and own_lane and seed_page_amount > 0 and abs(stored - seed_page_amount) >= 0.005:
            counts["own_offer_differs_from_seed_variant"] += 1
        if variant_source == "snapshot":
            fresh = seed_variants_by_id.get(vid)
            if fresh is not None:
                fresh_amount = normalize_amount(_json_value(fresh.get("price")))
                if fresh_amount > 0 and abs(fresh_amount - seed_amount) >= 0.005:
                    counts["snapshot_shadows_different_variants_price"] += 1
        if currency and currency != MARKET_CURRENCY.get(str((seed or {}).get("market") or "US").upper()):
            counts["variant_currency_not_market"] += 1
        if len(sample) < SAMPLE_VARIANTS:
            rows = by_vid.get(vid) or []
            checked = max((r["price_checked_at"] for r in rows if r.get("price_checked_at")), default=None)
            sample.append({
                "id": vid[:80], "readable": readable, "stored": stored, "seed_price": seed_page_amount or None,
                "currency": currency, "compare_at": compare_at,
                "offer_amount": own_amount(rows[0]) if rows else None,
                "price_checked_age_d": _age_days(checked, now),
            })
    # A one-variant page whose stored variant price is not the seed column the refresh re-read: the
    # refresh reconciled seed_data.variants (whenever that key is a list, even an empty one), the page
    # reads snapshot.variants first (routes/employee_products._refresh_external_seed_by_id ~6249).
    column = _num((seed or {}).get("price_amount"))
    if (not own_lane and len(variants) == 1 and sample and sample[0]["stored"] is not None
            and column is not None and abs(sample[0]["stored"] - column) >= 0.005):
        counts["single_variant_price_differs_from_seed_column"] += 1
    out["seed_variants_len"] = len(_json_list((seed or {}).get("variants"))) if seed else None
    out["variant_counts"] = dict(sorted(counts.items()))
    out["sample"] = sample

    # --- offer flags (every offer of the product)
    flags = Counter()
    for offer in offers:
        currency = str(offer.get("currency") or "").upper()
        market = str(offer.get("market") or "").upper()
        if currency != MARKET_CURRENCY.get(market):
            flags["currency_not_market"] += 1
        age = _age_days(offer.get("price_checked_at"), now)
        if age is None:
            flags["price_checked_at_null"] += 1
        elif age > stale_days:
            flags["price_checked_at_stale"] += 1
        mep, lp = _num(offer.get("merchant_effective_price")), _num(offer.get("list_price"))
        if mep is not None and lp is not None and abs(mep - lp) >= 0.005:
            flags["mep_differs_from_list_price"] += 1
        if mep is None and lp is not None:
            flags["mep_null_serves_list_price"] += 1
        flags[f"source:{offer.get('source_system')}"] += 1
    out["offer_flags"] = dict(sorted(flags.items()))
    if seed:
        out["seed"] = {
            "market": seed.get("market"), "currency": seed.get("price_currency"),
            "price_amount": _num(seed.get("price_amount")),
            "snapshot_price_amount": seed.get("snapshot_price_amount"),
            "snapshot_currency": seed.get("snapshot_price_currency"),
            "variant_refresh_status": seed.get("variant_refresh_status"),
            "updated_age_d": _age_days(seed.get("updated_at"), now),
            "last_crawled_age_d": _age_days(seed.get("last_crawled_at"), now),
            "compare_at": _json_value(seed.get("seed_compare_at")),
        }
    if cache:
        out["cache"] = {"cached_age_d": _age_days(cache.get("cached_at"), now),
                        "expired": bool(cache.get("expires_at") and _age_days(cache["expires_at"], now) > 0),
                        "currency": cache.get("currency")}
    out["verdict"] = verdict(out)
    return out


def verdict(line: Dict[str, Any]) -> str:
    """The first explanation that holds, in the order the comparator meets them."""
    counts = line.get("variant_counts") or {}
    if not counts.get("readable"):
        return "not_live_readable"
    if line.get("page_unavailable"):
        return "page_unpriced_counted_as_changed"
    if counts.get("readable_unpriced"):
        return "some_variants_unpriced_counted_as_changed"
    if counts.get("stored_equals_compare_at"):
        return "stored_is_compare_at"
    if counts.get("minor_unit_divided"):
        return "minor_unit_division"
    if counts.get("snapshot_shadows_different_variants_price") or counts.get(
            "single_variant_price_differs_from_seed_column"):
        return "stale_snapshot_variant_price"
    flags = line.get("offer_flags") or {}
    if line.get("variant_source") == "products_cache":
        return "priced_from_expired_cache" if (line.get("cache") or {}).get("expired") else "priced_from_merchant_cache"
    if line.get("own_money_lane") and (flags.get("price_checked_at_null") or flags.get("price_checked_at_stale")):
        return "priced_from_stale_or_unread_offer"
    if not line.get("own_money_lane") and (line.get("seed") or {}).get("last_crawled_age_d") is not None \
            and (line["seed"]["last_crawled_age_d"] or 0) > DEFAULT_STALE_DAYS:
        return "priced_from_stale_seed"
    return "priced_recent"


def _age_days(value: Any, now: datetime) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return round((now - value).total_seconds() / 86400.0, 2)
    return None


def host_lines(products: List[Dict[str, Any]], hosts: Iterable[str]) -> List[Dict[str, Any]]:
    by_host = defaultdict(list)
    for line in products:
        by_host[line["host"]].append(line)
    out = []
    for host in hosts:
        rows = by_host.get(host, [])
        agg = {"kind": "host", "host": host, "products": len(rows),
               "signatures": len({r["sig"] for r in rows}),
               "lanes": dict(Counter(r["lane"] for r in rows)),
               "verdicts": dict(Counter(r["verdict"] for r in rows).most_common()),
               "own_money_gaps": dict(Counter(r["own_money_gap"] for r in rows if r.get("own_money_gap")).most_common()),
               "variant_source": dict(Counter(r["variant_source"] for r in rows)),
               "seed_route": dict(Counter(r["seed_route"] for r in rows))}
        for key in ("variant_counts", "offer_flags", "own_offer_refusals"):
            total = Counter()
            for r in rows:
                total.update(r.get(key) or {})
            agg[key] = dict(sorted(total.items()))
        agg["offers"] = sum(r["offers"] for r in rows)
        agg["variants"] = sum(r["variants"] for r in rows)
        out.append(agg)
    return out


async def census(conn: Any, hosts: List[str], *, stale_days: float = DEFAULT_STALE_DAYS,
                 max_products_per_host: int = DEFAULT_MAX_PRODUCTS_PER_HOST,
                 signatures: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Run the census on an asyncpg connection, inside its own READ ONLY transaction. With `signatures`,
    the products are those PDPs' rows (whatever domain they carry) instead of the hosts'."""
    async with conn.transaction(readonly=True, isolation="repeatable_read"):
        await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        now = await conn.fetchval("SELECT now()")
        if signatures:
            products = [dict(r) for r in await conn.fetch(PRODUCTS_BY_SIGNATURE_SQL, signatures)]
        else:
            products = [dict(r) for r in await conn.fetch(PRODUCTS_SQL, hosts)]
        keys = sorted({p["product_key"] for p in products})
        epids = sorted({str(p["source_product_id"]) for p in products if p.get("source_product_id")})
        pairs = sorted({(str(p["merchant_id"]), str(p["source_product_id"])) for p in products
                        if p.get("platform") != EXTERNAL_SEED_PLATFORM and p.get("source_product_id")})
        offers = [dict(r) for r in await conn.fetch(OFFERS_SQL, keys)] if keys else []
        seeds = [dict(r) for r in await conn.fetch(SEEDS_SQL, epids, keys)] if keys else []
        caches = ([dict(r) for r in await conn.fetch(CACHE_SQL, [m for m, _ in pairs], [p for _, p in pairs])]
                  if pairs else [])
    offers_by_key = defaultdict(list)
    for offer in offers:
        offers_by_key[offer["product_key"]].append(offer)
    by_epid, by_attached = defaultdict(list), defaultdict(list)
    for seed in seeds:
        if seed.get("external_product_id"):
            by_epid[str(seed["external_product_id"])].append(seed)
        if seed.get("attached_product_key"):
            by_attached[str(seed["attached_product_key"])].append(seed)
    newest_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
    floor = datetime.min
    for row in sorted(caches, key=lambda r: r.get("cached_at") or floor, reverse=True):
        newest_cache.setdefault((str(row["merchant_id"]), str(row["platform_product_id"])), row)
    lines = []
    for product in sorted(products, key=lambda p: (p["host"], str(p["pivota_signature_id"]), p["product_key"])):
        seed, route = pick_seed(product, by_epid, by_attached)
        cache = newest_cache.get((str(product["merchant_id"]), str(product.get("source_product_id") or "")))
        lines.append(classify_product(product, offers_by_key.get(product["product_key"], []), seed, route,
                                      now=now, stale_days=stale_days, cache=cache))
    if signatures:
        hosts = sorted({line["host"] for line in lines})
    params = {"kind": "params", "hosts": hosts, "signatures": len(signatures or []), "now": now,
              "stale_days": stale_days,
              "rows": {"products": len(products), "offers": len(offers), "seeds": len(seeds), "caches": len(caches)}}
    printed: List[Dict[str, Any]] = []
    per_host = Counter()
    for line in lines:
        per_host[line["host"]] += 1
        if per_host[line["host"]] <= max_products_per_host:
            printed.append(line)
    return [params] + host_lines(lines, hosts) + printed


def render(lines: List[Dict[str, Any]], *, hosts_only: bool = False) -> List[str]:
    return [CENSUS_MARKER + json.dumps(line, default=str, sort_keys=True) for line in lines
            if not (hosts_only and line["kind"] == "product")]


def parse_hosts(raw: str) -> List[str]:
    hosts = []
    for part in raw.split(","):
        host = re.sub(r"^www[.]", "", part.strip().lower())
        if host and re.fullmatch(r"[a-z0-9.-]+[.][a-z]{2,}", host) and host not in hosts:
            hosts.append(host)
    return hosts


def parse_signatures(raw: str) -> List[str]:
    out = []
    for part in raw.split(","):
        sig = part.strip().lower()
        if re.fullmatch(r"sig_[a-z0-9]{8,64}", sig) and sig not in out:
            out.append(sig)
    return out


async def _main(argv: List[str]) -> int:
    import asyncpg

    parser = argparse.ArgumentParser(description="read-only census of what the PDP live read compares")
    parser.add_argument("--hosts", default=",".join(DEFAULT_HOSTS), help="comma-separated merchant hosts")
    parser.add_argument("--stale-days", type=float, default=DEFAULT_STALE_DAYS)
    parser.add_argument("--max-products-per-host", type=int, default=DEFAULT_MAX_PRODUCTS_PER_HOST)
    parser.add_argument("--signatures", default="",
                        help="comma-separated sig_ ids: census those PDPs' rows instead of the hosts'")
    parser.add_argument("--hosts-only", action="store_true", help="print the params and host lines only")
    args = parser.parse_args(argv)
    hosts = parse_hosts(args.hosts)
    if not hosts:
        parser.error("--hosts names no valid host")
    signatures = parse_signatures(args.signatures)
    if args.signatures and not signatures:
        parser.error("--signatures names no valid sig_ id")
    url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url.startswith("postgres"):
        print(CENSUS_MARKER + json.dumps({"kind": "error", "error": "DATABASE_URL must be Postgres"}))
        return 2
    conn = await asyncpg.connect(url)
    try:
        lines = await census(conn, hosts, stale_days=args.stale_days,
                             max_products_per_host=args.max_products_per_host, signatures=signatures or None)
    finally:
        await conn.close()
    for text in render(lines, hosts_only=args.hosts_only):
        print(text, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
