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

from services.beauty_enrichment import parse_inci

# An "Ingredients" / "INCI" section label, optionally "Full ingredients:".
_INGREDIENTS_LABEL_RE = re.compile(
    r"(?:full\s+|key\s+)?(?:ingredients?|inci|ingredient\s+list|composition)\s*[:\-—]\s*",
    re.IGNORECASE,
)
# Where an ingredient list stops: a blank line or the next PDP section.
_SECTION_END_RE = re.compile(
    r"\n\s*\n|(?:\b(?:how\s+to\s+use|directions|warnings?|caution|about|description|"
    r"benefits|suitable\s+for|size|reviews?)\b)",
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


def _clean_and_validate_inci(candidate: Optional[str]) -> Optional[str]:
    """Validate a raw candidate really is an INCI list (enough comma tokens AND
    an anchor ingredient), and return it re-joined as a clean comma list, or
    None. Shared by the PDP extractor and the structured-source adapters."""
    if not candidate:
        return None
    tokens = parse_inci(candidate)
    if len(tokens) < 4 or not _INCI_ANCHOR_RE.search(candidate):
        return None
    return ", ".join(tokens[:_MAX_INGREDIENTS])


def extract_inci_from_text(text: Optional[str]) -> Optional[str]:
    """Extract a clean INCI list from page text/HTML, or None.

    Finds an "Ingredients:" label, takes the list that follows up to the next
    section, and validates it looks like INCI. Handles the common brand-PDP
    pattern; structured sources (INCI databases) use their own adapters below.
    """
    if not text:
        return None
    cleaned = _strip_html(text)
    match = _INGREDIENTS_LABEL_RE.search(cleaned)
    if not match:
        return None
    after = _SECTION_END_RE.split(cleaned[match.end():], maxsplit=1)[0]
    return _clean_and_validate_inci(after)


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


async def resolve_inci_from_urls(urls: List[str], *, fetch: Fetcher) -> Optional[ResolvedInci]:
    """Try candidate authoritative URLs in order; return the first that yields a
    valid INCI, tagged with the URL it came from."""
    for url in urls:
        if not url:
            continue
        try:
            text = await fetch(url)
        except Exception:  # noqa: BLE001 -- a dead source must not abort resolution
            continue
        inci = extract_inci_from_text(text)
        if inci:
            return ResolvedInci(raw_inci=inci, source_url=url)
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
