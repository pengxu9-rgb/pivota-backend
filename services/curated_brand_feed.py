"""Curated-brand-list feed — the CLEAN primary source for catalog coverage.

Given a curated list of brand storefront domains, enumerate their products via
Shopify's PUBLIC `/products.json` and turn each into a Path-C *validated record*
(the `{pdp, offers}` shape `ingestion.ingest_validated_record` consumes). The
brand's own storefront is the authoritative PDP, so this bypasses Gemini URL
resolution entirely — it's deterministic, cheap, and carries the brand's own
title/price/image, variant **barcode (GTIN)**, and tags. The records then ingest
as depositable canonical anchors via the existing FK-order executor.

This is the "crawl the brand before they integrate" engine for the (very common)
Shopify-hosted D2C brand. Non-Shopify/failed or capped scans raise CrawlIncomplete
so callers cannot ingest a prefix as a completed catalog. PURE-ish: this module fetches public pages + builds records; the
caller runs `ingest_validated_jsonl` + `apply_ingest_plan` (gated).
"""

from __future__ import annotations

import asyncio
import collections
import html
import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin, urlsplit

import httpx

from services.catalog_identity import validated_source_gtin
from services import storefront_currency

from services.retailer_ingest.sitemap_crawler import _looks_like_inci_list
from services import crawl_politeness
from services.variant_identity import MERCHANT_ISSUED, variant_id_provenance

logger = logging.getLogger("curated_brand_feed")

_UA = "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)"
_PER_PAGE = 250  # Shopify max
# Lowest variant price (in the store's currency) that counts as a real offer. Across
# the four Meitu-US feeds measured 2026-09-05 (2,108 products) exactly one variant sat
# in (0, 1.00): the $0.01 stila promo described at the variant pick below. Nothing legitimate in a beauty D2C feed is
# priced under a dollar; a floor this low cannot drop a real product.
MIN_SELLABLE_PRICE = 1.0

# The axis a variant varies on, named the way the shop names it. Shopify reports
# a product's axes in `options`, and `option1` is a value on the FIRST of them.
# A shop with no axis at all reports the placeholder "Title" / "Default Title",
# which names nothing.
# "Color", NOT "Shade", and the difference is not cosmetic. The renderer treats
# the two names ASYMMETRICALLY when its own keyword gate does not read the
# product as cosmetic: `shade|tone|hue|undertone` falls through to
# NON_DISPLAYABLE, while `color|colour` falls through to a working `color` axis
# (both still divert a volume-looking value to a volume axis first). Measured
# across the folded bases of the six cached brand feeds, this literal alone is
# the difference between 48 and 75 of 79 products rendering a selector — the
# axis we invent should be the one the consumer accepts.
_DEFAULT_SHADE_OPTION_NAME = "Color"
_PLACEHOLDER_OPTION_NAMES = {"title", "option", "variant", "selection", "default title"}
_SHADE_AXIS_NAMES = {"shade", "color", "colour", "tone", "hue"}


def _base_option_name(product: Dict[str, Any]) -> str:
    """The shop's own name for the product's FIRST option axis, "" if it names none."""
    # Shopify `/products.json` emits `options` as a list of dicts, and it is the
    # only writer that reaches here — the string-shaped branches this used to
    # carry were unfalsifiable by any input, so they are gone rather than left
    # as coverage nobody can earn.
    options = product.get("options")
    first = options[0] if isinstance(options, list) and options else None
    name = str(first.get("name") or "").strip() if isinstance(first, dict) else ""
    if name.lower() in _PLACEHOLDER_OPTION_NAMES:
        return ""
    return name


_SIZE_LIKE_VALUE = re.compile(
    r"""(?ix)
    ^\s*(?:
        [\d.,/]+\s*(?:ml|l|g|kg|mg|oz|fl\.?\s*oz|floz|lb|ct|count|pc|pcs|pack|x)\b
      | (?:x?\s*[\d.,]+\s*(?:ml|g|oz))
      | (?:travel|mini|deluxe|jumbo|full|full\s*size|trial|sample|refill)\s*(?:size)?
      | (?:small|medium|large|x-?large|xs|s|m|l|xl|xxl|one\s*size)
    )\s*$
    """
)


def _looks_like_a_size(value: str) -> bool:
    """A quantity, a pack, or a garment/format size — never a colour."""
    return bool(_SIZE_LIKE_VALUE.match(value or ""))


def _variant_option_name(variant: Dict[str, Any], base_option_name: str) -> str:
    """The axis THIS variant varies on — per variant, because a folded product's
    variant list is not homogeneous.

    `fold_shade_listings` appends variants taken from OTHER products (the
    per-shade listings) onto a base that keeps its own `options`. On a base whose
    real axis is Size — a foundation sold in 30ml and 50ml — naming every variant
    from `options[0]` published the shades as "Size: NC15". That is not just an
    ugly label: the renderer only demands a swatch when the axis reads as a
    shade, so a mislabelled shade also rendered without one.

    A folded-in variant is on the shade axis BY CONSTRUCTION, whatever the base
    calls its own. The base's own variants really are on the base's axis, so they
    keep it — and "" when the shop names no axis, because a guess would be a
    label the merchant never wrote.
    """
    # PRESENCE, not truthiness. The fold stamps this key with the handle it took
    # the variant from, and a shade row with an empty handle stores "" — which a
    # truthiness test reads as "not folded", handing that shade the base's own
    # axis and reproducing the mislabel this function exists to prevent.
    if FOLDED_FROM_KEY in variant:
        if base_option_name.strip().lower() in _SHADE_AXIS_NAMES:
            return base_option_name
        # The fold collapses listings that differ by a TITLE SUFFIX, and a suffix
        # is not always a shade: "Fix+ - 3.4 fl oz" and "Blot Powder - Medium"
        # fold exactly like "Retro Matte Lipstick - Ruby Woo". Inventing a colour
        # axis over a quantity publishes "Color: 3.4 fl oz". When the shop has not
        # named an axis we can trust and the value reads as a size, decline —
        # naming nothing is the honest answer, and by the product-level rule the
        # whole product then serves as the bare list it was before.
        if _looks_like_a_size(str(variant.get("option1") or variant.get("title") or "")):
            return ""
        return _DEFAULT_SHADE_OPTION_NAME
    return base_option_name


