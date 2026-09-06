"""Curated-brand-list feed — the CLEAN primary source for catalog coverage.

Given a curated list of brand storefront domains, enumerate their products via
Shopify's PUBLIC `/products.json` and turn each into a Path-C *validated record*
(the `{pdp, offers}` shape `ingestion.ingest_validated_record` consumes). The
brand's own storefront is the authoritative PDP, so this bypasses Gemini URL
resolution entirely — it's deterministic, cheap, and carries the brand's own
title/price/image, variant **barcode (GTIN)**, and tags. The records then ingest
as depositable canonical anchors via the existing FK-order executor.

This is the "crawl the brand before they integrate" engine for the (very common)
Shopify-hosted D2C brand. Non-Shopify domains return [] (fall back to the audit/
agent feed). PURE-ish: this module fetches public pages + builds records; the
caller runs `ingest_validated_jsonl` + `apply_ingest_plan` (gated).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services import storefront_currency

from services.retailer_ingest.sitemap_crawler import _looks_like_inci_list
from services import crawl_politeness

logger = logging.getLogger("curated_brand_feed")

_UA = "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)"
_PER_PAGE = 250  # Shopify max
# Lowest variant price (in the store's currency) that counts as a real offer. Across
# the four Meitu-US feeds measured 2026-09-05 (2,108 products) exactly one variant sat
# in (0, 1.00): the $0.01 stila promo described at the variant pick below. Nothing legitimate in a beauty D2C feed is
# priced under a dollar; a floor this low cannot drop a real product.
MIN_SELLABLE_PRICE = 1.0


def _clean_domain(domain: str) -> str:
    d = str(domain or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").rstrip("/")
    return d.split("/")[0]


_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ISO_COUNTRY = re.compile(r"^[A-Z]{2}$")


async def fetch_shopify_shop_locale(
    domain: str,
    *,
    timeout_s: float = 10.0,
) -> Dict[str, Optional[str]]:
    """The storefront's own currency/country, via the module that already reads /meta.json.

    `/products.json` carries prices but NEVER the currency they are in, so every record this
    module built was currency-less and the ingest lane stamped USD on all of them. Measured
    2026-09-06: jsmbeauty.sg prices LIP-PRESSION Glowy Tint at 3000 minor = SGD 30.00, and
    ingesting it through that lane wrote USD 30.

    DELEGATES to `services.storefront_currency.fetch_storefront_meta` rather than fetching here.
    A first draft of this function was a second, uncached reader of the same endpoint -- which is
    why a THIRD gated fetch had to be registered in this file's crawl-politeness budget. That
    module already validates the currency, caches per domain for the process lifetime, and is the
    place this knowledge belongs.

    The politeness gate is preserved by injecting the fetch: `fetch_storefront_meta` takes a
    `fetch` seam precisely so a caller can supply its own transport, so the shared gate still sees
    every request this crawl lane makes against a merchant host.

    ONLY `currency` IS RETURNED FOR THE WRITE PATH's use. `storefront_currency`'s own docstring
    records why: "`market` and `currency` are different axes (destination served vs store base
    currency) -- a KR/HK exporter legitimately prices in USD". `country` is passed through for
    callers that want the storefront's home, never as a synonym for the market an offer serves.
    """
    host = _clean_domain(domain)
    if not host:
        return {"currency": None, "country": None}

    async def _gated_fetch(url: str) -> Optional[str]:
        headers = {"User-Agent": _UA, "Accept": "application/json"}
        timeout = httpx.Timeout(timeout_s, connect=5.0)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as client:
                await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
                resp = await client.get(url)
                crawl_politeness.note_response(
                    url, resp.status_code, retry_after=resp.headers.get("retry-after")
                )
                if resp.status_code != 200:
                    return None
                if "application/json" not in (resp.headers.get("content-type") or ""):
                    return None
                return resp.text
        except Exception:
            return None

    meta = await storefront_currency.fetch_storefront_meta(host, fetch=_gated_fetch)
    if not isinstance(meta, dict):
        return {"currency": None, "country": None}
    cur = str(meta.get("currency") or "").strip().upper()
    country = str(meta.get("country") or "").strip().upper()
    return {
        "currency": cur if _ISO_CURRENCY.match(cur) else None,
        "country": country if _ISO_COUNTRY.match(country) else None,
    }


async def fetch_shopify_products(
    domain: str,
    *,
    max_products: int = 500,
    timeout_s: float = 15.0,
) -> List[Dict[str, Any]]:
    """Page through `https://{domain}/products.json`. Returns raw Shopify product
    dicts (up to max_products), or [] if the store isn't Shopify / errors."""
    host = _clean_domain(domain)
    if not host:
        return []
    out: List[Dict[str, Any]] = []
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            page = 1
            while len(out) < max_products:
                url = f"https://{host}/products.json?limit={_PER_PAGE}&page={page}"
                # Shared politeness gate: merchant storefront, same reserved NAT address as every
                # other crawl lane. Paging is a loop against ONE host, which is exactly the shape
                # that earns a per-IP block. max_wait=0 — batch, so wait rather than drop pages.
                await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
                resp = await client.get(url)
                crawl_politeness.note_response(
                    url, resp.status_code, retry_after=resp.headers.get("retry-after")
                )
                if resp.status_code != 200 or "application/json" not in (resp.headers.get("content-type") or ""):
                    break
                products = (resp.json() or {}).get("products") or []
                if not products:
                    break
                out.extend(products)
                if len(products) < _PER_PAGE:
                    break
                page += 1
    except Exception as exc:  # noqa: BLE001 — a brand site being down must not break the batch
        logger.debug("fetch_shopify_products failed for %s: %s", host, str(exc)[:160])
    return out[:max_products]


