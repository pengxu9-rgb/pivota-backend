"""Generic sitemap-driven retailer crawler + deterministic product extraction.

Politeness: identifies as PivotaBot, bounds body size + timeout, and crawls with
a small bounded thread pool (default 6). Callers should respect the retailer's
robots.txt before enumerating (StyleKorean's allows /product/*; see the adapter).

Extraction REUSES the repo's deterministic JSON-LD parser
(`services/external_offers_service._parse_jsonld_texts` +
`_extract_jsonld_offer`) so there is exactly one JSON-LD Product parser in the
codebase. No LLM, no fabrication — fields come straight from the page's
schema.org markup.
"""

from __future__ import annotations

import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from services.external_offers_service import (
    _availability_from_raw,
    _extract_jsonld_offer,
    _parse_jsonld_texts,
)

USER_AGENT = "Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)"
_MAX_BODY_BYTES = 2_000_000
_FETCH_TIMEOUT_S = 25

_LDJSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.S | re.I,
)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def fetch_text(url: str, *, user_agent: str = USER_AGENT, timeout: int = _FETCH_TIMEOUT_S) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted retailer host)
        return resp.read(_MAX_BODY_BYTES).decode("utf-8", "ignore")


def sitemap_locs(xml_text: str) -> List[str]:
    """All <loc> values in a sitemap or sitemap index."""
    return [m.group(1).strip() for m in _LOC_RE.finditer(xml_text)]


def extract_product(html: str) -> Optional[Dict[str, Any]]:
    """Deterministic JSON-LD Product extraction. Returns a normalized dict or
    None when the page carries no usable Product markup.

    Keys: title, brand, description, image_url, price_raw, currency,
    availability (normalized)."""
    blocks = _LDJSON_RE.findall(html)
    if not blocks:
        return None
    best = _extract_jsonld_offer(_parse_jsonld_texts(blocks))
    if not best or not best.get("title"):
        return None
    return {
        "title": best.get("title"),
        "brand": best.get("brand"),
        "description": best.get("description"),
        "image_url": best.get("image_url"),
        "price_raw": best.get("price_raw"),
        "currency": best.get("currency"),
        "availability": _availability_from_raw(best.get("availability_raw")),
    }


def crawl_products(
    urls: List[str],
    *,
    concurrency: int = 6,
    user_agent: str = USER_AGENT,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """Fetch + extract each URL with a bounded thread pool.

    Returns {"records": [{"url":..., **extracted}], "failures": [{"url","reason"}]}.
    Deterministic order is not guaranteed; caller sorts if needed."""
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    total = len(urls)

    def _one(u: str) -> Dict[str, Any]:
        try:
            prod = extract_product(fetch_text(u, user_agent=user_agent))
            if not prod:
                return {"_status": "no_product", "url": u}
            return {"_status": "ok", "url": u, **prod}
        except Exception as e:  # network / decode — record, don't abort the crawl
            return {"_status": "error", "url": u, "reason": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = {ex.submit(_one, u): u for u in urls}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            status = res.pop("_status")
            if status == "ok":
                records.append(res)
            else:
                failures.append({"url": res["url"], "reason": res.get("reason") or status})
            if on_progress:
                on_progress(i, total)

    return {"records": records, "failures": failures}
