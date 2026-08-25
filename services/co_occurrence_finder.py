"""
Phase B — co-occurrence verifier.

Today the pitch_draft body says "They listed Lunya, Eberjey, Hill
House Home; your brand absent" — based on Gemini's
`competitors_appearing` self-report. Gemini reports which brands it
*mentioned* in its grounded answer. The article it cited may or may
not actually contain those brands; Gemini frequently paraphrases or
synthesizes across multiple grounding chunks.

Phase B fetches the actual article and verifies each claimed brand
via word-boundary regex on the stripped HTML text. Output:

  - `verified_brands`:    subset of claimed that DO appear in article
  - `contradicted_brands`: claimed but NOT found in article
                           (Gemini paraphrased / hallucinated)
  - `merchant_absent`:    merchant brand verified as not in article
  - `merchant_present`:   merchant brand IS in article
                          (surprising — a different play applies)
  - `fetch_status`:       'ok' | 'blocked' (robots) | 'error' | 'cached'

Cost / latency: per audit ~5-10 unique editorial article fetches at
most. We run them in parallel via asyncio.gather, with a 5s per-fetch
timeout, so the worst-case audit-time addition is ~5s regardless of N.
24h in-memory cache by URL + sha-of-merchant-brand handles repeat
audits cheaply.

Honesty rule: when verification fails (robots blocked, fetch error,
cache miss with no fetch budget), the pitch body must NOT make the
strong "they literally listed X" claim. It falls back to the softer
Gemini-self-report phrasing with a hedge.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from services import crawl_politeness

logger = logging.getLogger(__name__)


# 24h cache keyed by (url, merchant_brand_lower). Stores
# (verification_result, expires_at_epoch).
_CACHE: Dict[Tuple[str, str], Tuple[Dict[str, Any], float]] = {}
_CACHE_TTL_SECONDS = 24 * 3600


# Polite User-Agent — identifies us, includes a contact URL so site
# owners can reach out if our crawler misbehaves. Required by good
# crawler etiquette and most sites' robots.txt accept conditions.
_USER_AGENT = (
    "PivotaAuditBot/1.0 (+https://pivota.cc/about/audit-bot; "
    "verifies merchant audit claims against editorial articles)"
)
_FETCH_TIMEOUT_SECONDS = 5.0
# Max bytes to download per article — editorial articles are
# typically <500KB; stop reading at 2MB to bound memory.
_MAX_BYTES = 2 * 1024 * 1024


def reset_cache() -> None:
    """For tests."""
    _CACHE.clear()


async def verify_co_occurrence(
    article_url: str,
    *,
    claimed_brands: List[str],
    merchant_brand: Optional[str],
) -> Dict[str, Any]:
    """Verify Gemini's `competitors_appearing` self-report against
    the actual cited article. Async — uses httpx.AsyncClient.

    Output schema:
      {
        verified_brands:    List[str],   # claimed AND found in article
        contradicted_brands: List[str],  # claimed but NOT in article
        merchant_present:   bool,        # merchant brand found in article
        merchant_absent:    bool,        # merchant brand confirmed absent
        fetch_status:       'ok' | 'cached' | 'blocked' | 'error' | 'no_url',
        article_url:        str,
      }
    """
    out: Dict[str, Any] = {
        "verified_brands": [],
        "contradicted_brands": [],
        "merchant_present": False,
        "merchant_absent": False,
        "fetch_status": "no_url",
        "article_url": article_url or "",
    }
    if not article_url or not isinstance(article_url, str):
        return out

    cache_key = (article_url, (merchant_brand or "").lower())
    cached = _CACHE.get(cache_key)
    now = time.time()
    if cached and cached[1] > now:
        result = dict(cached[0])
        result["fetch_status"] = "cached"
        return result

    text, status = await _fetch_article_text(article_url)
    if status != "ok":
        out["fetch_status"] = status
        # Don't cache failures — let the next audit retry.
        return out

    text_lower = text.lower()
    merchant_brand_lower = (merchant_brand or "").strip().lower()

    verified: List[str] = []
    contradicted: List[str] = []
    for raw_brand in claimed_brands or []:
        if not isinstance(raw_brand, str) or not raw_brand.strip():
            continue
        brand = raw_brand.strip()
        if _brand_in_text(brand, text_lower):
            verified.append(brand)
        else:
            contradicted.append(brand)

    merchant_present = bool(
        merchant_brand_lower
        and len(merchant_brand_lower) >= 4
        and _brand_in_text(merchant_brand, text_lower)
    )

    out["verified_brands"] = verified
    out["contradicted_brands"] = contradicted
    out["merchant_present"] = merchant_present
    out["merchant_absent"] = bool(merchant_brand_lower and not merchant_present)
    out["fetch_status"] = "ok"
    _CACHE[cache_key] = (dict(out), now + _CACHE_TTL_SECONDS)
    return out


async def verify_many(
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Run multiple verifications in parallel. Each item must have
    `article_url` + `claimed_brands` + optional `merchant_brand`.
    Total wall time is bounded by the slowest fetch (5s), regardless
    of N — so the audit hot path is not multiplicatively slowed."""
    if not items:
        return []
    tasks = [
        verify_co_occurrence(
            item.get("article_url") or "",
            claimed_brands=item.get("claimed_brands") or [],
            merchant_brand=item.get("merchant_brand"),
        )
        for item in items
    ]
    return list(await asyncio.gather(*tasks, return_exceptions=False))