def _first(seq: Any) -> Optional[Dict[str, Any]]:
    return seq[0] if isinstance(seq, list) and seq and isinstance(seq[0], dict) else None


# <script>/<style> INNER TEXT is code, not prose — page-builder exports
# (PageFly/GemPages) routinely embed style blocks in body_html; a naive
# tag-strip would keep the CSS soup and could auto-publish it as the brand's
# words. Strip whole blocks (and comments) BEFORE the tag pass.
_HTML_BLOCK_RE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>|<!--.*?-->")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
BODY_TEXT_MAX_LEN = 2000  # PDP prose field, not a document store


def body_html_to_text(body_html: Optional[str]) -> str:
    """Deterministic Shopify body_html → plain text: drop script/style/comment
    blocks, strip tags, unescape entities, collapse whitespace, cap at a word
    boundary. Output is the brand's own words or ''."""
    if not body_html:
        return ""
    text = _HTML_BLOCK_RE.sub(" ", str(body_html))
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > BODY_TEXT_MAX_LEN:
        cut = text[:BODY_TEXT_MAX_LEN]
        text = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return text


# Brand storefronts routinely list the INCI under an "Ingredients" heading inside
# body_html. Capture it deterministically (no LLM): find the label, take the
# following text, and stop at the next section heading. Null when absent — never
# fabricated; the downstream INCI intake re-validates it parses as a real list.
_INCI_LABEL_RE = re.compile(
    r"(?is)\b(?:full\s+|all\s+|key\s+)?ingredients?\b(?:\s*list)?\s*[:\-]\s*(.{15,3000})"
)
_INCI_STOP_RE = re.compile(
    r"(?is)\b(?:how\s+to\s+use|directions|how\s+to\s+apply|usage|warnings?|caution|"
    r"about\s+the\s+brand|shipping|net\s+wt|precautions)\b"
)


def inci_from_body_html(body_html: Optional[str]) -> Optional[str]:
    """Deterministic INCI extraction from a Shopify body_html: strip tags, find an
    'Ingredients:' label, capture the following list, and cut at the next section
    heading. Returns None when there's no label or the captured text isn't a
    comma-delimited list (a single blob / prose is rejected here and again by the
    canonical INCI intake)."""
    if not body_html:
        return None
    text = _HTML_BLOCK_RE.sub(" ", str(body_html))
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    m = _INCI_LABEL_RE.search(text)
    if not m:
        return None
    tail = m.group(1).strip()
    stop = _INCI_STOP_RE.search(tail)
    if stop:
        tail = tail[: stop.start()].strip()
    tail = tail.rstrip(". ").strip()
    # A real INCI is a comma-delimited list of several ingredients, not a blurb.
    if len([p for p in tail.split(",") if p.strip()]) < 2:
        return None
    return tail or None


# --- Rendered-PDP INCI (the metafield / accordion source) ------------------
# The K-beauty cohort keeps its full INCI OUT of `/products.json` body_html — it
# lives in a Shopify product METAFIELD that the theme renders into the PDP either
# as a visible <p> inside a "Full Ingredients" popup/accordion/modal (cosrx,
# axis-y, iunik, skin1004 confirmed 2026-07-19) or as a JSON string inside a data
# island (rich-text `{"type":"text","value":"Water, ..."}`, or an escaped-HTML
# metafield string). Neither is in `/products.json`, so body_html capture
# recovered INCI for only ~4.6% of the cohort. This extractor reads the RENDERED
# PDP HTML from BOTH surfaces and recovers the list deterministically — never
# fabricated; re-validated by the canonical INCI intake before it can write.
_PDP_STRIP_RE = re.compile(r"(?is)<(script|style|template|noscript)\b.*?</\1\s*>|<!--.*?-->")
_PDP_BLOCK_BOUNDARY_RE = re.compile(r"(?is)</(p|div|li|td|section|h[1-6])>|<br\s*/?>")
# Block-level closers WITHOUT <br>, plus the <br> tag alone. Used by the join-<br>
# segmentation variant: some themes wrap ONE continuous ingredient list across
# several visual lines with <br> (even mid-ingredient-name, e.g. dasique's
# "Ethylhexyl<br/>Hydroxystearate"), so treating <br> as a break shreds the list.
# The join variant collapses <br> to a space and only breaks on real block tags.
_PDP_BLOCK_ONLY_RE = re.compile(r"(?is)</(p|div|li|td|section|h[1-6])>")
_PDP_BR_RE = re.compile(r"(?is)<br\s*/?>")
# A leading "Full Ingredients:" / "INCI —" / "[INGREDIENTS]" label sometimes shares
# the <p>/value with the list (cosrx / barr-cosmetics); strip it (incl. surrounding
# brackets/parens) so the written value is the list itself.
_PDP_INCI_LABEL_RE = re.compile(
    r"(?i)^\s*[\[(]?\s*"
    r"(?:full\s+|all\s+|key\s+|active\s+|main\s+)?"
    r"(?:ingredients?(?:\s+list)?|inci)"
    r"\s*[\])]?\s*[:\-–—]?\s*"
)
# A full INCI opens with the highest-concentration ingredient, which for an
# aqueous cosmetic is water/aqua (regulatory descending-order). Matching this is
# the FAST qualifier: it cleanly tells a full INCI from a short "key ingredients"
# highlight (which opens with an active) and rejects comma-heavy noise. But it is
# NOT required — a real full INCI can open with a non-water ingredient (a Centella
# serum whose extract outranks water; an anhydrous balm/cushion that opens with a
# wax/oil). Those qualify via `_pdp_is_full_inci`'s secondary path instead, so we
# no longer leave them null. The same regex also tests an individual comma-part for
# "is this ingredient the solvent?" (used to require water be PRESENT in the list).
_PDP_SOLVENT_OPENER_RE = re.compile(
    r"(?i)^\s*(?:purified\s+|deionized\s+|distilled\s+)?(?:aqua|water|eau)\b"
)
# JSON string literals in a data island; pre-filtered to comma-bearing strings
# that plausibly contain the solvent before the (costlier) decode+gate.
_PDP_JSON_STR_RE = re.compile(r'"((?:[^"\\]|\\.){20,8000})"')
_PDP_SOLVENT_HINT_RE = re.compile(r"(?i)(?:aqua|water|eau)")
# A full INCI lists many ingredients; a "key ingredients" highlight is short. The
# secondary (non-water-opener) qualifier requires at least this many parts so a
# short active-forward highlight can never be mistaken for a full list.
_PDP_FULL_INCI_MIN_PARTS = 10
# Shape of a real INCI opening token: a short noun phrase of chemical/botanical
# words, no sentence punctuation. Rejects a product-name/heading that a join-<br>
# pass may prepend to a list (e.g. "GLOW LAYERING FIT CUSHION (NO.17 IVORY) WATER").
_PDP_INGREDIENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9 ()\-./+'&]+$")
# When several distinct full lists appear on one page they are EITHER shade variants
# of the SAME product (collapse to one) OR different products — a bundle/kit, or a
# neighbor/related-product's list carried in a recommendations JSON island (stay
# ambiguous -> None, never attribute another product's list to this page). The
# separation is tight and precision-critical: measured 2026-07-20, real shade groups
# score ingredient-set Jaccard 0.90 (misshaus cushion, min pairwise) to 0.98
# (dasique balm), while two GENUINELY DIFFERENT same-line K-beauty products that
# share a large aqueous base score only ~0.67–0.73. A 0.7 floor (the first cut of
# this fix) wrongly collapsed those neighbors and, picking the longest, PUBLISHED the
# neighbor's list. The floor is raised to 0.85 (well above the ~0.73 different-product
# ceiling, below the 0.90 shade floor) AND gated by two corroborating signals that a
# shared-base neighbor fails: identical opening ingredient and near-identical length.
_PDP_SHADE_SIM_MIN = 0.85
# Shade variants are the same formula (reordered / pigment-tail only), so their
# ingredient COUNT barely moves; a different product (toner vs serum) differs more.
_PDP_SHADE_LEN_RATIO = 1.15


