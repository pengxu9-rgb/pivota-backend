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
from typing import Any, Dict, List, Optional

import httpx

from services.retailer_ingest.sitemap_crawler import _looks_like_inci_list

logger = logging.getLogger("curated_brand_feed")

_UA = "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)"
_PER_PAGE = 250  # Shopify max


def _clean_domain(domain: str) -> str:
    d = str(domain or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").rstrip("/")
    return d.split("/")[0]


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
                resp = await client.get(url)
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
# A leading "Full Ingredients:" / "INCI —" label sometimes shares the <p>/value
# with the list (cosrx); strip it so the written value is the list itself.
_PDP_INCI_LABEL_RE = re.compile(
    r"(?i)^\s*(?:full\s+|all\s+|key\s+|active\s+|main\s+)?ingredients?(?:\s+list)?\s*[:\-–—]?\s*"
)
# A full INCI opens with the highest-concentration ingredient, which for an
# aqueous cosmetic is water/aqua (regulatory descending-order). REQUIRING this
# does two jobs: it tells a full INCI from a short "key ingredients" highlight
# (which opens with an active) when a PDP carries both, AND it rejects comma-heavy
# non-INCI noise that can slip the list gate (image srcset URLs, JS arrays). The
# cost is anhydrous products (oil cleansers/balms) whose list opens with an oil —
# they stay null (counted), never mis-filled. The whole cohort is aqueous.
_PDP_SOLVENT_OPENER_RE = re.compile(
    r"(?i)^\s*(?:purified\s+|deionized\s+|distilled\s+)?(?:aqua|water|eau)\b"
)
# JSON string literals in a data island; pre-filtered to comma-bearing strings
# that plausibly contain the solvent before the (costlier) decode+gate.
_PDP_JSON_STR_RE = re.compile(r'"((?:[^"\\]|\\.){20,8000})"')
_PDP_SOLVENT_HINT_RE = re.compile(r"(?i)(?:aqua|water|eau)")


def _pdp_visible_segments(page_html: str) -> List[str]:
    """Block-level VISIBLE text lines: drop code/comment blocks, turn block-closers
    into newlines (so a self-contained <p> INCI is its own line), strip tags,
    unescape entities, collapse whitespace. Covers the accordion/modal/rich-text
    <div> surface."""
    view = _PDP_STRIP_RE.sub(" ", page_html)
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


def inci_from_pdp_html(page_html: Optional[str]) -> Optional[str]:
    """Deterministic INCI extraction from a RENDERED brand PDP.

    Scans BOTH the visible block text (accordion/modal/rich-text div) and the JSON
    data island (metafield rendered as a JSON string) for text that clears the
    strong INCI-list gate (`_looks_like_inci_list` — the same reseller-tier safety
    net the crawled-INCI lane relies on) AND opens with the solvent (a real full
    INCI, per INCI descending-order; this also rejects srcset/JS-array noise).
    Then, so we never guess or fabricate:

      * exactly one distinct full INCI -> return it;
      * >1 distinct full INCI (a bundle / related-product cards) -> None (ambiguous);
      * none -> None.

    Whatever it returns is re-validated by
    `canonical_inci_intake.ingest_canonical_inci` before any write."""
    if not page_html:
        return None
    page_html = str(page_html)
    # Normalized-key dedup: the same list rendered on two surfaces (visible <p> and
    # a JSON island, or mobile+desktop DOM) must collapse to one candidate, not read
    # as an ambiguous pair. Keep the longest raw string for a given key.
    by_key: Dict[str, str] = {}
    for seg in (*_pdp_visible_segments(page_html), *_pdp_json_island_segments(page_html)):
        cand = _PDP_INCI_LABEL_RE.sub("", seg).strip().rstrip(". ").strip()
        if not _PDP_SOLVENT_OPENER_RE.match(cand):
            continue
        if not _looks_like_inci_list(cand):
            continue
        key = re.sub(r"[^a-z0-9]", "", cand.lower())
        if key and (key not in by_key or len(cand) > len(by_key[key])):
            by_key[key] = cand
    if len(by_key) == 1:
        return next(iter(by_key.values()))
    return None  # 0 = none published; >1 = ambiguous (which product's list?)


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
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as c:
                resp = await c.get(url)
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
    # Pick the first sellable (positive-price) variant. Gift-with-purchase and other
    # $0/unpriced items have no purchasable offer — drop the product entirely so it
    # never enters the commerce index (these were landing as junk PDPs/seeds, the
    # offers_skipped noise seen onboarding kosas).
    variant = None
    price = None
    for v in variants:
        p = _to_float((v or {}).get("price"))
        if p is not None and p > 0:
            variant, price = v, p
            break
    if variant is None:
        return None
    image = _first(product.get("images")) or {}
    barcode = str(variant.get("barcode") or "").strip() or None
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


async def records_for_brand(
    *,
    domain: str,
    category_path: str,
    brand: Optional[str] = None,
    max_products: int = 500,
    enrich_missing_inci: bool = False,
    max_pdp_inci_fetches: int = 300,
    pdp_delay_s: float = 0.3,
) -> List[Dict[str, Any]]:
    """Fetch a curated brand's storefront and return Path-C validated records.

    When `enrich_missing_inci` is set, records whose body_html carried no INCI get
    a SECOND, polite try: fetch the product's own PDP and recover the metafield /
    accordion INCI via `fetch_pdp_inci` (the cohort keeps INCI out of body_html).
    Additive — body_html INCI stays the first try and is never overwritten here;
    the fetch is capped, delayed, and best-effort (a miss leaves raw_inci None)."""
    products = await fetch_shopify_products(domain, max_products=max_products)
    records: List[Dict[str, Any]] = []
    pairs: List[Dict[str, Any]] = []  # (product, record) needing a PDP INCI try
    for p in products:
        rec = shopify_product_to_record(
            p, domain=domain, category_path=category_path, brand_override=brand
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