def _clean_domain(domain: str) -> str:
    d = str(domain or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").rstrip("/")
    return d.split("/")[0]


def _same_storefront_host(requested: str, actual: Optional[str]) -> bool:
    """Is `actual` (a redirect chain's final host) the SAME storefront we asked?

    Only the `www.` prefix is treated as noise. A SUBDOMAIN is not: `uk.brand.com` and
    `shop.brand.com` are separate Shopify stores with their own `shop.description`, their own
    catalogue and their own currency, which is precisely the confusion a host pin exists to
    refuse. Suffix matching (`endswith(host)`) would accept both, and would additionally accept
    `evilbrand.com` for `brand.com`.
    """
    a = str(actual or "").strip().lower().rstrip(".")
    r = str(requested or "").strip().lower().rstrip(".")
    if not a or not r:
        return False
    return a.removeprefix("www.") == r.removeprefix("www.")


_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")


async def fetch_shopify_shop_locale(
    domain: str,
    *,
    timeout_s: float = 10.0,
) -> Dict[str, Optional[str]]:
    """The storefront's own currency, via the module that already reads /meta.json.

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

    CURRENCY ONLY, and `country` is deliberately NOT returned. `storefront_currency`'s own
    docstring records why they are different axes ("a KR/HK exporter legitimately prices in USD"),
    and measurement made the asymmetry concrete: `external_product_seeds.market` is a HARD serving
    partition -- `external_seed_search` appends `market = :market` and every serving caller passes
    DEFAULT_EXTERNAL_SEED_MARKET="US" -- so a seed stamped with the storefront's country vanishes
    from US seed search. Returning the value at all invites that mistake again.

    NEGATIVE RESULTS ARE NOT CACHED ACROSS BRANDS. `fetch_storefront_meta` caches per domain for
    the PROCESS lifetime, negatives included, and its docstring says a long-lived caller should
    clear periodically. This lane is exactly that caller (`catalog_onboard_worker` drains a queue
    with a retry budget), and a single transient timeout would otherwise pin that brand to None ->
    USD for the whole process, silently defeating the retry AND the fix. So a miss is evicted.
    """
    host = _clean_domain(domain)
    if not host:
        return {"currency": None}

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
                if not _same_storefront_host(host, getattr(getattr(resp, "url", None), "host", None)):
                    return None
                if resp.status_code != 200:
                    return None
                if "application/json" not in (resp.headers.get("content-type") or ""):
                    return None
                return resp.text
        except Exception:
            return None

    meta = await storefront_currency.fetch_storefront_meta(host, fetch=_gated_fetch)
    if not isinstance(meta, dict):
        # Evict the negative so the next brand (or a retry of this one) asks again.
        storefront_currency.clear_cache()
        return {"currency": None}
    cur = str(meta.get("currency") or "").strip().upper()
    return {"currency": cur if _ISO_CURRENCY.match(cur) else None}


class CrawlIncomplete(RuntimeError):
    """A bounded scan did not establish a complete catalog; never ingest its prefix.

    next_page is diagnostic retry position, not an ingestion checkpoint. Callers retry
    the whole read before writing, so no partial batch can become a successful job.
    """
    def __init__(self, message: str, *, status: str, next_page: int,
                 scanned_products: int, selected_products: int):
        super().__init__(message)
        self.status = status
        self.next_page = next_page
        self.scanned_products = scanned_products
        self.selected_products = selected_products

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "next_page": self.next_page,
                "scanned_products": self.scanned_products,
                "selected_products": self.selected_products, "reason": str(self)}


class ShopifyProductBatch(list):
    """List-compatible result; successful results always represent an exhausted feed."""
    def __init__(self, products: list, *, scanned_products: int, pages: int):
        super().__init__(products)
        self.crawl_report = {"status": "complete", "scanned_products": scanned_products,
                             "selected_products": len(products), "pages": pages}