def _pdp_visible_segments(page_html: str, *, join_br: bool = False) -> List[str]:
    """Block-level VISIBLE text lines: drop code/comment blocks, turn block-closers
    into newlines (so a self-contained <p> INCI is its own line), strip tags,
    unescape entities, collapse whitespace. Covers the accordion/modal/rich-text
    <div> surface.

    Two <br> modes, both run by the extractor: the default treats <br> as a line
    break (separates a product-name heading from its list — misshaus's
    `<strong>NAME</strong><br>WATER, ...`); `join_br=True` treats <br> as a space
    so a single list wrapped across many <br> lines is reassembled whole (dasique's
    `Ethylhexyl<br/>Hydroxystearate, ...`). Fragments from the break mode that are
    sub-strings of the reassembled list are dropped downstream."""
    view = _PDP_STRIP_RE.sub(" ", page_html)
    if join_br:
        view = _PDP_BR_RE.sub(" ", view)
        view = _PDP_BLOCK_ONLY_RE.sub("\n", view)
    else:
        view = _PDP_BLOCK_BOUNDARY_RE.sub("\n", view)
    view = _HTML_TAG_RE.sub(" ", view)
    view = html.unescape(view)
    out: List[str] = []
    for line in view.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            out.append(line)
    return out


def _pdp_json_island_segments(page_html: str) -> List[str]:
    """Candidate lines from JSON string literals in a data island (including inside
    <script> JSON), covering the metafield-as-JSON surface: a rich-text text node
    (`{"type":"text","value":"Water, ..."}`) or an escaped-HTML metafield string
    (`"\\u003cp\\u003eWater, ...\\u003c/p\\u003e"`). Each candidate string is
    JSON-decoded, tag-stripped, and split on rich-text paragraph breaks."""
    if not _PDP_SOLVENT_HINT_RE.search(page_html):
        return []
    out: List[str] = []
    for m in _PDP_JSON_STR_RE.finditer(page_html):
        raw = m.group(1)
        if "," not in raw or not _PDP_SOLVENT_HINT_RE.search(raw):
            continue
        try:
            decoded = json.loads('"' + raw + '"')
        except Exception:  # noqa: BLE001 — a non-JSON match just isn't a candidate
            continue
        decoded = html.unescape(_HTML_TAG_RE.sub(" ", decoded))
        for chunk in re.split(r"\n\s*\n|\r", decoded):
            chunk = re.sub(r"\s+", " ", chunk).strip()
            if chunk:
                out.append(chunk)
    return out


def _pdp_ingredient_like(part: str) -> bool:
    """Does a comma-part look like a single INCI ingredient (short chemical/botanical
    noun phrase) rather than a product-name/heading or a sentence? Guards the opener
    of a non-water-opening list so a join-<br> pass can't smuggle a leading heading
    ("GLOW LAYERING FIT CUSHION (NO.17 IVORY) WATER") in as the first "ingredient"."""
    p = part.strip()
    if not p or len(p.split()) > 6:
        return False
    if not _PDP_INGREDIENT_TOKEN_RE.match(p):
        return False
    return bool(re.search(r"[A-Za-z]{3}", p))


