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
import time
import urllib.error
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
# Per-request politeness delay (each pool worker sleeps this long before its
# fetch) and the retry/backoff for transient rejections. 429/503 honor
# Retry-After when the server sends one, capped so a hostile header can't hang
# a worker.
_POLITENESS_DELAY_S = 0.2
_RETRY_STATUS = {429, 500, 502, 503}
_RETRY_BACKOFF_S = 2.0
_RETRY_AFTER_CAP_S = 15.0

_LDJSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.S | re.I,
)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def fetch_text(url: str, *, user_agent: str = USER_AGENT, timeout: int = _FETCH_TIMEOUT_S) -> str:
    """Fetch with one retry on transient rejections (429/5xx), honoring a sane
    Retry-After. StyleKorean intermittently 500s on valid PDPs — a single retry
    recovers most of those without hammering."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted retailer host)
                return resp.read(_MAX_BODY_BYTES).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if attempt == 2 or e.code not in _RETRY_STATUS:
                raise
            retry_after = _RETRY_BACKOFF_S
            try:
                retry_after = min(float(e.headers.get("Retry-After", "")), _RETRY_AFTER_CAP_S)
            except (TypeError, ValueError):
                pass
            time.sleep(max(retry_after, _RETRY_BACKOFF_S))
    raise RuntimeError("unreachable")  # pragma: no cover


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
            time.sleep(_POLITENESS_DELAY_S)  # per-worker pacing, not just a pool bound
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