async def fetch_shopify_products(
    domain: str,
    *,
    max_products: int = 500,
    timeout_s: float = 15.0,
    only_vendors: Optional[Sequence[str]] = None,
    max_scan_products: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Enumerate to exhaustion, selecting vendors before the product budget.

    max_scan_products bounds the whole retailer scan, independently of the selected
    max_products budget. A cap or failed page raises CrawlIncomplete, never returns a
    misleading partial success. One lookahead request may establish exhaustion at an
    exact budget boundary. Transient errors get three paced attempts per page.
    """
    host = _clean_domain(domain)
    if not host:
        raise ValueError("a storefront domain is required")
    scan_limit = max_scan_products if max_scan_products is not None else max_products
    if max_products < 1 or scan_limit < 1:
        raise ValueError("product and scan budgets must be positive")
    if only_vendors is not None and not any(str(v or "").strip() for v in only_vendors):
        raise ValueError("only_vendors cannot contain only blank values")
    out: List[Dict[str, Any]] = []
    scanned = 0
    page = 1
    seen_pages: set = set()
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "application/json"}

    def incomplete(reason: str, status: str = "failed") -> CrawlIncomplete:
        return CrawlIncomplete(f"{host}: page {page}: {reason}", status=status,
                               next_page=page, scanned_products=scanned,
                               selected_products=len(out))

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            while True:
                url = f"https://{host}/products.json?limit={_PER_PAGE}&page={page}"
                for attempt in range(3):
                    await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
                    try:
                        resp = await client.get(url)
                    except (httpx.TimeoutException, httpx.TransportError):
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    crawl_politeness.note_response(
                        url, resp.status_code, retry_after=resp.headers.get("retry-after")
                    )
                    actual_host = getattr(getattr(resp, "url", None), "host", None)
                    if not _same_storefront_host(host, actual_host):
                        # A regional/sibling store can have a different catalog and
                        # currency. Never pair its prices with this host's locale.
                        raise incomplete(f"storefront host changed to {actual_host or '(unknown)'}")
                    if resp.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                        break
                    await asyncio.sleep(0.5 * (2 ** attempt))
                if resp.status_code != 200:
                    raise incomplete(f"HTTP {resp.status_code}")
                if "application/json" not in (resp.headers.get("content-type") or ""):
                    raise incomplete("storefront did not return JSON")
                body = resp.json()
                if not isinstance(body, dict) or not isinstance(body.get("products"), list):
                    raise incomplete("invalid products.json envelope")
                products = body["products"]
                if not all(isinstance(p, dict) for p in products):
                    raise incomplete("products.json contains a non-object product")
                if not products:
                    return ShopifyProductBatch(out, scanned_products=scanned, pages=page)
                fingerprint = json.dumps(products, sort_keys=True, default=str)
                if fingerprint in seen_pages:
                    raise incomplete("repeated page; pagination did not advance")
                seen_pages.add(fingerprint)
                if scanned + len(products) > scan_limit:
                    raise incomplete(f"scan budget {scan_limit} exhausted", "capped")
                scanned += len(products)
                selected = filter_products_by_vendor(products, only_vendors)
                if len(out) + len(selected) > max_products:
                    raise incomplete(f"selected-product budget {max_products} exhausted", "capped")
                out.extend(selected)
                if len(products) < _PER_PAGE:
                    return ShopifyProductBatch(out, scanned_products=scanned, pages=page)
                page += 1
    except CrawlIncomplete:
        raise
    except Exception as exc:
        raise incomplete(f"{type(exc).__name__}: {str(exc)[:160]}") from exc


def _native_shopify_id(value: Any) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if len(text) <= 30 and re.fullmatch(r"[0-9]+", text) and int(text) > 0 else None


def _missing_barcode(variant: Dict[str, Any]) -> bool:
    return variant.get("barcode") is None or str(variant["barcode"]).strip() == ""



async def _fetch_missing_variant_gtins(
    product: Dict[str, Any], *, domain: str, client: httpx.AsyncClient,
) -> Tuple[Dict[str, str], int]:
    """Return only validated (native variant ID -> GTIN), never detail-page copy.

    A product recovery uses at most three HTTP requests (two same-storefront
    redirects). Check each redirect before following, including www/apex aliases,
    and never visit another region/store. Price units differ on .js, so no price,
    product fields, or new variant identities may escape this helper.
    """
    requests = 0
    host = _clean_domain(domain)
    product_id = _native_shopify_id(product.get("id"))
    handle = str(product.get("handle") or "").strip()
    vendor = _vendor_token(product.get("vendor"))
    variants = product.get("variants") or []
    source_ids = [_native_shopify_id(v.get("id")) for v in variants if isinstance(v, dict)]
    wanted = {
        _native_shopify_id(v.get("id")) for v in variants
        if isinstance(v, dict) and _missing_barcode(v)
        and variant_id_provenance(str(v.get("id") or ""), product_id=product_id, handle=handle) == MERCHANT_ISSUED
    } - {None}
    if (not host or not product_id or not handle or not vendor or not wanted
            or any(source_ids.count(vid) != 1 for vid in wanted)):
        return {}, requests
    url = f"https://{host}/products/{quote(handle, safe='')}.js"
    try:
        for redirect in range(3):
            await crawl_politeness.before_request(url, user_agent=_UA, max_wait=10.0)
            requests += 1
            response = await client.get(url)
            crawl_politeness.note_response(url, response.status_code,
                                           retry_after=response.headers.get("retry-after"))
            if not _same_storefront_host(host, getattr(getattr(response, "url", None), "host", None)):
                return {}, requests
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                target = urlsplit(urljoin(url, location or ""))
                if (not location or redirect == 2 or target.scheme != "https"
                        or target.username or target.password or target.port not in (None, 443)
                        or not _same_storefront_host(host, target.hostname)):
                    return {}, requests
                url = target.geturl()
                continue
            if response.status_code != 200:
                return {}, requests
            detail = response.json()
            if (not isinstance(detail, dict) or _native_shopify_id(detail.get("id")) != product_id
                    or str(detail.get("handle") or "").strip() != handle
                    or _vendor_token(detail.get("vendor")) != vendor
                    or not isinstance(detail.get("variants"), list)):
                return {}, requests
            detail_variants = detail["variants"]
            if not all(isinstance(v, dict) for v in detail_variants):
                return {}, requests
            detail_ids = [_native_shopify_id(v.get("id")) for v in detail_variants]
            recovered = {}
            for variant in detail_variants:
                vid = _native_shopify_id(variant.get("id"))
                if vid not in wanted or detail_ids.count(vid) != 1:
                    continue
                gtin = validated_source_gtin(variant.get("barcode"))
                if gtin:
                    recovered[vid] = gtin
            return recovered, requests
    except Exception as exc:
        logger.debug("GTIN recovery refused for %s/%s: %s", host, handle, type(exc).__name__)
    return {}, requests


async def recover_missing_variant_gtins(
    products: List[Dict[str, Any]], *, domain: str, max_fetches: int = 100,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Recover selected products only, before shade folding changes product identity.

    attempted/recovered/failed/capped count PRODUCTS; recovered_gtins counts variant
    barcodes and http_requests includes bounded redirect hops. A failed observation
    never changes its product. Each attempt has at most three requests with the
    client's ten-second timeout, paced by the shared merchant crawl gate.
    """
    if type(max_fetches) is not int or max_fetches < 0:
        raise ValueError("max_pdp_identity_fetches must be a nonnegative integer")
    copied = [dict(p, variants=[dict(v) if isinstance(v, dict) else v for v in (p.get("variants") or [])])
              for p in products]
    candidates = [p for p in copied if any(isinstance(v, dict) and _missing_barcode(v)
                                          for v in p["variants"])]
    report = {"attempted": 0, "recovered": 0, "failed": 0,
              "capped": max(0, len(candidates) - max_fetches), "recovered_gtins": 0, "http_requests": 0}
    if not candidates or not max_fetches:
        return copied, report
    async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(10.0, connect=5.0),
                                 headers={"User-Agent": _UA, "Accept": "application/json"}) as client:
        for product in candidates[:max_fetches]:
            report["attempted"] += 1
            recovered, requests = await _fetch_missing_variant_gtins(product, domain=domain, client=client)
            report["http_requests"] += requests
            applied = 0
            for variant in product["variants"]:
                if not isinstance(variant, dict):
                    continue
                gtin = recovered.get(_native_shopify_id(variant.get("id")))
                if gtin and _missing_barcode(variant):
                    variant["barcode"] = gtin
                    applied += 1
            report["recovered_gtins"] += applied
            report["recovered" if applied else "failed"] += 1
    return copied, report


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


_META_DESC_RE = re.compile(
    r"<meta\b[^>]*>",
    re.I,
)


