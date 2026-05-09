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
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)


_SITEMAP_FETCH_TIMEOUT_S = 8.0
_SITEMAP_MAX_BYTES = 5_000_000  # 5 MB — most sitemaps are <1 MB


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