def _pdp_is_full_inci(cand: str) -> bool:
    """A candidate that already cleared `_looks_like_inci_list` is a FULL product
    INCI when EITHER it opens with the solvent (aqueous product, INCI descending-
    order — the fast, high-precision path) OR it is unambiguously a full list that
    happens to open with a non-water ingredient: a clean ingredient opener, MANY
    ingredients, and water/aqua present somewhere as an ingredient. The secondary
    path recovers Centella serums, balms and cushions (opener is an extract/wax/oil)
    without admitting a short "key ingredients" highlight (few parts, active opener,
    typically no water)."""
    if _PDP_SOLVENT_OPENER_RE.match(cand):
        return True
    parts = [p.strip() for p in cand.split(",") if p.strip()]
    if len(parts) < _PDP_FULL_INCI_MIN_PARTS:
        return False
    if not _pdp_ingredient_like(parts[0]):
        return False
    return any(_PDP_SOLVENT_OPENER_RE.match(p) for p in parts)


def _pdp_norm_key(text: str) -> str:
    """Alphanumeric-only lowercase fingerprint of a list (for dedup / substring tests)."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _pdp_parts(text: str) -> List[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _pdp_solvent_opener_count(cand: str) -> int:
    """How many comma-parts are a solvent opener (water/aqua/eau at the start of the
    part). A real INCI has EXACTLY ONE solvent entry; two means two lists were
    concatenated (a kit/routine block whose per-product lists were joined across a
    bare <br>). Note the first solvent may be mid-list — a Centella serum opens with
    the extract and lists Water at #4 — so we count occurrences, not position."""
    return sum(1 for p in _pdp_parts(cand) if _PDP_SOLVENT_OPENER_RE.match(p))


def _pdp_inci_similarity(a: str, b: str) -> float:
    """Jaccard overlap of two lists' normalized ingredient SETS (order-independent —
    shade variants reorder ingredients). 1.0 = identical set, 0.0 = disjoint."""
    def _set(text: str) -> set:
        return {_pdp_norm_key(p) for p in text.split(",") if _pdp_norm_key(p)}
    sa, sb = _set(a), _set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _pdp_collapse_shades(cands: List[str]) -> Optional[str]:
    """Given >1 distinct full lists, return one ONLY when they are shade variants of
    the SAME product; else None (never guess which product's list to attribute).

    Three corroborating signals must ALL hold vs the longest (most complete) list —
    a shared-base neighbor or a bundle component fails at least one:
      * ingredient-set Jaccard >= _PDP_SHADE_SIM_MIN (near-identical formula);
      * identical opening ingredient (same #1 by concentration);
      * ingredient count within _PDP_SHADE_LEN_RATIO (a variant barely changes size).
    Comparing every candidate to the longest ref (not just the top two) means one odd
    list out of many still forces None."""
    ref = max(cands, key=len)
    ref_parts = _pdp_parts(ref)
    if not ref_parts:
        return None
    ref_opener = _pdp_norm_key(ref_parts[0])
    for cand in cands:
        if cand is ref:
            continue
        parts = _pdp_parts(cand)
        if not parts or _pdp_norm_key(parts[0]) != ref_opener:
            return None
        lo, hi = sorted((len(parts), len(ref_parts)))
        if lo == 0 or hi > lo * _PDP_SHADE_LEN_RATIO:
            return None
        if _pdp_inci_similarity(ref, cand) < _PDP_SHADE_SIM_MIN:
            return None
    return ref


def inci_from_pdp_html(page_html: Optional[str]) -> Optional[str]:
    """Deterministic INCI extraction from a RENDERED brand PDP.

    Scans the visible block text (accordion/modal/rich-text div) and the JSON data
    island (metafield rendered as a JSON string) for FULL ingredient lists. A segment
    is a candidate when it clears the strong INCI-list gate (`_looks_like_inci_list` —
    the reseller-tier safety net) AND reads as a FULL list (`_pdp_is_full_inci`) AND
    is a SINGLE list (exactly one solvent opener; two = two products concatenated).
    Then, so we never guess or fabricate:

      * exactly one distinct full INCI -> return it;
      * several that are shade variants of ONE product -> the longest (via
        `_pdp_collapse_shades`; near-identical set + opener + length);
      * several genuinely different lists (a bundle, or a neighbor/related-product's
        list in a recommendations island) -> None (never attribute another's list);
      * none -> None.

    The <br> handling is deliberately two-tier for precision. The DEFAULT visible pass
    breaks on <br>, so two products separated by a bare <br> in one block become two
    SEPARATE candidates (their ambiguity is then visible). A SECOND join-<br> pass
    reassembles a single list a theme wrapped across many <br> — but it may only
    REPAIR: a join candidate is adopted only when it strictly contains exactly ONE
    trusted (default/JSON) candidate (the <br>-shredded fragment made whole). A join
    candidate that spans TWO trusted candidates is a concatenation, not a repair, and
    is discarded — so join-<br> can never fabricate a franken list the strict passes
    didn't already see as separate.

    Whatever it returns is re-validated by
    `canonical_inci_intake.ingest_canonical_inci` before any write."""
    if not page_html:
        return None
    page_html = str(page_html)

    def _collect(segments) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for seg in segments:
            cand = _PDP_INCI_LABEL_RE.sub("", seg).strip().rstrip(". ").strip()
            if not _looks_like_inci_list(cand):
                continue
            if not _pdp_is_full_inci(cand):
                continue
            if _pdp_solvent_opener_count(cand) >= 2:
                continue  # two lists concatenated into one string -> not a single INCI
            key = _pdp_norm_key(cand)
            if key and (key not in out or len(cand) > len(out[key])):
                out[key] = cand
        return out

    # Trusted candidates come from REAL block/JSON boundaries.
    base = _collect((
        *_pdp_visible_segments(page_html),
        *_pdp_json_island_segments(page_html),
    ))
    # Reassembly candidates come from collapsing <br> to a space within a block.
    joined = _collect(_pdp_visible_segments(page_html, join_br=True))

    pool: Dict[str, str] = dict(base)
    for jkey, jcand in joined.items():
        contained = [bkey for bkey in base if bkey != jkey and bkey in jkey]
        if len(contained) >= 2:
            # The reassembled string strictly contains TWO OR MORE distinct trusted
            # lists -> it concatenated separate products (a kit/routine block), not a
            # single <br>-wrapped list. Discard it and keep the trusted lists apart so
            # the ambiguity check sees them. (Aqueous+aqueous concatenations are
            # already rejected upstream by the two-solvent-opener guard; this also
            # catches a concatenation whose second list is anhydrous.)
            continue
        # 0 or 1 trusted fragment inside: a single list the theme wrapped across <br>.
        # Adopt the reassembled whole; drop the lone shredded fragment if base saw one.
        for bkey in contained:
            pool.pop(bkey, None)
        pool[jkey] = jcand

    if not pool:
        return None
    # Same-list cleanup among trusted candidates: drop a candidate that is a contiguous
    # substring of another (e.g. a JSON-island whole vs a visible partial of the SAME
    # list). Concatenations were already rejected above, so a substring here is a
    # genuine partial of one list, never a distinct product.
    keys = list(pool)
    survivors = [
        cand
        for key, cand in pool.items()
        if not any(other != key and key in other for other in keys)
    ]
    if len(survivors) == 1:
        return survivors[0]
    return _pdp_collapse_shades(survivors)