def description_from_pdp_html(raw_html: str) -> Optional[str]:
    """Brand-authored copy from a PDP's own meta description, or None.

    WHY THIS EXISTS. `backfill_brand_official_descriptions` takes body copy from
    `/products.json` body_html, and some storefronts publish none — measured on jsmbeauty.sg
    2026-09-06: 158 of 232 products carry under 50 characters of body_html TEXT (LIP-PRESSION
    Glowy Tint's 123 characters of markup render to zero), so the whole cohort failed the 50-char
    floor. The copy is not missing from the site, only from that field: the same PDP serves 190
    characters of real prose in its meta description.

    PREFERS `name="description"`, NOT og. The og tag is frequently theme-generated; the name tag
    is the per-product SEO field a merchant fills. Measured on kyliecosmetics.com: og carried the
    STORE blurb while name carried the product's own line, so preferring og took the strictly
    worse string.

    BOILERPLATE IS NOT REJECTED HERE, and deliberately so. A theme with no per-product SEO
    description substitutes the SHOP blurb (`page_description | default: shop.description`), and
    app vendors write operational text into the name tag; both clear the 50-char floor with zero
    product information, and both are worse than staying blocked, because `is_published_ready`
    auto-publishes this lane. But neither is visible in ONE page's markup — the tell is that the
    value REPEATS across the storefront, or equals the shop's own blurb. That needs the whole
    domain, so it lives in the caller (`backfill_brand_official_descriptions.
    drop_shared_boilerplate`), against `fetch_shop_description` below.

    An earlier version of this function keyed on a PRESENT-BUT-EMPTY name tag. That is a THEME
    detail, not the substitution mechanism: Dawn-family themes wrap the name tag in
    `{% if page_description %}` and so OMIT it entirely. A 60-PDP sweep across 10 storefronts
    (2026-09-06) found the shop blurb served under an ABSENT name tag 9 times and under a
    present-but-empty one 0 times — the rule fired only on the single storefront it was derived
    from, and three parser paths (a raw `>` inside the value, a `content`-less tag, an unquoted
    value) silently disabled it even there.

    Returns None when there is nothing trustworthy — never a partial and never a guess.
    """
    if not raw_html:
        return None

    og: Optional[str] = None
    name: Optional[str] = None

    for tag in _META_DESC_RE.findall(raw_html)[:400]:
        key = ""
        for attr in ("property", "name"):
            m = re.search(rf'{attr}\s*=\s*"([^"]+)"', tag, re.I) or re.search(
                rf"{attr}\s*=\s*'([^']+)'", tag, re.I
            )
            if m:
                key = m.group(1).strip().lower()
                break
        if key not in ("og:description", "description"):
            continue
        # MATCH THE OPENING DELIMITER. A character class of both quotes ends the capture at the
        # first apostrophe inside a double-quoted attribute — measured live on kyliecosmetics.com,
        # an 88-character sentence truncated to 25 at "that's", which still cleared the 50-char
        # floor as a mid-sentence fragment. Raw apostrophes are ubiquitous in this copy.
        m = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I | re.S) or re.search(
            r"content\s*=\s*'([^']*)'", tag, re.I | re.S
        )
        if not m:
            continue
        value = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
        if key == "description":
            name = name or (value or None)
        elif og is None and value:
            og = value

    if name:
        return name
    return og


async def fetch_pdp_description(
    domain: str,
    handle: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 15.0,
) -> Optional[str]:
    """Fetch one brand PDP and recover its meta description.

    Deliberately the same shape as `fetch_pdp_inci` beside it, including gating BOTH branches:
    the caller-supplied-client branch is the one a batch loop uses, so gating only the standalone
    branch would leave the high-volume path unpaced. Any failure returns None — a brand site
    hiccup must never fabricate copy or raise into a backfill loop.
    """
    host = _clean_domain(domain)
    handle = str(handle or "").strip().strip("/")
    if not host or not handle:
        return None
    url = f"https://{host}/products/{handle}"
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "text/html"}
    try:
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
        return description_from_pdp_html(resp.text)
    except Exception as exc:  # noqa: BLE001 — a brand PDP being down must not break the batch
        logger.debug("fetch_pdp_description failed for %s/%s: %s", host, handle, str(exc)[:160])
        return None


async def fetch_shop_description(
    domain: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 15.0,
) -> Optional[str]:
    """The storefront's OWN blurb, from its homepage meta description, or None.

    NOT product copy — the opposite of it, and that is the point. A Shopify theme renders
    og:description as `page_description | default: shop.description`, so every product without an
    SEO description serves THIS string on its PDP: over the 50-char floor, identical across the
    storefront, and carrying no product information. Measured 2026-09-06, it reached
    `description_from_pdp_html` unchallenged on 9 of 60 PDPs across cosrx.com, mixsoon.us and
    medicube.us.

    Fetching it once per domain turns "is this the shop blurb?" from a guess about the theme's
    markup into an exact string comparison against the blurb itself — the substitution mechanism,
    not a symptom of it. One request per domain, gated like every other call in this module.
    Returns None on any failure, which simply leaves the comparison unarmed.
    """
    host = _clean_domain(domain)
    if not host:
        return None
    url = f"https://{host}/"
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "text/html"}
    try:
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
        return description_from_pdp_html(resp.text)
    except Exception as exc:  # noqa: BLE001 — a homepage being down must not break the batch
        logger.debug("fetch_shop_description failed for %s: %s", host, str(exc)[:160])
        return None


async def fetch_shop_description_from_meta(
    domain: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 15.0,
) -> Optional[str]:
    """The storefront's OWN blurb again, read from Shopify's `/meta.json` `description`.

    THE SAME STRING AS THE HOMEPAGE META, from a different door. Measured on jsmbeauty.sg
    2026-09-08: the homepage meta description and `/meta.json` `description` are the identical
    135 characters. The homepage is a 700 KB themed page that a Cloudflare-fronted store starts
    refusing after a run has fetched sixty product pages from the same egress, while the JSON
    endpoints keep answering — which is exactly the moment the description backfill asks for the
    blurb. This is the fallback for that moment, not a replacement: `fetch_shop_description`
    stays first because a theme can override `shop.description` on the homepage, and it is the
    HOMEPAGE string a PDP without its own SEO copy repeats. Returns None on any failure.

    NOT THE SAME STRING AS THE THEME RENDERS, and the caller must be told. `/meta.json` returns
    `shop.description` RAW, while the homepage meta and the PDP og tag both pass it through the
    theme (escaping, truncation, `| append: shop.name`), so the two can differ by exactly the
    filters that make the PDP comparison an EXACT match. Callers that use this to arm an
    equality test must treat the result as unverified — see `_load_shop_blurb` in
    scripts/backfill_brand_official_descriptions.py.
    """
    host = _clean_domain(domain)
    if not host:
        return None
    url = f"https://{host}/meta.json"
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    try:
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
        # THE ANSWER MUST COME FROM THE HOST WE ASKED. `follow_redirects=True` with no check
        # will happily read `/meta.json` off whatever storefront the redirect chain ends on --
        # and a regional redirect (brand.com -> uk.brand.com, or an apex parked on a partner's
        # shop) lands on a DIFFERENT Shopify store with a different `shop.description`. That
        # string would then arm an equality comparison for a domain whose theme never renders
        # it: not merely useless, but the exact "non-empty blurb that matches nothing" shape
        # that switches OFF this lane's fail-closed refusal. www<->apex is the same storefront
        # and is allowed; anything else is not.
        if not _same_storefront_host(host, getattr(resp.url, "host", None)):
            logger.debug(
                "fetch_shop_description_from_meta: %s redirected off-host to %s — refusing",
                host, getattr(resp.url, "host", None),
            )
            return None
        data = resp.json()
        desc = data.get("description") if isinstance(data, dict) else None
        # A STRING OR NOTHING. `str(desc)` of a list renders `['a', 'b']` -- punctuation and all,
        # comfortably over the 50-char floor -- and a dict renders its repr. Both would be
        # published-shaped garbage rather than the shop blurb, and neither can ever equal a PDP
        # meta description, so both arrive as an unmatchable "blurb" instead of an absent one.
        if not isinstance(desc, str):
            return None
        desc = " ".join(desc.split())
        return desc or None
    except Exception as exc:  # noqa: BLE001 — same contract as fetch_shop_description
        logger.debug("fetch_shop_description_from_meta failed for %s: %s", host, str(exc)[:160])
        return None


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


