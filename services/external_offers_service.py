import hashlib
import html as html_lib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse, urlunparse

import httpx
from sqlalchemy import and_, select, update

from db.database import database
from db.external_offers import external_offer_snapshots
from services import crawl_global_pacer
from services import crawl_identity
from services import crawl_politeness
from services import shopify_edge_pacer
from utils.availability_vocabulary import normalize_availability
from utils.crawled_price import (
    agreed_hint,
    decimal_hint_from_currency,
    decimal_hint_from_locale,
    decimal_hint_from_money_format,
    parse_crawled_price,
    read_currency_code,
)


MAX_BODY_BYTES = int(os.getenv("EXTERNAL_OFFER_MAX_BODY_BYTES") or "1200000")  # ~1.2MB
MAX_AGE_DAYS = int(os.getenv("EXTERNAL_OFFER_MAX_AGE_DAYS") or "7")
DEFAULT_UA = os.getenv("EXTERNAL_OFFER_USER_AGENT") or "Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)"


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: Dict[Tuple[str, str], str] = {}
        self.meta_list: Dict[Tuple[str, str], list[str]] = {}
        self.title: Optional[str] = None
        self._in_title = False
        self.links: Dict[str, str] = {}
        self.jsonld: list[str] = []
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []
        self.img_attrs: list[Dict[str, str]] = []
        self.preload_images: list[str] = []
        self.html_lang: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "html":
            if self.html_lang is None and a.get("lang"):
                self.html_lang = a.get("lang")
        elif tag.lower() == "meta":
            key = ""
            if a.get("property"):
                key = f"property:{a.get('property')}"
            elif a.get("name"):
                key = f"name:{a.get('name')}"
            if key and a.get("content"):
                content = a.get("content") or ""
                self.meta[(tag, key.lower())] = content
                self.meta_list.setdefault((tag, key.lower()), []).append(content)
        elif tag.lower() == "title":
            self._in_title = True
        elif tag.lower() == "link":
            rel = (a.get("rel") or "").strip().lower()
            href = (a.get("href") or "").strip()
            if rel and href:
                self.links[rel] = href
            # Track preload images for galleries (best-effort).
            rel_tokens = {t for t in rel.split() if t}
            if "preload" in rel_tokens and (a.get("as") or "").strip().lower() == "image" and href:
                self.preload_images.append(href)
        elif tag.lower() == "script":
            t = (a.get("type") or "").strip().lower()
            if t == "application/ld+json":
                self._in_jsonld = True
                self._jsonld_buf = []
        elif tag.lower() in {"img", "source"}:
            # Capture common lazy-load / responsive attributes.
            captured: Dict[str, str] = {}
            for key in (
                "src",
                "srcset",
                "data-src",
                "data-srcset",
                "data-original",
                "data-lazy-src",
                "data-zoom-image",
                "data-image",
                "data-large_image",
                "data-large-image",
                "href",
            ):
                val = (a.get(key) or "").strip()
                if val:
                    captured[key] = val
            if captured:
                self.img_attrs.append(captured)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        elif tag.lower() == "script" and self._in_jsonld:
            self._in_jsonld = False
            raw = "".join(self._jsonld_buf).strip()
            if raw:
                self.jsonld.append(raw)
            self._jsonld_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = (self.title or "") + data
        elif self._in_jsonld:
            self._jsonld_buf.append(data)


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("INVALID_URL")
    if not parsed.hostname:
        raise ValueError("INVALID_URL")

    # Drop fragment and common tracking params to improve cache hit-rate.
    qs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    qs = [(k, v) for k, v in qs if k.lower() not in {"fbclid", "gclid", "yclid", "msclkid"}]
    query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(fragment="", query=query))


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _domain(url: str) -> str:
    return urlparse(url).hostname or ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_db() -> datetime:
    """
    Return a DB-safe datetime for columns that may be `timestamp` (without tz) in some environments.

    We have seen production tables drift from `timestamptz` to `timestamp` historically; asyncpg
    will error if an offset-aware datetime is bound to a `timestamp` column.
    """
    return _now().replace(tzinfo=None)


def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc)
    except Exception:
        return dt

@dataclass(frozen=True)
class PriceSignals:
    """What the page says about how it writes numbers. Only a price whose text is ambiguous on
    its own ("1,234", "2.400") consults these; see utils/crawled_price.py.

    `money_format` is the Shopify storefront money format's hint, `locale` the `og:locale` /
    `<html lang>` hint. Each is "." or "," or None (absent, or the page's own copies disagree).
    """

    money_format: Optional[str] = None
    locale: Optional[str] = None

    def hint_for(self, currency: Optional[str], *, jsonld: bool = False) -> Optional[str]:
        # JSON-LD adds the schema.org rule ("use '.' ... to indicate a decimal point") as one
        # more signal that must agree. Without it, a German page's spec-conforming `"28.000"`
        # would read as 28000 on the page's `,` locale; with it, the two disagree and it is
        # refused. Meta tags get no such signal: EU themes print the shop's own format there.
        return agreed_hint(
            (self.money_format, self.locale, decimal_hint_from_currency(currency), "." if jsonld else None)
        )


NO_SIGNALS = PriceSignals()

_MONEY_FORMAT_RE = re.compile(
    r"money_?(?:with_currency_)?format[^{}]{0,40}?(\{\{\s*amount\w*\s*\}\})", re.IGNORECASE
)


def _page_price_signals(p: "_MetaParser", html: str) -> PriceSignals:
    formats = {decimal_hint_from_money_format(m.group(1)) for m in _MONEY_FORMAT_RE.finditer(html)}
    locales = {
        decimal_hint_from_locale(tag)
        for tag in (p.meta.get(("meta", "property:og:locale")), p.html_lang)
        if tag
    }
    return PriceSignals(money_format=agreed_hint(formats), locale=agreed_hint(locales))


def _read_price(
    raw: Any, currency: Optional[str], signals: PriceSignals, *, jsonld: bool = False
) -> Tuple[Optional[float], str]:
    """(amount, status) for one crawled price, read with the page's own separator signals.

    Replaces `_parse_price`, which kept digits and dots and dropped everything else: "28,80"
    read as 2880 and "1.234,56" as 1.23456. A refusal is (None, reason), never a guess.
    """
    if not _has_price_raw(raw):
        return None, "empty"
    read = parse_crawled_price(raw, currency=currency, decimal_hint=signals.hint_for(currency, jsonld=jsonld))
    return read.amount, read.status


def _has_price_raw(raw: Any) -> bool:
    return raw is not None and raw != ""


def _raw_price_value(price: Any) -> Any:
    """A JSON-LD price as the page typed it: a JSON number stays a number (already unambiguous;
    stringifying 1.234 would make it look like "1.234"), anything else becomes stripped text."""
    if price is None:
        return None
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        return price
    return str(price).strip()


MAX_VARIANTS = int(os.getenv("EXTERNAL_OFFER_MAX_VARIANTS") or "50")
MAX_IMAGES = int(os.getenv("EXTERNAL_OFFER_MAX_IMAGES") or "20")

_IMG_EXT_RE = re.compile(r"\.(?:avif|webp|jpe?g|png|gif)(?:\\?|#|$)", re.IGNORECASE)
_IMG_FORMAT_QS_RE = re.compile(r"(?:[?&](?:fm|format)=)(?:avif|webp|jpe?g|png|gif)\\b", re.IGNORECASE)
_IMG_BLACKLIST_RE = re.compile(
    r"(?:^|/|_|-)(?:logo|icon|sprite|favicon|badge|payment|klarna|paypal)(?:$|/|\\.|_|-)",
    re.IGNORECASE,
)


_SIZE_TITLE_KEY_CANDIDATES = (
    "size",
    "size_label",
    "sizeLabel",
    "size_name",
    "sizeName",
    "size_description",
    "sizeDescription",
    "volume",
    "volume_label",
    "volumeLabel",
    "volume_name",
    "volumeName",
    "dimension",
    "dimensions",
    "capacity",
)


_SIZE_VALUE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ml|mL|l|L|oz|fl\s*oz|g|kg|lb|lbs|cm|mm|in|inch|inches)\b",
    re.IGNORECASE,
)