async def fetch_pdp_inci(
    domain: str,
    handle: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 15.0,
) -> Optional[str]:
    """Fetch a single brand PDP and recover its INCI via `inci_from_pdp_html`.

    The polite, additive fallback for the (very common) cohort product whose
    `/products.json` body_html carries no ingredients. Returns None on any
    network/parse failure or when the PDP doesn't publish a recoverable INCI — a
    brand site hiccup must never fabricate or raise into the mint/backfill loop."""
    host = _clean_domain(domain)
    handle = str(handle or "").strip().strip("/")
    if not host or not handle:
        return None
    url = f"https://{host}/products/{handle}"
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "text/html"}
    try:
        # Gated on BOTH branches. The caller-supplied-client branch is the one the batch loop
        # uses, so gating only the standalone branch would leave the high-volume path unpaced —
        # the shape of "a guard on one path does not cover the path that bypasses it".
        await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as c:
                resp = await c.get(url)
        crawl_politeness.note_response(
            url, resp.status_code, retry_after=resp.headers.get("retry-after")
        )
        if resp.status_code != 200:
            return None
        return inci_from_pdp_html(resp.text)
    except Exception as exc:  # noqa: BLE001 — a brand PDP being down must not break the batch
        logger.debug("fetch_pdp_inci failed for %s/%s: %s", host, handle, str(exc)[:160])
        return None