def _brand_key(value: Optional[str]) -> str:
    """Alphanumeric-only comparison form of a brand/vendor label.

    Tighter than `_vendor_token` (which only casefolds) because the comparison here is
    "are these two labels the same BRAND", and the spellings that must compare equal
    differ by punctuation and spacing: `A'PIEU`/`Apieu`, `MISSHA US`/`Missha`.
    `_vendor_token` is left alone — it backs `filter_products_by_vendor`, where exact
    selection is the point and loose matching is the documented hazard.
    """
    return "".join(c for c in str(value or "").casefold() if c.isalnum())


def _looks_like_a_brand_name(value: Optional[str]) -> bool:
    """Only explicit supplier-code shapes are codes; short/Unicode names are brands.

    3CE and Chinese/Korean labels are actual Meitu vendors. Length or ASCII-only
    checks cannot establish that a merchant's different label is not a brand.
    """
    raw = str(value or "").strip()
    return bool(raw) and not raw.isdecimal() and not bool(re.fullmatch(r"[A-Za-z]{1,3}-[A-Za-z]?\d{3,}", raw))


def resolve_record_brand(
    vendor: Optional[str], brand_override: Optional[str], domain: Optional[str]
) -> Tuple[str, str]:
    """Decide the brand for ONE product, returning `(brand, reason)`.

    `brand_override` is a per-DOMAIN claim by the operator; `vendor` is the storefront's
    own per-PRODUCT claim. They disagree on brand-family storefronts: misshaus.com
    publishes 125 products of which 17 carry `vendor: "APIEU"` (Able C&C owns both
    labels). Renaming those to "Missha" is not a spelling normalisation, it is an
    assertion that A'pieu's products are Missha's — and it was measured live on
    2026-09-11 doing exactly that: 15 A'pieu rows in the index branded `Missha`, which
    also made them invisible to brand-strict recall (`external_seed_brand_strict_rows: 0`
    on a search for `A'PIEU` that nonetheless returned all 15).

    So the override still wins everywhere it is a NORMALISATION, and loses where it
    would be a RELABEL:
      * no vendor              -> override           (nothing to contradict it)
      * same brand, differently spelt -> override    (`MISSHA US` -> `Missha`)
      * vendor names the STORE -> override           (`thefaceshopny` on thefaceshopny.com)
      * genuine disagreement   -> VENDOR             (`APIEU` on misshaus.com)

    Containment, not equality, decides "same brand": `missha` is a substring of
    `misshaus`. It is deliberately narrow — both sides are short brand labels, and the
    cost of a false "same" is only today's behaviour, while the cost of a false
    "different" is a wrong brand on the row. Guarded by a 3-character floor so a
    2-letter vendor cannot be a substring of half the brands in the catalogue.
    """
    v_raw = str(vendor or "").strip()
    o_raw = str(brand_override or "").strip()
    if not o_raw:
        return v_raw, "vendor_only"
    if not v_raw:
        return o_raw, "override_no_vendor"
    v, o = _brand_key(v_raw), _brand_key(o_raw)
    if not v:
        return v_raw, "vendor_disagrees"
    # Containment subsumes equality (`v == o` implies `v in o`), including below the
    # 3-char floor: two equal sub-floor keys cannot carry a 3-letter run either, so
    # they fall to `_looks_like_a_brand_name` and return the same string anyway. A
    # separate equality arm here was provably unreachable — it changed no output under
    # mutation — and is deliberately absent rather than kept as untested reassurance.
    if len(v) >= 3 and len(o) >= 3 and (v in o or o in v):
        return o_raw, "override_same_brand"
    host_label = _brand_key(_clean_domain(domain or "").split(".")[0])
    if host_label and len(v) >= 3 and (v in host_label or host_label in v):
        # The vendor field is the STORE's name, not a brand — the override is the only
        # brand claim available and is what the operator came to assert. Measured:
        # metro.com.sg publishes `vendor: "Metro Singapore Departmental Store -
        # Celebrating 69 Years in SG"`, which contains the host label and names no brand.
        return o_raw, "override_vendor_is_store"
    if not _looks_like_a_brand_name(v_raw):
        # A SUPPLIER CODE is not a brand. sukoshi.com publishes `vendor: "VC-B004"` on
        # products whose brand appears only in the title; adopting that verbatim would
        # put "VC-B004" in the brand column, which is worse than the override it
        # replaced. The override at least names a real brand.
        return o_raw, "override_vendor_is_not_a_name"
    return v_raw, "vendor_disagrees"


# Reuses PR #2158's confidence contract and distinct-path ambiguity guard. The
# source label remains enrichment_agent_v1: it is also a canonical-scope lane ID.
CATEGORY_CONFIDENCE_MERCHANT_TYPE = 0.9
CATEGORY_CONFIDENCE_EXPLICIT_TITLE = 0.8
CATEGORY_CONFIDENCE_FEED_DEFAULT = 0.3

# Generic shelves are not assertions of a purchasable product class. In particular,
# the shared legacy regex maps Lip Care to balm, contradicting the measured lip oil.
_GENERIC_PRODUCT_TYPES = frozenset({
    "beauty", "cosmetics", "makeup", "make up", "skin care", "skincare",
    "face care", "hair care", "haircare", "lip care", "lip treatment", "lip treatments",
})
_GENERIC_LIP_TYPES = frozenset({"lip care", "lip treatment", "lip treatments"})
_TOOL_NOUN_SUFFIX = re.compile(r"\bbrush(?:es)?(?:\s+#?\d{1,4})?\s*$", re.I)
# Formula names ending in an included applicator are not tool names. This is a
# noun/suffix exception, not a general pass of marketing titles through the taxonomy.
_TOOL_FORMULA_CONTEXT = re.compile(r"[+&/]|\b(?:and|with|includes?|including|for|using|built[- ]in)\b|brush[- ]on", re.I)


def _pattern_matches(text: Optional[str]) -> int:
    """PR #2158 guard: first-match-wins is not evidence when multiple paths match."""
    from services.pdp_category_classifier import CATEGORY_PATTERNS
    return len({path for _label, path, pattern in CATEGORY_PATTERNS if pattern.search(str(text or ""))})