# Internal helpers


def _brand_in_text(brand: str, text_lower: str) -> bool:
    """Word-boundary regex match. 4-char threshold mirrors
    score_category_visibility — short brand names false-positive
    on common words."""
    b = (brand or "").strip().lower()
    if not b or len(b) < 4:
        return False
    pattern = re.compile(r"\b" + re.escape(b) + r"\b")
    return pattern.search(text_lower) is not None


async def _fetch_article_text(
    article_url: str,
) -> Tuple[str, str]:
    """Returns (stripped_text, status). status is one of:
      - 'ok': fetched + parsed
      - 'blocked': robots.txt disallows our UA
      - 'error': network failure, non-2xx, oversized, or parse failure
    """
    try:
        parsed = urlparse(article_url)
    except Exception:
        return ("", "error")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ("", "error")

    # Robots.txt check first — if disallowed, don't even try.
    if not await _robots_allows(article_url):
        return ("", "blocked")

    try:
        import httpx
    except ImportError:
        logger.warning("httpx not available; co-occurrence fetch disabled")
        return ("", "error")

    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            # Shared politeness gate. These lanes crawl merchant storefronts from the SAME
            # reserved NAT address as every other outbound lane, so a ban earned here takes the
            # whole crawl subnet down. `max_wait=0` because this is batch work: refusing rather
            # than waiting would turn a brief 429 into silently-dropped rows.
            await crawl_politeness.before_request(article_url, user_agent=_USER_AGENT, max_wait=0)
            resp = await client.get(article_url)
            crawl_politeness.note_response(
                article_url, resp.status_code, retry_after=resp.headers.get("retry-after")
            )
            if resp.status_code != 200:
                return ("", "error")
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/html" not in ctype and "text/plain" not in ctype:
                return ("", "error")
            content = resp.content[:_MAX_BYTES]
            text = content.decode(resp.encoding or "utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        logger.info("co-occurrence fetch failed for %s: %s", article_url, exc)
        return ("", "error")

    # Naive but robust HTML strip: remove script/style + tags.
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return (text.strip(), "ok")


async def _robots_allows(article_url: str) -> bool:
    """Is `article_url` crawlable for our User-Agent? Delegates to the shared politeness gate.

    This one was already correct about the PATH — unlike the sibling in
    brand_product_discovery, it passed the full url to `can_fetch`. What it lacked was a cache:
    it refetched robots.txt on EVERY article, so a run over 200 articles on one host made 200
    robots requests to that host purely to ask permission. The shared gate caches per host, caps
    the response size, and bounds the redirect chain.

    Unchanged contract: errors are permissive; only an explicit Disallow blocks.
    """
    return await crawl_politeness.robots_allows(article_url, user_agent=_USER_AGENT)


async def verify_brand_report_co_occurrence(
    brand_report: Dict[str, Any],
    *,
    merchant_brand: Optional[str],
) -> Dict[str, Any]:
    """Walk every per-product report's playbook actions, look up the
    cited article URL for each action's target_host (via the
    `failed_queries_detailed` block on the same report), and verify
    Gemini's `competitors_named` claim against the actual article
    text. Mutates each action in place by adding an
    `evidence.co_occurrence_verification` field — the portal renders
    a "Verified" / "Unverified" / "Couldn't fetch" badge from this.

    Idempotent: re-running on a brand_report whose actions already
    carry verification metadata is cheap (cache hits) and safe (last
    write wins, identical inputs produce identical output).

    Best-effort: any failure inside the verifier produces
    fetch_status='error' on the action; never raises.
    """
    if not isinstance(brand_report, dict):
        return brand_report

    # Collect (action_ref, article_url, claimed_brands) tuples first
    # so we can run all fetches in parallel.
    batch: List[Dict[str, Any]] = []
    action_refs: List[Dict[str, Any]] = []
    for report in brand_report.get("per_product") or []:
        if not isinstance(report, dict):
            continue
        mv = report.get("merchant_view") or {}
        receipts = mv.get("receipts") or {}
        failed_queries = receipts.get("failed_queries_detailed") or []
        # Build host → article_url lookup. Multiple failed queries
        # may share a host; first-cited wins (the most-frequent host
        # gets the most-cited URL).
        host_to_url: Dict[str, str] = {}
        for fq in failed_queries:
            host = (fq.get("top_cited_host") or "").lower()
            url = fq.get("top_cited_url") or ""
            if host and url and host not in host_to_url:
                host_to_url[host] = url
        for action in mv.get("actions") or []:
            if not isinstance(action, dict):
                continue
            target_host = (action.get("target_host") or "").lower()
            if not target_host or target_host not in host_to_url:
                continue
            evidence = action.get("evidence") or {}
            claimed = evidence.get("competitors_named") or []
            if not claimed:
                continue
            batch.append({
                "article_url": host_to_url[target_host],
                "claimed_brands": list(claimed),
                "merchant_brand": merchant_brand,
            })
            action_refs.append(action)

    if not batch:
        return brand_report

    try:
        results = await verify_many(batch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("co-occurrence verify_many failed: %s", exc)
        return brand_report

    for action, verification in zip(action_refs, results):
        evidence = action.setdefault("evidence", {})
        evidence["co_occurrence_verification"] = verification

    return brand_report


def co_occurrence_phrase(
    verification: Dict[str, Any],
    *,
    fallback_brands: List[str],
) -> str:
    """Render the pitch-body phrase using whichever signal is
    strongest:

      1. fetch_status='ok' with verified_brands ≥ 1 + merchant_absent:
         strongest claim — names verified brands, asserts merchant
         absence as a fact about the article ("Their article literally
         lists Lunya, Eberjey; your brand is absent").
      2. fetch_status='ok' with verified_brands = 0 OR merchant_present:
         degraded — drop the "your brand absent" framing.
      3. fetch_status != 'ok' (blocked / error / no_url):
         fallback to Gemini's self-report with hedge.
    """
    status = (verification or {}).get("fetch_status") or ""
    verified = list((verification or {}).get("verified_brands") or [])
    merchant_absent = bool((verification or {}).get("merchant_absent"))
    merchant_present = bool((verification or {}).get("merchant_present"))

    if status in ("ok", "cached") and verified and merchant_absent:
        names = verified[:3]
        if len(names) == 1:
            list_str = names[0]
        elif len(names) == 2:
            list_str = f"{names[0]} and {names[1]}"
        else:
            list_str = f"{', '.join(names[:-1])}, and {names[-1]}"
        return (
            f"Their article literally lists {list_str}; your brand is "
            f"absent. "
        )

    if status in ("ok", "cached") and merchant_present:
        return (
            "Their article includes your brand among others; the pitch "
            "should reference where the existing coverage leaves room. "
        )

    # Fallback: Gemini self-report, hedged.
    names = [n for n in (fallback_brands or []) if isinstance(n, str) and n.strip()][:3]
    if not names:
        return ""
    if len(names) == 1:
        list_str = names[0]
    elif len(names) == 2:
        list_str = f"{names[0]} and {names[1]}"
    else:
        list_str = f"{', '.join(names[:-1])}, and {names[-1]}"
    return (
        f"Gemini's response surfaced {list_str} alongside this article "
        f"(unverified — we couldn't fetch the article text). "
    )
