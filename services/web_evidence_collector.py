"""Phase 2c: web-evidence collector.

Leverage the EXISTING SerpAPI search infra (services.social_search_client — the
same client the audit uses for grounded web retrieval) to DISCOVER press / review
citations for a product, and record them as REVIEWABLE evidence candidates in the
cross-vertical product_evidence store (Phase 2a/2b).

Trust spine: web-discovered claims enter UNVERIFIED — they surface to the merchant
for confirmation in the evidence panel, exactly like lab-report candidates, and are
NEVER auto-served to agents. The merchant confirms one (POST /evidence with the
matching source_type + source_ref=url) → it grades substantiated → the serve gate
publishes it. This is the "crawl accelerates head merchants" path; long-tail
merchants still rely on direct intake.

Reuses, never rebuilds:
  - services.social_search_client.search_web (SerpAPI; honesty contract: no key /
    failure -> no results, so this whole path is a safe no-op without a key)
  - db.product_evidence.{fetch_product_evidence_row, upsert_product_evidence}
  - the claim_safety ProductClaim vocab (source_type / substantiation_status)

Best-effort throughout: discovery or persistence failure never raises into the
caller (an audit / scheduled job must not fail because evidence enrichment hiccuped).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SOURCE_EDITORIAL = "editorial_press"
SOURCE_REVIEW = "third_party_review"
SOURCE_SOCIAL = "social_mention"
SOURCE_BRAND = "brand_owned"
SOURCE_UNKNOWN = "web_mention"

# Host keywords (substring match on the registrable host). Editorial press +
# review aggregators are third-party authorities worth PROPOSING as claims; social
# / UGC is noted as a weaker mention but never proposed; brand-owned hosts are
# skipped (self-reference, not third-party evidence). Heuristic + intentionally
# conservative — a misclass only yields an unverified candidate the merchant can
# dismiss, never an auto-served claim.
_EDITORIAL_HOSTS = (
    "vogue", "allure", "elle.", "byrdie", "refinery29", "cosmopolitan",
    "harpersbazaar", "glamour", "instyle", "wwd.", "marieclaire", "self.",
    "popsugar", "thecut", "nytimes", "forbes", "wirecutter", "goodhousekeeping",
    "townandcountry", "bustle", "wellandgood",
)
_REVIEW_HOSTS = (
    "sephora", "ulta", "amazon", "trustpilot", "makeupalley", "influenster",
    "beautypedia", "yelp",
)
_SOCIAL_HOSTS = (
    "reddit", "youtube", "youtu.be", "tiktok", "instagram", "facebook",
    "pinterest", "x.com", "twitter",
)

# source_type -> the evidence_grade it WOULD earn once substantiated (matches the
# 2b intake / serving a/b/c gate). Informational on an unverified candidate.
_SOURCE_GRADE = {SOURCE_EDITORIAL: "b", SOURCE_REVIEW: "b"}
_CLAIMABLE_SOURCES = frozenset({SOURCE_EDITORIAL, SOURCE_REVIEW})


def _host(url: Any) -> str:
    try:
        netloc = urlparse(str(url or "").strip()).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


# Common words that appear in brand names but must NOT drive host matching (else
# "The Ordinary" would mark thecut.com as brand-owned and drop a real editorial).
_BRAND_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "your", "our", "inc", "llc", "ltd", "co", "company", "brand"}
)


def _brand_tokens(brand: Optional[str]) -> List[str]:
    return [
        t
        for t in str(brand or "").lower().replace("-", " ").split()
        if len(t) > 2 and t not in _BRAND_STOPWORDS
    ]


def _normalize_host(value: Any) -> str:
    """Host for a value that may be a bare host or a full URL (with/without scheme)."""
    s = str(value or "").strip()
    return _host(s if "://" in s else "http://" + s)


def _clip(text: Any, n: int) -> str:
    t = str(text or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def classify_web_source(
    url: str,
    *,
    brand: Optional[str] = None,
    merchant_host: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """(source_type, evidence_grade) for a discovered URL, by host heuristics.
    Brand-owned / merchant host -> brand_owned (skip; self-reference). Pure."""
    host = _host(url)
    if not host:
        return SOURCE_UNKNOWN, None
    mh = _normalize_host(merchant_host) if merchant_host else ""
    if mh and mh in host:
        return SOURCE_BRAND, None
    for tok in _brand_tokens(brand):
        if tok in host:
            return SOURCE_BRAND, None
    if any(h in host for h in _SOCIAL_HOSTS):
        return SOURCE_SOCIAL, None
    if any(h in host for h in _EDITORIAL_HOSTS):
        return SOURCE_EDITORIAL, _SOURCE_GRADE[SOURCE_EDITORIAL]
    if any(h in host for h in _REVIEW_HOSTS):
        return SOURCE_REVIEW, _SOURCE_GRADE[SOURCE_REVIEW]
    return SOURCE_UNKNOWN, None


def build_web_evidence_candidates(
    search_results: Any,
    *,
    brand: Optional[str] = None,
    merchant_host: Optional[str] = None,
    max_claims: int = 6,
) -> List[Dict[str, Any]]:
    """[{title,url,snippet}] -> UNVERIFIED candidate ProductClaim dicts for
    press/review sources only (one per outlet, deduped by host, capped).
    source_ref=url is the citation the merchant verifies. Pure."""
    out: List[Dict[str, Any]] = []
    seen_hosts = set()
    for r in search_results or []:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "").strip()
        title = str(r.get("title") or "").strip()
        if not url or not title:
            continue
        source_type, grade = classify_web_source(url, brand=brand, merchant_host=merchant_host)
        if source_type not in _CLAIMABLE_SOURCES:
            continue
        host = _host(url)
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        claim: Dict[str, Any] = {
            "claim_text": _clip(title, 200),
            "source_ref": url,
            "source_type": source_type,
            "evidence_grade": grade,
            # UNVERIFIED until the merchant confirms — never auto-served.
            "substantiation_status": "unverified",
            "discovered_via": "web_crawl",
        }
        out.append(claim)
        if len(out) >= max_claims:
            break
    return out


async def _default_search(query: str) -> Tuple[List[Dict[str, str]], str]:
    from services.social_search_client import search_web

    return await search_web(query)


async def _merge_candidates_into_evidence(
    product_key: str,
    merchant_id: Optional[str],
    candidates: List[Dict[str, Any]],
    *,
    geo_code: str = "default",
    db: Any = None,
) -> int:
    """Append new web candidates to the product's existing evidence WITHOUT
    clobbering merchant-entered claims (upsert REPLACES the array, so we
    read-merge-write). Dedupe by source_ref and claim_text. Returns count added."""
    from db.product_evidence import fetch_product_evidence_row, upsert_product_evidence

    existing = await fetch_product_evidence_row(product_key, geo_code=geo_code, db=db)
    existing_claims = list((existing or {}).get("claims") or [])
    review_state = (existing or {}).get("review_state") or "observed"
    # upsert_product_evidence does a FULL-ROW upsert (merchant_id / required_disclaimers
    # = EXCLUDED.*), so we must carry the existing row's values forward or a crawl/job
    # that doesn't know the merchant (merchant_id=None — the headline use case) would
    # null out the stored merchant_id (the indexed scoping column) + disclaimers.
    # Prefer the existing non-null merchant_id over the (possibly None) argument.
    existing_merchant_id = (existing or {}).get("merchant_id")
    effective_merchant_id = existing_merchant_id or merchant_id
    existing_disclaimers = (existing or {}).get("required_disclaimers")
    seen_refs = {
        str(c.get("source_ref") or "").strip().lower()
        for c in existing_claims
        if isinstance(c, dict) and c.get("source_ref")
    }
    seen_texts = {
        str(c.get("claim_text") or "").strip().lower()
        for c in existing_claims
        if isinstance(c, dict)
    }
    added: List[Dict[str, Any]] = []
    for cand in candidates:
        ref = str(cand.get("source_ref") or "").strip().lower()
        txt = str(cand.get("claim_text") or "").strip().lower()
        if (ref and ref in seen_refs) or (txt and txt in seen_texts):
            continue
        seen_refs.add(ref)
        seen_texts.add(txt)
        added.append(cand)
    if not added:
        return 0
    await upsert_product_evidence(
        product_key,
        merchant_id=effective_merchant_id,
        claims=existing_claims + added,
        geo_code=geo_code,
        review_state=review_state,
        required_disclaimers=existing_disclaimers,
        db=db,
    )
    return len(added)


async def collect_web_evidence_for_product(
    product_key: str,
    merchant_id: Optional[str],
    *,
    brand: Optional[str] = None,
    title: Optional[str] = None,
    merchant_host: Optional[str] = None,
    geo_code: str = "default",
    db: Any = None,
    search: Optional[Callable[[str], Awaitable[Tuple[List[Dict[str, str]], str]]]] = None,
) -> Dict[str, Any]:
    """Discover press/review citations for a product via SerpAPI and merge them as
    UNVERIFIED candidate claims into product_evidence (for merchant review).

    Best-effort: returns a summary, never raises. A no-op (status from the search
    client, candidates_added=0) when there is no SerpAPI key or no usable results.
    `search` is injectable for tests."""
    summary: Dict[str, Any] = {
        "product_key": product_key,
        "discovered": 0,
        "candidates_added": 0,
        "status": "skipped",
    }
    if not product_key or not (brand or title):
        return summary
    searcher = search or _default_search
    query = " ".join(
        t for t in [str(brand or "").strip(), str(title or "").strip(), "review"] if t
    ).strip()
    try:
        results, status = await searcher(query)
    except Exception:
        summary["status"] = "search_error"
        return summary
    summary["status"] = status
    summary["discovered"] = len(results or [])
    candidates = build_web_evidence_candidates(
        results or [], brand=brand, merchant_host=merchant_host
    )
    if not candidates:
        return summary
    try:
        added = await _merge_candidates_into_evidence(
            product_key, merchant_id, candidates, geo_code=geo_code, db=db
        )
        summary["candidates_added"] = added
        if added:
            summary["status"] = "stored"
    except Exception as exc:
        logger.warning("web evidence persist failed for %s: %s", product_key, str(exc)[:200])
        summary["status"] = "persist_error"
    return summary