def _resolve_category(*, product_type: Optional[str], title: Optional[str], flag_path: str) -> Tuple[str, float]:
    """One category evidence policy shared by feed mapping and repair planning.

    Retain #2158's ambiguity guard, conservative marketing-title behavior and
    confidence semantics. Deliberately replace its storefront-area veto: strong
    per-product evidence can disagree with a COARSE storefront shelf (MISSHA tools
    were all labelled skincare). An explicit taxonomy leaf remains protected.
    Title evidence has only two narrow doors: a tool noun suffix without formula
    context, and an explicit lip-oil title refining the measured generic lip shelves.
    """
    from services.pdp_category_classifier import CATEGORY_PATTERNS, classify
    fallback = str(flag_path or "").strip().strip("/").lower()
    if fallback.split("/", 1)[0] != "beauty":
        return flag_path, CATEGORY_CONFIDENCE_FEED_DEFAULT
    leaves = {path for _label, path, _pattern in CATEGORY_PATTERNS}

    def accept(path: str, confidence: float) -> Tuple[str, float]:
        if not path.startswith("beauty/") or (fallback in leaves and path != fallback):
            return fallback, CATEGORY_CONFIDENCE_FEED_DEFAULT
        return path, confidence

    ptype = " ".join(str(product_type or "").casefold().split())
    matches = _pattern_matches(product_type)
    if matches > 1:
        return fallback, CATEGORY_CONFIDENCE_FEED_DEFAULT
    if ptype in _GENERIC_LIP_TYPES:
        title_hit = classify(title)
        if title_hit and title_hit[1] == "beauty/makeup/lip/oil" and _pattern_matches(title) == 1:
            return accept(title_hit[1], CATEGORY_CONFIDENCE_EXPLICIT_TITLE)
    if ptype not in _GENERIC_PRODUCT_TYPES and matches == 1:
        hit = classify(product_type)
        if hit:
            return accept(hit[1], CATEGORY_CONFIDENCE_MERCHANT_TYPE)
    # An unclassifiable multi-use label (e.g. Lip & Cheek) is not permission to
    # choose a competing category from its title.
    if matches == 0 and re.search(r"[&/]|\band\b", ptype):
        return fallback, CATEGORY_CONFIDENCE_FEED_DEFAULT
    if _TOOL_NOUN_SUFFIX.search(str(title or "")) and not _TOOL_FORMULA_CONTEXT.search(str(title or "")):
        return accept("beauty/tools/brush", CATEGORY_CONFIDENCE_EXPLICIT_TITLE)
    # Powder Kiss Liquid Lipcolour / Slim Stick and Strobe Cream are explicitly
    # not classified from their marketing titles; shallow backfill owns the residue.
    return fallback, CATEGORY_CONFIDENCE_FEED_DEFAULT


def product_category_path(*, title: Optional[str], product_type: Optional[str], fallback: str) -> str:
    """Path-only wrapper for the review-only repair planner; no second classifier."""
    return _resolve_category(product_type=product_type, title=title, flag_path=fallback)[0]


