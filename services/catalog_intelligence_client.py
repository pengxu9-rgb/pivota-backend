"""
HTTP client for the Pivota-catalog-intelligence service.

Catalog-intelligence is a separate Express + Puppeteer service (in
`/Users/pengchydan/dev/Pivota-catalog-intelligence`) that does
heavy-duty product extraction from brand websites — handles JS-
rendered storefronts, Shopify variants, schema.org, ad-copy,
ingredient harvesting. Far more capable than this backend's
in-process regex-based discovery (services/brand_product_discovery).

This client is the BRIDGE: BD cold-start audits call this first
to get a rich catalog; if catalog-intelligence is unreachable or
fails for the target site, the orchestrator falls back to
brand_product_discovery.

Best-effort: this module never raises into the audit pipeline.
It returns a normalized payload on success or `None` on any
failure, with full error context logged for ops debugging.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """True iff catalog-intelligence base URL is set in settings.
    Without it, the orchestrator skips this path entirely (goes
    straight to fallback discovery)."""
    from config.settings import settings
    return bool((settings.catalog_intelligence_base_url or "").strip())


def _normalize_base_url(raw: str) -> str:
    """Ensure the configured URL has a scheme. Without it, httpx
    raises httpx.UnsupportedProtocol — which, before this fix, got
    silently swallowed by the bare HTTPError catch and the
    orchestrator fell back without operators realizing the env var
    was misconfigured.

    Auto-prepends `https://` (only valid scheme for Railway service
    URLs in production). Defensive footgun fix — was burned once on
    gruns.co smoke test; never again.
    """
    s = (raw or "").strip()
    if not s:
        return s
    if s.startswith("http://") or s.startswith("https://"):
        return s.rstrip("/")
    return f"https://{s.rstrip('/')}"


async def extract_catalog(
    *,
    brand: str,
    domain: str,
    market: str = "US",
    limit: int = 50,
) -> Optional[Dict[str, Any]]:
    """Call catalog-intelligence's POST /api/extract.

    Returns a normalized payload on success:
      {
        "products": [
          {"title": str, "pdp_url": str, "vendor": None,
           "product_type": None, "raw_extracted": <full source>},
          ...
        ],
        "platform": str | None,        # 'shopify' | 'woocommerce' | etc.
        "discovery_strategy": str | None,
        "diagnostics": {...} | None,
        "raw_response": <full source>,
      }

    Returns None on:
      - service not configured (CATALOG_INTELLIGENCE_BASE_URL unset)
      - HTTP error (timeout, network, non-200)
      - service returned a body without `products` array
      - extracted products list is empty (caller falls back)

    Never raises. All errors logged at WARNING/ERROR.
    """
    if not is_configured():
        return None

    from config.settings import settings
    if not brand or not brand.strip() or not domain or not domain.strip():
        logger.warning(
            "catalog_intelligence: empty brand or domain (brand=%r domain=%r)",
            brand, domain,
        )
        return None

    base = _normalize_base_url(settings.catalog_intelligence_base_url)
    url = f"{base}/api/extract"
    body = {
        "brand": brand.strip(),
        "domain": domain.strip(),
        "market": market or "US",
        "limit": int(limit),
    }
    headers = {"Content-Type": "application/json"}
    api_key = (settings.catalog_intelligence_api_key or "").strip()
    if api_key:
        # Convention: forward shared secret as Bearer for now.
        # When catalog-intelligence formalizes its auth scheme,
        # this header name swaps in one place.
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        import httpx
    except ImportError:
        logger.error("catalog_intelligence: httpx missing")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=settings.catalog_intelligence_timeout_s,
        ) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.UnsupportedProtocol as exc:
        # Misconfigured env var: bare hostname without scheme.
        # _normalize_base_url should prevent this now, but still log
        # loudly if it slips through (e.g., env var contains
        # whitespace or invalid scheme like "ftp://").
        logger.error(
            "catalog_intelligence: env var CATALOG_INTELLIGENCE_BASE_URL "
            "is malformed — %s. Resolved URL=%r. Fix the env var to "
            "start with https://",
            exc, url,
        )
        return None
    except httpx.ConnectError as exc:
        logger.error(
            "catalog_intelligence: connection refused / DNS failure "
            "for %s — service may be down or URL points at the "
            "wrong host. Resolved URL=%r: %s",
            domain, url, exc,
        )
        return None
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        logger.warning(
            "catalog_intelligence: timeout for %s after %.0fs — "
            "Puppeteer extraction may be slow on large catalogs; "
            "consider increasing CATALOG_INTELLIGENCE_TIMEOUT_S "
            "(currently %s). Falling back to lightweight discovery.",
            domain, settings.catalog_intelligence_timeout_s,
            settings.catalog_intelligence_timeout_s,
        )
        return None
    except httpx.HTTPError as exc:
        logger.warning(
            "catalog_intelligence: HTTP error for %s — %s: %s",
            domain, type(exc).__name__, exc,
        )
        return None

    if resp.status_code != 200:
        logger.warning(
            "catalog_intelligence: non-200 for %s — status=%d body=%s",
            domain, resp.status_code, resp.text[:300],
        )
        return None

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "catalog_intelligence: non-JSON response for %s — %s",
            domain, exc,
        )
        return None

    products_raw = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products_raw, list) or not products_raw:
        logger.info(
            "catalog_intelligence: empty products list for %s "
            "(diagnostics=%s)",
            domain,
            (payload.get("diagnostics") if isinstance(payload, dict) else None),
        )
        return None

    normalized = _normalize_products(products_raw)
    if not normalized:
        # Every entry was missing url/title. Treat as empty so the
        # orchestrator falls back.
        return None

    diagnostics = payload.get("diagnostics") if isinstance(payload, dict) else None
    return {
        "products": normalized,
        "platform": payload.get("platform") if isinstance(payload, dict) else None,
        "discovery_strategy": (
            (diagnostics or {}).get("discovery_strategy")
            if isinstance(diagnostics, dict) else None
        ),
        "diagnostics": diagnostics,
        "raw_response": payload,
    }


def _normalize_products(raw: List[Any]) -> List[Dict[str, Any]]:
    """Project the catalog-intelligence ExtractedProduct shape down
    to the {title, pdp_url, vendor, product_type, raw_extracted}
    shape the audit pipeline + prospect_products table expect.
    Skips entries missing url or title — never synthesizes.

    `vendor` and `product_type` are read from the source payload when
    present (forward-compatible for when catalog-intelligence's
    ExtractedProduct type adds them — currently always None). When
    absent, downstream enrichment in services/bd_cold_start_service.py
    fills them via Shopify-native refetch + brand-level inference.
    """
    out: List[Dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        if not url or not title:
            continue
        # Accept either snake_case (Python convention) or camelCase
        # (TypeScript convention) — catalog-intelligence is a Node
        # service so its eventual field names may be either.
        vendor_raw = item.get("vendor") or item.get("brand") or None
        type_raw = item.get("product_type") or item.get("productType") or None
        vendor = (vendor_raw or "").strip() or None if isinstance(vendor_raw, str) else None
        product_type = (type_raw or "").strip() or None if isinstance(type_raw, str) else None
        out.append({
            "title": title,
            "pdp_url": url,
            "vendor": vendor,
            "product_type": product_type,
            "raw_extracted": item,
        })
    return out
