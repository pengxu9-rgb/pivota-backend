"""Brand-level signal extraction for BD cold-start audits.

Surfaces deterministic brand intelligence from a target's homepage +
sitemap so the BD operator gets a richer pitch artifact than the bare
product audit. Sub-extractors are pure / sync; the public
collect_brand_signals() coroutine fans them out and adds a sitemap
fetch.

Outputs the `brand_signals` block consumed by:
- merchant_view.brand_snapshot (frontend BrandSnapshot.tsx)
- render_brand_markdown for the .md export

Honesty rules:
- Returns null/empty values when a field can't be extracted. Never
  fabricates a follower count, a founder name, a rating value.
- Sitemap fetch is best-effort with a short timeout; failure leaves
  sitemap_structure=None.
- Robots.txt directives are surfaced raw — we don't editorialize about
  whether they hurt SEO.

Future PR-D extends this module with Gemini-grounded social
intelligence (TikTok / Instagram presence + KOL endorsements +
competitive comparison). Future PR-C extends with retail / founder /
press context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
from services import vertex_gemini

logger = logging.getLogger(__name__)


_SITEMAP_FETCH_TIMEOUT_S = 8.0
_SITEMAP_MAX_BYTES = 5_000_000  # 5 MB — most sitemaps are <1 MB
_HOMEPAGE_FETCH_TIMEOUT_S = 6.0
_HOMEPAGE_MAX_BYTES = 2_000_000  # 2 MB — only the <head> + nav links matter

# Gemini grounded API — same shape used by gemini_url_validator.py +
# bd_brand_category_inferrer.py. No new SDK dep.
_GEMINI_MODEL = "gemini-2.5-flash"
# Model used by the social probes (own presence / KOL / competitive).
# Currently == _GEMINI_MODEL: three model levers were tried for the social
# grounding problem and ALL failed to move it — prompt escalation (PR #530,
# 0/13 recovery), gemini-2.5-pro (PR #531, ~15% flat at ~3x cost), and
# gemini-3-flash-preview (PR #533, 7% — worst, plus a parse_error bug).
# The bottleneck is `google_search` tool-discretion, not model tier, so
# swapping models does not help. This constant is kept as the per-case
# model seam for the planned model-orchestration layer (routing + outage
# fallback); until that lands it just points at the stable flash model.
# Full write-up: ~/tmp/social-grounding-check/verdict.md.
_GEMINI_SOCIAL_MODEL = "gemini-2.5-flash"
# A grounded call that actually runs a web search legitimately takes
# longer than a from-memory answer — and the Pro tier longer still. The
# old flat 20s timeout was the dominant social-probe failure mode (43%
# of bd_ calls died on `transport: ReadTimeout`, 2026-05-14). Give the
# read leg a generous budget; keep connect tight.
_GEMINI_TIMEOUT = httpx.Timeout(connect=10.0, read=75.0, write=10.0, pool=10.0)
# One retry on a transport/timeout failure. #528 added this for the main
# probe client (agent_center_llm_client) — but the bd_ social path uses
# its own client and never got it. Same fix, here. Bounded at one extra
# attempt; fires only after a failure, never on the happy path, so it is
# not the per-query call multiplier the LLM-cost-safety rule guards against.
_GEMINI_TRANSPORT_RETRY_EXCS = (
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.NetworkError,
)
_GEMINI_RETRY_BACKOFF_S = 0.5


# ---------------------------------------------------------------------------
# Open Graph + Twitter Card meta tags
# ---------------------------------------------------------------------------

_META_RE = re.compile(
    r'<meta\s+[^>]*?(?:property|name)\s*=\s*["\']([^"\']+)["\'][^>]*?'
    r'content\s*=\s*["\']([^"\']*)["\']',
    re.IGNORECASE,
)
# Some sites order content first — handle both orderings.
_META_RE_ALT = re.compile(
    r'<meta\s+[^>]*?content\s*=\s*["\']([^"\']*)["\'][^>]*?'
    r'(?:property|name)\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _extract_open_graph(html: str) -> Dict[str, Any]:
    """Extract Open Graph + Twitter Card meta tags from HTML head.
    Returns a dict with og:title, og:description, og:image, og:type,
    og:site_name, twitter:card, twitter:site, twitter:image (None when
    not present)."""
    if not html:
        return {}
    found: Dict[str, str] = {}
    for match in _META_RE.finditer(html):
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if key not in found:
            found[key] = value
    for match in _META_RE_ALT.finditer(html):
        key = match.group(2).strip().lower()
        value = match.group(1).strip()
        if key not in found:
            found[key] = value
    return {
        "og_title": found.get("og:title"),
        "og_description": found.get("og:description"),
        "og_image": found.get("og:image"),
        "og_type": found.get("og:type"),
        "og_site_name": found.get("og:site_name"),
        "twitter_card": found.get("twitter:card"),
        "twitter_site": found.get("twitter:site"),
        "twitter_image": found.get("twitter:image"),
        "meta_description": found.get("description"),
    }


# ---------------------------------------------------------------------------
# Schema.org JSON-LD parsing
# ---------------------------------------------------------------------------

_JSONLD_BLOCK_RE = re.compile(
    r'<script\s+[^>]*?type\s*=\s*["\']application/ld\+json["\'][^>]*?>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _parse_jsonld_blocks(html: str) -> List[Any]:
    """Parse all <script type="application/ld+json"> blocks; return list
    of decoded JSON values (dict | list). Skips blocks that fail to
    parse — never raises."""
    if not html:
        return []
    out: List[Any] = []
    for match in _JSONLD_BLOCK_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            # Some sites embed multiple JSON objects in one block — try
            # to find the first valid object.
            obj_match = re.search(r"\{[\s\S]*\}", raw)
            if obj_match:
                try:
                    out.append(json.loads(obj_match.group(0)))
                except json.JSONDecodeError:
                    pass
    return out


def _flatten_jsonld(blocks: List[Any]) -> List[Dict[str, Any]]:
    """A JSON-LD block can be a single object, a list of objects, or
    contain a @graph array of nested objects. Walk all of them and
    flatten into a single list of dicts."""
    out: List[Dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict):
                    out.append(item)
                    graph = item.get("@graph") or []
                    if isinstance(graph, list):
                        out.extend(g for g in graph if isinstance(g, dict))
        elif isinstance(block, dict):
            out.append(block)
            graph = block.get("@graph") or []
            if isinstance(graph, list):
                out.extend(g for g in graph if isinstance(g, dict))
    return out


def _matches_type(item: Dict[str, Any], target: str) -> bool:
    """JSON-LD @type can be a string OR a list of strings. Match
    case-insensitively against the bare type name (ignoring schema
    prefix)."""
    t = item.get("@type")
    if isinstance(t, str):
        return t.split("/")[-1].lower() == target.lower()
    if isinstance(t, list):
        return any(
            isinstance(x, str) and x.split("/")[-1].lower() == target.lower()
            for x in t
        )
    return False


def _extract_schema_org_organization(jsonld_items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the first Organization (or Brand or LocalBusiness) block
    and project to a flat dict: name, url, logo, description, sameAs
    (social links), founder, foundingDate, address, telephone."""
    org_types = ("Organization", "Brand", "LocalBusiness", "Corporation", "OnlineStore")
    for item in jsonld_items:
        if not any(_matches_type(item, t) for t in org_types):
            continue
        # Founder may be a string, dict (Person), or list of either.
        founders: List[str] = []
        f = item.get("founder")
        if isinstance(f, str):
            founders.append(f)
        elif isinstance(f, dict):
            n = f.get("name")
            if isinstance(n, str):
                founders.append(n)
        elif isinstance(f, list):
            for entry in f:
                if isinstance(entry, str):
                    founders.append(entry)
                elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    founders.append(entry["name"])
        # Logo can be a string URL or an ImageObject dict.
        logo = item.get("logo")
        if isinstance(logo, dict):
            logo = logo.get("url")
        # sameAs is a list of social/profile URLs.
        same_as = item.get("sameAs")
        same_as_list: List[str] = []
        if isinstance(same_as, str):
            same_as_list = [same_as]
        elif isinstance(same_as, list):
            same_as_list = [s for s in same_as if isinstance(s, str)]
        # Address can be a PostalAddress dict or a string.
        addr = item.get("address")
        addr_text: Optional[str] = None
        if isinstance(addr, str):
            addr_text = addr
        elif isinstance(addr, dict):
            parts = [
                addr.get("streetAddress"),
                addr.get("addressLocality"),
                addr.get("addressRegion"),
                addr.get("postalCode"),
                addr.get("addressCountry"),
            ]
            addr_text = ", ".join(p for p in parts if isinstance(p, str)) or None
        return {
            "name": item.get("name") if isinstance(item.get("name"), str) else None,
            "url": item.get("url") if isinstance(item.get("url"), str) else None,
            "logo": logo if isinstance(logo, str) else None,
            "description": (
                item.get("description") if isinstance(item.get("description"), str) else None
            ),
            "same_as": same_as_list,
            "founders": founders,
            "founding_date": (
                item.get("foundingDate") if isinstance(item.get("foundingDate"), str) else None
            ),
            "address": addr_text,
            "telephone": (
                item.get("telephone") if isinstance(item.get("telephone"), str) else None
            ),
            "@type_raw": item.get("@type"),
        }
    return None