def _to_float(value: Any) -> Optional[float]:
    """Coerce Shopify's string prices (e.g. '56.00') to float; None if absent/invalid.
    Numeric columns (catalog_offers.*_price, external_product_seeds.price_amount) reject
    strings, so the mapper must hand downstream a real number or None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def shopify_product_to_record(
    product: Dict[str, Any],
    *,
    domain: str,
    category_path: str,
    brand_override: Optional[str] = None,
    emit_variants: bool = False,
    currency: Optional[str] = None,
    market: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Map one Shopify `/products.json` product → a Path-C validated record
    (`{pdp, offers}`). Returns None if it lacks a title/handle (not actionable).
    The brand storefront is the authoritative PDP, so the offer is brand-direct
    and carries the variant barcode (GTIN) when present."""
    if not isinstance(product, dict):
        return None
    host = _clean_domain(domain)
    title = str(product.get("title") or "").strip()
    handle = str(product.get("handle") or "").strip()
    if not title or not handle:
        return None
    brand = str(brand_override or product.get("vendor") or "").strip()
    if not brand:
        return None
    variants = product.get("variants")
    variants = variants if isinstance(variants, list) else []
    # Pick the first sellable variant — priced at or above MIN_SELLABLE_PRICE.
    # Gift-with-purchase and other $0/unpriced items have no purchasable offer —
    # drop the product entirely so it never enters the commerce index (these were
    # landing as junk PDPs/seeds, the offers_skipped noise seen onboarding kosas).
    # The floor exists because "positive" was not enough: stilacosmetics.com lists a
    # "Free Travel … (TikTok Shop)" promo at $0.01, which cleared `p > 0`, ingested
    # as a canonical anchor and served (measured 2026-09-05). A token price is a
    # promo mechanic, not an offer.
    variant = None
    price = None
    for v in variants:
        p = _to_float((v or {}).get("price"))
        if p is not None and p >= MIN_SELLABLE_PRICE:
            variant, price = v, p
            break
    if variant is None:
        return None
    image = _first(product.get("images")) or {}
    if not str(image.get("src") or "").strip():
        # No product-level image: a variant's own swatch is a real image of this
        # product and is better than publishing a row the scorer counts as
        # imageless. Only a fallback — a product image always wins.
        for _v in variants:
            _fi = _v.get("featured_image") if isinstance(_v, dict) else None
            _src = (
                str((_fi or {}).get("src") or "").strip() if isinstance(_fi, dict)
                else str((_v or {}).get("image_src") or "").strip()
            )
            if _src:
                image = {"src": _src}
                break
    # A FOLDED row is a product LINE, not one physical item: its variants are the
    # shades, each with its own GTIN. Taking the first shade's barcode as the line's
    # would publish (say) Ruby Woo's GTIN on "Retro Matte Lipstick", and GTIN is
    # Tier-0a in identity resolution — it OUTRANKS brand+title, so a retailer's
    # single-shade PDP carrying that GTIN would attach to the whole line. The stub
    # the fold replaced carried no barcode; the line keeps none.
    barcode = (
        None if product.get(FOLDED_INTO_KEY)
        else (str(variant.get("barcode") or "").strip() or None)
    )
    # Every sellable variant, when the product has more than one: the ingest
    # writes one SKU + offer per entry beside the canonical SKU, so a folded
    # shade line (see fold_shade_listings) keeps its purchasable SKUs. Single-
    # variant products emit nothing here — the canonical SKU already is the row.
    sellable = [
        v for v in variants
        if isinstance(v, dict) and (_to_float(v.get("price")) or 0.0) >= MIN_SELLABLE_PRICE
    ]
    pdp_variants: List[Dict[str, Any]] = []
    # OPT-IN. Emitting variants writes one extra SKU + offer per variant downstream,
    # which changes recall fan-out, offer aggregation and INCI attachment for EVERY
    # row a caller ingests — so it fires only for the fold lane that asked for it.
    # `records_for_brand(base_listings_only=True)` is the only caller that does.
    if emit_variants and len(sellable) >= 1 and product.get(FOLDED_INTO_KEY):
        seen_ids: set = set()
        for i, v in enumerate(sellable):
            vid = str(v.get("id") or v.get("variant_id") or f"{handle}:{i}").strip()
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            # option1 is the merchant's own shade value and outranks a name derived
            # from the title suffix; `featured_image` is where a real Shopify variant
            # carries its swatch (`image_src` is set only by the fold).
            featured = v.get("featured_image")
            featured_src = str((featured or {}).get("src") or "").strip() if isinstance(featured, dict) else ""
            pdp_variants.append({
                "variant_id": vid,
                "sku": str(v.get("sku") or "").strip() or None,
                "barcode": str(v.get("barcode") or "").strip() or None,
                "title": str(v.get("option1") or v.get("title") or "").strip() or None,
                "price": _to_float(v.get("price")),
                "in_stock": bool(v.get("available")),
                "image_url": (
                    featured_src
                    or str(v.get("image_src") or "").strip()
                    or str(image.get("src") or "").strip()
                    or None
                ),
                "source_handle": str(v.get(FOLDED_FROM_KEY) or "").strip() or None,
            })
    raw_tags = product.get("tags")
    tags = (
        raw_tags
        if isinstance(raw_tags, list)
        else [t.strip() for t in str(raw_tags or "").split(",") if t.strip()]
    )
    canonical_url = f"https://{host}/products/{handle}"
    return {
        "pdp": {
            "brand": brand,
            "product_name": title,
            "category_path": category_path,
            # Brand-authored body copy when present (it becomes the row's
            # description and feeds the lifecycle candidate gate + taxonomy
            # extractors); product_type alone otherwise. Rows minted without
            # body copy land 'draft' and rely on the description backfill /
            # LLM enrichment lane to promote.
            "attribute_summary": (
                body_html_to_text(product.get("body_html"))
                or str(product.get("product_type") or "").strip()
            ),
            "barcode": barcode,  # real GTIN when the brand fills it — strongest deposit basis
            "source_domain": host,
            "tags": tags,
            # Brand-official INCI when the storefront lists it under an Ingredients
            # heading (many don't — None then, ingest skips it). brand_official is
            # the top INCI authority tier (ADR-001) so it outranks reseller lists.
            "raw_inci": inci_from_body_html(product.get("body_html")),
            "inci_source": "brand_official",
            # Shopify /products.json exposes no review aggregate — ratings stay null
            # on this lane (captured on the retailer-PDP lane instead).
            "rating_value": None,
            "rating_count": None,
            # The STOREFRONT's own currency/market, from /meta.json. Omitted (None) rather than
            # defaulted here: the ingest lane owns the fallback, so a record that never learned
            # its currency is indistinguishable from one that did and is genuinely USD.
            "currency": currency,
            "market": market,
            "variants": pdp_variants,
        },
        "offers": [
            {
                "merchant_inferred": brand,
                "canonical_url": canonical_url,
                "destination_url": canonical_url,
                "image_url": str(image.get("src") or "").strip(),
                "price": price,
                "in_stock": bool(variant.get("available")),
                "validated_at": "shopify_products_json",
            }
        ],
    }