def _looks_like_size_label(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    if "http://" in t or "https://" in t:
        return False
    # common in beauty: "30 ml", "1.7 oz", etc.
    if _SIZE_VALUE_RE.search(t):
        return True
    # allow bare numbers for sites that already imply units (rare)
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        return True
    return False


def _normalize_variant_title(title: Optional[str]) -> Optional[str]:
    if title is None:
        return None
    t = str(title).strip()
    if not t:
        return None
    t = re.sub(r"\s+", " ", t)
    return t


def _extract_size_label_from_sku(sku: Dict[str, Any]) -> Optional[str]:
    if not isinstance(sku, dict):
        return None

    # 1) direct candidates
    for key in _SIZE_TITLE_KEY_CANDIDATES:
        val = sku.get(key)
        if isinstance(val, str) and _looks_like_size_label(val):
            return _normalize_variant_title(val)

    # 2) nested attributes dicts
    for container_key in ("attributes", "attributeValues", "attribute_values", "props", "properties"):
        attrs = sku.get(container_key)
        if isinstance(attrs, dict):
            for key in _SIZE_TITLE_KEY_CANDIDATES:
                val = attrs.get(key)
                if isinstance(val, str) and _looks_like_size_label(val):
                    return _normalize_variant_title(val)
            # common: {"size": {"label":"30 ml"}}
            for key in _SIZE_TITLE_KEY_CANDIDATES:
                val = attrs.get(key)
                if isinstance(val, dict):
                    for subkey in ("label", "value", "name", "displayValue", "display_value"):
                        subv = val.get(subkey)
                        if isinstance(subv, str) and _looks_like_size_label(subv):
                            return _normalize_variant_title(subv)

    # 3) variationAttributes style
    var_attrs = sku.get("variationAttributes") or sku.get("variation_attributes") or sku.get("variationAttrs")
    if isinstance(var_attrs, list):
        for it in var_attrs:
            if not isinstance(it, dict):
                continue
            attr_id = str(it.get("attributeId") or it.get("id") or it.get("name") or "").strip().lower()
            display = str(it.get("displayName") or it.get("label") or "").strip().lower()
            if "size" not in attr_id and "size" not in display and "volume" not in attr_id and "volume" not in display:
                continue
            for key in ("value", "displayValue", "display_value", "selectedValue", "selected_value", "name", "label"):
                val = it.get(key)
                if isinstance(val, str) and _looks_like_size_label(val):
                    return _normalize_variant_title(val)
                if isinstance(val, dict):
                    for subkey in ("value", "label", "name", "displayValue", "display_value"):
                        subv = val.get(subkey)
                        if isinstance(subv, str) and _looks_like_size_label(subv):
                            return _normalize_variant_title(subv)

    # 4) last resort: recursive scan for short size-like strings
    def _scan(obj: Any) -> Optional[str]:
        if isinstance(obj, str):
            if len(obj) <= 30 and _looks_like_size_label(obj):
                return _normalize_variant_title(obj)
            return None
        if isinstance(obj, dict):
            for v in obj.values():
                out = _scan(v)
                if out:
                    return out
        if isinstance(obj, list):
            for v in obj:
                out = _scan(v)
                if out:
                    return out
        return None

    return _scan(sku)


def _variant_title_score(*, title: Optional[str], product_name: Optional[str]) -> int:
    t = _normalize_variant_title(title)
    if not t:
        return 0
    if product_name and t.strip().lower() == str(product_name).strip().lower():
        return 0
    if _looks_like_size_label(t):
        return 3
    if len(t) <= 12:
        return 2
    if len(t) <= 32:
        return 1
    return 0


def _best_variant_title_score(variants: list[Dict[str, Any]], product_name: Optional[str]) -> int:
    best = 0
    for v in variants:
        if not isinstance(v, dict):
            continue
        best = max(best, _variant_title_score(title=v.get("title"), product_name=product_name))
    return best


def _parse_jsonld_texts(json_texts: list[str]) -> list[Any]:
    parsed_objs: list[Any] = []
    for raw in json_texts:
        try:
            parsed_objs.append(json.loads(raw))
        except Exception:
            continue
    return parsed_objs


def _iter_jsonld_nodes(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_jsonld_nodes(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_jsonld_nodes(it)


def _offer_price_and_currency(offer: Dict[str, Any]) -> Tuple[Any, Optional[str]]:
    """(raw price, ISO currency) of one offer. The price stays as the page typed it (see
    `_raw_price_value`); the currency is None unless the offer states one we can read."""
    price = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
    currency = offer.get("priceCurrency")

    spec = offer.get("priceSpecification")
    if isinstance(spec, dict):
        price = price or spec.get("price") or spec.get("minPrice") or spec.get("maxPrice")
        currency = currency or spec.get("priceCurrency")
    elif isinstance(spec, list) and spec:
        first = spec[0] if isinstance(spec[0], dict) else None
        if first:
            price = price or first.get("price") or first.get("minPrice") or first.get("maxPrice")
            currency = currency or first.get("priceCurrency")

    return (_raw_price_value(price), read_currency_code(currency))


def _is_aggregate_offer(offer: Dict[str, Any]) -> bool:
    t = offer.get("@type")
    types = {str(x).lower() for x in t} if isinstance(t, list) else ({str(t).lower()} if t else set())
    return "aggregateoffer" in types


def _offer_price_is_exact(offer: Dict[str, Any]) -> bool:
    """Is the amount `_offer_price_and_currency` returned THIS offer's own price?

    Not when it came from `lowPrice` / `highPrice` / `minPrice` / `maxPrice`, and never for an
    AggregateOffer: those are the bounds of a RANGE across variants (an AggregateOffer with no
    nested `offers` arrives here as one `offer_1` at `lowPrice`). Such an amount is fine as a
    product summary, but it names no variant, so the seed refresh must not write it into one.
    """
    if _is_aggregate_offer(offer):
        return False
    if offer.get("price") not in (None, ""):
        return True
    spec = offer.get("priceSpecification")
    first = spec if isinstance(spec, dict) else (spec[0] if isinstance(spec, list) and spec and isinstance(spec[0], dict) else None)
    return bool(first and first.get("price") not in (None, "") and not offer.get("lowPrice") and not offer.get("highPrice"))


# Per-variant provenance the extractor attaches for `routes/employee_products`'s reconcile.
# Stored on the snapshot evidence; never on a seed (see `seed_variants_from_evidence`).
READ_PROVENANCE_KEYS = ("price_exact", "offer_aggregate", "id_collided", "offer_shopify_variant_id")


# THE SHOPIFY VARIANT AN OFFER NAMES IN ITS OWN LINK. A Shopify storefront's JSON-LD names each
# size offer by `@id` "/products/<handle>?variant=<digits>#offer" (or a `url` carrying ?variant=),
# often with no `sku`, so `variant_id` is that whole string -- while the retailer-ingest drain
# stores the merchant's numeric variant id. The digits are read into `offer_shopify_variant_id`
# so the seed refresh's reconcile can match the two and re-read per-size prices.
#
# A SEPARATE FIELD, NOT `variant_id` and NOT `shopify_variant_id`. `variant_id` keeps what the
# serving builders and the de-dupe key on. `shopify_variant_id` is the cart-proof stamp
# (`services/shopify_variant_identity`) that prefills a cart permalink; a link on a page proves
# nothing about the cart. And it is read provenance: a seed never stores it, so what the seed
# refresh compares to decide whether a read may REPLACE a seed's variants is unchanged.
_SHOPIFY_PRODUCT_PATH = re.compile(r"/products/([^/]+?)(?:\.(?:js|json))?/?$")


def _shopify_product_ref(url: str) -> Optional[Tuple[str, str]]:
    """(host without www., handle) of a Shopify product URL, or None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    match = _SHOPIFY_PRODUCT_PATH.search(parsed.path or "")
    if not host or not match:
        return None
    return (host[4:] if host.startswith("www.") else host), unquote(match.group(1)).lower()


def _offer_shopify_variant_id(offer: Dict[str, Any], item: Dict[str, Any], page_urls: Tuple[str, ...]) -> Optional[str]:
    """The ?variant= digits the offer's own `@id`/`url` carry, when they name THIS page's product.

    None unless every ?variant= the offer carries is digits, all agree, and each sits on a link to
    the page's own product (its canonical or fetched URL, www-insensitive). A ?variant= on another
    product's link -- a related product's offer, a bundle component -- names that product's variant.
    """
    page_products = {ref for ref in (_shopify_product_ref(u) for u in page_urls if u) if ref}
    if not page_products:
        return None
    named: set = set()
    for node in (offer, item):
        for key in ("@id", "url"):
            raw = node.get(key)
            if not isinstance(raw, str) or "variant=" not in raw:
                continue
            try:
                absolute = urljoin(page_urls[0], raw.strip())
                values = [v for k, v in parse_qsl(urlparse(absolute).query, keep_blank_values=True) if k == "variant"]
            except ValueError:  # a malformed link names nothing, and must not fail the page's read
                return None
            if not values:
                continue
            if _shopify_product_ref(absolute) not in page_products:
                return None
            for value in values:
                if not (value.isascii() and value.isdigit()):
                    return None
                named.add(value)
    return next(iter(named)) if len(named) == 1 else None


def seed_variants_from_evidence(evidence: Any) -> Optional[list[Dict[str, Any]]]:
    """A snapshot's variants as a SEED stores them: without the per-read provenance marks.

    The marks describe one fetch (was this offer's price exact, did its id collide) and are
    read from the evidence by the seed refresh; copied into `seed_data.variants` they would
    outlive the fetch they describe. Every writer that adopts page variants into a seed goes
    through here. None when the evidence lists no variants.
    """
    raw = evidence.get("variants") if isinstance(evidence, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    return [{k: val for k, val in v.items() if k not in READ_PROVENANCE_KEYS} for v in raw if isinstance(v, dict)]


def _offer_item(offer: Dict[str, Any]) -> Dict[str, Any]:
    item = offer.get("itemOffered")
    if isinstance(item, list) and item:
        item = item[0]
    return item if isinstance(item, dict) else {}


# THE IDS AN OFFER MAY BE KEYED BY, in order. A BARCODE IS NOT ONE (2026-10-16). `gtin13` used to
# sit after `mpn`, so a Shopify offer carrying its barcode and its `?variant=` link was keyed by
# the 13 digits -- which `routes/employee_products._native_refresh_variant_ids` (`[0-9]{8,}`) and
# `services.variant_identity` read as a MERCHANT-ISSUED variant id, and which sat beside the same
# variant's link entry as a second "variant" (228 of a 454-seed sample pair a 13-digit entry with
# another at the same price). A GTIN identifies a trade item for a scanner, not a variant in the
# merchant's cart; it is kept on the entry as `barcode` (the key the stored-variant readers
# already consult), never as its id.
_OFFER_ID_KEYS = ("sku", "skuId", "productID", "mpn")
_OFFER_BARCODE_KEYS = ("gtin13", "gtin", "gtin14", "gtin12", "gtin8")


def _offer_variant_id(
    offer: Dict[str, Any], item: Dict[str, Any], idx: int, linked: Optional[str] = None
) -> Tuple[str, str]:
    """(variant_id, the field it came from) for the offer at position `idx` of its list.

    A SKU-LESS OFFER IS KEYED BY THE VARIANT IT LINKS, NOT BY ITS POSITION (review of #2634). A
    Dawn-style Shopify offer carries no `sku` and no `@id`, only its barcode and a `url`
    ".../products/<handle>?variant=<digits>". With the barcode out of the chain it would fall to
    `offer_N`, which the seed refresh never matches, so a stored size could never be re-read. It
    is keyed by the `?variant=` digits when they name the page's own product (`linked`, the
    `offer_shopify_variant_id` mark), else by its `url` when that names one variant. A `url`
    naming NO variant (the bare product page every offer repeats) stays positional: keying by it
    would collapse every size of the page into one.
    """
    from services.handover_variant_identity import url_named_variant_id

    for node in (offer, item):
        for key in _OFFER_ID_KEYS:
            if node.get(key):
                return str(node[key]), key
    for node in (offer, item):
        if node.get("@id"):
            return str(node["@id"]), "@id"
    if linked:
        return str(linked), "link"
    for node in (offer, item):
        url = node.get("url")
        if isinstance(url, str) and url_named_variant_id(url.strip()):
            return url.strip(), "url"
    return f"offer_{idx + 1}", "position"


def _offer_barcode(offer: Dict[str, Any], item: Dict[str, Any]) -> Optional[str]:
    for node in (offer, item):
        for key in _OFFER_BARCODE_KEYS:
            raw = node.get(key)
            if raw not in (None, "") and not isinstance(raw, (dict, list, bool)):
                text = str(raw).strip()
                if text:
                    return text
    return None


# A SUBSCRIPTION PLAN IS NOT A VARIANT (2026-10-16). Subscription apps (Recharge, Skio, Shopify
# selling plans) put the plans into the page's JSON-LD as offers of the SAME variant: murad
# eps_0f70f7177cecd95416d98b53 stored `offer_1..offer_N` titled "One-Time Purchase", "1 month",
# "2 months"... each at its plan's price, and production served every one as a size. Each signal
# below is explicit, and only the structural ones apply to an offer that carries an id:
#
#   STRUCTURAL (any offer)
#     * `selling_plan_key`  -- the offer or its item states a plan under `sellingPlan`,
#                              `selling_plan`, `sellingPlanId` or `selling_plan_id` with a TRUTHY,
#                              non-boolean value (`sellingPlan: false`, `selling_plan_id: 0` are a
#                              theme saying "no plan");
#     * `selling_plan_link` -- its `@id`/`url` (or its item's) carries a NON-EMPTY `selling_plan=`
#                              query value (Shopify's plan permalink; an empty one is a theme's
#                              one-time link);
#     * `billing_spec`      -- a priceSpecification component is a RECURRING charge: it states
#                              `billingDuration` and is not an Installment / Downpayment
#                              (`priceComponentType` or `@type`): "pay in 4 over 4 weeks" is a
#                              one-time sale paid in parts. Nothing else on a spec is read: not
#                              `billingIncrement` (billing granularity), not `unitCode`/`unitText`
#                              alone, and not `referenceQuantity` in ANY unit (unit pricing, "per
#                              100 ml" or "per day", of a one-time sale). Review of #2634.
#   BY NAME (positional `offer_N` offers only: nothing else identifies them; never a lone offer,
#   never an offer named as the product, and never when it would leave the list with no offer)
#     * `plan_name`         -- "subscribe"/"subscription", "auto-replenish", "auto-ship",
#                              "auto-deliver", "deliver(y)/ship(s)/sent every", "every N
#                              day(s)/week(s)/month(s)";
#     * `plan_cadence`      -- a name that is ONLY a cadence ("1 month", "2 months", "every 6
#                              weeks") when a sibling is a one-time purchase or another plan. "1
#                              Month Supply" is a size and does not match.
#   A positional sibling named "One-Time Purchase" (or "one time", "buy once", "single purchase")
#   is never dropped: once its siblings are dropped as plans it IS the product's one offer, and it
#   is titled as an unnamed offer would be (the product's name), since the label names a
#   purchase mode, not a variant.
_SELLING_PLAN_KEYS = ("sellingPlan", "selling_plan", "sellingPlanId", "selling_plan_id")
_NON_RECURRING_COMPONENTS = ("installment", "downpayment")
_PLAN_NAME = re.compile(
    r"\bsubscri(?:be|bed|bing|ption)s?\b"
    r"|\bauto[\s-]?(?:replenish\w*|ship\w*|deliver\w*|renew\w*)"
    r"|\b(?:deliver(?:y|ed|s)?|ship(?:s|ped|ment)?|sent)\s+every\b"
    r"|\bevery\s+\d+\s*-?\s*(?:day|week|month)s?\b",
    re.IGNORECASE,
)
_PLAN_CADENCE = re.compile(r"^\s*(?:every\s+)?\d+\s*-?\s*(?:day|week|month)s?\s*$", re.IGNORECASE)
_ONE_TIME_NAME = re.compile(
    r"^\s*(?:one[\s-]?time(?:\s+(?:purchase|order|buy|delivery))?|buy\s+once|purchase\s+once|single\s+purchase)\s*$",
    re.IGNORECASE,
)


def _offer_links(offer: Dict[str, Any], item: Dict[str, Any]) -> list[str]:
    return [raw for node in (offer, item) for key in ("@id", "url") if isinstance((raw := node.get(key)), str)]


def _structural_plan_signal(offer: Dict[str, Any], item: Dict[str, Any]) -> Optional[str]:
    for node in (offer, item):
        if any(node.get(key) and not isinstance(node.get(key), bool) for key in _SELLING_PLAN_KEYS):
            return "selling_plan_key"
    for raw in _offer_links(offer, item):
        if "selling_plan" not in raw:
            continue
        try:
            query = parse_qsl(urlparse(raw.strip()).query, keep_blank_values=True)
        except ValueError:
            continue
        if any(k == "selling_plan" and v.strip() for k, v in query):
            return "selling_plan_link"
    spec = offer.get("priceSpecification")
    for s in spec if isinstance(spec, list) else [spec]:
        if not isinstance(s, dict):
            continue
        kinds = [s.get("priceComponentType"), s.get("@type")]
        kinds = [str(x) for k in kinds for x in (k if isinstance(k, list) else [k]) if x]
        if any(k.rstrip("/").rsplit("/", 1)[-1].strip().lower() in _NON_RECURRING_COMPONENTS for k in kinds):
            continue
        if s.get("billingDuration") not in (None, "", 0, "0") and not isinstance(s.get("billingDuration"), bool):
            return "billing_spec"
    return None


def _selling_plan_reasons(offer_list: list[Any], product_name: Optional[str]) -> Tuple[Dict[int, str], set]:
    """({id(offer): signal} for the offers in ONE offers list that are subscription plans, and the
    ids of the positional one-time offers to retitle once a sibling was dropped). See above."""
    reasons: Dict[int, str] = {}
    named: list[Tuple[int, str]] = []  # (id(offer), name) of the positional offers left to judge
    one_time: set = set()
    dict_offers = [o for o in offer_list if isinstance(o, dict)]
    for idx, offer in enumerate(offer_list):
        if not isinstance(offer, dict):
            continue
        item = _offer_item(offer)
        signal = _structural_plan_signal(offer, item)
        if signal:
            reasons[id(offer)] = signal
            continue
        if _offer_variant_id(offer, item, idx)[1] != "position":
            continue
        name = offer.get("name") or item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if product_name and name.strip().lower() == str(product_name).strip().lower():
            continue
        if _ONE_TIME_NAME.match(name):
            one_time.add(id(offer))
        else:
            named.append((id(offer), name))

    if len(dict_offers) > 1:
        by_name = {ref: "plan_name" for ref, name in named if _PLAN_NAME.search(name)}
        if one_time or by_name or reasons:
            by_name.update(
                {ref: "plan_cadence" for ref, name in named if ref not in by_name and _PLAN_CADENCE.match(name)}
            )
        # Names never drop every offer: a list that is ALL plan-named (a subscription box sold by
        # duration) has no one-time offer to fall back to, so nothing is dropped by name.
        if by_name and len(reasons) + len(by_name) < len(dict_offers):
            reasons.update(by_name)
    return reasons, (one_time if reasons else set())


def _offer_variants_from_node(
    offers: Any, product_name: Optional[str], signals: PriceSignals = NO_SIGNALS, page_urls: Tuple[str, ...] = ()
) -> list[Dict[str, Any]]:
    if offers is None:
        return []

    offer_list: list[Any] = []
    if isinstance(offers, list):
        offer_list = offers
    elif isinstance(offers, dict):
        nested = offers.get("offers")
        if isinstance(nested, list):
            offer_list = nested
        else:
            offer_list = [offers]

    plan_reasons, one_time_offers = _selling_plan_reasons(offer_list, product_name)

    # A LINK KEYS AN OFFER ONLY WHEN NO SIBLING SHARES IT (review of #2634). A theme that stamps the
    # SELECTED variant's url on every offer links two shades to one `?variant=`; keyed by it they
    # would collide and one shade would be dropped. Such offers stay positional -- their barcode
    # (kept as `barcode`) is what still matches a stored id.
    keyed: Dict[int, Tuple[str, str, Optional[str]]] = {}
    for idx, offer in enumerate(offer_list):
        if isinstance(offer, dict):
            item = _offer_item(offer)
            linked = _offer_shopify_variant_id(offer, item, page_urls)
            keyed[idx] = (*_offer_variant_id(offer, item, idx, linked), linked)
    link_keys = [vid for vid, src, _ in keyed.values() if src in ("link", "url")]
    for idx, (vid, src, linked) in list(keyed.items()):
        if src in ("link", "url") and link_keys.count(vid) > 1:
            keyed[idx] = (f"offer_{idx + 1}", "position", linked)

    variants: list[Dict[str, Any]] = []
    for idx, offer in enumerate(offer_list):
        if not isinstance(offer, dict):
            continue
        item = _offer_item(offer)

        variant_id, id_source, shopify_variant = keyed[idx]
        barcode = _offer_barcode(offer, item)

        # A one-time offer beside dropped plans is titled as an unnamed offer: its name is a
        # purchase mode, not a variant.
        title = None if id(offer) in one_time_offers else (offer.get("name") or item.get("name"))
        if not title:
            attrs = []
            for key in ("size", "color", "model", "material", "pattern"):
                val = item.get(key) or offer.get(key)
                if val:
                    attrs.append(str(val))
            if attrs:
                title = " / ".join(attrs)
            elif product_name:
                title = product_name
        # Prefer size-like labels over generic product titles when possible.
        if product_name and isinstance(title, str) and title.strip().lower() == str(product_name).strip().lower():
            size_like = None
            for candidate in ("size", "volume", "capacity"):
                v = item.get(candidate) or offer.get(candidate)
                if isinstance(v, str) and _looks_like_size_label(v):
                    size_like = v
                    break
            if size_like:
                title = size_like

        price_raw, currency = _offer_price_and_currency(offer)
        price_amount, price_status = _read_price(price_raw, currency, signals, jsonld=True)
        availability_raw = offer.get("availability") or item.get("availability")
        availability = _availability_from_raw(str(availability_raw)) if availability_raw else "unknown"

        variants.append(
            {
                "variant_id": str(variant_id),
                "title": str(title).strip() if title else None,
                "price_amount": price_amount,
                "price_currency": currency,
                "availability": availability,
                "price_exact": price_amount is not None and _offer_price_is_exact(offer),
                # Transient, popped with `_offer_ref`: why a price the page carried was not read.
                "_price_refused": price_status if _has_price_raw(price_raw) and price_amount is None else None,
                **({"offer_aggregate": True} if _is_aggregate_offer(offer) else {}),
                **({"offer_shopify_variant_id": shopify_variant} if shopify_variant else {}),
                **({"barcode": barcode} if barcode else {}),
                # Transient: WHICH offer object this came from. `_iter_jsonld_nodes` visits a
                # Product's offers twice (via the Product, then as Offer nodes), so only object
                # identity tells a real id collision from a second visit. Popped by the caller.
                "_offer_ref": id(offer),
                # Transient, popped with `_offer_ref`: the field `variant_id` came from, and the
                # selling-plan signal that makes this offer a plan rather than a variant (the
                # caller drops it by object, on every visit).
                "_id_source": id_source,
                "_selling_plan": plan_reasons.get(id(offer)),
                # Transient: the product this offer was read under, so the fold can tell an
                # offer titled as its product from one titled as a shade/size.
                "_product_name": product_name,
            }
        )

        if len(variants) >= MAX_VARIANTS:
            break

    return variants


def _detect_currency_from_text(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip()
    # No "$" case: "$" is USD, CAD, AUD, SGD, HKD... A bare "$" read as USD is the same invented
    # currency `resolve_external_offer` used to write (see `read_currency_code`).
    if text.startswith("€"):
        return "EUR"
    if text.startswith("£"):
        return "GBP"
    if text.startswith("¥"):
        return "JPY"
    # Fallback: match currency codes in text.
    m = re.search(r"\b([A-Z]{3})\b", text)
    return m.group(1) if m else None


def _extract_variants_from_data_attrs(
    html: str, fallback_currency: Optional[str], base_url: str, signals: PriceSignals = NO_SIGNALS
) -> list[Dict[str, Any]]:
    payload = _extract_skus_payload_from_data_attrs(html)
    if not payload:
        return []

    variants: list[Dict[str, Any]] = []
    seen: set[str] = set()

    def normalize(url: Any) -> Optional[str]:
        if not url:
            return None
        if isinstance(url, str):
            s = url.strip()
        elif isinstance(url, dict):
            raw = url.get("url") or url.get("image_url") or url.get("src")
            s = str(raw).strip() if raw else ""
        else:
            return None
        if not s:
            return None
        if s.startswith("//"):
            try:
                scheme = urlparse(base_url).scheme or "https"
            except Exception:
                scheme = "https"
            s = f"{scheme}:{s}"
        if not s.startswith(("http://", "https://")):
            s = urljoin(base_url, s)
        return s if s.startswith(("http://", "https://")) else None

    for sku in payload:
        if not isinstance(sku, dict):
            continue
        variant_id = (
            sku.get("id")
            or sku.get("sku")
            or sku.get("sku_id")
            or sku.get("variant_id")
            or sku.get("variantId")
        )
        if not variant_id:
            continue
        variant_id = str(variant_id).strip()
        if variant_id in seen:
            # Same silent de-dupe as the JSON-LD reader; say so on the variant that was kept.
            for kept in variants:
                if kept.get("variant_id") == variant_id:
                    kept["id_collided"] = True
            continue
        if not variant_id:
            continue

        title = _extract_size_label_from_sku(sku) or sku.get("size") or sku.get("name") or sku.get("title")
        price_amount = sku.get("price_with_discount") or sku.get("price")
        if price_amount is None:
            price_amount = sku.get("price_with_discount_with_currency_code") or sku.get("price_with_currency_code")
        currency = (
            read_currency_code(sku.get("price_currency") or sku.get("priceCurrency"))
            or _detect_currency_from_text(sku.get("price_with_discount_with_currency_code"))
            or _detect_currency_from_text(sku.get("price_with_currency_code"))
            or fallback_currency
        )
        read_amount, _status = _read_price(_raw_price_value(price_amount), currency, signals)

        availability_raw = sku.get("inventory_status") or sku.get("availability")
        availability = _availability_from_raw(str(availability_raw)) if availability_raw else "unknown"

        image_url = None
        label_image_url = None
        raw_images = sku.get("images") or sku.get("image_urls") or sku.get("imageUrl") or sku.get("imageURL")
        candidates: list[str] = []
        if isinstance(raw_images, list):
            for raw in raw_images:
                resolved = normalize(raw)
                if resolved:
                    candidates.append(resolved)
        else:
            resolved = normalize(raw_images)
            if resolved:
                candidates.append(resolved)

        if candidates:
            unique: list[str] = []
            seen_urls: set[str] = set()
            for u in candidates:
                if u in seen_urls:
                    continue
                seen_urls.add(u)
                unique.append(u)

            label_candidates = [u for u in unique if _looks_like_variant_label_image(u)]
            if label_candidates:
                label_image_url = sorted(label_candidates, key=_image_resolution_score, reverse=True)[0]
            hero_candidates = [u for u in unique if not _looks_like_variant_label_image(u)]
            if hero_candidates:
                image_url = sorted(hero_candidates, key=_image_resolution_score, reverse=True)[0]

        variants.append(
            {
                "variant_id": variant_id,
                "title": str(title).strip() if title else None,
                "price_amount": read_amount,
                "price_currency": str(currency).strip().upper() if currency else None,
                "availability": availability,
                "price_exact": read_amount is not None,
                **({"image_url": image_url} if image_url else {}),
                **({"label_image_url": label_image_url} if label_image_url else {}),
            }
        )
        seen.add(variant_id)
        if len(variants) >= MAX_VARIANTS:
            break

    return variants


def _extract_skus_payload_from_data_attrs(html: str) -> list[dict[str, Any]]:
    attr_patterns = [
        r'data-product-skus-value="([^"]+)"',
        r"data-product-skus-value='([^']+)'",
    ]
    raw_attr = None
    for pattern in attr_patterns:
        match = re.search(pattern, html)
        if match:
            raw_attr = match.group(1)
            break

    if not raw_attr:
        return []

    try:
        decoded = html_lib.unescape(raw_attr)
        payload = json.loads(decoded)
    except Exception:
        return []

    if not isinstance(payload, list):
        return []
    return [p for p in payload if isinstance(p, dict)]


def _extract_image_urls_from_data_attrs(html: str, base_url: str) -> list[str]:
    payload = _extract_skus_payload_from_data_attrs(html)
    if not payload:
        return []

    urls: list[str] = []
    seen: set[str] = set()

    def normalize(url: Any) -> Optional[str]:
        if not url:
            return None
        if isinstance(url, str):
            s = url.strip()
        elif isinstance(url, dict):
            raw = url.get("url") or url.get("image_url") or url.get("src")
            s = str(raw).strip() if raw else ""
        else:
            return None
        if not s:
            return None
        if s.startswith("//"):
            try:
                scheme = urlparse(base_url).scheme or "https"
            except Exception:
                scheme = "https"
            s = f"{scheme}:{s}"
        if not s.startswith(("http://", "https://")):
            s = urljoin(base_url, s)
        return s if s.startswith(("http://", "https://")) else None

    for sku in payload:
        raw_images = sku.get("images") or sku.get("image_urls") or []
        if not isinstance(raw_images, list):
            continue
        for raw in raw_images:
            resolved = normalize(raw)
            if not resolved or resolved in seen:
                continue
            seen.add(resolved)
            urls.append(resolved)
            if len(urls) >= MAX_IMAGES:
                return urls
    return urls


def _link_variant_digits(raw: Any) -> str:
    """The one `?variant=` digits value a link carries, selling plan or not ("" otherwise)."""
    text = str(raw or "").strip()
    if "variant=" not in text:
        return ""
    try:
        values = {v for k, v in parse_qsl(urlparse(text).query, keep_blank_values=True) if k == "variant"}
    except ValueError:
        return ""
    value = values.pop() if len(values) == 1 else ""
    return value if value.isascii() and value.isdigit() else ""


def _entry_variant_names(entry: Dict[str, Any]) -> set:
    """Every merchant variant id a read entry (a plan's included) names: its link mark, the
    `?variant=` its key spells, and a bare numeric / gid key."""
    from services.outbound_links_service import extract_shopify_numeric_variant_id

    vid = str(entry.get("variant_id") or "").strip()
    names = {str(entry.get("offer_shopify_variant_id") or "").strip(), _link_variant_digits(vid),
             extract_shopify_numeric_variant_id(vid) or ""}
    return names - {""}


def _read_variant_claim(entry: Dict[str, Any]) -> Optional[str]:
    """The ONE merchant variant id a read entry positively names, or None.

    A link names it first: the `?variant=` its own key spells (`url_named_variant_id`, "" for a
    selling-plan link or two values) and the `offer_shopify_variant_id` mark its `@id`/`url` carry
    (only off a link to the page's own product). Two different linked values name nothing. With
    no link, a bare numeric / `gid://` key (`canonical_variant_id`'s numeric form) is the claim.
    A SKU like "114807" is a claim only of itself, so it can only equal another entry that names
    those very digits as its variant.
    """
    from services.handover_variant_identity import url_named_variant_id
    from services.outbound_links_service import extract_shopify_numeric_variant_id

    vid = str(entry.get("variant_id") or "").strip()
    linked = {x for x in (url_named_variant_id(vid), str(entry.get("offer_shopify_variant_id") or "").strip()) if x}
    if len(linked) > 1:
        return None
    if linked:
        return next(iter(linked))
    return extract_shopify_numeric_variant_id(vid) or None


def _entries_name_different_variants(group: list[Dict[str, Any]]) -> bool:
    """Do entries that share one `?variant=` link still say they are DIFFERENT variants?

    A theme that stamps the SELECTED variant's url on every offer (review of #2634: LIP-RUBY and
    LIP-CORAL, titled Ruby and Coral, one price, one `?variant=`) links two shades to one id. The
    link is then wrong, not the shades: they stay apart when they carry two different SKUs, two
    different barcodes, or two different titles neither of which is just the product's name.
    """
    def norm(value: Any) -> str:
        return str(value or "").strip().casefold()

    skus = {norm(e.get("variant_id") if e.get("_id_source") in ("sku", "skuId") else e.get("sku")) for e in group} - {""}
    barcodes = {norm(e.get("barcode")) for e in group} - {""}
    titles = {norm(e.get("title")) for e in group if norm(e.get("title")) != norm(e.get("_product_name"))} - {""}
    return len(skus) > 1 or len(barcodes) > 1 or len(titles) > 1


def _fold_same_variant_reads(entries: list[Dict[str, Any]]) -> Tuple[list[Dict[str, Any]], set]:
    """Collapse entries ONE read lists for the SAME merchant variant. Returns (entries, the
    `_offer_ref`s folded away).

    THE DEFECT (2026-10-16). The extractor walks every Product/Offer node of every JSON-LD block
    and de-dupes only on the exact `variant_id` string, so a Shopify page naming a variant once
    by its link (`@id` "/products/h?variant=45253610242298#offer", no sku) and once by a node
    carrying its SKU came out as two "variants" at one price (jurlique
    eps_019f2b9eef9a429385cccc5c), and production served both.

    THE RULE. Entries fold ONLY when positively linked to the same variant -- each names exactly
    one variant id (`_read_variant_claim`) and it is the same one -- AND they cannot disagree
    about it: the same known exact price, the same currency, no AggregateOffer, and no two
    different stated availabilities. Equal price alone never folds (a same-price shade is another
    variant), two different `?variant=` ids never fold, and an entry whose key is a variant id
    that ANOTHER entry claims while it links a different one is ambiguous and never folds. A SKU
    node with no link at all is NOT folded into a link entry at the same price: nothing on the
    page ties them (left for a products.js-backed repair). Listings that disagree stay apart, so
    `routes/employee_products._reconcile_seed_variants_with_read` still refuses to re-read a
    variant the page prices twice (review of #2631).

    Entries sharing one link that still name different variants (two SKUs, two barcodes, two
    titles that are not the product's name: `_entries_name_different_variants`) never fold.

    The kept entry is the one whose own key is the bare numeric id, else the one whose key is the
    link; it keeps the other's SKU as `sku` (or its `mpn`), its link mark and its barcode.
    """
    from services.outbound_links_service import extract_shopify_numeric_variant_id

    claims = [_read_variant_claim(e) for e in entries]
    claimed = {c for c in claims if c}
    for i, e in enumerate(entries):
        own = extract_shopify_numeric_variant_id(str(e.get("variant_id") or "").strip())
        if claims[i] and own and own != claims[i] and own in claimed:
            claims[i] = None

    groups: Dict[str, list[int]] = {}
    for i, c in enumerate(claims):
        if c:
            groups.setdefault(c, []).append(i)

    def money(e: Dict[str, Any]) -> Tuple[Optional[float], str]:
        amount = e.get("price_amount")
        return (round(float(amount), 2) if isinstance(amount, (int, float)) and not isinstance(amount, bool) else None,
                str(e.get("price_currency") or "").strip().upper())

    replace: Dict[int, Dict[str, Any]] = {}  # group's first position -> the kept, merged entry
    dropped: set = set()  # every other position of a folded group
    folded_refs: set = set()
    for claim, members in groups.items():
        if len(members) < 2:
            continue
        group = [entries[i] for i in members]
        prices = {money(e) for e in group}
        states = {str(e.get("availability") or "unknown") for e in group} - {"unknown"}
        if (
            len(prices) != 1
            or next(iter(prices))[0] is None
            or not all(e.get("price_exact") is True for e in group)
            or any(e.get("offer_aggregate") for e in group)
            or len(states) > 1
            or _entries_name_different_variants(group)
        ):
            continue

        def rank(i: int) -> int:
            vid = str(entries[i].get("variant_id") or "").strip()
            if extract_shopify_numeric_variant_id(vid) == claim:
                return 0
            from services.handover_variant_identity import url_named_variant_id

            return 1 if url_named_variant_id(vid) == claim else 2

        keep_i = min(members, key=lambda i: (rank(i), i))
        kept = dict(entries[keep_i])
        for i in members:
            if i == keep_i:
                continue
            other = entries[i]
            src = other.get("_id_source")
            if src in ("sku", "skuId") and not kept.get("sku"):
                kept["sku"] = other.get("variant_id")
            elif src == "mpn" and not kept.get("mpn"):
                kept["mpn"] = other.get("variant_id")
            for key in ("offer_shopify_variant_id", "barcode"):
                if other.get(key) and not kept.get(key):
                    kept[key] = other[key]
            if not kept.get("title") and other.get("title"):
                kept["title"] = other["title"]
            if other.get("id_collided"):
                kept["id_collided"] = True
            folded_refs.add(other.get("_offer_ref"))
        # The kept entry takes the group's FIRST position, so page order is preserved.
        replace[members[0]] = kept
        dropped.update(members[1:])

    out = [replace.get(i, e) for i, e in enumerate(entries) if i not in dropped]
    return out, folded_refs


def _extract_jsonld_variants(parsed_objs: list[Any]) -> list[Dict[str, Any]]:
    return _extract_jsonld_variants_with_census(parsed_objs)[0]


def _extract_jsonld_variants_with_census(
    parsed_objs: list[Any], signals: PriceSignals = NO_SIGNALS, page_urls: Tuple[str, ...] = ()
) -> Tuple[list[Dict[str, Any]], Dict[str, Any]]:
    """The page's JSON-LD variants, one per id, plus what the de-dupe would otherwise hide.

    THE DE-DUPE BELOW KEEPS THE FIRST OFFER PER ID AND DROPS THE REST SILENTLY. A page whose
    offers all carry the product `@id` (misshaus) comes out as ONE variant at the first offer's
    price, indistinguishable from a page that really lists one offer. The seed refresh writes
    per-variant prices from this list, so it has to be able to tell: the kept variant is marked
    `id_collided` when a DIFFERENT offer object carried its id, and the census counts the
    distinct offers and their exact prices before anything is dropped.
    """
    variants: list[Dict[str, Any]] = []
    seen: set[str] = set()

    for root in parsed_objs:
        for node in _iter_jsonld_nodes(root):
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            if isinstance(t, list):
                tset = {str(x).lower() for x in t}
            else:
                tset = {str(t).lower()} if t else set()

            product_name = node.get("name") if isinstance(node.get("name"), str) else None
            if "product" in tset:
                variants += _offer_variants_from_node(node.get("offers"), product_name, signals, page_urls)
            elif "offer" in tset:
                variants += _offer_variants_from_node(node, product_name, signals, page_urls)

            if len(variants) >= MAX_VARIANTS:
                break
        if len(variants) >= MAX_VARIANTS:
            break

    truncated = len(variants) >= MAX_VARIANTS
    # A selling plan is dropped BY OBJECT: the Product visit judged it among its siblings, and its
    # second visit as a lone Offer node (where no sibling is visible) must not bring it back.
    plan_refs = {v.get("_offer_ref") for v in variants if v.get("_selling_plan")}
    all_variants = variants
    # ONE ID PER OFFER OBJECT: the one its first visit gave it. The Product visit sees the offer
    # among its siblings; the lone-Offer revisit cannot (a positional offer comes back `offer_1`,
    # a sibling-shared link comes back keyed by the link), so a revisit's different id is not a
    # second offer and must not read as an id collision.
    first_id: Dict[int, str] = {}
    for v in variants:
        first_id.setdefault(v.get("_offer_ref"), str(v.get("variant_id") or "").strip())
    variants = [
        v for v in variants
        if v.get("_offer_ref") not in plan_refs and str(v.get("variant_id") or "").strip() == first_id[v.get("_offer_ref")]
    ]
    refs_by_id: Dict[str, set] = {}
    by_ref: Dict[int, Dict[str, Any]] = {}
    for v in variants:
        ref = v.get("_offer_ref")
        refs_by_id.setdefault(str(v.get("variant_id") or "").strip(), set()).add(ref)
        by_ref.setdefault(ref, v)

    normalized: list[Dict[str, Any]] = []
    emitted_refs: set = set()
    for v in variants:
        vid = str(v.get("variant_id") or "").strip()
        # One entry per offer OBJECT too: a positional offer's second visit is keyed `offer_1`
        # whatever its position in the Product's list, which would list it twice.
        if not vid or vid in seen or v.get("_offer_ref") in emitted_refs:
            continue
        seen.add(vid)
        emitted_refs.add(v.get("_offer_ref"))
        if len(refs_by_id.get(vid) or ()) > 1:
            v["id_collided"] = True
        normalized.append(v)
        if len(normalized) >= MAX_VARIANTS:
            truncated = True
            break

    normalized, folded_refs = _fold_same_variant_reads(normalized)

    # The census counts what the page OFFERS: neither a dropped plan nor a folded second listing
    # of one variant is a sibling offer (`_page_names_one_value` in the refresh reads `offers`).
    distinct = [v for ref, v in by_ref.items() if ref not in folded_refs]
    census = {
        "offers": len(distinct),
        "exact_prices": sorted(
            {round(v["price_amount"], 2) for v in distinct if v.get("price_exact") and v.get("price_amount") is not None}
        ),
        "availabilities": sorted(
            {str(v.get("availability")) for v in distinct if str(v.get("availability") or "unknown") != "unknown"}
        ),
        "inexact": any(v.get("price_amount") is not None and not v.get("price_exact") for v in distinct),
        "aggregate": any(v.get("offer_aggregate") for v in distinct),
        "duplicate_ids": sum(1 for refs in refs_by_id.values() if len(refs) > 1),
        "truncated": truncated,
        # Offers whose price text was refused (ambiguous separator, two numbers, ...): counted so
        # a page that stops yielding prices is visible, not just priceless.
        "price_refused": sum(1 for v in distinct if v.get("_price_refused")),
        # Offers dropped as subscription plans, and second listings folded into the variant they
        # name (see `_selling_plan_reasons`, `_fold_same_variant_reads`): neither is in `offers`.
        "selling_plans": len(plan_refs),
        "folded_duplicates": len(folded_refs),
        # EVERY offer the page listed was a plan (review of #2634). `offers` is then 0, which
        # `_page_names_one_value` reads as "one value", while the product price falls back to
        # og:price -- the first or selected variant's. The refresh refuses a census carrying this,
        # so a single stored variant never takes another variant's price off it.
        "plans_only": bool(plan_refs) and not distinct,
        # The variants a dropped plan names that NO remaining offer names (review of #2634): the
        # page sells them, only by subscription, so the offers left are not the page's every value
        # and the refresh refuses its single-variant product-level re-read off this census.
        "orphan_plan_variants": sorted(
            set().union(*(_entry_variant_names(v) for v in all_variants if v.get("_selling_plan")))
            - set().union(*(_entry_variant_names(v) for v in list(distinct) + normalized))
        ),
    }
    for v in list(all_variants) + normalized:
        for key in ("_offer_ref", "_price_refused", "_id_source", "_selling_plan", "_product_name"):
            v.pop(key, None)
    return normalized, census


def _extract_jsonld_image_urls(parsed_objs: list[Any], base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def normalize(url: Any) -> Optional[str]:
        if not url:
            return None
        if isinstance(url, str):
            s = url.strip()
        elif isinstance(url, dict):
            raw = url.get("url") or url.get("contentUrl") or url.get("content_url")
            s = str(raw).strip() if raw else ""
        else:
            return None
        if not s:
            return None
        if s.startswith("//"):
            try:
                scheme = urlparse(base_url).scheme or "https"
            except Exception:
                scheme = "https"
            s = f"{scheme}:{s}"
        if not s.startswith(("http://", "https://")):
            s = urljoin(base_url, s)
        return s if s.startswith(("http://", "https://")) else None

    for root in parsed_objs:
        for node in _iter_jsonld_nodes(root):
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            if isinstance(t, list):
                tset = {str(x).lower() for x in t}
            else:
                tset = {str(t).lower()} if t else set()
            if "product" not in tset:
                continue
            img = node.get("image")
            candidates: list[Any] = []
            if isinstance(img, list):
                candidates = img
            elif img is not None:
                candidates = [img]

            for candidate in candidates:
                resolved = normalize(candidate)
                if not resolved or resolved in seen:
                    continue
                seen.add(resolved)
                urls.append(resolved)
                if len(urls) >= MAX_IMAGES:
                    return urls
    return urls


def _extract_meta_image_urls(parser: _MetaParser, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def normalize(url: str) -> Optional[str]:
        s = str(url or "").strip()
        if not s:
            return None
        if s.startswith("//"):
            try:
                scheme = urlparse(base_url).scheme or "https"
            except Exception:
                scheme = "https"
            s = f"{scheme}:{s}"
        if not s.startswith(("http://", "https://")):
            s = urljoin(base_url, s)
        return s if s.startswith(("http://", "https://")) else None

    keys = [
        "property:og:image",
        "property:og:image:url",
        "property:og:image:secure_url",
        "name:twitter:image",
    ]
    for key in keys:
        for raw in parser.meta_list.get(("meta", key), []):
            resolved = normalize(raw)
            if not resolved or resolved in seen:
                continue
            seen.add(resolved)
            urls.append(resolved)
            if len(urls) >= MAX_IMAGES:
                return urls
    return urls


def _extract_dom_image_urls(parser: _MetaParser, base_url: str) -> list[str]:
    """
    Best-effort gallery extraction from <img>/<source>/<link rel=preload as=image> tags.

    This is intentionally heuristic to pick up common ecommerce galleries (Shopify themes, etc.)
    when JSON-LD/OG only provides a single hero image.
    """
    candidates: list[str] = []
    candidates.extend([c for c in parser.preload_images if isinstance(c, str)])
    for attrs in parser.img_attrs:
        if not isinstance(attrs, dict):
            continue
        for key in (
            "src",
            "data-src",
            "data-original",
            "data-lazy-src",
            "data-zoom-image",
            "data-image",
            "data-large_image",
            "data-large-image",
            "href",
        ):
            raw = attrs.get(key)
            if raw:
                candidates.append(raw)
        # Prefer the highest-resolution entry in a srcset/data-srcset.
        for key in ("srcset", "data-srcset"):
            raw = attrs.get(key)
            if not raw:
                continue
            raw = html_lib.unescape(str(raw))
            best_url = None
            best_score = -1.0
            for part in raw.split(","):
                token = part.strip()
                if not token:
                    continue
                bits = token.split()
                url = bits[0].strip()
                if not url:
                    continue
                score = 0.0
                if len(bits) >= 2:
                    d = bits[1].strip().lower()
                    if d.endswith("w"):
                        try:
                            score = float(int(re.sub(r"[^0-9]", "", d) or "0"))
                        except Exception:
                            score = 0.0
                    elif d.endswith("x"):
                        try:
                            score = float(re.sub(r"[^0-9.]", "", d) or "0")
                        except Exception:
                            score = 0.0
                if score >= best_score:
                    best_score = score
                    best_url = url
            if best_url:
                candidates.append(best_url)

    urls: list[str] = []
    seen: set[str] = set()

    def normalize(raw: Any) -> Optional[str]:
        if not raw:
            return None
        s = str(raw).strip()
        if not s:
            return None
        s = html_lib.unescape(s)
        if s.startswith("data:"):
            return None
        if s.startswith("//"):
            try:
                scheme = urlparse(base_url).scheme or "https"
            except Exception:
                scheme = "https"
            s = f"{scheme}:{s}"
        if not s.startswith(("http://", "https://")):
            s = urljoin(base_url, s)
        return s if s.startswith(("http://", "https://")) else None

    def looks_like_image(url: str) -> bool:
        u = url.lower()
        if u.startswith("data:"):
            return False
        if ".svg" in u:
            return False
        if _IMG_BLACKLIST_RE.search(u):
            return False
        # Prefer extension, but allow common CDN format query strings (e.g. ?fm=webp).
        if _IMG_EXT_RE.search(u) or _IMG_FORMAT_QS_RE.search(u):
            return True
        # Some CDNs omit extensions; accept those as a fallback if they live under /images or /image.
        if "/images" in u or "/image" in u:
            return True
        return False

    for raw in candidates:
        resolved = normalize(raw)
        if not resolved or resolved in seen:
            continue
        if not looks_like_image(resolved):
            continue
        seen.add(resolved)
        urls.append(resolved)
        if len(urls) >= MAX_IMAGES:
            break
    return urls


_SHOPIFY_SIZE_SUFFIX_RE = re.compile(r"_(\d{2,4})x(\d{0,4})?(?=\.(?:jpe?g|png|webp|gif|avif))", re.IGNORECASE)
_SWATCH_HINT_RE = re.compile(r"\b(?:swatch|swatches|thumb|thumbnail|icon|sprite|badge)\b", re.IGNORECASE)


def _image_dedupe_key(url: str) -> str:
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        parsed = urlparse(s)
    except Exception:
        return s
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if "cdn.shopify.com" in netloc:
        path = _SHOPIFY_SIZE_SUFFIX_RE.sub("", path)
    qs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in {"width", "w", "height", "h", "dpr", "quality", "q"}
    ]
    query = urlencode(sorted(qs, key=lambda kv: (kv[0].lower(), kv[1])), doseq=True)
    if query:
        return f"{netloc}{path}?{query}"
    return f"{netloc}{path}"


def _image_dimensions(url: str) -> tuple[Optional[int], Optional[int], Optional[float]]:
    s = str(url or "").strip()
    if not s:
        return None, None, None
    try:
        parsed = urlparse(s)
    except Exception:
        return None, None, None

    width: Optional[int] = None
    height: Optional[int] = None
    dpr: Optional[float] = None
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        kl = k.lower()
        if width is None and kl in {"width", "w"}:
            try:
                width = int(re.sub(r"[^0-9]", "", v) or "0") or None
            except Exception:
                width = None
        elif height is None and kl in {"height", "h"}:
            try:
                height = int(re.sub(r"[^0-9]", "", v) or "0") or None
            except Exception:
                height = None
        elif dpr is None and kl == "dpr":
            try:
                dpr = float(re.sub(r"[^0-9.]", "", v) or "0") or None
            except Exception:
                dpr = None

    m = _SHOPIFY_SIZE_SUFFIX_RE.search(parsed.path or "")
    if m:
        try:
            w = int(m.group(1) or "0") or 0
        except Exception:
            w = 0
        try:
            h = int(m.group(2) or "0") if m.group(2) else 0
        except Exception:
            h = 0
        if w and width is None:
            width = w
        if h and height is None:
            height = h

    return width, height, dpr


def _looks_like_variant_label_image(url: str) -> bool:
    u = str(url or "").strip().lower()
    if not u:
        return False
    if _SWATCH_HINT_RE.search(u):
        return True

    width, height, dpr = _image_dimensions(u)
    if width is None and height is None:
        return False

    scale = float(dpr) if dpr and dpr > 0 else 1.0
    if width is not None and height is not None:
        return max(width, height) * scale <= 200
    if width is not None:
        return width * scale <= 200
    if height is not None:
        return height * scale <= 200
    return False


def _image_resolution_score(url: str) -> float:
    s = str(url or "").strip()
    if not s:
        return 0.0
    try:
        parsed = urlparse(s)
    except Exception:
        return 0.0

    width, height, dpr = _image_dimensions(s)

    score = 0.0
    if width is not None and height is not None:
        score = float(width * height)
    elif width is not None:
        score = float(width)
    elif height is not None:
        score = float(height)

    if score and dpr and dpr > 0:
        score *= float(dpr * dpr)
    return score


def _parse_aggregate_rating(node: Any) -> tuple[Optional[float], Optional[int]]:
    """Pull (rating_value, rating_count) from a schema.org node's optional
    `aggregateRating` block, normalized to a 0–5 star scale. Returns (None, None)
    when absent/unparseable — a product without reviews stays null, never
    invented. `reviewCount` is preferred, `ratingCount` is the schema.org
    fallback some stores emit.

    Range safety (rating_value feeds the decision-intelligence lane with no CHECK
    constraint): a value outside [0, 5] is rejected as NULL rather than written as
    a mis-scaled number — a 0–10/0–100 scale or a negative is "no data", not 87
    stars. When `bestRating` says the page isn't on a 5-point scale we also NULL
    the value (we don't guess a rescale). A negative reviewCount is dropped."""
    if not isinstance(node, dict):
        return None, None
    agg = node.get("aggregateRating")
    if not isinstance(agg, dict):
        return None, None

    best_rating: Optional[float] = None
    raw_best = agg.get("bestRating")
    if raw_best is not None:
        try:
            best_rating = float(str(raw_best).strip())
        except (TypeError, ValueError):
            best_rating = None

    rating_value: Optional[float] = None
    raw_value = agg.get("ratingValue")
    if raw_value is not None:
        try:
            rating_value = float(str(raw_value).strip())
        except (TypeError, ValueError):
            rating_value = None
    if rating_value is not None:
        if rating_value < 0 or rating_value > 5:
            rating_value = None  # out of the 5-star range → mis-scaled / no data
        elif best_rating is not None and abs(best_rating - 5.0) > 1e-9:
            rating_value = None  # page isn't on a 5-point scale — don't guess

    rating_count: Optional[int] = None
    raw_count = agg.get("reviewCount")
    if raw_count is None:
        raw_count = agg.get("ratingCount")
    if raw_count is not None:
        try:
            rating_count = int(float(str(raw_count).strip()))
        except (TypeError, ValueError):
            rating_count = None
    if rating_count is not None and rating_count < 0:
        rating_count = None
    return rating_value, rating_count


def _variant_parent_names(parsed_objs: list[Any]) -> Dict[int, str]:
    """`id(variant node) -> its product's name`, for variants nested in a `hasVariant` list.

    A Shopify ProductGroup page (tatcha.com, 2026-09-29) is
    `{"@type": "ProductGroup", "name": "The Dewy Serum", "hasVariant": [{"@type": "Product",
    "name": "30 ml | 1.0 oz", "offers": ...}, ...]}`. The group is not a `product` type below, so
    only its variants compete, and a variant's `name` is its OPTION LABEL ("30 ml | 1.0 oz",
    "Default Title", "Black"), not the product's. Keyed by object identity because a variant
    carries no reliable back-reference (`isVariantOf` is usually a bare `@id`).
    """
    names: Dict[int, str] = {}
    for root in parsed_objs:
        for node in _iter_jsonld_nodes(root):
            if not isinstance(node, dict):
                continue
            group_name = node.get("name")
            children = node.get("hasVariant")
            if not isinstance(group_name, str) or not group_name.strip():
                continue
            if isinstance(children, dict):
                children = [children]
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        names[id(child)] = group_name.strip()
    return names


def _extract_jsonld_offer(parsed_objs: list[Any]) -> Dict[str, Any]:
    best: Dict[str, Any] = {}
    parent_names = _variant_parent_names(parsed_objs)
    for root in parsed_objs:
        for node in _iter_jsonld_nodes(root):
            t = node.get("@type") if isinstance(node, dict) else None
            if isinstance(t, list):
                tset = {str(x).lower() for x in t}
            else:
                tset = {str(t).lower()} if t else set()
            if "product" not in tset and "offer" not in tset:
                continue

            # A variant's own `name` is its option label; the product is named by its group.
            parent = node.get("isVariantOf") if isinstance(node, dict) else None
            parent_name = parent.get("name") if isinstance(parent, dict) else None
            name = (
                parent_names.get(id(node))
                or (parent_name if isinstance(parent_name, str) and parent_name.strip() else None)
                or (node.get("name") if isinstance(node, dict) else None)
            )
            description = node.get("description") if isinstance(node, dict) else None
            brand = None
            b = node.get("brand") if isinstance(node, dict) else None
            if isinstance(b, dict):
                brand = b.get("name")
            elif isinstance(b, str):
                brand = b

            image = node.get("image") if isinstance(node, dict) else None
            image_url = image[0] if isinstance(image, list) and image else image if isinstance(image, str) else None

            offers = node.get("offers") if isinstance(node, dict) else None
            # The product's price is never a subscription plan's (`_selling_plan_reasons`): the
            # variant list drops plans, so a census of one offer would otherwise vouch for a plan
            # price as the product's. The first offer that is not a plan; none when all are.
            offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
            plans, _ = _selling_plan_reasons(offer_list, name if isinstance(name, str) else None)
            if plans:
                offers = next((o for o in offer_list if isinstance(o, dict) and id(o) not in plans), None)
            elif isinstance(offers, list) and offers:
                offers = offers[0]
            price = None
            currency = None
            availability = None
            price_exact = False
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
                currency = offers.get("priceCurrency")
                availability = offers.get("availability")
                price_exact = bool(offers.get("price")) and not _is_aggregate_offer(offers)

            # Prefer nodes that include price/currency.
            score = 0
            if name:
                score += 1
            if image_url:
                score += 1
            if description:
                score += 1
            if price and currency:
                score += 2
            if availability:
                score += 1

            if score and score > best.get("_score", 0):
                rating_value, rating_count = _parse_aggregate_rating(node)
                best = {
                    "_score": score,
                    "title": str(name).strip() if name else None,
                    "description": str(description).strip() if isinstance(description, str) else None,
                    "brand": str(brand).strip() if brand else None,
                    "image_url": str(image_url).strip() if image_url else None,
                    "price_raw": _raw_price_value(price),
                    # False for an AggregateOffer's lowPrice/highPrice: a range bound, not a price.
                    "price_exact": price_exact,
                    "currency": read_currency_code(currency),
                    "availability_raw": str(availability).strip() if availability else None,
                    # Optional review signal off the winning Product node — null when
                    # the page carries no aggregateRating (never fabricated).
                    "rating_value": rating_value,
                    "rating_count": rating_count,
                }

    best.pop("_score", None)
    return best


def _availability_from_raw(raw: Optional[str]) -> str:
    """Map a raw availability signal (often a JSON-LD schema.org IRI) to our canonical token.

    This previously matched only the InStock/OutOfStock substrings, so ten of the twelve
    schema.org ItemAvailability values resolved to "unknown" — including SoldOut and
    Reserved, which schema.org itself defines as not available. Plain "sold out",
    "unavailable" and "oos" missed too.
    """
    canonical = normalize_availability(raw)
    return canonical if canonical is not None else "unknown"


def _normalize_description_text(raw: Optional[str]) -> Optional[str]:
    if not raw or not str(raw).strip():
        return None

    text = (
        str(raw)
        .replace("\xa0", " ")
        .replace("<br>", "\n")
        .replace("<br/>", "\n")
        .replace("<br />", "\n")
        .replace("</p>", "\n")
        .replace("</div>", "\n")
        .replace("</li>", "\n")
    )
    text = re.sub(r"</?(?:ul|ol)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text or None


def _prefer_detailed_description_with_source(
    *, primary: Optional[str], detailed: Optional[str], fallback: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """The description and where it came from: "jsonld" (the structured Product description),
    "page_long" (the page's own long-description block) or "meta" (og/meta description tags)."""
    structured = _normalize_description_text(primary)
    detailed_text = _normalize_description_text(detailed)
    fallback_text = _normalize_description_text(fallback)

    if detailed_text:
        if not structured:
            return detailed_text, "page_long"
        structured_lower = structured.lower()
        detailed_lower = detailed_text.lower()
        starts_with_structured = detailed_lower.startswith(structured_lower)
        materially_longer = len(detailed_text) >= max(len(structured) + 60, round(len(structured) * 1.35))
        looks_like_expanded = bool(re.search(r"\bthis set includes\b|\bproduct details\b|\n|•|\bto use\b", detailed_text, re.IGNORECASE))
        if starts_with_structured or (materially_longer and looks_like_expanded):
            return detailed_text, "page_long"

    if structured:
        return structured, "jsonld"
    if fallback_text:
        return fallback_text, "meta"
    return None, None


def _prefer_detailed_description(*, primary: Optional[str], detailed: Optional[str], fallback: Optional[str]) -> Optional[str]:
    return _prefer_detailed_description_with_source(primary=primary, detailed=detailed, fallback=fallback)[0]


def _extract_long_description_from_html(html: str) -> Optional[str]:
    hidden_match = re.search(
        r'<input[^>]+id=["\\\']overview-about-text["\\\'][^>]+value=["\\\']([^"\\\']+)["\\\']',
        html,
        flags=re.IGNORECASE,
    )
    if hidden_match:
        raw = html_lib.unescape(hidden_match.group(1))
        try:
            decoded = unquote(raw)
        except Exception:
            decoded = raw
        text = _normalize_description_text(decoded)
        if text:
            return text

    modal_match = re.search(
        r'<div[^>]+class=["\\\'][^"\\\']*more-about-product-content[^"\\\']*["\\\'][^>]*>(.*?)</div>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if modal_match:
        text = _normalize_description_text(modal_match.group(1))
        if text:
            return text

    return None


def _mark_from_cache(observed: Optional[Dict[str, Any]]) -> None:
    """Record that the snapshot being returned came from the CACHE, not from this request.

    `observed` is an out-parameter, and its `status_code` says only that a response arrived —
    not that the snapshot the caller receives was built from it. Anything that needs to know
    "did we actually re-read this page" (a freshness stamp, a gate that clears a serving
    blocker) must consult this, because a post-response failure leaves `status_code` set and
    still hands back the previous row.
    """
    if observed is not None:
        observed["from_cache"] = True


class ExternalOfferUnavailable(RuntimeError):
    """The ORIGIN answered, and its answer was "not here".

    Split out from the bare `httpx.HTTPStatusError` `raise_for_status()` used to raise
    because callers could not tell it apart from a timeout, a TLS failure, or a bot
    challenge — and the difference is the whole question. A 404 is a fact about the
    product; a timeout is a fact about the network. `_refresh_external_seed_by_id`
    swallowed both into `{"status": "degraded"}`, which is why a dead destination could
    never become known (docs/external-seed-dead-pdp-link-audit.md §4.2).

    `final_url` is the URL the request ENDED on, so a caller can tell a 200 that stayed
    on the product from a 301 that dropped the shopper on a collection page.
    """

    def __init__(self, *, status_code: int, url: str, final_url: Optional[str] = None) -> None:
        super().__init__(f"origin answered {status_code} for {url}")
        self.status_code = int(status_code)
        self.url = url
        self.final_url = final_url or url


async def _fetch_html(
    url: str,
    *,
    max_wait: Optional[float] = None,
    observed: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    # `observed` is an OUT-parameter, not a behaviour switch: pass a dict and it is filled with
    # {"status_code", "final_url"} for the request that actually left. It exists because the
    # return type is (text, content_type) and the FINAL url is the only way to tell a product
    # page from a 301 that dropped the shopper on a collection — a distinction three existing
    # callers do not need and must not have to care about. Adding it as a kwarg keeps every
    # `html, ct = await _fetch_html(...)` call site working unchanged.
    # Every crawl now leaves from ONE reserved NAT address per environment
    # (infra/gcp/setup_crawl_egress.sh), so an unpaced loop earns a per-IP block that takes the
    # whole lane down rather than one worker. Robots + per-domain pacing are the prerequisites
    # docs/commerce-index-crawl-lane.md sets before any job runs on that subnet.
    #
    # RobotsDisallowed propagates. The caller already treats a fetch exception as "keep the
    # cached snapshot", which is the right answer for "we were told not to" as well — and the
    # distinct type means a caller that wants to stop retrying can tell it from a timeout.
    # `max_wait` is the caller's patience, and it MATTERS which caller you are. The default
    # ceiling exists for the live route; a BATCH job must pass 0 (unbounded) or the backoff curve
    # above ~16s becomes unreachable — `await_slot` would refuse instead of waiting, and the
    # script's `except Exception` would silently record the row as fetch_failed. A host that 429s
    # four times in a row would then void the rest of the run in milliseconds while looking like
    # the host was down.
    await crawl_politeness.before_request(url, user_agent=crawl_identity.robots_user_agent(DEFAULT_UA), max_wait=max_wait)

    timeout = httpx.Timeout(10.0, connect=5.0)
    # Signed (Web Bot Auth) when CRAWL_WEB_BOT_AUTH_ENABLED and a key are set; every redirect hop is
    # signed for its own host. Off: the same client as before (services/crawl_identity.py).
    #
    # REDIRECT HOPS ARE ORIGIN REQUESTS (review M2, 2026-10-17). `before_request` paces the FIRST
    # request only; with httpx's default 20 hops a row could send 21 requests back to back, and a hop
    # onto another host skipped the new-hosts-per-minute cap. `redirect_client_kwargs` installs the
    # global pacer's request hook (a slot per hop after the first; a hop's host is admitted like any
    # first contact) and caps hops at `crawl_global_pacer.MAX_REDIRECTS` (5). With no pacer
    # installed (the interactive route) the hook does nothing and only the cap applies.
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers={"User-Agent": DEFAULT_UA},
                                 **crawl_global_pacer.redirect_client_kwargs(
                                     robots_user_agent=crawl_identity.robots_user_agent(DEFAULT_UA)),
                                 **crawl_identity.transport_kwargs()) as client:
        resp = await client.get(url)
        # BEFORE raise_for_status: a 429 IS the signal the backoff exists to consume, and
        # raise_for_status would leave with it unrecorded — so the next call would hit the same
        # host at the same rate that just got us throttled.
        crawl_politeness.note_response(
            url, resp.status_code, retry_after=resp.headers.get("retry-after"),
            headers=resp.headers,
        )
        if observed is not None:
            observed["status_code"] = resp.status_code
            observed["final_url"] = str(resp.url)
            observed["bot_challenged"] = bool(resp.headers.get("cf-mitigated"))
        # Teaches the shared Shopify-edge budget which hosts it covers, keyed by the host that
        # ANSWERED (the final URL after redirects). A no-op with its flag off.
        shopify_edge_pacer.learn_from_response(str(resp.url), resp.headers)
        # WAS `resp.raise_for_status()`. Same control flow — every existing caller
        # catches `Exception` — but the status and the final URL now survive the throw,
        # which is what lets the refresh record "this product is gone" instead of
        # "something went wrong". See ExternalOfferUnavailable.
        #
        # The 2xx RANGE, not `>= 400`, because those are NOT the same predicate and the
        # difference is a silent behaviour change: `raise_for_status` throws for anything
        # outside 2xx, which includes a 3xx that survived `follow_redirects=True` (a 302 with
        # no Location has no redirect to follow). Under `>= 400` such a response would be
        # parsed as if it were the product page, and its body written into the snapshot.
        #
        # Spelled out rather than using httpx's `resp.is_success`: the predicate is the point
        # and it should not depend on an attribute every test double has to remember to grow.
        if not (200 <= resp.status_code < 300):
            raise ExternalOfferUnavailable(
                status_code=resp.status_code, url=url, final_url=str(resp.url)
            )
        content_type = (resp.headers.get("content-type") or "").lower()
        # Read up to MAX_BODY_BYTES to keep latency predictable.
        body = resp.content[:MAX_BODY_BYTES]
        text = body.decode(resp.encoding or "utf-8", errors="replace")
        return text, content_type


def _read_product_price(
    jsonld: Dict[str, Any], meta_price: Optional[str], meta_currency: Optional[str], signals: PriceSignals
) -> Dict[str, Any]:
    """The product-level amount: JSON-LD's, else the `product:price:amount` meta tag's.

    Each is read with the currency the page pairs with IT. The meta tag is where EU Shopify
    themes print `money_without_currency` ("28,80"), which the old parser read as 2880. A
    JSON-LD text the parser refuses falls back to the meta tag (a different rendering of the
    same price); when both are present and refused, the JSON-LD refusal is the one reported.

    Returns {"amount", "source", "raw", "status", "decimal_hint"}; `raw` is the text the amount
    came from (or was refused from), truncated, kept on the snapshot evidence.
    """
    attempts = []
    if _has_price_raw(jsonld.get("price_raw")):
        attempts.append(("jsonld", jsonld.get("price_raw"), jsonld.get("currency") or meta_currency))
    if _has_price_raw(meta_price):
        attempts.append(("meta", meta_price, meta_currency or jsonld.get("currency")))
    first_refusal: Optional[Dict[str, Any]] = None
    for source, raw, cur in attempts:
        amount, status = _read_price(raw, cur, signals, jsonld=source == "jsonld")
        outcome = {
            "amount": amount,
            "source": source,
            "raw": str(raw)[:64],
            "status": status,
            "decimal_hint": signals.hint_for(cur, jsonld=source == "jsonld"),
        }
        # `amount` falsy, not None: as the old `_parse_price(jsonld) or _parse_price(meta)` did, a
        # 0 falls through to the meta tag and, with nothing after it, is no amount at all (a 0 is
        # a broken-offer shape here; the refresh reports it `unavailable`).
        if amount:
            return outcome
        if first_refusal is None:
            first_refusal = dict(outcome, amount=None)
    return first_refusal or {"amount": None, "source": None, "raw": None, "status": "empty", "decimal_hint": None}


def _extract_from_html(base_url: str, html: str) -> Dict[str, Any]:
    p = _MetaParser()
    p.feed(html)

    def meta(*keys: str) -> Optional[str]:
        for k in keys:
            v = p.meta.get(("meta", k.lower()))
            if v and v.strip():
                return v.strip()
        return None

    canonical = p.links.get("canonical") or meta("property:og:url") or base_url
    title = meta("property:og:title", "name:twitter:title") or (p.title.strip() if p.title else None)
    image = meta("property:og:image", "property:og:image:secure_url", "name:twitter:image")
    description = meta("property:og:description", "name:description", "name:twitter:description")
    detailed_description = _extract_long_description_from_html(html)

    price = meta("property:product:price:amount", "property:og:price:amount", "property:product:price") or None
    currency = read_currency_code(meta("property:product:price:currency", "property:og:price:currency"))
    signals = _page_price_signals(p, html)

    parsed_jsonld = _parse_jsonld_texts(p.jsonld)
    jsonld = _extract_jsonld_offer(parsed_jsonld)
    jsonld_image_urls = _extract_jsonld_image_urls(parsed_jsonld, canonical)
    meta_image_urls = _extract_meta_image_urls(p, canonical)
    data_attr_image_urls = _extract_image_urls_from_data_attrs(html, canonical)
    dom_image_urls = _extract_dom_image_urls(p, canonical)
    variants_jsonld, jsonld_census = _extract_jsonld_variants_with_census(
        parsed_jsonld, signals, page_urls=(canonical, base_url)
    )
    fallback_currency = jsonld.get("currency") or currency
    variants_data_attr = _extract_variants_from_data_attrs(html, fallback_currency, canonical, signals)
    price_read = _read_product_price(jsonld, price, currency, signals)

    variants: list[Dict[str, Any]] = []
    if variants_jsonld and variants_data_attr:
        # Prefer JSON-LD prices/availability but use data-attr titles when they are better (e.g. size labels).
        title_map: Dict[str, str] = {}
        for v in variants_data_attr:
            vid = str(v.get("variant_id") or "").strip()
            t = _normalize_variant_title(v.get("title"))
            if vid and t:
                title_map[vid] = t
        merged: list[Dict[str, Any]] = []
        for v in variants_jsonld:
            if not isinstance(v, dict):
                continue
            vid = str(v.get("variant_id") or "").strip()
            incoming_title = title_map.get(vid)
            if incoming_title and _variant_title_score(title=incoming_title, product_name=jsonld.get("title") or title) > _variant_title_score(
                title=v.get("title"), product_name=jsonld.get("title") or title
            ):
                vv = dict(v)
                vv["title"] = incoming_title
                merged.append(vv)
            else:
                merged.append(v)
        variants = merged
        # If JSON-LD variant titles are weak and data-attr variants are strong, prefer the data-attr list.
        if _best_variant_title_score(variants_jsonld, jsonld.get("title") or title) < _best_variant_title_score(
            variants_data_attr, jsonld.get("title") or title
        ):
            variants = variants_data_attr
    elif variants_jsonld:
        variants = variants_jsonld
    elif variants_data_attr:
        variants = variants_data_attr

    def build_gallery(raws: list[Any]) -> list[str]:
        """Absolute, de-duplicated (one entry per image, its largest rendition), capped, with
        swatch-sized assets dropped when anything else is left."""
        urls: list[str] = []
        idx_by_key: dict[str, int] = {}
        score_by_key: dict[str, float] = {}
        for raw in raws:
            if not raw:
                continue
            u = str(raw).strip()
            if not u:
                continue
            if u.startswith("//"):
                scheme = urlparse(canonical).scheme or urlparse(base_url).scheme or "https"
                u = f"{scheme}:{u}"
            if not u.startswith(("http://", "https://")):
                u = urljoin(canonical or base_url, u)
            if not u.startswith(("http://", "https://")):
                continue
            key = _image_dedupe_key(u) or u
            score = _image_resolution_score(u)
            if key in idx_by_key:
                if score > score_by_key.get(key, -1.0):
                    urls[idx_by_key[key]] = u
                    score_by_key[key] = score
                continue
            if len(urls) >= MAX_IMAGES:
                continue
            idx_by_key[key] = len(urls)
            score_by_key[key] = score
            urls.append(u)
        filtered = [u for u in urls if not _looks_like_variant_label_image(u)]
        return filtered[:MAX_IMAGES] if filtered else urls

    # THE GALLERY IS WHAT THE PAGE DECLARES AS THE PRODUCT'S IMAGES, NOT EVERY IMAGE IT RENDERS.
    # JSON-LD `image`, og/twitter image tags and the SKU data payload name the product. A page's
    # <img>/srcset/preload tags also render the nav menu, promo banners, "you may also like" rows,
    # an Instagram strip and the announcement bar, and nothing in the markup says which is which.
    # Live 2026-10-14: us.laneige.com Lip Sleeping Mask served an Advent calendar, other masks,
    # nav tiles and a duffle bag; cocomo.sg's iUNIK sunscreen served a 1983x133 store banner and
    # 14 Instagram posts. The gateway's relevance filter can't tell them apart either, and when
    # nothing passes it serves everything. So the page-rendered set is reported as
    # `page_image_urls` (audit only) and never becomes `image_urls`.
    image_urls = build_gallery(
        [(jsonld.get("image_url") or "").strip()] + jsonld_image_urls + meta_image_urls + data_attr_image_urls
    )
    page_image_urls = build_gallery(dom_image_urls)
    description_text, description_source = _prefer_detailed_description_with_source(
        primary=jsonld.get("description"),
        detailed=detailed_description,
        fallback=description,
    )

    # JSON-LD is preferred when it provides structured offers.
    out = {
        "canonical_url": canonical,
        "title": jsonld.get("title") or title,
        "description": description_text,
        # "jsonld" | "page_long" | "meta" | None. A "meta" description is the page's
        # <meta name=description> / og:description: SEO copy written to sell the click ("Buy now
        # for a visibly plumper pout!"), not the product's description. Consumers that store copy
        # decide on this, not on the text.
        "description_source": description_source,
        # The page's own meta/og description, whatever was chosen above: a seed that already
        # stores this exact SEO line can then be told apart from real copy and upgraded.
        "meta_description": _normalize_description_text(description),
        "brand": jsonld.get("brand") or None,
        "image_url": (image_urls[0] if image_urls else None) or jsonld.get("image_url") or image,
        "image_urls": image_urls,
        # Every image the page renders (see `image_urls` above). Audit only; never a gallery.
        "page_image_urls": page_image_urls,
        "price_amount": price_read["amount"],
        "price_currency": fallback_currency,
        # How the product price was read, so a refusal can be counted and a stored amount traced
        # back to the text it came from. See `_read_product_price`.
        "price_read": {k: v for k, v in price_read.items() if k != "amount"},
        "availability": _availability_from_raw(jsonld.get("availability_raw")),
        "evidence_provider": "jsonld"
        if _has_price_raw(jsonld.get("price_raw")) or jsonld.get("title")
        else ("data_attr" if variants else ("og" if title or image else "manual")),
        "variants": variants,
        # What the variant list alone cannot say (see `_extract_jsonld_variants_with_census`).
        # `data_attr_skus` counts the data-attribute payload, the other variant source.
        "variant_census": {
            **jsonld_census,
            "data_attr_skus": len(variants_data_attr),
            "data_attr_exact_prices": sorted(
                {round(v["price_amount"], 2) for v in variants_data_attr if v.get("price_amount") is not None}
            ),
            "data_attr_availabilities": sorted(
                {str(v.get("availability")) for v in variants_data_attr if str(v.get("availability") or "unknown") != "unknown"}
            ),
            "data_attr_duplicate_ids": sum(1 for v in variants_data_attr if v.get("id_collided")),
            # The product-level amount above: JSON-LD's own when it has one (exact unless it is an
            # AggregateOffer bound), else a meta-tag `product:price:amount`, which names one price.
            "product_price_exact": bool(jsonld.get("price_exact")) if price_read.get("source") == "jsonld" else True,
        },
    }
    return out


# Marks a snapshot whose `image_urls` holds only images the page declares as the product's
# (see `_extract_from_html`). Snapshots written before it carry every image the page rendered,
# so a consumer that stores a gallery must not take `image_urls` from evidence without it.
IMAGE_URLS_SCOPE_DECLARED = "declared"


def evidence_content_fields(extracted: Dict[str, Any]) -> Dict[str, Any]:
    """The description and gallery a snapshot's evidence stores, from one `_extract_from_html`,
    with where each came from. One function so the refresh's tests build evidence exactly as
    `resolve_external_offer` does."""
    out: Dict[str, Any] = {"image_urls_scope": IMAGE_URLS_SCOPE_DECLARED}
    description = extracted.get("description")
    if isinstance(description, str) and description.strip():
        out["description"] = description.strip()
        if extracted.get("description_source"):
            out["description_source"] = extracted["description_source"]
    meta_description = extracted.get("meta_description")
    if isinstance(meta_description, str) and meta_description.strip():
        out["meta_description"] = meta_description.strip()
    image_urls = extracted.get("image_urls") or []
    if isinstance(image_urls, list):
        cleaned = [str(u).strip() for u in image_urls if isinstance(u, str) and str(u).strip()]
        if cleaned:
            out["image_urls"] = cleaned[:MAX_IMAGES]
    page_images = extracted.get("page_image_urls")
    if isinstance(page_images, list):
        out["page_image_count"] = len(page_images)
    return out


def snapshot_price_fields(extracted: Dict[str, Any]) -> Tuple[Optional[float], Optional[str], Dict[str, Any]]:
    """(amount, currency, price_read) as a snapshot stores them, from one `_extract_from_html`.

    A PRICE IS A PAIR, AND AN UNREAD CURRENCY IS NOT THE MARKET'S. `resolve_external_offer` used
    to write `"JPY" if market == "JP" else "USD"` when the page stated no currency, so a ₩24,000
    or 28,80 € page was stored as 24000 USD / 28.80 USD, and the seed refresh applied it whenever
    the stored currency was USD too. An amount whose currency was not read is now not stored at
    all; `price_read["status"]` says `currency_unread` and the refresh counts it. One function so
    the refresh's tests build snapshots exactly as production does.
    """
    currency = (extracted.get("price_currency") or "").strip().upper() or None
    amount = extracted.get("price_amount")
    price_read = dict(extracted.get("price_read") or {})
    if amount is not None and currency is None:
        price_read["status"] = "currency_unread"
        amount = None
    return amount, currency, price_read


def evidence_variant_fields(extracted: Dict[str, Any]) -> Dict[str, Any]:
    """The variant part of a snapshot's `evidence`, from one `_extract_from_html` result.

    One function so the seed refresh's tests can build evidence exactly as production does.
    """
    out: Dict[str, Any] = {}
    variants = extracted.get("variants") or []
    if variants:
        out["variants"] = variants[:MAX_VARIANTS]
    if isinstance(extracted.get("variant_census"), dict):
        out["variant_census"] = extracted["variant_census"]
    return out


@dataclass(frozen=True)
class ExternalOfferSnapshot:
    snapshot_id: str
    market: str
    canonical_url: str
    domain: str
    title: Optional[str]
    brand: Optional[str]
    image_url: Optional[str]
    price_amount: Optional[float]
    price_currency: Optional[str]
    availability: str
    last_checked_at: Optional[datetime]
    evidence: Optional[Dict[str, Any]]

    # Manual override fields (phase 2)
    override_title: Optional[str] = None
    override_brand: Optional[str] = None
    override_image_url: Optional[str] = None
    override_price_amount: Optional[float] = None
    override_price_currency: Optional[str] = None

    def to_public(self) -> Dict[str, Any]:
        title = self.override_title or self.title
        brand = self.override_brand or self.brand
        image_url = self.override_image_url or self.image_url
        price_amount = self.override_price_amount if self.override_price_amount is not None else self.price_amount
        price_currency = self.override_price_currency or self.price_currency
        return {
            "snapshotId": self.snapshot_id,
            "market": self.market,
            "canonicalUrl": self.canonical_url,
            "domain": self.domain,
            "title": title,
            "brand": brand,
            "imageUrl": image_url,
            "price": {"amount": float(price_amount), "currency": price_currency} if price_amount is not None and price_currency else None,
            "availability": self.availability,
            "lastCheckedAt": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "evidence": self.evidence,
        }


async def _get_snapshot_row(market: str, url_hash: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        select(external_offer_snapshots).where(
            and_(
                external_offer_snapshots.c.market == market,
                external_offer_snapshots.c.url_hash == url_hash,
            )
        )
    )
    return dict(row) if row else None


def _row_to_snapshot(row: Dict[str, Any]) -> ExternalOfferSnapshot:
    return ExternalOfferSnapshot(
        snapshot_id=row["id"],
        market=row["market"],
        canonical_url=row["canonical_url"],
        domain=row["domain"],
        title=row.get("title"),
        brand=row.get("brand"),
        image_url=row.get("image_url"),
        price_amount=float(row["price_amount"]) if row.get("price_amount") is not None else None,
        price_currency=row.get("price_currency"),
        availability=row.get("availability") or "unknown",
        last_checked_at=_as_aware_utc(row.get("last_checked_at")),
        evidence=row.get("evidence"),
        override_title=row.get("override_title"),
        override_brand=row.get("override_brand"),
        override_image_url=row.get("override_image_url"),
        override_price_amount=float(row["override_price_amount"]) if row.get("override_price_amount") is not None else None,
        override_price_currency=row.get("override_price_currency"),
    )


def _is_stale(last_checked_at: Optional[datetime]) -> bool:
    last_checked_at = _as_aware_utc(last_checked_at)
    if not last_checked_at:
        return True
    # Some deployments return naive datetimes from the DB; normalize to UTC
    # so comparisons against aware timestamps do not raise.
    if last_checked_at.tzinfo is None:
        last_checked_at = last_checked_at.replace(tzinfo=timezone.utc)
    return last_checked_at < (_now() - timedelta(days=MAX_AGE_DAYS))


async def resolve_external_offer(
    *,
    market: str,
    url: str,
    force_refresh: bool = False,
    raise_on_unavailable: bool = False,
    observed: Optional[Dict[str, Any]] = None,
    max_wait: Optional[float] = None,
) -> ExternalOfferSnapshot:
    """Resolve (and opportunistically refresh) the offer snapshot for a third-party URL.

    `raise_on_unavailable` DEFAULTS TO FALSE, which is today's behaviour exactly: a failed
    fetch silently returns the cached snapshot. That fallback is why a dead destination could
    masquerade as a healthy one — the seed refresh got a snapshot back, wrote it, and bumped
    `updated_at`, so a 404 actually made the row look FRESHER. A caller that is asking about
    the LINK rather than the CONTENT passes True and gets the `ExternalOfferUnavailable`.

    `observed` is an out-parameter forwarded to `_fetch_html`; see its note.
    """
    market_norm = str(market or "US").upper()
    url_norm = _normalize_url(url)
    url_hash = _url_hash(url_norm)

    existing = await _get_snapshot_row(market_norm, url_hash)
    if existing and not force_refresh and not _is_stale(existing.get("last_checked_at")):
        return _row_to_snapshot(existing)

    # Try to refresh (best-effort). If refresh fails, return existing cached value.
    try:
        # `max_wait` IS THE CALLER'S PATIENCE, and this function had no way to express it.
        # `_fetch_html` documents the rule: the default ceiling exists for the LIVE route, and
        # a BATCH job must pass 0 (unbounded) or the backoff curve above ~16s becomes
        # unreachable -- `await_slot` refuses instead of waiting, the refusal is swallowed as
        # a generic failure, and one throttled host voids the rest of its own rows in
        # milliseconds while looking like the host was down. The sibling destination sweep
        # passes 0 explicitly; until now the content refresh could not, because there was no
        # parameter between it and here. Default None keeps the live route exactly as it was.
        html, _ct = await _fetch_html(url_norm, observed=observed, max_wait=max_wait)
        extracted = _extract_from_html(url_norm, html)

        canonical_url = _normalize_url(extracted.get("canonical_url") or url_norm)
        domain = _domain(canonical_url) or _domain(url_norm)

        amount, currency, price_read = snapshot_price_fields(extracted)

        rid = f"eo_{url_hash[:24]}"
        now = _now()
        now_db = _now_db()
        evidence = {
            "provider": extracted.get("evidence_provider") or "manual",
            "fetchedAt": now.isoformat(),
            "snapshotId": (existing or {}).get("id") or rid,
            # Where the currency came from, as the seed refresh's canonical-offer projection
            # (#2416) reads it: it projects only "page". There is no `market_default` any more:
            # an unread currency voids the amount (`snapshot_price_fields`).
            "price_currency_source": "page" if currency else "unread",
        }
        evidence.update(evidence_content_fields(extracted))
        evidence.update(evidence_variant_fields(extracted))
        if price_read:
            evidence["price_read"] = price_read
        if existing:
            stmt = (
                update(external_offer_snapshots)
                .where(and_(external_offer_snapshots.c.market == market_norm, external_offer_snapshots.c.url_hash == url_hash))
                .values(
                    canonical_url=canonical_url,
                    domain=domain,
                    title=extracted.get("title"),
                    brand=extracted.get("brand"),
                    image_url=extracted.get("image_url"),
                    price_amount=amount,
                    price_currency=currency,
                    availability=extracted.get("availability") or "unknown",
                    evidence=evidence,
                    last_checked_at=now_db,
                    updated_at=now_db,
                )
            )
            await database.execute(stmt)
        else:
            await database.execute(
                external_offer_snapshots.insert(),
                {
                    "id": rid,
                    "market": market_norm,
                    "canonical_url": canonical_url,
                    "url_hash": url_hash,
                    "domain": domain,
                    "title": extracted.get("title"),
                    "brand": extracted.get("brand"),
                    "image_url": extracted.get("image_url"),
                    "price_amount": amount,
                    "price_currency": currency,
                    "availability": extracted.get("availability") or "unknown",
                    "evidence": evidence,
                    "last_checked_at": now_db,
                },
            )

        refreshed_row = await _get_snapshot_row(market_norm, url_hash)
        if refreshed_row:
            return _row_to_snapshot(refreshed_row)
    except crawl_global_pacer.GlobalPaceStopped:
        # NOTHING WAS ASKED (review M1, 2026-10-17). The run's pacer refused the request -- the IP
        # breaker's final trip, or the budget ending -- so there is no answer to fall back from.
        # Handing back the cached snapshot here let `_refresh_external_seed_by_id` write it as a
        # success and stamp `last_crawl_attempt_at`, sending a row that was never read to the back
        # of tomorrow's queue. It propagates; the batch counts the row as deferred, unstamped.
        raise
    except ExternalOfferUnavailable:
        # The origin ANSWERED and said no. Serving still prefers the cached snapshot (that is
        # what keeps a transient outage from blanking the catalogue), but a caller that asked
        # to hear about it must not be handed a stale row as if the fetch had worked.
        if raise_on_unavailable:
            raise
        if existing:
            _mark_from_cache(observed)
            return _row_to_snapshot(existing)
        raise
    except Exception:
        if existing:
            # A CACHED ROW IS NOT A READING, AND `observed` CANNOT SHOW THAT ON ITS OWN.
            # `_fetch_html` stamps `observed["status_code"]` BEFORE it returns, so every
            # failure AFTER the response — the extractor, the snapshot upsert, the post-write
            # re-read — lands here with `observed` already populated and the CACHED snapshot
            # going back to the caller. A caller checking only `status_code` would conclude it
            # had just re-read a page it never parsed. Say so explicitly instead.
            _mark_from_cache(observed)
            return _row_to_snapshot(existing)
        raise

    # Fallback: should never happen, but keep function total. Same reasoning as above: the
    # post-write re-read returning None reaches here with a populated `observed`.
    if existing:
        _mark_from_cache(observed)
        return _row_to_snapshot(existing)
    raise ValueError("FETCH_FAILED")
