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

import json
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

# Next.js RSC "flight" streaming: the payload is split across
# `self.__next_f.push([1,"<chunk>"])` calls; concatenating the JS-unescaped
# chunk bodies reconstructs the flight. Rows are `<id>:<Type><data>`; text rows
# are `<id>:T<hexlen>,<bytes>` where hexlen is the BYTE length. A field can
# reference a row by value `"$<id>"`. StyleKorean PDPs carry the published
# ingredient list in an `allIngredients` field (inline, or a `$<id>` reference
# resolved out of the flight). No LLM, no fabrication — this reads the page's own
# text; empty/absent/ambiguous/not-INCI → None.
_NEXT_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_ALL_INGREDIENTS_RE = re.compile(r'"allIngredients":"(\$[0-9a-f]+|[^"]*)"')
# A flight row boundary is a newline followed by `<hexid>:<TypeLetter>` — used to
# stop a text slice from bleeding into the next row when the declared byte length
# is loose (defense-in-depth over the byte-accurate slice).
_FLIGHT_ROW_BOUNDARY_RE = re.compile(r"\n[0-9a-f]+:[A-Za-z]")

# Recognizable INCI morphemes/ingredients. A real ingredient list contains at
# least one; prose ("this serum hydrates deeply") contains none. Substring match,
# so "sodium hyaluronate", "-extract", "seed oil", "(ci 77499)" all hit.
_INCI_ANCHOR_TOKENS = (
    "water", "aqua", "glycerin", "glycol", "sodium", "acid", "extract", "oil",
    "butylene", "niacinamide", "hyaluron", "dimethicone", "alcohol", "citric",
    "panthenol", "tocopher", "phenoxyethanol", "cetyl", "stearyl", "stear",
    "carbomer", "xanthan", "propanediol", "ceramide", "allantoin", "adenosine",
    "caprylyl", "ethylhexyl", "butyrospermum", "centella", "peptide", "retinol",
    "polysorbate", "lecithin", "squalane", "parfum", "fragrance", "seed", "leaf",
    "root", "flower", "fruit", "butter", "benzoate", "salicyl", "glyceryl",
    "sorbitan", "peg-", "ppg-", "laur", "ci 77", "ci 4", "ci 1", "hydroxide",
    "chloride", "gluconate", "edta", "carrageenan", "cellulose", "silica",
)
# Prose/marketing words: their density flags a sentence, not a delimited list.
_INCI_PROSE_STOPWORDS = frozenset({
    "the", "and", "is", "are", "this", "that", "with", "your", "you", "our",
    "for", "helps", "help", "made", "best", "will", "ever", "try", "from", "have",
    "has", "was", "were", "to", "of", "in", "on", "it", "its", "absorbs", "hydrates",
    "hydrate", "lightweight", "deeply", "fast", "gently", "leaves", "feeling",
    "perfect", "designed", "formulated", "provides", "delivers",
})


def _reconstruct_next_flight(html: str) -> str:
    """Concatenate the JS-unescaped bodies of every `__next_f.push` chunk into the
    raw RSC flight string. Returns '' when the page isn't a Next.js RSC stream."""
    parts: List[str] = []
    for m in _NEXT_FLIGHT_RE.finditer(html):
        body = m.group(1)
        try:
            parts.append(json.loads('"' + body + '"'))
        except Exception:  # noqa: BLE001 — a malformed chunk just contributes raw
            parts.append(body)
    return "".join(parts)