# Some storefronts (maccosmetics.com, measured 2026-09-04: 1,366 of a 1,500-product
# sample) publish EVERY shade as its own single-variant product — "Retro Matte
# Lipstick - Ruby Woo" beside the base "Retro Matte Lipstick". The Path-C plan keys
# PDPs on (brand, title), so ingesting that feed as-is mints one PDP per shade:
# ~1,900 near-duplicates for one brand.
#
# `fold_shade_listings` FOLDS those shade rows into the base listing's variants
# instead of dropping them: the base keeps one PDP, and every shade becomes a
# variant of it (title = shade name, its own sku / barcode / price / image), so
# the purchasable SKUs survive. Measured on the MAC feed, every base row is
# itself a single-variant PARENT STUB (variants[0].option1 == title, sku P2000_*):
# that stub variant is replaced by the shades, never kept beside them. A base
# that already carries real variants keeps them and gains the folded shades.
#
# Titles are compared through `normalize_title` (the same normaliser
# `make_content_key` uses downstream), because the feed is not case- or
# punctuation-stable across a line: stila lists "HUGE™ Extreme Lash Mascara" beside
# "Huge™ Extreme Lash Mascara - Intense Black", and "Heaven's" beside "Heaven’s".
# Shade names may themselves contain hyphens ("Lady-Be-Good", "Brick-O-La"), so
# every " - " split point is tried, longest base first.
_SHADE_SEP = " - "
FOLDED_FROM_KEY = "_folded_from_handle"
FOLDED_INTO_KEY = "_folded_shades"
# A suffix that names a merchandising state, not a shade. tarte sells "<line> - <X>
# charm" as separate $10 accessories and stila suffixes "- Last Chance"/"- Limited
# Edition" onto whole palettes; folding those makes an accessory a "shade" of the
# product it accessorises and destroys its own PDP. Measured 2026-09-05: 9 such
# false folds across the five cached feeds, 0 legitimate shades excluded.
_NON_SHADE_SUFFIX_RE = re.compile(
    r"(?i)\b(charm|last chance|limited edition|refill|travel size|mini|set|kit|bundle|gift card|sample)\b"
)
# A shade of a product costs what the product costs. A folded row priced far from its
# base is a different item wearing a similar name.
_FOLD_PRICE_RATIO = 1.5


def _shade_bases(title: str) -> List[str]:
    """Every '<base>' a '<base> - <shade>' title could be split into, longest
    base first, so 'Lip Pencil - Brick-O-La' yields ['Lip Pencil - Brick-O',
    'Lip Pencil']. Only ' - ' (space-hyphen-space) is a separator."""
    parts = title.split(_SHADE_SEP)
    return [_SHADE_SEP.join(parts[:i]).strip() for i in range(len(parts) - 1, 0, -1)]


def _image_srcs(product: Dict[str, Any]) -> List[str]:
    """Every usable image URL on a Shopify product row, in feed order."""
    out: List[str] = []
    for img in (product or {}).get("images") or []:
        src = str((img or {}).get("src") or "").strip() if isinstance(img, dict) else str(img or "").strip()
        if src:
            out.append(src)
    return out


def _first_price(product: Dict[str, Any]) -> Optional[float]:
    for v in (product or {}).get("variants") or []:
        p = _to_float((v or {}).get("price")) if isinstance(v, dict) else None
        if p is not None and p > 0:
            return p
    return None


def _fold_refused(base: Dict[str, Any], shade: Dict[str, Any], suffix: str) -> Optional[str]:
    """Why this row must NOT be folded into that base, or None to fold."""
    if _NON_SHADE_SUFFIX_RE.search(suffix or ""):
        return "non_shade_suffix"
    bp, sp = _first_price(base), _first_price(shade)
    if bp and sp and (max(bp, sp) / min(bp, sp)) > _FOLD_PRICE_RATIO:
        return "price_mismatch"
    return None


def _is_stub_variant(product: Dict[str, Any]) -> bool:
    """A single placeholder variant that names no shade: its option/title is the
    product's own title or Shopify's 'Default Title'. MAC's P2000_ parents are
    this shape; a real single-shade product ('Ruby Woo' as option1) is not."""
    variants = (product or {}).get("variants") or []
    if len(variants) != 1:
        return False
    v = variants[0] or {}
    title = str((product or {}).get("title") or "").strip()
    label = str(v.get("option1") or v.get("title") or "").strip()
    return label in ("", "Default Title", title)


