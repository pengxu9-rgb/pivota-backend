"""Canonical INCI resolver — the discovery half of canonical sourcing (ADR-001).

The intake (services.canonical_inci_intake) writes a canonical INCI into the
record by source precedence; this RESOLVER finds that INCI from an authoritative
source so a thin reseller sync (no INCI) becomes decision-grade. Two halves:

  * extract_inci_from_text(text) -- the deterministic core: pull an INCI list out
    of a fetched page (brand-official PDP, INCI database, supplier doc). Validates
    that what it found really is an ingredient list, so marketing prose never
    masquerades as INCI. Pure, regex-only, unit-tested.
  * resolve_inci_from_urls(urls, fetch) -- try candidate authoritative sources in
    priority order, fetch each, extract, return the first valid INCI + which URL
    it came from (provenance for the intake).

Fetching is injected (`fetch`) so the logic is testable without network; the
production fetcher (http_fetch, httpx) is a thin default. Source DISCOVERY (how
to find the brand-official URL) is the caller's concern -- pass the product's
brand-official PDP URL(s); a search/crawl layer can feed them later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional
from urllib.parse import urlparse

from services.beauty_enrichment import parse_inci

# An "Ingredients" / "INCI" section label, optionally "Full ingredients:".
_INGREDIENTS_LABEL_RE = re.compile(
    # The separator is optional: brand PDPs use "Ingredients: <list>", but many
    # (Shopify accordions) render a bare "INGREDIENTS" header then the list.
    r"\b(?:full\s+|key\s+)?(?:ingredients?|inci|ingredient\s+list|composition)\b\s*[:\-—]?\s*",
    re.IGNORECASE,
)
# UI / nav tokens that may follow an ingredient list in a scraped page -- they
# end the INCI run (they are not ingredients).
_UI_STOPWORD_RE = re.compile(
    r"^(?:add\s+to\s+cart|buy\s+now|reviews?|subscribe|share|tweet|pin\s+it|"
    r"quantity|sold\s+out|shipping|returns?|description|how\s+to\s+use|"
    r"directions|home|shop|menu|search|account|cart|sign\s+in|log\s+in)\b",
    re.IGNORECASE,
)
# A single INCI ingredient token: starts with a letter, only ingredient-ish chars.
_INCI_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 ()\-'./%*&+]*$")
# Where an ingredient list stops: a blank line or the next PDP section.
_SECTION_END_RE = re.compile(
    r"\n\s*\n|(?:\b(?:how\s+to\s+use|directions|warnings?|caution|about|description|"
    r"benefits|suitable\s+for|size|reviews?|add\s+to\s+cart|add\s+to\s+bag|"
    r"buy\s+now|sold\s+out|quantity|checkout|you\s+may\s+also\s+like)\b)",
    re.IGNORECASE,
)
# INCI anchor words -- a real ingredient list almost always contains one of these.
_INCI_ANCHOR_RE = re.compile(
    r"\b(?:aqua|water|eau|glycer(?:in|ine)|butylene\s+glycol|propanediol|"
    r"niacinamide|phenoxyethanol|dimethicone|tocopherol|sodium\s+\w+|"
    r"\w+\s+extract|\w+\s+acid|parfum|fragrance|citric\s+acid)\b",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(?:[a-z]+|#\d+);", re.IGNORECASE)

_MAX_INGREDIENTS = 100


@dataclass(frozen=True)
class ResolvedInci:
    raw_inci: str
    source_url: str


def _strip_html(text: str) -> str:
    text = _HTML_TAG_RE.sub(" ", text)
    text = _HTML_ENTITY_RE.sub(" ", text)
    return text


def _is_inci_token(tok: str) -> bool:
    t = tok.strip()
    return bool(2 <= len(t) <= 60 and _INCI_TOKEN_RE.match(t) and not _UI_STOPWORD_RE.match(t))


def _leading_inci_run(tokens: List[str]) -> List[str]:
    """The leading consecutive run of INCI-looking tokens -- stops at the first
    non-ingredient (page/UI text that followed the list in a scraped accordion)."""
    run: List[str] = []
    for tok in tokens:
        if _is_inci_token(tok):
            run.append(tok)
        else:
            break
    return run


def _clean_and_validate_inci(candidate: Optional[str]) -> Optional[str]:
    """Validate a raw candidate really is an INCI list and return it as a clean
    comma list, or None. Takes the LEADING run of ingredient-looking tokens (so
    trailing page/UI text after a scraped accordion list is dropped), then
    requires >=4 tokens AND an anchor ingredient. Shared by the PDP extractor and
    the structured-source adapters."""
    if not candidate:
        return None
    run = _leading_inci_run(parse_inci(candidate))
    if len(run) < 4:
        return None
    joined = ", ".join(run[:_MAX_INGREDIENTS])
    if not _INCI_ANCHOR_RE.search(joined):
        return None
    return joined


def extract_inci_from_text(text: Optional[str]) -> Optional[str]:
    """Extract a clean INCI list from page text/HTML, or None.

    Finds an "Ingredients:" label, takes the list that follows up to the next
    section, and validates it looks like INCI. Handles the common brand-PDP
    pattern; structured sources (INCI databases) use their own adapters below.
    """
    if not text:
        return None
    cleaned = _strip_html(text)
    # Try each "Ingredients"/"INCI" occurrence -- the first is often a nav link or
    # heading; the one that precedes the real list is what validates.
    for match in _INGREDIENTS_LABEL_RE.finditer(cleaned):
        after = _SECTION_END_RE.split(cleaned[match.end():], maxsplit=1)[0]
        inci = _clean_and_validate_inci(after)
        if inci:
            return inci
    return None


def extract_inci_from_openbeautyfacts(json_text: Optional[str]) -> Optional[str]:
    """Pull the INCI list from an Open Beauty Facts product JSON response.

    OBF is a structured INCI authority: its `ingredients_text` is already a comma
    list (no label), so it validates directly. A clean source -- no HTML scraping
    -- when the product (by barcode) exists in OBF."""
    if not json_text:
        return None
    try:
        data = json.loads(json_text)
    except Exception:  # noqa: BLE001
        return None
    product = data.get("product") if isinstance(data, dict) else None
    if not isinstance(product, dict):
        return None
    for key in ("ingredients_text_en", "ingredients_text"):
        inci = _clean_and_validate_inci(product.get(key))
        if inci:
            return inci
    return None


def open_beauty_facts_url(barcode: str) -> str:
    """OBF product API URL for a barcode (only the fields we need)."""
    return (
        f"https://world.openbeautyfacts.org/api/v2/product/{barcode}.json"
        "?fields=ingredients_text,ingredients_text_en,product_name,brands"
    )


async def resolve_inci_from_openbeautyfacts(
    barcode: Optional[str], *, fetch: "Fetcher"
) -> Optional["ResolvedInci"]:
    """Resolve INCI from Open Beauty Facts by barcode, or None."""
    if not barcode or not str(barcode).strip():
        return None
    url = open_beauty_facts_url(str(barcode).strip())
    try:
        body = await fetch(url)
    except Exception:  # noqa: BLE001
        return None
    inci = extract_inci_from_openbeautyfacts(body)
    return ResolvedInci(raw_inci=inci, source_url=url) if inci else None


# A fetcher: given a URL, return the page text (or None on miss). Async so the
# production impl can use httpx; injected so tests need no network.
Fetcher = Callable[[str], Awaitable[Optional[str]]]


_SHOPIFY_PRODUCT_PATH_RE = re.compile(r"/products/[^/?#]+", re.IGNORECASE)


def shopify_product_json_url(url: Optional[str]) -> Optional[str]:
    """Map a Shopify PDP URL to its public product-JSON endpoint, or None.

    https://brand.com/products/handle?variant=1 -> https://brand.com/products/handle.json
    Also collapses a /collections/x/products/handle path to /products/handle.json.
    The .json endpoint returns structured product data (body_html etc.) even when
    the rendered PDP loads ingredients via JavaScript -- which a plain GET misses.
    """
    if not url:
        return None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    match = _SHOPIFY_PRODUCT_PATH_RE.search(parsed.path)
    if not match or not parsed.netloc:
        return None
    handle_path = match.group(0)
    if not handle_path.endswith(".json"):
        handle_path = f"{handle_path}.json"
    return f"{parsed.scheme or 'https'}://{parsed.netloc}{handle_path}"


def extract_inci_from_shopify_json(json_text: Optional[str]) -> Optional[str]:
    """Pull an INCI list from a Shopify product-JSON response. Ingredients live in
    body_html (the description), so it runs through the HTML-aware text extractor."""
    if not json_text:
        return None
    try:
        data = json.loads(json_text)
    except Exception:  # noqa: BLE001
        return None
    product = data.get("product") if isinstance(data, dict) else None
    if not isinstance(product, dict):
        return None
    return extract_inci_from_text(product.get("body_html"))


async def resolve_inci_from_url(url: str, *, fetch: "Fetcher") -> Optional["ResolvedInci"]:
    """Resolve INCI from one URL: try the Shopify product-JSON endpoint first
    (structured, survives JS-rendered PDPs), then the rendered page as fallback."""
    json_url = shopify_product_json_url(url)
    if json_url:
        try:
            body = await fetch(json_url)
        except Exception:  # noqa: BLE001
            body = None
        inci = extract_inci_from_shopify_json(body)
        if inci:
            return ResolvedInci(raw_inci=inci, source_url=json_url)
    try:
        text = await fetch(url)
    except Exception:  # noqa: BLE001
        return None
    inci = extract_inci_from_text(text)
    return ResolvedInci(raw_inci=inci, source_url=url) if inci else None


async def resolve_inci_from_urls(urls: List[str], *, fetch: Fetcher) -> Optional[ResolvedInci]:
    """Try candidate authoritative URLs in order; return the first that yields a
    valid INCI (Shopify product-JSON preferred per URL), tagged with its source."""
    for url in urls:
        if not url:
            continue
        resolved = await resolve_inci_from_url(url, fetch=fetch)
        if resolved:
            return resolved
    return None


async def http_fetch(url: str, *, timeout: float = 10.0) -> Optional[str]:
    """Default production fetcher (httpx). Returns page text or None.

    Best-effort + polite: short timeout, redirects followed, a descriptive
    User-Agent. Never raises -- a fetch failure is just a miss."""
    try:
        import httpx

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "PivotaBot/1.0 (+https://pivota.cc; canonical-inci-resolver)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception:  # noqa: BLE001
        return None