def shopify_product_to_record(
    product: Dict[str, Any],
    *,
    domain: str,
    category_path: str,
    brand_override: Optional[str] = None,
    emit_variants: bool = False,
    # Separate switch for NATIVE (un-folded) multi-variant rows, so the fold lane's
    # `emit_variants=True` cannot silently start emitting variants for the rows it
    # did not fold — `--base-listings-only` runs (MAC) stay byte-identical.
    emit_native_variants: bool = False,
    currency: Optional[str] = None,
    source_role: str = "brand_official",
    retailer_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Map one Shopify `/products.json` product → a Path-C validated record
    (`{pdp, offers}`). Returns None if it lacks a title/handle (not actionable).
    The brand storefront is the authoritative PDP, so the offer is brand-direct
    and carries the variant barcode (GTIN) when present."""
    if not isinstance(product, dict):
        return None
    if source_role not in {"brand_official", "retailer"}:
        raise ValueError("source_role must be brand_official or retailer")
    host = _clean_domain(domain)
    title = str(product.get("title") or "").strip()
    handle = str(product.get("handle") or "").strip()
    if not title or not handle:
        return None
    # NOT `brand_override or vendor`: that renames across a brand boundary. See
    # `resolve_record_brand` — the override normalises spelling, it does not relabel
    # a sibling brand the storefront names itself.
    brand, _brand_reason = resolve_record_brand(
        product.get("vendor"), brand_override, host
    )
    brand = str(brand or "").strip()
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
    # Every sellable variant: the ingest writes one SKU + offer per entry beside
    # the canonical SKU, so a folded shade line (see fold_shade_listings) keeps
    # its purchasable SKUs. Since #2120 a single-variant product emits its one
    # variant too -- the canonical SKU's source_variant_id is a storage token the
    # gateway refuses to spend against, so the merchant's own id has to ride here
    # for #2113 (SKU) and #2123 (seed) to keep it.
    sellable = [
        v for v in variants
        if isinstance(v, dict) and (_to_float(v.get("price")) or 0.0) >= MIN_SELLABLE_PRICE
    ]
    pdp_variants: List[Dict[str, Any]] = []
    # OPT-IN. Emitting variants writes one extra SKU + offer per variant downstream,
    # which changes recall fan-out, offer aggregation and INCI attachment for EVERY
    # row a caller ingests — so it never fires unless a caller asked for it.
    #
    # WHY THE FOLD IS NO LONGER THE ONLY GATE. This used to additionally require
    # `product.get(FOLDED_INTO_KEY)`, i.e. only a listing the shade-fold had just
    # BUILT could carry variants. That silently excluded every storefront that
    # publishes its shades natively, as one product with many variants — which is
    # the normal Shopify shape, not the exception. Measured on flowerbeauty.com
    # 2026-09-07: `/products.json` serves 49 products, 29 of them multi-variant,
    # carrying 185 real numeric Shopify variant ids; `fold_shade_listings` folds
    # ZERO of them (bases=0, shades=0), so the gate refused all 185 and the brand
    # ingested 49 SKUs whose `source_variant_id` was the product key. #2113 taught
    # ingestion to stop DISCARDING real variant ids; this is the other half — the
    # feed never put them in the record for it to keep.
    #
    # The two lanes admit on different rules, deliberately. A FOLDED row's variants
    # were assembled by us out of separate per-shade listings and a variant there
    # may legitimately carry a synthesised `<handle>:<i>` id (see the fallback
    # below), which is display data the shade selector needs; ingestion's own
    # provenance check decides whether it may also be sold. A NATIVE row has no
    # such excuse: its ids come straight off the merchant's own feed, so anything
    # `variant_identity` cannot positively place as merchant-issued is dropped
    # here rather than carried forward as a decoy that looks purchasable.
    native = not product.get(FOLDED_INTO_KEY)
    emit_here = emit_native_variants if native else emit_variants
    if emit_here and len(sellable) >= 1:
        seen_ids: set = set()
        base_option_name = _base_option_name(product)
        for i, v in enumerate(sellable):
            vid = str(v.get("id") or v.get("variant_id") or f"{handle}:{i}").strip()
            if vid in seen_ids:
                continue
            if native and variant_id_provenance(
                vid,
                product_id=product.get("id"),
                handle=handle,
            ) != MERCHANT_ISSUED:
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
                "option_name": _variant_option_name(v, base_option_name),
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
    category_path, category_confidence = _resolve_category(
        title=title, product_type=product.get("product_type"), flag_path=category_path,
    )
    return {
        "pdp": {
            "brand": brand,
            "product_name": title,
            "category_path": category_path,
            "category_confidence": category_confidence,
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
            "source_role": source_role,
            "tags": tags,
            # Brand-official INCI when the storefront lists it under an Ingredients
            # heading (many don't — None then, ingest skips it). brand_official is
            # the top INCI authority tier (ADR-001) so it outranks reseller lists.
            "raw_inci": inci_from_body_html(product.get("body_html")),
            "inci_source": "reseller_listing" if source_role == "retailer" else "brand_official",
            # Shopify /products.json exposes no review aggregate — ratings stay null
            # on this lane (captured on the retailer-PDP lane instead).
            "rating_value": None,
            "rating_count": None,
            # The STOREFRONT's own currency, from /meta.json. Omitted (None) rather than
            # defaulted here: the ingest lane owns the fallback, so a record that never learned
            # its currency is indistinguishable from one that did and is genuinely USD.
            "currency": currency,
            "variants": pdp_variants,
        },
        "offers": [
            {
                # The MERCHANT is the storefront, which is not always the brand: once
                # `resolve_record_brand` can keep a sibling brand's own vendor (APIEU on
                # misshaus.com), `brand` names the maker and the override names the shop.
                # Identical on every single-brand feed, where the two are the same string.
                "merchant_inferred": (
                    str(retailer_name or "").strip() or host
                    if source_role == "retailer"
                    else str(brand_override or "").strip() or brand
                ),
                # Seller identity must be host-based in retailer mode even when two
                # storefronts use the same friendly name. Official-mode IDs are untouched.
                "seller_domain": host if source_role == "retailer" else None,
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


class CurrencyNotProven(RuntimeError):
    """A storefront's own currency could not be proven, or is not the one asked for.

    Raised INSTEAD of ingesting, because the alternative is silent and wrong: a record
    that never learned its currency arrives at `ingestion._currency_of` with None and
    is stamped the DEFAULT, `USD`. That default is correct for the US corpus it was
    written for and is a mispricing everywhere else -- an SGD 30.00 lip tint served as
    USD 30.00 is a 1.35x overstatement carrying no signal that it is wrong.

    The failure is not hypothetical and not rare. Measured 2026-09-07 while probing the
    three Singapore storefronts: `/meta.json` on cocomo.sg answered 429 with a
    "Verifying your connection..." bot-check HTML page (the shop had just served four
    pages of `/products.json`), so `fetch_shopify_shop_locale` returned
    `{'currency': None}` -- a store that prices in SGD, one throttled request away from
    1,000 USD-stamped rows. `fetch_shopify_shop_locale` is best-effort BY DESIGN and
    must stay so; what was missing is a caller that can say "I know this is an SGD
    store, refuse the run if you cannot see SGD".

    NEVER a conversion. This raises; it does not rewrite an amount into another
    currency. See services/region_pricing (ADR-024 commitment 5).
    """


def _vendor_token(value: Optional[str]) -> str:
    """Comparison form of a Shopify `vendor` string: casefolded, whitespace collapsed.

    Deliberately NOT `catalog_identity.normalize_title`: that one is tuned for product
    titles (it keeps hyphens because 'Anti-Aging Serum' is a real distinction) and a
    vendor field is a short label where the only variation worth absorbing is case and
    stray spacing. Anything looser would be a hazard on a multi-brand retailer feed,
    which is the whole reason this exists -- cocomo.sg lists 224 vendors, and a filter
    that matched approximately would quietly pull in a neighbour brand's products under
    the target brand's name.
    """
    return " ".join(str(value or "").split()).casefold()


def filter_products_by_vendor(
    products: List[Dict[str, Any]], vendors: "Sequence[str]"
) -> List[Dict[str, Any]]:
    """Keep only the products whose Shopify `vendor` is one of `vendors`. Pure.

    A multi-brand RETAILER feed is not a brand feed. `records_for_brand`'s existing
    `brand` argument is a brand_override -- it RENAMES every product it sees -- so
    pointing it at cocomo.sg with brand='VELY VELY' would not select VELY VELY's 24
    products, it would relabel all 1,000 of that retailer's products (MEDICUBE, ANUA,
    BEAUTY OF JOSEON, ...) as VELY VELY and deposit them as brand-official anchors.
    Selection and renaming are different operations and this is the selecting one.

    Matching is exact after `_vendor_token` normalisation. An empty/None `vendors`
    returns the list unchanged -- "no filter asked for", which is what every existing
    single-brand caller means.
    """
    wanted = {_vendor_token(v) for v in (vendors or []) if str(v or "").strip()}
    if not wanted:
        return list(products)
    return [
        p for p in products
        if isinstance(p, dict) and _vendor_token(p.get("vendor")) in wanted
    ]


class CuratedRecordBatch(list):
    """Validated records and their own scan outcome, safe across concurrent calls."""
    def __init__(self, records: list, *, crawl_report: Optional[Dict[str, Any]] = None):
        super().__init__(records)
        self.crawl_report = dict(crawl_report) if crawl_report is not None else None


async def records_for_brand(
    *,
    domain: str,
    category_path: str,
    brand: Optional[str] = None,
    max_products: int = 500,
    base_listings_only: bool = False,
    # Emit the merchant's OWN variants for products that are natively multi-variant
    # (the normal Shopify shape). Off by default: it adds one SKU + one offer per
    # variant, which moves recall fan-out and offer aggregation for every row the
    # caller ingests. `base_listings_only` implies it for the rows the fold builds.
    emit_real_variants: bool = False,
    only_vendors: Optional[Sequence[str]] = None,
    require_currency: Optional[str] = None,
    source_role: str = "brand_official",
    retailer_name: Optional[str] = None,
    max_scan_products: int = 10000,
    enrich_missing_inci: bool = False,
    max_pdp_inci_fetches: int = 300,
    enrich_missing_gtin: bool = False,
    max_pdp_identity_fetches: int = 100,
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
    the fetch is capped, delayed, and best-effort (a miss leaves raw_inci None).

    `enrich_missing_gtin` optionally recovers validated missing variant barcodes
    from identity-matched product .js responses, before folding. The product-attempt
    budget is `max_pdp_identity_fetches`; batch crawl_report.gtin_recovery records
    attempts, successes, failures, capped products and actual HTTP requests.

    `source_role="retailer"` uses storefront seller identity and reseller INCI authority,
    and refuses unproven currency. Official-mode identity stays backward compatible.

    `only_vendors` selects a subset of a MULTI-BRAND retailer feed by Shopify `vendor`
    (see `filter_products_by_vendor`) — the selecting operation, as distinct from
    `brand`, which renames. Applied BEFORE the shade fold, so a fold never matches a
    base across a brand boundary.

    `require_currency` (an ISO-4217 code) refuses the whole brand with
    `CurrencyNotProven` unless the storefront's own `/meta.json` proves that currency.
    Opt-in: omitted, behaviour is exactly what it was — best-effort, `None` on a miss,
    and `USD` from the ingest lane's default.
    """
    if source_role not in {"brand_official", "retailer"}:
        raise ValueError("source_role must be brand_official or retailer")
    if not isinstance(enrich_missing_gtin, bool):
        raise ValueError("enrich_missing_gtin must be a boolean")
    if type(max_pdp_identity_fetches) is not int or max_pdp_identity_fetches < 0:
        raise ValueError("max_pdp_identity_fetches must be a nonnegative integer")
    fetch_options: Dict[str, Any] = {"max_products": max_products}
    if only_vendors is not None or source_role == "retailer":
        fetch_options.update(only_vendors=only_vendors, max_scan_products=max_scan_products)
    products = await fetch_shopify_products(domain, **fetch_options)
    crawl_report = getattr(products, "crawl_report", None)
    # Compatibility/debug only; callers must use the returned batch's own report.
    records_for_brand.last_crawl_report = crawl_report  # type: ignore[attr-defined]
    # ONCE per brand, not per product: it is one storefront-wide setting and a per-product fetch
    # would multiply outbound requests by the catalogue size against a single host.
    locale = await fetch_shopify_shop_locale(domain)
    if source_role == "retailer" and not _ISO_CURRENCY.fullmatch(str(locale.get("currency") or "")):
        raise CurrencyNotProven(f"{domain}: retailer currency is unproven; refusing ingestion")
    if require_currency:
        expected = str(require_currency).strip().upper()
        actual = locale.get("currency")
        if actual != expected:
            # BEFORE the records are built, so a refused brand cannot half-ingest.
            raise CurrencyNotProven(
                f"{domain}: expected currency {expected}, storefront /meta.json proved "
                f"{actual or 'nothing'}. Refusing rather than ingesting — the ingest lane "
                f"defaults an unknown currency to USD, and this run cannot show the prices "
                f"are in {expected}. If /meta.json was unreadable, retry: a 429 bot-check "
                f"page reads exactly like a missing file. If {actual or 'the proven value'} "
                f"is genuinely right, pass --require-currency {actual or '<code>'} instead."
            )
    if only_vendors is not None:
        # A filter that was ASKED FOR but normalises to nothing is refused, not skipped.
        # `--only-vendor "$VENDOR"` with the variable unset, or a jsonl row
        # `"only_vendors": [""]`, would otherwise pass an empty set to the filter, which
        # returns the whole feed — and the brand override then relabels every product of a
        # 224-vendor retailer as the target brand, signalled only by a "1000 -> 1000" line.
        wanted = [v for v in only_vendors if str(v or "").strip()]
        if not wanted:
            raise ValueError(
                f"{domain}: --only-vendor was given but every entry is blank "
                f"({list(only_vendors)!r}). Name the vendor, or drop the flag to ingest "
                f"the whole feed deliberately."
            )
        only_vendors = wanted
        before = (getattr(products, "crawl_report", None) or {}).get("scanned_products", len(products))
        products = filter_products_by_vendor(products, only_vendors)
        if not products:
            # LOUD, not empty. An unmatched vendor filter otherwise reports the same
            # "0 products" a non-Shopify storefront does, and the operator reads a typo
            # ("Vely Vely " with a stray character) as "this brand has nothing here".
            raise ValueError(
                f"{domain}: --only-vendor {list(only_vendors)!r} matched 0 of {before} "
                f"products. Check the spelling against the feed's own `vendor` values."
            )
        records_for_brand.last_vendor_filter_report = {  # type: ignore[attr-defined]
            "vendors": list(only_vendors), "before": before, "after": len(products),
        }
    identity_report = None
    if enrich_missing_gtin:
        products, identity_report = await recover_missing_variant_gtins(
            products, domain=domain, max_fetches=max_pdp_identity_fetches)
    # A brand-family storefront (misshaus.com: 89 MISSHA + 17 APIEU + 7 CHOGONGJIN)
    # is not visibly different from a single-brand one until something counts the
    # vendors. Computed from the SAME resolver the record builder uses, so the report
    # cannot drift from the decision it describes.
    brand_census: Dict[str, Dict[str, Any]] = {}
    for _p in products:
        if not isinstance(_p, dict):
            continue
        _v = str(_p.get("vendor") or "").strip()
        _resolved, _why = resolve_record_brand(_v, brand, domain)
        _slot = brand_census.setdefault(
            _v or "(no vendor)", {"count": 0, "resolved_brand": _resolved, "reason": _why}
        )
        _slot["count"] += 1
    records_for_brand.last_brand_census = {  # type: ignore[attr-defined]
        "brand_override": brand,
        "vendors": brand_census,
        "kept_vendor_count": sum(
            v["count"] for v in brand_census.values() if v["reason"] == "vendor_disagrees"
        ),
    }
    if base_listings_only:
        products, fold_report = fold_shade_listings(products)
        records_for_brand.last_fold_report = fold_report  # type: ignore[attr-defined]
    records: List[Dict[str, Any]] = []
    pairs: List[Dict[str, Any]] = []  # (product, record) needing a PDP INCI try
    for p in products:
        rec = shopify_product_to_record(
            p, domain=domain, category_path=category_path, brand_override=brand,
            emit_variants=base_listings_only,
            emit_native_variants=emit_real_variants,
            currency=locale.get("currency"),
            source_role=source_role, retailer_name=retailer_name,
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
    # ONE spelling per brand. misshaus.com publishes both `APIEU` (16 products) and
    # `Apieu` (1); kept verbatim they are two brands to every consumer that groups by
    # the brand string, which splits a brand's catalogue for exactly the reason this
    # fix exists. Fold each `_brand_key` group onto its most common raw spelling —
    # a no-op for the override groups, whose members already share one string.
    spellings: Dict[str, "collections.Counter[str]"] = {}
    for rec in records:
        b = str((rec.get("pdp") or {}).get("brand") or "")
        if b:
            spellings.setdefault(_brand_key(b), collections.Counter())[b] += 1
    canonical = {
        k: c.most_common(1)[0][0] for k, c in spellings.items() if len(c) > 1
    }
    if canonical:
        for rec in records:
            pdp = rec.get("pdp") or {}
            b = str(pdp.get("brand") or "")
            want = canonical.get(_brand_key(b))
            if want and want != b:
                pdp["brand"] = want
        records_for_brand.last_brand_spelling_folds = canonical  # type: ignore[attr-defined]
    else:
        records_for_brand.last_brand_spelling_folds = {}  # type: ignore[attr-defined]
    report = {**crawl_report, "emitted_records": len(records)} if crawl_report is not None else None
    if report is not None and identity_report is not None:
        report["gtin_recovery"] = identity_report
    return CuratedRecordBatch(records, crawl_report=report)