def _extract_aggregate_rating(jsonld_items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the first AggregateRating block. Could be at the top level
    (brand-wide) or nested inside an Organization/Product. Walk the
    flat list — caller already flattened."""
    for item in jsonld_items:
        if _matches_type(item, "AggregateRating"):
            return _project_rating(item)
        # Nested under aggregateRating field on Product / Organization.
        nested = item.get("aggregateRating")
        if isinstance(nested, dict) and _matches_type(nested, "AggregateRating"):
            return _project_rating(nested)
    return None


def _project_rating(item: Dict[str, Any]) -> Dict[str, Any]:
    def _num(v: Any) -> Optional[float]:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                return None
        return None

    return {
        "rating_value": _num(item.get("ratingValue")),
        "rating_count": int(item.get("ratingCount") or 0) if item.get("ratingCount") else None,
        "review_count": int(item.get("reviewCount") or 0) if item.get("reviewCount") else None,
        "best_rating": _num(item.get("bestRating")),
        "worst_rating": _num(item.get("worstRating")),
    }


# ---------------------------------------------------------------------------
# Social handle discovery
# ---------------------------------------------------------------------------

# Per-platform regex. Captures the handle (without @ for TikTok/IG since
# handles never include @ in the URL path; X/Twitter handles same).
# Uses negative lookahead to reject non-profile paths like /watch, /news.
_SOCIAL_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("instagram", re.compile(
        r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?",
        re.IGNORECASE,
    )),
    ("tiktok", re.compile(
        r"(?:https?://)?(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{2,24})/?",
        re.IGNORECASE,
    )),
    ("youtube", re.compile(
        r"(?:https?://)?(?:www\.)?youtube\.com/@([A-Za-z0-9_.\-]{3,30})/?",
        re.IGNORECASE,
    )),
    ("twitter", re.compile(
        r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})/?",
        re.IGNORECASE,
    )),
    ("linkedin", re.compile(
        r"(?:https?://)?(?:www\.)?linkedin\.com/(?:company|school)/([A-Za-z0-9\-_.]{2,100})/?",
        re.IGNORECASE,
    )),
    ("facebook", re.compile(
        r"(?:https?://)?(?:www\.)?facebook\.com/([A-Za-z0-9.]{3,50})/?",
        re.IGNORECASE,
    )),
    ("pinterest", re.compile(
        r"(?:https?://)?(?:www\.)?pinterest\.com/([A-Za-z0-9_]{2,30})/?",
        re.IGNORECASE,
    )),
]

# Reject handles that look like generic paths (Instagram/TikTok/etc all
# expose these as profile URLs that aren't real brand profiles).
_SOCIAL_HANDLE_BLACKLIST = {
    "explore", "p", "reel", "stories", "tv", "developer", "directory",
    "about", "help", "legal", "share", "search", "tag", "tags",
    "hashtag", "discover", "watch", "live", "feed", "trending",
    "shop", "shopping", "music", "playlist", "video", "videos",
    "jobs", "company", "school", "news", "pulse", "events",
}


def _extract_social_handles(html: str) -> List[Dict[str, str]]:
    """Find social profile links anywhere in the HTML. De-dupes by
    (platform, handle) — if a brand links to its IG in both header and
    footer, we surface one entry."""
    if not html:
        return []
    seen: set = set()
    out: List[Dict[str, str]] = []
    for platform, pattern in _SOCIAL_PATTERNS:
        for match in pattern.finditer(html):
            handle = match.group(1).strip().rstrip(".")
            if not handle or handle.lower() in _SOCIAL_HANDLE_BLACKLIST:
                continue
            key = (platform, handle.lower())
            if key in seen:
                continue
            seen.add(key)
            url = _build_social_url(platform, handle)
            out.append({"platform": platform, "handle": handle, "url": url})
    return out


def _build_social_url(platform: str, handle: str) -> str:
    """Reconstruct a canonical profile URL. Some platforms use @handle
    in the URL (TikTok, YouTube), others don't."""
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{handle}"
    if platform == "youtube":
        return f"https://www.youtube.com/@{handle}"
    if platform == "linkedin":
        return f"https://www.linkedin.com/company/{handle}"
    if platform == "twitter":
        return f"https://x.com/{handle}"
    if platform == "facebook":
        return f"https://www.facebook.com/{handle}"
    if platform == "pinterest":
        return f"https://www.pinterest.com/{handle}"
    return f"https://www.{platform}.com/{handle}"


# ---------------------------------------------------------------------------
# Sitemap structure analysis
# ---------------------------------------------------------------------------

_PATH_BUCKETS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("products", re.compile(r"/products?(?:/|$)", re.IGNORECASE)),
    ("collections", re.compile(r"/collections?(?:/|$)", re.IGNORECASE)),
    ("blog", re.compile(r"/(?:blog|blogs|journal|stories|articles?)(?:/|$)", re.IGNORECASE)),
    ("press", re.compile(r"/(?:press|news|media|in-the-news)(?:/|$)", re.IGNORECASE)),
    ("about", re.compile(r"/(?:about|our-story|story|mission|values|team)(?:/|$)", re.IGNORECASE)),
    ("contact", re.compile(r"/(?:contact|locations|stores|find-us)(?:/|$)", re.IGNORECASE)),
    ("faq", re.compile(r"/(?:faq|help|support|customer-care)(?:/|$)", re.IGNORECASE)),
    ("policies", re.compile(r"/(?:policies|terms|privacy|shipping|returns)(?:/|$)", re.IGNORECASE)),
    ("careers", re.compile(r"/(?:careers|jobs|join-us)(?:/|$)", re.IGNORECASE)),
    ("retail", re.compile(r"/(?:wholesale|retailers|partners|stockists)(?:/|$)", re.IGNORECASE)),
]


