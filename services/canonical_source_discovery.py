"""Canonical source discovery — find a product's authoritative INCI sources.

The resolver (services.canonical_inci_resolver) fetches+extracts INCI from
candidate sources; this DISCOVERS those candidates for a product and labels each
with its authority tier, then runs the full discover -> resolve -> ingest flow.
Closes the loop of the canonical-sourcing engine (ADR-001): a product_key in,
verified canonical INCI written (by precedence) out.

Discovery uses data already on the record -- no web search yet:
  * canonical_url, classified brand-official (its domain matches the brand) vs a
    reseller listing -- this drives the intake's source precedence, so a
    brand-official PDP outranks a reseller PDP.
  * barcode -> an Open Beauty Facts (INCI-database) lookup.
Ordered highest-authority first: brand-official PDP > INCI database > reseller
PDP. A search/crawl layer can feed richer candidates later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional
from urllib.parse import urlparse

from db.database import database
from services.canonical_inci_intake import (
    INCI_SOURCE_BRAND_OFFICIAL,
    INCI_SOURCE_RESELLER,
    INCI_SOURCE_SUPPLIER,
    ingest_canonical_inci,
)
from services.canonical_inci_resolver import (
    Fetcher,
    http_fetch,
    resolve_inci_from_openbeautyfacts,
    resolve_inci_from_urls,
)

# An INCI authority (crowdsourced DB) -- not the brand, but a real INCI source.
SOURCE_INCI_DATABASE = "inci_database"

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# Generic domain tokens that are never a brand match.
_GENERIC_DOMAINS = {
    "shopify", "myshopify", "amazon", "ebay", "walmart", "target", "sephora",
    "ulta", "iherb", "yesstyle", "stylevana", "oliveyoung", "etsy", "lazada",
    "shopee", "coupang", "rakuten", "qoo10", "wixsite", "bigcartel", "squarespace",
}
# Domain labels that are TLDs / platform suffixes, never a brand.
_TLD_LABELS = {
    "com", "co", "net", "org", "io", "store", "shop", "kr", "jp", "us", "uk",
    "eu", "cc", "app", "ai", "biz",
}


@dataclass(frozen=True)
class SourceCandidate:
    method: str   # "url" | "openbeautyfacts"
    value: str    # the URL or barcode
    source: str   # intake source label (drives precedence)


def _norm(text: Optional[str]) -> str:
    return _NON_ALNUM_RE.sub("", (text or "").lower())


def _domain_labels(url: Optional[str]) -> List[str]:
    if not url:
        return []
    host = urlparse(url if "://" in url else f"http://{url}").netloc.lower().split(":")[0]
    return [p for p in host.split(".") if p and p != "www"]


def registrable_name(url: Optional[str]) -> str:
    """The first meaningful host label (e.g. theordinary.com -> theordinary,
    anuko.myshopify.com -> anuko, brand.co.uk -> brand); empty when unparseable."""
    labels = _domain_labels(url)
    return labels[0] if labels else ""


def is_brand_official_url(brand: Optional[str], url: Optional[str]) -> bool:
    """True when the URL's domain looks like the brand's own site -- the brand
    token (>=3 chars) matches ANY non-generic, non-TLD host label (covers
    brand.com, brand.myshopify.com, shop.brand.com, brand.co.uk)."""
    brand_token = _norm(brand)
    if len(brand_token) < 3 or not url:
        return False
    for label in _domain_labels(url):
        if label in _GENERIC_DOMAINS or label in _TLD_LABELS:
            continue
        if brand_token in label or label in brand_token:
            return True
    return False


def discover_sources(
    *, brand: Optional[str], canonical_url: Optional[str], barcode: Optional[str]
) -> List[SourceCandidate]:
    """Ordered candidate sources for a product, highest authority first."""
    brand_url: Optional[str] = None
    reseller_url: Optional[str] = None
    if canonical_url:
        if is_brand_official_url(brand, canonical_url):
            brand_url = canonical_url
        else:
            reseller_url = canonical_url

    candidates: List[SourceCandidate] = []
    if brand_url:
        candidates.append(SourceCandidate("url", brand_url, INCI_SOURCE_BRAND_OFFICIAL))
    if barcode and str(barcode).strip():
        candidates.append(SourceCandidate("openbeautyfacts", str(barcode).strip(), SOURCE_INCI_DATABASE))
    if reseller_url:
        candidates.append(SourceCandidate("url", reseller_url, INCI_SOURCE_RESELLER))
    return candidates


async def source_canonical_inci(
    product_key: str,
    *,
    db: Any = database,
    fetch: Fetcher = http_fetch,
    dry_run: bool = False,
) -> dict:
    """Discover -> resolve -> ingest a product's canonical INCI. Tries candidate
    sources in authority order; the first that yields a valid INCI is ingested
    (by source precedence) and wins. Read-only-safe under dry_run."""
    row = await db.fetch_one(
        """
        SELECT cp.brand, cp.canonical_url,
               (SELECT barcode FROM catalog_skus
                 WHERE product_key = cp.product_key AND barcode IS NOT NULL LIMIT 1) AS barcode
        FROM catalog_products cp
        WHERE cp.product_key = :pk
        LIMIT 1
        """,
        {"pk": product_key},
    )
    if row is None:
        return {"product_key": product_key, "status": "not_found"}
    row = dict(row)
    candidates = discover_sources(
        brand=row.get("brand"), canonical_url=row.get("canonical_url"), barcode=row.get("barcode")
    )
    if not candidates:
        return {"product_key": product_key, "status": "no_candidates", "brand": row.get("brand")}

    for cand in candidates:
        if cand.method == "openbeautyfacts":
            resolved = await resolve_inci_from_openbeautyfacts(cand.value, fetch=fetch)
        else:
            resolved = await resolve_inci_from_urls([cand.value], fetch=fetch)
        if resolved is None:
            continue
        result = await ingest_canonical_inci(
            product_key, resolved.raw_inci, cand.source, db=db, dry_run=dry_run
        )
        result["sourced_from"] = {"method": cand.method, "value": cand.value, "source": cand.source}
        result["resolved_url"] = resolved.source_url
        return result

    return {
        "product_key": product_key,
        "status": "no_inci_sourced",
        "candidates_tried": [c.source for c in candidates],
    }


# supplier-input is also a valid discovery outcome (the merchant hands us INCI);
# re-exported so callers can ingest it with the right precedence.
__all__ = [
    "SourceCandidate",
    "SOURCE_INCI_DATABASE",
    "INCI_SOURCE_SUPPLIER",
    "discover_sources",
    "is_brand_official_url",
    "registrable_name",
    "source_canonical_inci",
]
