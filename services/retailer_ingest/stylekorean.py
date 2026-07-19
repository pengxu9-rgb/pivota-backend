"""StyleKorean adapter — a multi-brand K-beauty retailer (stylekorean.com).

StyleKorean is a Next.js storefront whose Global site serves **USD** prices and
US-buyable checkout, exposes a clean sitemap set (sitemap-products-*.xml,
sitemap-brands.xml) and per-PDP schema.org `Product` JSON-LD. robots.txt allows
/product/* (only /adm/, /plugin/ are disallowed) and does not block PivotaBot.

Per ADR-001 (canonical record vs supplier): StyleKorean is a SUPPLIER. Its
listing is an OFFER (retailer redirect, `offer_type='retailer'`,
`is_first_party=False`); the identity/content anchor is the brand-direct record.
So the intake door attaches a StyleKorean offer to a matched canonical, and for
new-to-us SKUs flags them for canonical ingest + brand-official enrichment —
NOT treating StyleKorean copy as the canonical content.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from services.retailer_ingest.sitemap_crawler import fetch_text, sitemap_locs

BASE = "https://www.stylekorean.com"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"
MERCHANT_ID = "stylekorean_global"
MERCHANT_NAME = "StyleKorean"
SOURCE_DOMAIN = "stylekorean.com"
MARKET = "US"

# /product/{slug}/{numeric_id} — slug begins with the brand slug.
_PRODUCT_PATH_RE = re.compile(r"/product/([a-z0-9-]+)/(\d+)/?$", re.I)


def product_sitemap_urls(index_xml: Optional[str] = None) -> List[str]:
    """The product sub-sitemaps listed in the sitemap index."""
    xml = index_xml if index_xml is not None else fetch_text(SITEMAP_INDEX)
    return [u for u in sitemap_locs(xml) if "sitemap-products" in u]


def _slug_matches_brand(product_slug: str, brand_slug: str) -> bool:
    # brand slug is the leading path segment of the product slug, boundary-safe:
    # 'cosrx-...' matches brand 'cosrx' but 'cosrx' does not match 'cos'.
    return product_slug == brand_slug or product_slug.startswith(brand_slug + "-")


def brand_product_urls(brand_slug: str, product_sitemap_texts: List[str]) -> List[str]:
    """Filter product sitemaps to a single brand's PDP URLs (deduped, ordered)."""
    seen: set[str] = set()
    out: List[str] = []
    for xml in product_sitemap_texts:
        for loc in sitemap_locs(xml):
            m = _PRODUCT_PATH_RE.search(loc)
            if not m:
                continue
            if _slug_matches_brand(m.group(1).lower(), brand_slug.lower()) and loc not in seen:
                seen.add(loc)
                out.append(loc)
    return out


def to_validated_record(crawled: Dict[str, Any]) -> Dict[str, Any]:
    """Map a crawled product dict (from sitemap_crawler.crawl_products) into the
    `{pdp, offers}` validated-record shape the intake fork consumes.

    The offer is a retailer referral: price is the retailer's own USD price from
    the page's JSON-LD (never invented). A record with no price yields a
    destination-only offer that will not surface until priced."""
    price_raw = crawled.get("price_raw")
    try:
        price = float(price_raw) if price_raw not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    # Optional review + ingredient signals off the PDP. Both stay null when the
    # page didn't expose them (never invented). rating_value/rating_count feed the
    # catalog_products review columns; raw_inci feeds beauty_sku_ingredients at
    # ingest. StyleKorean is a SUPPLIER (ADR-001), so its INCI is tagged
    # reseller-tier — it can seed an empty canonical but never downgrades a
    # brand-official/supplier list.
    return {
        "pdp": {
            "brand": crawled.get("brand"),
            "product_name": crawled.get("title"),
            "attribute_summary": (crawled.get("description") or "")[:400],
            "image_url": crawled.get("image_url"),
            "source_domain": SOURCE_DOMAIN,
            "gtin": None,  # StyleKorean does not expose GTIN; hardened via brand-official enrich
            "rating_value": crawled.get("rating_value"),
            "rating_count": crawled.get("rating_count"),
            "raw_inci": crawled.get("raw_inci"),
            "inci_source": "reseller_listing",
        },
        "offers": [
            {
                "merchant_id": MERCHANT_ID,
                "merchant_name": MERCHANT_NAME,
                "offer_type": "retailer",
                "is_first_party": False,
                "destination_url": crawled.get("url"),
                "market": MARKET,
                "currency": crawled.get("currency"),
                "list_price": price,
                "availability": crawled.get("availability") or "unknown",
            }
        ],
    }
