"""Resolve a storefront's REAL currency instead of inferring it from the TLD.

Ingest was stamping every external-seed offer `currency='USD'`, which is wrong
whenever the store prices in something else. A `.us` domain is not a currency:
`mintree.us` is a Nagpur, India store that prices in INR, so a Rs.1,999 hand-cream
5-pack was served to agents as $1,999 (~83x overstated).

Shopify publishes the answer for free at `https://<domain>/meta.json`:

    {"name": "Mintree", "country": "IN", "currency": "INR", "domain": "vmintree.in"}

This module is deliberately READ-ONLY and best-effort: it never raises, caches per
domain for the process lifetime, and returns None when it cannot prove the answer.
Callers must treat None as "unknown" (keep the existing value + flag for review) —
never as "assume USD", which is the bug this exists to detect.

Scope note: this began as a DETECTIVE aid (scripts/audit_offer_currency.py). Since
2026-09-06 it is ALSO read on the ingest write path -- services/curated_brand_feed.py
delegates to it so a storefront's own currency reaches the rows instead of a USD
default -- so a change here now moves what gets persisted, not just what gets audited.
What has NOT changed is the market half: `market` and `currency` are different
axes (destination served vs store base currency) — a KR/HK exporter legitimately
prices in USD — so equating them and rejecting offers at ingest would destroy real
inventory. The serving layer already excludes non-USD offers from US answers
(index_pipeline_state has_us_offer + pivot_query cross-border tagging).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}

META_PATH = "/meta.json"
DEFAULT_TIMEOUT = 6.0


def normalize_domain(value: Optional[str]) -> str:
    """Bare hostname from a domain or URL ('https://www.x.com/a' -> 'x.com')."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "//" not in raw:
        raw = f"https://{raw}"
    host = (urlparse(raw).netloc or "").strip()
    return host[4:] if host.startswith("www.") else host


def plausible_domain(host: Optional[str]) -> bool:
    """Sanity gate for PERMANENT domain writes (fill-only backfills can never be
    corrected by a later pass). normalize_domain is a parser, not a validator:
    it happily returns 'n' for 'N/A', 'unknown', or 'a b.com'. A storefront host
    must carry at least one dot and no whitespace."""
    h = str(host or "")
    return bool(h) and "." in h and not any(c.isspace() for c in h)


def parse_meta(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Pull {currency, country, domain, name} out of a Shopify /meta.json body."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    currency = str(data.get("currency") or "").strip().upper()
    if not _CURRENCY_RE.match(currency):
        return None
    return {
        "currency": currency,
        "country": str(data.get("country") or "").strip().upper() or None,
        "domain": str(data.get("domain") or "").strip().lower() or None,
        "name": str(data.get("name") or "").strip() or None,
    }


def clear_cache() -> None:
    """Drop the per-domain cache (tests; a long-lived worker that wants freshness)."""
    _CACHE.clear()


async def fetch_storefront_meta(
    domain: str, *, timeout: float = DEFAULT_TIMEOUT, fetch=None
) -> Optional[Dict[str, Any]]:
    """Best-effort {currency,country,...} for a storefront. None when unknown.

    Cached per domain for the process lifetime — including negative results, so a
    transient failure will not be retried until clear_cache(). Acceptable for the
    one-shot audit script; a long-lived caller should clear_cache() periodically.
    `fetch` is injectable for tests; the default uses httpx.
    """
    host = normalize_domain(domain)
    if not host:
        return None
    if host in _CACHE:
        return _CACHE[host]

    async def _default_fetch(url: str) -> Optional[str]:
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "PivotaBot/1.0 (+https://pivota.cc; currency-check)"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception:
            return None

    body = await (fetch or _default_fetch)(f"https://{host}{META_PATH}")
    meta = parse_meta(body)
    _CACHE[host] = meta
    if meta:
        logger.debug("storefront_currency %s -> %s", host, meta.get("currency"))
    return meta


def currency_mismatch(stamped: Optional[str], meta: Optional[Dict[str, Any]]) -> bool:
    """True only when we can PROVE the stamped currency is wrong.

    Unknown (meta is None, or either side blank) is never a mismatch — no guessing.
    """
    if not meta:
        return False
    actual = meta.get("currency")
    stamped_norm = (stamped or "").strip().upper()
    if not actual or not stamped_norm:
        return False
    return stamped_norm != actual