def fold_shade_listings(products: List[Dict[str, Any]]) -> "Tuple[List[Dict[str, Any]], Dict[str, Any]]":
    """Pure. Fold single-variant `<base> - <shade>` rows into the variants of
    the base row (matched through normalize_title). Returns (products, report):
    the base rows now carry the shades as variants (a stub placeholder variant
    is replaced; real variants are kept and extended), the shade rows are
    removed, order is otherwise preserved, and multi-variant rows are never
    folded — a suffixed multi-variant title is a distinct line, not a shade.
    `report` names what happened so the caller can print it: bases folded,
    shade rows folded, stub variants replaced, and every folded handle by base."""
    from services.catalog_identity import normalize_title

    by_norm: Dict[str, Dict[str, Any]] = {}
    for p in products:
        key = normalize_title(str((p or {}).get("title") or ""))
        if key and key not in by_norm:
            by_norm[key] = p
    folded_into: Dict[int, List[Dict[str, Any]]] = {}  # id(base) -> shade rows
    shade_of: Dict[int, Dict[str, Any]] = {}            # id(shade row) -> base
    refusals: List[Dict[str, str]] = []
    for p in products:
        title = str((p or {}).get("title") or "").strip()
        variants = (p or {}).get("variants") or []
        if len(variants) > 1:
            continue
        for base_title in _shade_bases(title):
            base = by_norm.get(normalize_title(base_title)) if base_title else None
            if base is None or base is p:
                continue
            suffix = title[len(base_title):].lstrip(" -").strip()
            refused = _fold_refused(base, p, suffix)
            if refused:
                refusals.append({"handle": str(p.get("handle") or ""), "title": title, "reason": refused})
                break
            folded_into.setdefault(id(base), []).append(p)
            shade_of[id(p)] = base
            break
    report: Dict[str, Any] = {"bases": 0, "shades": 0, "stubs_replaced": 0, "images_adopted": 0,
                             "folded": {}, "refused": refusals}
    out: List[Dict[str, Any]] = []
    for p in products:
        if id(p) in shade_of:
            continue
        shades = folded_into.get(id(p))
        if not shades:
            out.append(p)
            continue
        base_title = str(p.get("title") or "").strip()
        base = dict(p)
        own = [] if _is_stub_variant(p) else [
            dict(v, title=str(v.get("title") or v.get("option1") or "").strip())
            for v in (p.get("variants") or []) if isinstance(v, dict)
        ]
        if not own and (p.get("variants") or []):
            report["stubs_replaced"] += 1
        new_variants: List[Dict[str, Any]] = list(own)
        handles: List[str] = []
        for s in shades:
            shade_title = str(s.get("title") or "").strip()
            for bt in _shade_bases(shade_title):
                if normalize_title(bt) == normalize_title(base_title):
                    shade_name = shade_title[len(bt):].lstrip(" -").strip() or shade_title
                    break
            else:
                shade_name = shade_title
            sv = dict((s.get("variants") or [{}])[0] or {})
            # The shade row's OWN option1 is the merchant's shade value and wins:
            # stila's "Calligraphy Lip Stain - Last Chance Shade" carries
            # option1 "Elizabeth (Pinky Nude)", and taking the title suffix minted a
            # phantom second SKU for the same merchant code.
            own_label = str(sv.get("option1") or "").strip()
            if own_label and own_label.lower() not in ("default title",):
                shade_name = own_label
            sv["title"] = shade_name
            sv["option1"] = shade_name
            sv.setdefault("id", s.get("id"))
            img = _first(s.get("images")) or {}
            if img.get("src"):
                sv["image_src"] = str(img.get("src"))
            sv[FOLDED_FROM_KEY] = str(s.get("handle") or "")
            new_variants.append(sv)
            handles.append(str(s.get("handle") or ""))
        base["variants"] = new_variants
        base[FOLDED_INTO_KEY] = len(shades)
        # A parent stub carries no images of its own — measured on maccosmetics.com
        # 2026-09-05, 106 of 109 folded bases have an EMPTY `images` list while the
        # shade rows carry the swatches. The product row is what the quality scorer
        # reads (`_extract_main_image`), so a base left imageless forfeits the whole
        # images component: MAC scored 66.7 against a 71.4 gate and every row was
        # blocked `low_quality`. Adopt the folded shades' images when the base has
        # none; a base with its own images keeps them untouched.
        if not _image_srcs(p):
            adopted: List[Dict[str, Any]] = []
            seen_src: set = set()
            for s in shades:
                for src in _image_srcs(s):
                    if src not in seen_src:
                        seen_src.add(src)
                        adopted.append({"src": src})
            if adopted:
                base["images"] = adopted
                report["images_adopted"] += 1
        report["bases"] += 1
        report["shades"] += len(shades)
        report["folded"][str(p.get("handle") or base_title)] = handles
        out.append(base)
    return out, report


def drop_shade_listings(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compatibility name for `fold_shade_listings`: same collapse, report dropped."""
    return fold_shade_listings(products)[0]


async def records_for_brand(
    *,
    domain: str,
    category_path: str,
    brand: Optional[str] = None,
    max_products: int = 500,
    base_listings_only: bool = False,
    enrich_missing_inci: bool = False,
    max_pdp_inci_fetches: int = 300,
    # 0.0 since the shared politeness gate owns pacing. This ad-hoc sleep predates it and now
    # STACKS on top: every INCI fetch already waits its per-host interval, so a 0.3s sleep on
    # each of 300 fetches added ~90s of pure duplication. Left as a parameter rather than deleted
    # so a caller that wants extra slack on a specific brand can still ask for it.
    pdp_delay_s: float = 0.0,
) -> List[Dict[str, Any]]:
    """Fetch a curated brand's storefront and return Path-C validated records.

    When `enrich_missing_inci` is set, records whose body_html carried no INCI get
    a SECOND, polite try: fetch the product's own PDP and recover the metafield /
    accordion INCI via `fetch_pdp_inci` (the cohort keeps INCI out of body_html).
    Additive — body_html INCI stays the first try and is never overwritten here;
    the fetch is capped, delayed, and best-effort (a miss leaves raw_inci None)."""
    products = await fetch_shopify_products(domain, max_products=max_products)
    # ONCE per brand, not per product: it is one storefront-wide setting and a per-product fetch
    # would multiply outbound requests by the catalogue size against a single host.
    locale = await fetch_shopify_shop_locale(domain)
    if base_listings_only:
        products, fold_report = fold_shade_listings(products)
        records_for_brand.last_fold_report = fold_report  # type: ignore[attr-defined]
    records: List[Dict[str, Any]] = []
    pairs: List[Dict[str, Any]] = []  # (product, record) needing a PDP INCI try
    for p in products:
        rec = shopify_product_to_record(
            p, domain=domain, category_path=category_path, brand_override=brand,
            emit_variants=base_listings_only,
            currency=locale.get("currency"), market=locale.get("country"),
        )
        if not rec:
            continue
        records.append(rec)
        if enrich_missing_inci and not (rec.get("pdp") or {}).get("raw_inci"):
            handle = str((p or {}).get("handle") or "").strip()
            if handle:
                pairs.append({"handle": handle, "rec": rec})
    if enrich_missing_inci and pairs:
        timeout = httpx.Timeout(15.0, connect=5.0)
        headers = {"User-Agent": _UA, "Accept": "text/html"}
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            for i, pair in enumerate(pairs[:max_pdp_inci_fetches]):
                inci = await fetch_pdp_inci(domain, pair["handle"], client=client)
                if inci:
                    pair["rec"]["pdp"]["raw_inci"] = inci
                if pdp_delay_s and i + 1 < min(len(pairs), max_pdp_inci_fetches):
                    await asyncio.sleep(pdp_delay_s)
    return records