async def _fetch_sitemap_urls(base_url: str) -> Tuple[List[str], Optional[str]]:
    """Fetch /sitemap.xml. Follows sitemap-index nesting one level deep
    (Shopify uses an index pointing to sitemap_products_*.xml etc).
    Returns (urls, error_reason)."""
    sitemap_url = base_url.rstrip("/") + "/sitemap.xml"
    try:
        async with httpx.AsyncClient(
            timeout=_SITEMAP_FETCH_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            r = await client.get(
                sitemap_url,
                headers={"User-Agent": "Pivota-BD-Audit/1.0"},
            )
            if r.status_code != 200:
                return [], f"sitemap returned {r.status_code}"
            content = r.content[:_SITEMAP_MAX_BYTES]
            urls = _parse_sitemap_xml(content)
            # If the root is a sitemap index, fetch up to 5 child sitemaps
            # to get a representative URL sample. (Don't fetch all — some
            # brands have 50+ child sitemaps.) Shopify child sitemap URLs
            # include query params (...sitemap_products_1.xml?from=...&to=...)
            # so match on the path component, not the bare extension.
            def _is_child_sitemap(u: str) -> bool:
                try:
                    p = urlparse(u).path or ""
                except Exception:  # noqa: BLE001
                    return False
                return p.lower().endswith(".xml") and "sitemap" in p.lower()
            child_indexes = [u for u in urls if _is_child_sitemap(u)]
            if child_indexes:
                fetch_count = min(5, len(child_indexes))
                results = await asyncio.gather(
                    *[_fetch_one_sitemap(client, u) for u in child_indexes[:fetch_count]],
                    return_exceptions=True,
                )
                aggregated = [u for u in urls if not _is_child_sitemap(u)]
                for res in results:
                    if isinstance(res, list):
                        aggregated.extend(res)
                urls = aggregated
            return urls, None
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        return [], f"fetch error: {exc.__class__.__name__}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("sitemap parse error for %s: %s", sitemap_url, exc)
        return [], f"parse error: {exc.__class__.__name__}"


async def _fetch_one_sitemap(client: httpx.AsyncClient, url: str) -> List[str]:
    try:
        r = await client.get(
            url, headers={"User-Agent": "Pivota-BD-Audit/1.0"},
        )
        if r.status_code != 200:
            return []
        return _parse_sitemap_xml(r.content[:_SITEMAP_MAX_BYTES])
    except Exception:  # noqa: BLE001
        return []


def _parse_sitemap_xml(content: bytes) -> List[str]:
    """Pull all <loc> URLs out of a sitemap XML. Tolerant of namespace
    prefixes / malformed XML — falls back to regex if ElementTree fails.
    """
    try:
        root = ET.fromstring(content)
        # Strip namespace from tag names for case-insensitive matching.
        urls: List[str] = []
        for loc in root.iter():
            if loc.tag.split("}")[-1].lower() == "loc" and loc.text:
                urls.append(loc.text.strip())
        return urls
    except ET.ParseError:
        return [
            m.decode("utf-8", errors="replace").strip()
            for m in re.findall(rb"<loc>([^<]+)</loc>", content)
        ]


def _classify_sitemap_urls(urls: List[str]) -> Dict[str, Any]:
    """Bucket sitemap URLs by path-prefix pattern. Returns counts +
    total + sample URLs per non-empty bucket (up to 3 each, useful for
    debugging). 'other' catches paths not matching any bucket."""
    buckets: Dict[str, List[str]] = {name: [] for name, _ in _PATH_BUCKETS}
    buckets["other"] = []
    for url in urls:
        path = ""
        try:
            path = urlparse(url).path or ""
        except Exception:  # noqa: BLE001
            continue
        matched = False
        for name, pattern in _PATH_BUCKETS:
            if pattern.search(path):
                buckets[name].append(url)
                matched = True
                break
        if not matched:
            buckets["other"].append(url)
    out: Dict[str, Any] = {"total_urls": len(urls)}
    for name, items in buckets.items():
        out[name] = {
            "count": len(items),
            "sample_urls": items[:3],
        }
    return out


# ---------------------------------------------------------------------------
# robots.txt directive extraction (extends _robots_allows from
# brand_product_discovery; that one only returns a bool)
# ---------------------------------------------------------------------------

_ROBOTS_FETCH_TIMEOUT_S = 5.0


async def _fetch_robots_txt(base_url: str) -> Optional[str]:
    """Best-effort fetch of /robots.txt. Returns text content or None."""
    url = base_url.rstrip("/") + "/robots.txt"
    try:
        async with httpx.AsyncClient(
            timeout=_ROBOTS_FETCH_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            r = await client.get(
                url, headers={"User-Agent": "Pivota-BD-Audit/1.0"},
            )
            if r.status_code != 200:
                return None
            return r.text
    except (httpx.TimeoutException, httpx.RequestError):
        return None


async def _fetch_homepage_html(domain: str) -> Optional[str]:
    """Best-effort fetch of a merchant's homepage HTML, used as the
    handle-detection fallback for social intelligence.

    The cold-start flow already has homepage HTML in hand (it fetches
    it for the whole brand-signals extraction). But `run_brand_report`
    — the main audit path — doesn't, so it passed `detected_handles=None`
    into `infer_social_intelligence` and the social probes never had a
    handle to anchor on. The PR-8 prod run showed both TikTok +
    Instagram own_presence entries with handle:null as a result.

    This helper lets `infer_social_intelligence` self-serve a homepage
    fetch when no handles were threaded in. One cheap HTTP GET (not a
    Gemini call); failure returns None and the social probes fall back
    to "search for the brand's account" prompting as before.
    """
    if not domain or not domain.strip():
        return None
    d = domain.strip()
    if not d.startswith(("http://", "https://")):
        d = "https://" + d
    try:
        async with httpx.AsyncClient(
            timeout=_HOMEPAGE_FETCH_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            r = await client.get(
                d, headers={"User-Agent": "Pivota-BD-Audit/1.0"},
            )
            if r.status_code != 200:
                return None
            # Cap the body — only the <head> meta + nav/footer social
            # links matter for handle extraction; no need to hold a
            # multi-MB product-grid homepage in memory.
            return r.text[:_HOMEPAGE_MAX_BYTES]
    except (httpx.TimeoutException, httpx.RequestError):
        return None


def _extract_robots_directives(robots_txt: Optional[str]) -> Dict[str, Any]:
    """Surface raw directive counts + sitemap declarations + whether
    crawl is restrictive (any User-agent: * Disallow: /)."""
    if not robots_txt:
        return {
            "present": False,
            "user_agent_groups": 0,
            "disallow_count": 0,
            "sitemaps_declared": [],
            "blocks_all_crawlers": False,
        }
    lines = [ln.strip() for ln in robots_txt.splitlines()]
    sitemaps = [
        ln.split(":", 1)[1].strip()
        for ln in lines
        if ln.lower().startswith("sitemap:")
    ]
    disallow_count = sum(1 for ln in lines if ln.lower().startswith("disallow:"))
    user_agents = sum(1 for ln in lines if ln.lower().startswith("user-agent:"))
    # blocks_all_crawlers: any block with `User-agent: *` followed by
    # `Disallow: /` (with nothing after).
    blocks_all = False
    in_star_group = False
    for ln in lines:
        low = ln.lower()
        if low.startswith("user-agent:"):
            in_star_group = low.split(":", 1)[1].strip() == "*"
        elif in_star_group and low == "disallow: /":
            blocks_all = True
            break
    return {
        "present": True,
        "user_agent_groups": user_agents,
        "disallow_count": disallow_count,
        "sitemaps_declared": sitemaps,
        "blocks_all_crawlers": blocks_all,
    }


# ---------------------------------------------------------------------------
# SEO completeness scoring
# ---------------------------------------------------------------------------


def _score_seo_completeness(
    open_graph: Dict[str, Any],
    jsonld_count: int,
    robots: Dict[str, Any],
    sitemap_url_count: int,
) -> Dict[str, Any]:
    """Roll boolean signals into a 0.0-1.0 score with a per-signal
    breakdown the UI can render. Missing-but-fixable signals are the
    leverage points BD can pitch ('your competitors have this; you
    don't')."""
    signals = {
        "og_title_present": bool(open_graph.get("og_title")),
        "og_description_present": bool(open_graph.get("og_description")),
        "og_image_present": bool(open_graph.get("og_image")),
        "twitter_card_present": bool(open_graph.get("twitter_card")),
        "meta_description_present": bool(open_graph.get("meta_description")),
        "json_ld_blocks_present": jsonld_count > 0,
        "robots_txt_present": bool(robots.get("present")),
        "sitemap_present": sitemap_url_count > 0,
        "sitemap_declared_in_robots": bool(robots.get("sitemaps_declared")),
        "not_blocking_all_crawlers": not robots.get("blocks_all_crawlers", False),
    }
    score = sum(1 for v in signals.values() if v) / len(signals)
    return {
        "score": round(score, 2),
        "signals": signals,
        "missing_signals": [k for k, v in signals.items() if not v],
    }


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


async def collect_brand_signals(
    homepage_html: str,
    domain: str,
    base_url: str,
) -> Dict[str, Any]:
    """Extract the full brand-signals payload. Pure-parsing extractors
    run synchronously over the already-fetched homepage HTML; sitemap
    + robots fetches run in parallel.

    Returns a dict with keys:
      branding, schema_org_organization, aggregate_rating, social,
      sitemap_structure, robots, seo_signals.

    Sub-fields are None / empty when the corresponding signal isn't
    present — never fabricated.
    """
    # Pure parsers — no I/O.
    open_graph = _extract_open_graph(homepage_html)
    jsonld_blocks = _parse_jsonld_blocks(homepage_html)
    jsonld_items = _flatten_jsonld(jsonld_blocks)
    schema_org_org = _extract_schema_org_organization(jsonld_items)
    aggregate_rating = _extract_aggregate_rating(jsonld_items)
    social = _extract_social_handles(homepage_html)
    # If schema.org Organization carried sameAs links, fold those in
    # too — sometimes the homepage HTML doesn't link footer social
    # icons but the JSON-LD does.
    if schema_org_org and schema_org_org.get("same_as"):
        for url in schema_org_org["same_as"]:
            for platform, pattern in _SOCIAL_PATTERNS:
                m = pattern.search(url)
                if m:
                    handle = m.group(1).strip().rstrip(".")
                    if handle and handle.lower() not in _SOCIAL_HANDLE_BLACKLIST:
                        # Avoid dupes against already-found social.
                        already = any(
                            s["platform"] == platform and s["handle"].lower() == handle.lower()
                            for s in social
                        )
                        if not already:
                            social.append({
                                "platform": platform,
                                "handle": handle,
                                "url": _build_social_url(platform, handle),
                            })
                    break

    # Parallel I/O — sitemap + robots.
    sitemap_urls, sitemap_error = [], None
    robots_txt = None
    try:
        results = await asyncio.gather(
            _fetch_sitemap_urls(base_url),
            _fetch_robots_txt(base_url),
        )
        (sitemap_urls, sitemap_error), robots_txt = results
    except Exception as exc:  # noqa: BLE001
        logger.warning("brand_signals I/O error for %s: %s", domain, exc)

    sitemap_structure = _classify_sitemap_urls(sitemap_urls) if sitemap_urls else None
    if sitemap_error and sitemap_structure is None:
        sitemap_structure = {"error": sitemap_error, "total_urls": 0}

    robots = _extract_robots_directives(robots_txt)
    seo_signals = _score_seo_completeness(
        open_graph,
        jsonld_count=len(jsonld_items),
        robots=robots,
        sitemap_url_count=len(sitemap_urls),
    )

    return {
        "branding": {
            "tagline": open_graph.get("og_description") or open_graph.get("meta_description"),
            "logo_url": open_graph.get("og_image") or (
                schema_org_org.get("logo") if schema_org_org else None
            ),
            "site_name": open_graph.get("og_site_name"),
            "twitter_image": open_graph.get("twitter_image"),
        },
        "schema_org_organization": schema_org_org,
        "aggregate_rating": aggregate_rating,
        "social": social,
        "sitemap_structure": sitemap_structure,
        "robots": robots,
        "seo_signals": seo_signals,
    }


# ---------------------------------------------------------------------------
# PR-C: Gemini-backed brand context (retail / founder / press)
# ---------------------------------------------------------------------------


def _resolve_gemini_api_key() -> Optional[str]:
    for var in ("GEMINI_API_KEY", "PIVOTA_GEMINI_API_KEY"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


def _parse_gemini_text(payload: Dict[str, Any]) -> Optional[str]:
    """Extract concatenated text from Gemini response. Mirrors the
    pattern used in gemini_url_validator.py."""
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    chunks: List[str] = []
    for p in parts:
        t = p.get("text")
        if isinstance(t, str) and t.strip():
            chunks.append(t)
    return "\n".join(chunks).strip() or None


def _unwrap_json(text: Optional[str]) -> Optional[Any]:
    """Try to parse JSON from a Gemini response. Strips ``` fences and
    falls back to extracting the first balanced { or [ block."""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Pick whichever opener appears first — otherwise we'd grab the
        # object nested inside an array and lose the array wrapper.
        i_obj = s.find("{")
        i_arr = s.find("[")
        candidates: List[Tuple[int, str, str]] = []
        if i_obj >= 0:
            candidates.append((i_obj, "{", "}"))
        if i_arr >= 0:
            candidates.append((i_arr, "[", "]"))
        candidates.sort()
        for i, opener, closer in candidates:
            depth = 0
            for j in range(i, len(s)):
                if s[j] == opener:
                    depth += 1
                elif s[j] == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(s[i:j + 1])
                        except json.JSONDecodeError:
                            break
        return None


def _coerce_to_array(parsed: Any) -> Optional[List[Any]]:
    """When a Gemini call should have returned an array but might have
    returned an object wrapper or a single object, recover the array.

    Handles three cases:
      1. Already a list → return as-is
      2. Object with a single list-valued field → return that list
         (e.g. {"creators": [...]} or {"results": [...]})
      3. Object that looks like a single item → wrap in a list
         (e.g. Gemini returned one retailer instead of an array of one)
      4. None or other → return None
    """
    if parsed is None:
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # Case 2: single list-valued field — common wrappers Gemini emits
        list_values = [v for v in parsed.values() if isinstance(v, list)]
        if len(list_values) == 1:
            return list_values[0]
        # Case 3: looks like a single item (has any of the expected
        # field names from our schemas) — wrap in [obj].
        item_indicator_fields = {
            "retailer", "publication", "creator_handle", "brand",
            "headline", "url", "name",
        }
        if any(k in parsed for k in item_indicator_fields):
            return [parsed]
    return None


async def _gemini_extract_call(
    prompt: str,
    *,
    api_key: str,
    scan_mode: str = "bd_extract",
    model: str = _GEMINI_SOCIAL_MODEL,
) -> Optional[Dict[str, Any]]:
    """Plain (non-grounded) Gemini completion — the EXTRACTION step of the
    search-then-extract pipeline. Used by all 7 bd_ probes.

    Transport handling: one bounded retry on `_GEMINI_TRANSPORT_RETRY_EXCS`
    + `_GEMINI_TIMEOUT`; telemetry via `_record_bd_telemetry`. Two
    deliberate things to know:

      - NO `google_search` tool — retrieval already happened (SerpAPI);
        this call only extracts structured JSON from snippets the caller
        embedded in `prompt`.
      - `maxOutputTokens` raised to 4096 — without google_search query
        spam eating the budget, this is generous, and it fixes the
        `_infer_competitive_social` `finish=MAX_TOKENS` bug.

    Crucially this NEVER records `error_message="ungrounded"` —
    groundedness is decided upstream by whether the SerpAPI search
    returned results, not by `groundingMetadata`.
    """
    import time as _time
    _telemetry_started_at = _time.perf_counter()
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
    }
    url = vertex_gemini.generate_content_url(model)
    headers = await vertex_gemini.auth_headers(api_key)
    r = None
    last_exc: Optional[BaseException] = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=_GEMINI_TIMEOUT) as client:
                r = await client.post(url, headers=headers, json=body)
            break
        except _GEMINI_TRANSPORT_RETRY_EXCS as exc:
            last_exc = exc
            logger.warning(
                "bd_extract: transport error (attempt %s/2) scan_mode=%s: %s",
                attempt, scan_mode, exc,
            )
            if attempt == 2:
                break
            await asyncio.sleep(_GEMINI_RETRY_BACKOFF_S)
        except httpx.RequestError as exc:
            last_exc = exc
            logger.warning("bd_extract: HTTP error: %s", exc)
            break
    if r is None:
        await _record_bd_telemetry(
            scan_mode=scan_mode, status="failed",
            started_at_perf=_telemetry_started_at,
            error_message=(
                f"transport: {type(last_exc).__name__}"
                if last_exc is not None else "transport: unknown"
            ),
        )
        return None
    if r.status_code != 200:
        logger.warning("bd_extract: non-200 %s body=%s", r.status_code, r.text[:300])
        await _record_bd_telemetry(
            scan_mode=scan_mode,
            status="rate_limited" if r.status_code == 429 else "failed",
            started_at_perf=_telemetry_started_at,
            error_message=f"http_{r.status_code}",
        )
        return None
    try:
        result = r.json()
    except json.JSONDecodeError:
        await _record_bd_telemetry(
            scan_mode=scan_mode, status="failed",
            started_at_perf=_telemetry_started_at,
            error_message="non_json_response",
        )
        return None
    await _record_bd_telemetry(
        scan_mode=scan_mode, status="succeeded",
        started_at_perf=_telemetry_started_at,
        error_message=None,
    )
    return result


async def _record_bd_telemetry(
    *,
    scan_mode: str,
    status: str,
    started_at_perf: float,
    error_message: Optional[str] = None,
) -> None:
    """P2.5b: best-effort telemetry record for one BD grounded call.
    Gemini REST API doesn't return token usage, so input/output
    tokens + cost are None — the row still captures latency + status
    for per-merchant call-count accounting."""
    try:
        import time as _time
        from db.llm_probe_runs import record_probe_run
        from services.audit_telemetry_context import (
            current_audit_context,
        )
        ctx = current_audit_context()
        latency_ms = int(
            (_time.perf_counter() - started_at_perf) * 1000.0,
        )
        await record_probe_run(
            provider="gemini",
            scan_mode=scan_mode,
            status=status,
            merchant_id=ctx.merchant_id,
            audit_run_id=ctx.audit_run_id,
            latency_ms=latency_ms,
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            error_message=error_message,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "_record_bd_telemetry suppressed error: %s",
            str(exc)[:200],
        )


# HISTORY — Gemini `google_search` grounding (`_gemini_grounded_call` +
# `_grounding_chunk_count`) was the original retrieval path for all 7 bd_
# probes. Two attempts to make it work in-place (PR #530's prompt
# escalation + ungrounded retry, 0/13 recovery; PR #531/#533's model-tier
# swaps, ~7-15% grounded flat) both failed. A 2026-05-14 diagnostic proved
# the model searches every call but Gemini's `google_search` *retrieval*
# returns 0 chunks ~75% of the time. PRs #535 (social) + #536 (non-social)
# replaced it with deterministic SerpAPI retrieval + `_gemini_extract_call`.
# `_gemini_grounded_call`/`_grounding_chunk_count`/`_GROUNDING_DIRECTIVE`
# were deleted in the follow-up cleanup. Full write-up:
# ~/tmp/social-grounding-check/verdict.md.


def _build_retail_prompt(
    brand: str, domain: str, search_results: List[Dict[str, str]],
) -> str:
    return f"""You are a retail-distribution analyst. From the search results below, extract up to 8 online retailers (department stores, marketplaces, beauty/category specialists) that currently sell products from {brand} ({domain}).

{_EXTRACTION_DIRECTIVE}

SEARCH RESULTS:
{_format_search_results(search_results)}

For each retailer give:
- retailer: the retailer name (e.g. "Sephora", "Amazon", "Target")
- url: the brand's landing or product page on that retailer site, if present in the results
- confidence: "high" (named explicitly in the results), "medium" (referenced indirectly), or "low"

OUTPUT FORMAT — strict:
- Reply with a bare JSON array starting with [ and ending with ]
- Do NOT wrap in markdown fences (```)
- Do NOT wrap in an outer object like {{"retailers": [...]}}
- Do NOT add prose before or after
- If the search results name no retailers, reply with the literal: []

Schema per element:
{{"retailer": "...", "url": "...", "confidence": "high"}}"""


def _build_founder_prompt(
    brand: str, domain: str, search_results: List[Dict[str, str]],
) -> str:
    return f"""You are a brand-research analyst. From the search results below, extract founder + founding context for {brand} ({domain}).

{_EXTRACTION_DIRECTIVE}

SEARCH RESULTS:
{_format_search_results(search_results)}

Reply with ONLY a JSON object. No prose:

{{
  "founders": ["Name 1", "Name 2"],   // founder names from the results; empty array if unknown
  "founding_year": 2018,              // integer from the results; null if unknown
  "origin_story": "One-to-two sentence origin grounded in the results: what gap they saw, who started it, why."
}}

If the search results don't establish these, return {{"founders": [], "founding_year": null, "origin_story": null}}."""


def _build_press_prompt(
    brand: str, domain: str, search_results: List[Dict[str, str]],
) -> str:
    return f"""You are a media-monitoring analyst. From the search results below, extract up to 6 press articles, editorial features, or notable mentions of {brand} ({domain}) from the last 12 months.

{_EXTRACTION_DIRECTIVE}

SEARCH RESULTS:
{_format_search_results(search_results)}

For each:
- publication: name (e.g. "NYMag Strategist", "Forbes Vetted")
- headline: article headline
- url: article URL (from the search results)
- date: publication date (ISO format YYYY-MM-DD if known, else year)

OUTPUT FORMAT — strict:
- Reply with a bare JSON array starting with [ and ending with ]
- Do NOT wrap in markdown fences (```)
- Do NOT wrap in an outer object like {{"press": [...]}}
- Do NOT add prose before or after
- If the search results contain no coverage, reply with the literal: []

Schema per element:
{{"publication": "...", "headline": "...", "url": "...", "date": "YYYY-MM-DD"}}"""


async def _infer_retail_presence(brand: str, domain: str, api_key: str) -> Optional[List[Dict[str, Any]]]:
    payload, search_status = await _search_then_extract(
        queries=_retail_queries(brand, domain),
        extract_prompt_fn=lambda r: _build_retail_prompt(brand, domain, r),
        scan_mode="bd_retail_presence",
        api_key=api_key,
    )
    if search_status != _SEARCH_OK or not payload:
        # search empty OR transport-failed → honest empty, never a fabricated
        # retailer list. Downstream `(brand_context or {}).get(...)` handles None.
        return None
    raw_text = _parse_gemini_text(payload)
    parsed = _coerce_to_array(_unwrap_json(raw_text))
    if parsed is None:
        logger.info("retail_presence: could not coerce response to array; raw=%r", (raw_text or "")[:300])
        return None
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        retailer = item.get("retailer")
        if not isinstance(retailer, str) or not retailer.strip():
            continue
        confidence = item.get("confidence")
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        out.append({
            "retailer": retailer.strip(),
            "url": item.get("url") if isinstance(item.get("url"), str) else None,
            "confidence": confidence,
        })
    return out


async def _infer_founder_story(brand: str, domain: str, api_key: str) -> Optional[Dict[str, Any]]:
    payload, search_status = await _search_then_extract(
        queries=_founder_queries(brand, domain),
        extract_prompt_fn=lambda r: _build_founder_prompt(brand, domain, r),
        scan_mode="bd_founder_story",
        api_key=api_key,
    )
    if search_status != _SEARCH_OK or not payload:
        return None
    parsed = _unwrap_json(_parse_gemini_text(payload))
    if not isinstance(parsed, dict):
        return None
    founders_raw = parsed.get("founders")
    founders = (
        [f for f in founders_raw if isinstance(f, str) and f.strip()]
        if isinstance(founders_raw, list) else []
    )
    year = parsed.get("founding_year")
    if not isinstance(year, int):
        year = None
    origin = parsed.get("origin_story")
    if not isinstance(origin, str) or not origin.strip():
        origin = None
    if not founders and not year and not origin:
        return None
    return {
        "founders": founders,
        "founding_year": year,
        "origin_story": origin,
    }


def _build_corporate_prompt(
    brand: str, domain: str, search_results: List[Dict[str, str]],
) -> str:
    """PR-7a corporate intel prompt — extraction variant. Reads ownership +
    funding signals from the SerpAPI search results below, with strict null
    defaults to prevent fabrication of corporate facts. Acquisition signals
    must come from explicit text in the results (press release / SEC
    filing), never from inference; the downstream hallucination-defense
    layer additionally nulls low-confidence acquired/subsidiary claims."""
    return (
        f"Research the corporate ownership and funding status of "
        f"the brand '{brand}' (website: {domain}) from the search "
        f"results below.\n\n"
        f"{_EXTRACTION_DIRECTIVE}\n\n"
        f"SEARCH RESULTS:\n{_format_search_results(search_results)}\n\n"
        f"Respond ONLY with a JSON object of this exact shape:\n"
        f'{{\n'
        f'  "ownership_status": "independent" | "acquired" | '
        f'"public" | "subsidiary" | null,\n'
        f'  "parent_company": "<name>" | null,\n'
        f'  "parent_acquisition_year": <integer> | null,\n'
        f'  "funding_stage": "bootstrapped" | "seed" | "series_a" | '
        f'"series_b" | "series_c" | "series_d_plus" | "ipo" | "pe_owned" | null,\n'
        f'  "valuation_band_usd": "under_10m" | "10m_to_100m" | '
        f'"100m_to_1b" | "1b_plus" | null,\n'
        f'  "confidence": "high" | "medium" | "low"\n'
        f'}}\n'
        f"Use null for any field not supported by the search results. "
        f"Acquisition signals MUST be explicitly stated in the results "
        f"(press release / SEC filing) — do not infer from tone or "
        f"category. Set confidence='low' if any field required a "
        f"non-trivial inference from the snippets."
    )


async def _infer_corporate_intel(
    brand: str, domain: str, api_key: str,
) -> Optional[Dict[str, Any]]:
    """PR-7a: surface corporate ownership / funding signals (e.g.
    'Grüns is owned by Unilever'). Single grounded probe; conservative
    null defaults; rejects responses with confidence='low' on
    ownership_status to defend against hallucinated acquisitions.
    """
    payload, search_status = await _search_then_extract(
        queries=_corporate_queries(brand, domain),
        extract_prompt_fn=lambda r: _build_corporate_prompt(brand, domain, r),
        scan_mode="bd_corporate_intel",
        api_key=api_key,
    )
    if search_status != _SEARCH_OK or not payload:
        # search empty / transport-failed → honest empty. This is the
        # ONLY non-social field actively rendered to merchants (executive
        # summary editorial framing), so the suppress-on-ungrounded
        # discipline matters most here.
        return None
    parsed = _unwrap_json(_parse_gemini_text(payload))
    if not isinstance(parsed, dict):
        return None

    confidence = parsed.get("confidence")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    ownership_status = parsed.get("ownership_status")
    if ownership_status not in (
        "independent", "acquired", "public", "subsidiary",
    ):
        ownership_status = None

    # Hallucination defense: if model claims acquired/subsidiary at
    # low confidence, downgrade to null. Acquisition claims are
    # high-stakes — better to omit than to fabricate.
    if (
        ownership_status in ("acquired", "subsidiary")
        and confidence == "low"
    ):
        ownership_status = None

    parent = parsed.get("parent_company")
    if not isinstance(parent, str) or not parent.strip():
        parent = None
    elif ownership_status not in ("acquired", "subsidiary"):
        # Parent only meaningful when ownership_status confirms it
        parent = None

    acq_year = parsed.get("parent_acquisition_year")
    if not isinstance(acq_year, int) or acq_year < 1900 or acq_year > 2100:
        acq_year = None

    funding_stage = parsed.get("funding_stage")
    if funding_stage not in (
        "bootstrapped", "seed", "series_a", "series_b",
        "series_c", "series_d_plus", "ipo", "pe_owned",
    ):
        funding_stage = None

    valuation_band = parsed.get("valuation_band_usd")
    if valuation_band not in (
        "under_10m", "10m_to_100m", "100m_to_1b", "1b_plus",
    ):
        valuation_band = None

    # If everything is null, return None so downstream renderers
    # know we don't have corporate intel for this brand
    if not any([
        ownership_status, parent, acq_year, funding_stage,
        valuation_band,
    ]):
        return None

    return {
        "ownership_status": ownership_status,
        "parent_company": parent,
        "parent_acquisition_year": acq_year,
        "funding_stage": funding_stage,
        "valuation_band_usd": valuation_band,
        "confidence": confidence,
    }


async def _infer_press_coverage(brand: str, domain: str, api_key: str) -> Optional[List[Dict[str, Any]]]:
    payload, search_status = await _search_then_extract(
        queries=_press_queries(brand, domain),
        extract_prompt_fn=lambda r: _build_press_prompt(brand, domain, r),
        scan_mode="bd_press_coverage",
        api_key=api_key,
    )
    if search_status != _SEARCH_OK or not payload:
        # search empty / transport-failed → honest empty. (Diagnostic also
        # showed 3/3 hit MAX_TOKENS pre-rework; the extraction-path 4096
        # token budget side-fixes that.)
        return None
    raw_text = _parse_gemini_text(payload)
    parsed = _coerce_to_array(_unwrap_json(raw_text))
    if parsed is None:
        logger.info("press_coverage: could not coerce response to array; raw=%r", (raw_text or "")[:300])
        return None
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        pub = item.get("publication")
        head = item.get("headline")
        if not (isinstance(pub, str) and pub.strip() and isinstance(head, str) and head.strip()):
            continue
        out.append({
            "publication": pub.strip(),
            "headline": head.strip(),
            "url": item.get("url") if isinstance(item.get("url"), str) else None,
            "date": item.get("date") if isinstance(item.get("date"), str) else None,
        })
    return out


async def infer_brand_context(brand: str, domain: str) -> Dict[str, Any]:
    """Four Gemini grounded queries in parallel — retail presence,
    founder story, press coverage, **corporate intel (PR-7a)**.
    Each fails gracefully (None / empty) so the caller can render
    "data not available" instead of fabricating.

    Returns:
      {
        "retail_presence": [{retailer, url, confidence}, ...] | null,
        "founder_story": {founders, founding_year, origin_story} | null,
        "press_coverage": [{publication, headline, url, date}, ...] | null,
        "corporate": {
          "ownership_status", "parent_company",
          "parent_acquisition_year", "funding_stage",
          "valuation_band_usd", "confidence"
        } | null,                                                # PR-7a
        "available": bool,  # at least one query succeeded
      }
    """
    api_key = _resolve_gemini_api_key()
    if not vertex_gemini.credentials_available(api_key):
        logger.info("brand_context: no Gemini credentials; skipping inference for %s", brand)
        return {
            "retail_presence": None,
            "founder_story": None,
            "press_coverage": None,
            "corporate": None,
            "available": False,
        }

    if not brand or not brand.strip() or not domain or not domain.strip():
        return {
            "retail_presence": None,
            "founder_story": None,
            "press_coverage": None,
            "corporate": None,
            "available": False,
        }

    results = await asyncio.gather(
        _infer_retail_presence(brand, domain, api_key),
        _infer_founder_story(brand, domain, api_key),
        _infer_press_coverage(brand, domain, api_key),
        _infer_corporate_intel(brand, domain, api_key),    # PR-7a
        return_exceptions=True,
    )

    def _safe(r: Any) -> Any:
        return r if not isinstance(r, Exception) else None

    retail = _safe(results[0])
    founder = _safe(results[1])
    press = _safe(results[2])
    corporate = _safe(results[3])

    return {
        "retail_presence": retail,
        "founder_story": founder,
        "press_coverage": press,
        "corporate": corporate,
        "available": any(x is not None for x in (retail, founder, press, corporate)),
    }


# ---------------------------------------------------------------------------
# PR-D: Social-media intelligence (TikTok + Instagram)
# ---------------------------------------------------------------------------
#
# Brands prioritize TikTok + Instagram presence when evaluating
# distribution partners. We surface:
#   1. Brand's own reach per platform (followers, view-per-post,
#      content focus)
#   2. KOL/creator endorsements per platform (top creators in 10k-1M
#      band who posted about the brand in the last 12 months)
#   3. Competitive social comparison vs category competitor brands
#      (gated on competitor list — caller passes audit's
#      category_competitor_brands when available)
#
# Why Gemini grounded vs paid social-intel APIs (Modash / HypeAuditor /
# Tubular): paid APIs cost $300-10k/mo and need procurement. Gemini
# grounded gives qualitative answers good enough for a BD pitch
# artifact (creator names + competitive position reliable; exact
# follower counts ±20%). UI tags this section "AI-grounded estimates ·
# verify before pitching" so BD operators don't take counts as gospel.
# Upgrade path documented in plan: wire each paid API as alternative
# when configured; fall back to Gemini when not.


def _detected_handle(detected: List[Dict[str, str]], platform: str) -> Optional[str]:
    """Look up the brand's own handle for a platform from PR-B's
    _extract_social_handles output. Returns None if not detected."""
    for entry in detected or []:
        if entry.get("platform") == platform:
            return entry.get("handle")
    return None


# Extraction directive — the social prompts now embed REAL search-result
# snippets (SerpAPI) and ask the model only to EXTRACT structured data
# from them. With search-then-extract the fabrication surface moves from
# "did the model consult the web" to "did the model invent beyond the
# snippets we gave it" — this directive is the guard for that.
_EXTRACTION_DIRECTIVE = (
    "Below are real web search results. Base every figure ONLY on what "
    "these search results actually state. Do NOT use prior knowledge or "
    "memory. If a field is not supported by the search results, set it "
    "to null — never guess or estimate."
)


def _format_search_results(results: List[Dict[str, str]]) -> str:
    """Render normalized SerpAPI results as a numbered context block for
    an extraction prompt."""
    lines: List[str] = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        lines.append(f"[{i}] {title}\n    {url}\n    {snippet}")
    return "\n".join(lines) if lines else "(no search results)"


# ---------------------------------------------------------------------------
# Search query builders — deterministic + bounded. The 2026-05-14 diagnostic
# showed Gemini's own search queries were essentially these templates with
# the brand/handle/platform slotted in, so we template them directly: no
# extra LLM call for query generation, fully testable.
# ---------------------------------------------------------------------------

def _own_presence_queries(
    brand: str, platform: str, handle: Optional[str],
) -> List[str]:
    label = "TikTok" if platform == "tiktok" else "Instagram"
    queries = [
        f"{brand} {label} followers",
        f"{brand} official {label} account",
    ]
    if handle:
        queries.append(f"@{handle} {label} follower count")
    return queries[:3]


def _kol_queries(brand: str, platform: str) -> List[str]:
    label = "TikTok" if platform == "tiktok" else "Instagram"
    return [
        f"{label} creators who posted about {brand}",
        f"{brand} {label} influencer review",
        f"{brand} {label} sponsored creator post",
    ][:3]


def _competitive_queries(brand: str, competitors: List[str]) -> List[str]:
    return [
        f"{comp} TikTok Instagram followers" for comp in competitors[:4]
    ]


# Query builders for the 4 non-social bd_ probes (retail / founder / corporate /
# press). Mirror what Gemini's google_search actually issued in the 2026-05-14
# non-social diagnostic — the search-then-extract replacements for the
# discretionary grounding path that returned 0 chunks 9/12 times (see HISTORY
# block above `_gemini_extract_call`).
def _retail_queries(brand: str, domain: str) -> List[str]:
    return [
        f"where to buy {brand}",
        f"{brand} retailers",
        f"{brand} sephora ulta amazon target",
    ][:3]


def _founder_queries(brand: str, domain: str) -> List[str]:
    return [
        f"{brand} founder",
        f"{brand} founding year origin story",
        f"who started {brand} {domain}",
    ][:3]


def _corporate_queries(brand: str, domain: str) -> List[str]:
    return [
        f"{brand} parent company",
        f"{brand} acquired by",
        f"{brand} funding round valuation",
    ][:3]


def _press_queries(brand: str, domain: str) -> List[str]:
    return [
        f"{brand} press coverage 2025 2026",
        f"{brand} editorial feature review",
        f"{brand} forbes nymag bustle vogue",
    ][:3]


def _build_own_presence_prompt(
    brand: str, platform: str, handle: Optional[str],
    search_results: List[Dict[str, str]],
) -> str:
    handle_clause = (
        f"Their {platform} handle is @{handle}." if handle
        else f"Identify {brand}'s official {platform} account from the results."
    )
    label = "TikTok" if platform == "tiktok" else "Instagram"
    metric_hint = (
        '"view_per_post_estimate"' if platform == "tiktok"
        else '"engagement_rate_estimate"'
    )
    return f"""You are a social-media analyst. Report {brand}'s own {label} reach.

{_EXTRACTION_DIRECTIVE}

{handle_clause}

SEARCH RESULTS:
{_format_search_results(search_results)}

OUTPUT FORMAT — strict:
- Reply with a bare JSON object starting with {{ and ending with }}
- Do NOT wrap in markdown fences (```)
- Do NOT add prose before or after
- Do NOT wrap in an outer object like {{"data": {{...}}}}
- Use null for any field not supported by the search results above (don't guess)

Schema:
{{
  "follower_estimate": 12000,
  "follower_band": "10k-100k",
  {metric_hint}: 5000,
  "content_focus": "product demos and lifestyle",
  "post_frequency": "3-4 per week",
  "verified_account": true
}}

Allowed values for follower_band: "<10k", "10k-100k", "100k-1M", "1M-10M", ">10M", or null."""


def _build_kol_prompt(
    brand: str, platform: str, search_results: List[Dict[str, str]],
) -> str:
    label = "TikTok" if platform == "tiktok" else "Instagram"
    return f"""You are a creator-marketing analyst. From the search results below, list up to 8 {label} creators (10k-1M follower band) who have posted about {brand} in the last 12 months.

{_EXTRACTION_DIRECTIVE}

SEARCH RESULTS:
{_format_search_results(search_results)}

For each creator give:
- creator_handle: the @ handle
- follower_band: one of "10k-100k", "100k-1M", "<10k", ">1M"
- post_url: link to the specific post if present in the results, else null
- view_count_estimate: rough view count, integer or null
- post_date: ISO date YYYY-MM-DD if known, else year, else null
- content_summary: one short sentence about the post

OUTPUT FORMAT — strict:
- Reply with a bare JSON array starting with [ and ending with ]
- Do NOT wrap in markdown fences (```)
- Do NOT wrap in an outer object like {{"creators": [...]}}
- Do NOT add prose before or after
- If the search results name no creators, reply with the literal: []

Schema per element:
{{"creator_handle": "...", "follower_band": "10k-100k", "post_url": null, "view_count_estimate": 12000, "post_date": "2025-01-15", "content_summary": "..."}}"""


def _build_competitive_prompt(
    brand: str, competitors: List[str],
    search_results: List[Dict[str, str]],
) -> str:
    competitor_list = ", ".join(competitors[:6])
    return f"""You are a competitive social-media analyst. From the search results below, compare {brand}'s TikTok + Instagram presence to these competitors: {competitor_list}.

{_EXTRACTION_DIRECTIVE}

SEARCH RESULTS:
{_format_search_results(search_results)}

For each competitor brand give:
- brand: the competitor brand name
- tiktok_followers_estimate: integer or null
- instagram_followers_estimate: integer or null
- kol_endorsements_count_estimate: rough count of KOL posts (10k-1M followers) about that competitor in the last 12 months, integer or null
- gap_summary: one sentence comparing this competitor's social position to {brand}'s

Reply with ONLY a JSON array. No prose:

[
  {{"brand": "...", "tiktok_followers_estimate": 50000, "instagram_followers_estimate": 180000, "kol_endorsements_count_estimate": 12, "gap_summary": "..."}},
  ...
]

If the search results contain no comparable data, reply with: []"""


_SUFFIX_MULT = {"k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000, "b": 1_000_000_000, "B": 1_000_000_000}
_SUFFIX_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([kKmMbB])$")


def _coerce_int(v: Any) -> Optional[int]:
    """Best-effort numeric coercion. Handles "12,000", "12k", "1.5M",
    "180,000", etc. Used for follower / view count fields the LLM may
    return in human-readable shorthand."""
    if isinstance(v, bool):
        return None  # bool is a subclass of int — exclude
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        cleaned = v.replace(",", "").strip()
        m = _SUFFIX_RE.match(cleaned)
        if m:
            try:
                return int(float(m.group(1)) * _SUFFIX_MULT[m.group(2)])
            except (ValueError, KeyError):
                return None
        try:
            return int(float(cleaned))
        except ValueError:
            return None
    return None


def _coerce_str(v: Any) -> Optional[str]:
    return v.strip() if isinstance(v, str) and v.strip() else None


# Failure-reason tokens shared by the three social sub-calls. Surfaced
# in infer_social_intelligence's `failure_reasons` dict so the renderer
# can explain WHY a sub-section is empty instead of silently omitting
# it. None = the sub-call succeeded with data.
_FR_TRANSPORT = "transport_error"   # search API OR extraction call failed (HTTP/timeout)
_FR_PARSE = "parse_error"           # extraction response didn't parse to the expected shape
_FR_UNGROUNDED = "ungrounded"       # search returned nothing usable (was: 0 grounding chunks)
_FR_NO_DATA = "no_data"             # search had results + parsed but yielded nothing usable


# search-then-extract status — `_search_then_extract` returns one of these
# alongside the payload, so callers distinguish "search found nothing"
# (_FR_UNGROUNDED) from "the search/extraction call failed" (_FR_TRANSPORT).
_SEARCH_OK = "ok"
_SEARCH_EMPTY = "empty"
_SEARCH_TRANSPORT = "transport_error"


async def _search_then_extract(
    *,
    queries: List[str],
    extract_prompt_fn: Callable[[List[Dict[str, str]]], str],
    scan_mode: str,
    api_key: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Run `queries` via the SerpAPI client, then (if there were results)
    run the Gemini extraction call. The single deterministic front half
    for all 7 bd_ probes.

    Returns `(payload, search_status)`:
      - `(extract_payload, _SEARCH_OK)`  search had results, extraction ran
      - `(None, _SEARCH_EMPTY)`          search returned 0 results — ungrounded
      - `(None, _SEARCH_TRANSPORT)`      search OR extraction failed at transport

    The caller maps `search_status` onto its `_FR_*` token + honesty-gate
    branch. `extract_prompt_fn` takes the normalized search results and
    returns the extraction prompt (each probe closes over its own
    brand/platform/competitor context).
    """
    from services.social_search_client import search_web_many

    results, search_status = await search_web_many(queries)
    if search_status == "transport_error":
        return (None, _SEARCH_TRANSPORT)
    # "no_key" and "empty" both mean: no usable results, never fabricate —
    # the honesty gate treats them identically (ungrounded).
    if search_status in ("empty", "no_key") or not results:
        return (None, _SEARCH_EMPTY)

    payload = await _gemini_extract_call(
        extract_prompt_fn(results), api_key=api_key, scan_mode=scan_mode,
    )
    if payload is None:
        # Extraction call transport-failed — distinct from "search empty".
        return (None, _SEARCH_TRANSPORT)
    return (payload, _SEARCH_OK)


async def _infer_own_presence(
    brand: str, platform: str, handle: Optional[str], api_key: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Returns (presence_dict, failure_reason). On success the dict is
    populated and reason is None. On failure the dict is None and
    reason names why (see _FR_* tokens). An ungrounded-but-handle-
    present result still SURFACES (dict non-None, reason None) — it's
    marked grounding="ungrounded" internally; the PR-9 gate only nulls
    the metric fields, it doesn't suppress the whole result."""
    payload, search_status = await _search_then_extract(
        queries=_own_presence_queries(brand, platform, handle),
        extract_prompt_fn=lambda results: _build_own_presence_prompt(
            brand, platform, handle, results,
        ),
        scan_mode="bd_own_presence",
        api_key=api_key,
    )
    if search_status == _SEARCH_TRANSPORT:
        return (None, _FR_TRANSPORT)
    metric_key = "view_per_post_estimate" if platform == "tiktok" else "engagement_rate_estimate"
    # PR-9 honesty gate. Follower counts, view/engagement estimates, and
    # content-focus claims are only trustworthy when they came out of real
    # search results. `grounded` is now deterministic: the SerpAPI search
    # returned usable results (vs. returned nothing). When NOT grounded,
    # no extraction ran — `parsed` stays {} so every metric field nulls
    # out, but the caller-supplied handle still surfaces. Identical
    # outward behavior to the old "0 grounding chunks" path.
    grounded = search_status == _SEARCH_OK
    if not grounded:
        parsed: Dict[str, Any] = {}
    else:
        raw_text = _parse_gemini_text(payload)
        parsed = _unwrap_json(raw_text)
        # If Gemini wrapped the object in a single-field container, peek inside.
        if isinstance(parsed, dict) and len(parsed) == 1:
            only_value = next(iter(parsed.values()))
            if isinstance(only_value, dict):
                parsed = only_value
        if not isinstance(parsed, dict):
            logger.info(
                "own_presence(%s): could not parse object; raw=%r",
                platform, (raw_text or "")[:300],
            )
            return (None, _FR_PARSE)
    out: Dict[str, Any] = {
        "platform": platform,
        # handle is safe to keep even ungrounded — it's either the
        # caller-supplied param or a name the operator can click
        # through to verify; it's not a fabricated metric.
        "handle": handle or _coerce_str(parsed.get("handle")),
        "follower_estimate": (
            _coerce_int(parsed.get("follower_estimate")) if grounded else None
        ),
        "follower_band": (
            _coerce_str(parsed.get("follower_band")) if grounded else None
        ),
        metric_key: (
            (_coerce_int(parsed.get(metric_key)) if platform == "tiktok"
             else (parsed.get(metric_key) if isinstance(parsed.get(metric_key), (int, float, str)) else None))
            if grounded else None
        ),
        "content_focus": (
            _coerce_str(parsed.get("content_focus")) if grounded else None
        ),
        "post_frequency": (
            _coerce_str(parsed.get("post_frequency")) if grounded else None
        ),
        "verified_account": (
            parsed.get("verified_account")
            if grounded and isinstance(parsed.get("verified_account"), bool)
            else None
        ),
        "grounding": "grounded" if grounded else "ungrounded",
    }
    # Surface even partial data — handle alone is meaningful (operator
    # can click through to verify) and content_focus tells the BD what
    # the brand actually posts. Only fully-empty (no handle AND no
    # other field) is suppressed. An ungrounded response with a
    # caller-supplied handle still surfaces (handle=truthy) — but with
    # every metric nulled, so the renderer shows the handle + "not
    # verified" rather than fabricated counts.
    if not any(out[k] for k in ("handle", "follower_estimate", "follower_band", "content_focus", "post_frequency")):
        # Nothing to surface. Distinguish WHY: if the response wasn't
        # grounded the PR-9 gate nulled everything → "ungrounded";
        # if it WAS grounded but the model still returned an all-empty
        # object → "no_data".
        return (None, _FR_UNGROUNDED if not grounded else _FR_NO_DATA)
    return (out, None)


async def _infer_kol_endorsements(
    brand: str, platform: str, api_key: str,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Returns (kol_list, failure_reason). Success → non-empty list +
    None. An ungrounded response → (None, "ungrounded") — a creator
    list is the highest-fabrication-risk output, suppressed wholesale
    by the PR-9 gate. Grounded-but-empty → (None, "no_data")."""
    payload, search_status = await _search_then_extract(
        queries=_kol_queries(brand, platform),
        extract_prompt_fn=lambda results: _build_kol_prompt(
            brand, platform, results,
        ),
        scan_mode="bd_kol_endorsements",
        api_key=api_key,
    )
    if search_status == _SEARCH_TRANSPORT:
        return (None, _FR_TRANSPORT)
    # PR-9 honesty gate. A list of named creators who "posted about the
    # brand in the last 12 months" is the highest-fabrication-risk output
    # of the three social sub-calls. When the search returns nothing
    # there is nothing to extract from — suppress wholesale rather than
    # let the extractor invent plausible-looking creator handles.
    if search_status == _SEARCH_EMPTY:
        logger.info(
            "kol_endorsements(%s): search returned nothing for %s — "
            "suppressing to avoid a fabricated creator list",
            platform, brand,
        )
        return (None, _FR_UNGROUNDED)
    raw_text = _parse_gemini_text(payload)
    parsed = _coerce_to_array(_unwrap_json(raw_text))
    if parsed is None:
        logger.info(
            "kol_endorsements(%s): could not coerce response to array; raw=%r",
            platform, (raw_text or "")[:300],
        )
        return (None, _FR_PARSE)
    out: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        handle = _coerce_str(item.get("creator_handle"))
        if not handle:
            continue
        # Strip leading @ from handle if model included it.
        handle = handle.lstrip("@")
        out.append({
            "creator_handle": handle,
            "follower_band": _coerce_str(item.get("follower_band")),
            "post_url": _coerce_str(item.get("post_url")),
            "view_count_estimate": _coerce_int(item.get("view_count_estimate")),
            "post_date": _coerce_str(item.get("post_date")),
            "content_summary": _coerce_str(item.get("content_summary")),
        })
    # Grounded + parsed but no usable creators — a legitimate "we
    # checked, found nothing" outcome. Surface it as no_data so the
    # renderer can say "no endorsements found" rather than omitting.
    if not out:
        return (None, _FR_NO_DATA)
    return (out, None)


async def _infer_competitive_social(
    brand: str, competitors: List[str], api_key: str,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Returns (comparison_list, failure_reason). No competitors to
    compare → (None, None) — not a failure, nothing was attempted.
    Otherwise the standard failure tokens apply."""
    if not competitors:
        return (None, None)
    payload, search_status = await _search_then_extract(
        queries=_competitive_queries(brand, competitors),
        extract_prompt_fn=lambda results: _build_competitive_prompt(
            brand, competitors, results,
        ),
        scan_mode="bd_competitive_social",
        api_key=api_key,
    )
    if search_status == _SEARCH_TRANSPORT:
        return (None, _FR_TRANSPORT)
    # PR-9 honesty gate. Competitor follower counts + KOL-count estimates
    # are fabrication risk — when the search returns nothing there is
    # nothing to extract from, so suppress entirely rather than let the
    # extractor emit plausible numbers for every competitor brand.
    if search_status == _SEARCH_EMPTY:
        logger.info(
            "competitive_social: search returned nothing for %s — "
            "suppressing to avoid fabricated competitor figures",
            brand,
        )
        return (None, _FR_UNGROUNDED)
    raw_text = _parse_gemini_text(payload)
    parsed = _coerce_to_array(_unwrap_json(raw_text))
    if parsed is None:
        logger.info(
            "competitive_social: could not coerce response to array; raw=%r",
            (raw_text or "")[:300],
        )
        return (None, _FR_PARSE)
    out: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = _coerce_str(item.get("brand"))
        if not name:
            continue
        out.append({
            "brand": name,
            "tiktok_followers_estimate": _coerce_int(item.get("tiktok_followers_estimate")),
            "instagram_followers_estimate": _coerce_int(item.get("instagram_followers_estimate")),
            "kol_endorsements_count_estimate": _coerce_int(item.get("kol_endorsements_count_estimate")),
            "gap_summary": _coerce_str(item.get("gap_summary")),
        })
    if not out:
        return (None, _FR_NO_DATA)
    return (out, None)


# PR-10: per-competitor benchmark — number of named competitors we
# run a full own-presence probe against. The brand's own follower
# numbers are meaningless without a peer benchmark ("662k TikTok" —
# is that good?). The single-call `_infer_competitive_social` asks
# Gemini to estimate followers for ~5 brands at once and came back
# null in the PR-8 prod run. Running `_infer_own_presence` per
# competitor gives apples-to-apples comparison (identical method +
# PR-9 honesty gate per-competitor) at a bounded cost. Capped at 2
# so the added grounded-call count stays 4 (2 competitors × 2
# platforms) on top of the existing 4-5.
_COMPETITOR_BENCHMARK_CAP = 2


async def infer_social_intelligence(
    brand: str,
    domain: str,
    detected_handles: Optional[List[Dict[str, str]]] = None,
    competitor_brands: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Gemini grounded queries in parallel:
    - own TikTok presence
    - own Instagram presence
    - TikTok KOL endorsements (last 12 months)
    - Instagram KOL endorsements (last 12 months)
    - Competitive social comparison (only when competitor_brands non-empty)
    - PR-10: per-competitor own-presence probes for the top
      `_COMPETITOR_BENCHMARK_CAP` named competitors — same
      `_infer_own_presence` method used for the merchant, so the
      brand's numbers have an apples-to-apples benchmark.

    Returns:
      {
        "own_presence": {
          "tiktok": {handle, follower_estimate, follower_band, view_per_post_estimate, content_focus, ...} | null,
          "instagram": {handle, follower_estimate, follower_band, engagement_rate_estimate, content_focus, ...} | null,
        },
        "kol_endorsements": {
          "tiktok": [...] | null,
          "instagram": [...] | null,
        },
        "competitive_comparison": [...] | null,   # secondary gap_summary narrative
        "competitor_presence": {                   # PR-10 primary benchmark
          "<competitor name>": {"tiktok": {...} | null, "instagram": {...} | null},
          ...
        } | null,
        "available": bool,  # at least one signal succeeded
      }

    Honesty: returns None / empty for any sub-call that failed parse;
    {available: false} when no GEMINI_API_KEY or when brand+domain
    inputs are empty. UI renders "data not available — manual research
    recommended" instead of fabricating. PR-9's grounding gate applies
    per sub-call, INCLUDING each per-competitor probe.

    LLM-multiplier safety: 4-5 base single-shot grounded calls + up to
    4 per-competitor probes (2 competitors × 2 platforms) = max 9.
    asyncio.gather parallelism keeps wall-time bounded at ~20-25s.
    Per-merchant semaphore (cap=5) in agent_center_llm_client bounds
    concurrent load. The whole function is opt-in behind
    `include_social_intelligence` so the bump only lands when the
    operator asked for it.
    """
    api_key = _resolve_gemini_api_key()
    empty = {
        "own_presence": {"tiktok": None, "instagram": None},
        "kol_endorsements": {"tiktok": None, "instagram": None},
        "competitive_comparison": None,
        "competitor_presence": None,
        # No sub-call ran — failure_reasons keys all None so downstream
        # consumers can rely on the key existing without special-casing.
        "failure_reasons": {
            "own_presence_tiktok": None,
            "own_presence_instagram": None,
            "kol_tiktok": None,
            "kol_instagram": None,
            "competitive_comparison": None,
            "competitor_presence": None,
        },
        "available": False,
    }
    if not vertex_gemini.credentials_available(api_key):
        logger.info("social_intelligence: no Gemini credentials; skipping for %s", brand)
        return empty
    if not brand or not brand.strip() or not domain or not domain.strip():
        return empty
    # Search-then-extract needs SerpAPI for retrieval. Without it every
    # social probe degrades to "ungrounded" (honest empty, never fabricated)
    # — surface it so the operator knows the section will come back empty.
    from services.social_search_client import _resolve_search_api_key
    if not _resolve_search_api_key():
        logger.info(
            "social_intelligence: no SERPAPI_API_KEY — social probes will "
            "return ungrounded (empty) for %s", brand,
        )

    # Handle-detection fallback. The cold-start flow threads
    # `detected_handles` (scraped from homepage HTML it already
    # fetched). The main audit path (run_brand_report) passes None —
    # so when no handles were threaded in, self-serve a homepage
    # fetch + scrape here. Without this, the social probes get
    # handle=None and the PR-8 prod run's "handle:null everywhere"
    # is the result. One cheap HTTP GET; failure → handles stay None
    # and the probes fall back to "search for the account" prompting.
    handles = list(detected_handles or [])
    if not handles:
        homepage_html = await _fetch_homepage_html(domain)
        if homepage_html:
            handles = _extract_social_handles(homepage_html)
    tt_handle = _detected_handle(handles, "tiktok")
    ig_handle = _detected_handle(handles, "instagram")
    competitors = [c for c in (competitor_brands or []) if isinstance(c, str) and c.strip()]

    # Base coros — own presence + KOL endorsements. Fixed indices 0-3.
    coros: List[Any] = [
        _infer_own_presence(brand, "tiktok", tt_handle, api_key),
        _infer_own_presence(brand, "instagram", ig_handle, api_key),
        _infer_kol_endorsements(brand, "tiktok", api_key),
        _infer_kol_endorsements(brand, "instagram", api_key),
    ]

    # Competitive comparison narrative — index tracked dynamically.
    competitive_idx: Optional[int] = None
    if competitors:
        competitive_idx = len(coros)
        coros.append(_infer_competitive_social(brand, competitors, api_key))

    # PR-10: per-competitor benchmark probes. Top N competitors get
    # the same own-presence probe the merchant gets. Track each
    # competitor's (tiktok_idx, instagram_idx) so we can re-assemble
    # after the gather.
    benchmark_competitors = competitors[:_COMPETITOR_BENCHMARK_CAP]
    competitor_idx_map: Dict[str, Tuple[int, int]] = {}
    for comp in benchmark_competitors:
        tt_idx = len(coros)
        coros.append(_infer_own_presence(comp, "tiktok", None, api_key))
        ig_idx = len(coros)
        coros.append(_infer_own_presence(comp, "instagram", None, api_key))
        competitor_idx_map[comp] = (tt_idx, ig_idx)

    results = await asyncio.gather(*coros, return_exceptions=True)

    def _safe(r: Any) -> Tuple[Any, Optional[str]]:
        """Every sub-call now returns a (data, failure_reason) tuple.
        An exception escaping the coro → (None, transport_error) so
        the failure still gets a reason instead of vanishing."""
        if isinstance(r, BaseException):
            return (None, _FR_TRANSPORT)
        if isinstance(r, tuple) and len(r) == 2:
            return r
        # Defensive: a sub-call that somehow returned a bare value.
        return (r, None if r else _FR_NO_DATA)

    # Re-assemble per-competitor presence. A competitor surfaces only
    # if at least one of its platform probes returned data — same
    # "surface partial, suppress fully-empty" rule as own_presence.
    # Per-platform failure reasons are tracked so the renderer can
    # explain a missing competitor benchmark.
    competitor_presence: Dict[str, Any] = {}
    competitor_failure_reasons: Dict[str, Dict[str, Optional[str]]] = {}
    for comp, (tt_idx, ig_idx) in competitor_idx_map.items():
        comp_tt, comp_tt_reason = _safe(results[tt_idx])
        comp_ig, comp_ig_reason = _safe(results[ig_idx])
        if comp_tt or comp_ig:
            competitor_presence[comp] = {"tiktok": comp_tt, "instagram": comp_ig}
        if comp_tt_reason or comp_ig_reason:
            competitor_failure_reasons[comp] = {
                "tiktok": comp_tt_reason,
                "instagram": comp_ig_reason,
            }

    own_tt, own_tt_reason = _safe(results[0])
    own_ig, own_ig_reason = _safe(results[1])
    kol_tt, kol_tt_reason = _safe(results[2])
    kol_ig, kol_ig_reason = _safe(results[3])
    if competitive_idx is not None:
        competitive, competitive_reason = _safe(results[competitive_idx])
    else:
        competitive, competitive_reason = None, None

    out = {
        "own_presence": {
            "tiktok": own_tt,
            "instagram": own_ig,
        },
        "kol_endorsements": {
            "tiktok": kol_tt,
            "instagram": kol_ig,
        },
        "competitive_comparison": competitive,
        "competitor_presence": competitor_presence or None,
        # PR: failure-reason surfacing. Why each sub-call came back
        # empty — None when the sub-call succeeded with data. The
        # renderer maps these tokens to merchant-readable one-liners
        # so an empty sub-section is explained, not silently omitted.
        "failure_reasons": {
            "own_presence_tiktok": own_tt_reason,
            "own_presence_instagram": own_ig_reason,
            "kol_tiktok": kol_tt_reason,
            "kol_instagram": kol_ig_reason,
            "competitive_comparison": competitive_reason,
            "competitor_presence": competitor_failure_reasons or None,
        },
    }
    out["available"] = any(
        v for v in (
            out["own_presence"]["tiktok"], out["own_presence"]["instagram"],
            out["kol_endorsements"]["tiktok"], out["kol_endorsements"]["instagram"],
            out["competitive_comparison"],
            out["competitor_presence"],
        )
    )
    return out