def _resolve_flight_text_row(flight: str, row_id: str) -> Optional[str]:
    """Resolve a `$<row_id>` reference to its text-row payload. The row id is
    ANCHORED to a row boundary (start-of-flight or a preceding newline) so ref
    `$51` never matches inside an earlier `151:T…` row. The declared length is in
    BYTES, so the slice is taken on the utf-8 encoding (a ™/°/Korean glyph can't
    push it into the next row), then trimmed at the next row marker as a backstop.
    None when the row is absent."""
    m = re.search(r"(?:^|\n)" + re.escape(row_id) + r":T([0-9a-f]+),", flight)
    if not m:
        return None
    char_start = m.end()
    byte_len = int(m.group(1), 16)
    encoded = flight.encode("utf-8")
    byte_start = len(flight[:char_start].encode("utf-8"))
    text = encoded[byte_start:byte_start + byte_len].decode("utf-8", "ignore")
    boundary = _FLIGHT_ROW_BOUNDARY_RE.search(text)
    if boundary:
        text = text[: boundary.start()]
    return text


def _clean_inci_text(text: Optional[str]) -> Optional[str]:
    """Normalize a captured ingredient string: unescape stray CR/LF, collapse
    whitespace, and drop a leading product-name label ('<Name> (1step): Water,
    ...') when the pre-colon segment carries no comma (a real INCI list has no
    bare label). Returns None when nothing usable remains — never invents text."""
    if not text:
        return None
    s = text.replace("\\r", " ").replace("\\n", " ").replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    head, sep, tail = s.partition(":")
    if sep and "," not in head and "," in tail:
        s = tail.strip()
    return s or None


def _looks_like_inci_list(text: Optional[str]) -> bool:
    """Stronger-than-`>=2-commas` gate for RESELLER-tier INCI: a real ingredient
    list is a comma-delimited run of short noun phrases, contains at least one
    recognizable ingredient morpheme, and is not prose. This is the safety net the
    ref-resolution + byte-slice fixes rely on — a stray prose/next-row leak that
    slips through must still be rejected before it can fill an empty canonical."""
    if not text:
        return False
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 3:
        return False  # a genuine INCI lists several ingredients
    # Ingredient tokens are short noun phrases, not sentences.
    long_parts = sum(1 for p in parts if len(p.split()) > 6)
    if long_parts > max(1, len(parts) // 4):
        return False
    low = text.lower()
    if not any(tok in low for tok in _INCI_ANCHOR_TOKENS):
        return False  # no recognizable ingredient → not an INCI list
    words = re.findall(r"[a-z]+", low)
    if words:
        stop = sum(1 for w in words if w in _INCI_PROSE_STOPWORDS)
        if stop > len(words) * 0.15:
            return False  # sentence/marketing prose, not a list
    return True


def extract_inci_from_next_rsc(html: str) -> Optional[str]:
    """Deterministic ingredient-list extraction from a Next.js RSC PDP.

    Resolves every `allIngredients` value (a `$<id>` flight reference — anchored,
    byte-accurate — or an inline string), cleans it, and keeps only those that
    clear the strong INCI gate. Returns the list ONLY when exactly one distinct
    valid INCI resolves: a page with related-product cards can carry several
    `allIngredients`, and without a product binding we must not guess which is the
    PDP's own — ambiguity yields None. Never fabricated; validated again downstream."""
    flight = _reconstruct_next_flight(html)
    if not flight:
        return None
    found: List[str] = []
    for value in _ALL_INGREDIENTS_RE.findall(flight):
        if value.startswith("$"):
            raw = _resolve_flight_text_row(flight, value[1:])
        elif "," in value:
            raw = value
        else:
            raw = None
        candidate = _clean_inci_text(raw)
        if candidate and _looks_like_inci_list(candidate):
            found.append(candidate)
    distinct = list(dict.fromkeys(found))
    if len(distinct) == 1:
        return distinct[0]
    return None  # 0 = none published; >1 = ambiguous (which product's list?)


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
    availability (normalized), rating_value, rating_count, raw_inci. The last
    three are OPTIONAL — null when the page exposes no review block / ingredient
    list, and never fabricated."""
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
        # Optional review signal from the JSON-LD aggregateRating block.
        "rating_value": best.get("rating_value"),
        "rating_count": best.get("rating_count"),
        # Optional published INCI list from the Next.js RSC payload.
        "raw_inci": extract_inci_from_next_rsc(html),
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
