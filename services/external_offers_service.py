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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "meta":
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

def _parse_price(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    # Keep digits and dot.
    cleaned = re.sub(r"[^0-9.]", "", value)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except Exception:
        return None


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


def _offer_price_and_currency(offer: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
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

    return (str(price).strip() if price is not None else None, str(currency).strip().upper() if currency else None)


def _offer_variants_from_node(offers: Any, product_name: Optional[str]) -> list[Dict[str, Any]]:
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

    variants: list[Dict[str, Any]] = []
    for idx, offer in enumerate(offer_list):
        if not isinstance(offer, dict):
            continue
        item = offer.get("itemOffered")
        if isinstance(item, list) and item:
            item = item[0]
        if not isinstance(item, dict):
            item = {}

        variant_id = (
            offer.get("sku")
            or offer.get("skuId")
            or offer.get("productID")
            or offer.get("mpn")
            or offer.get("gtin13")
            or item.get("sku")
            or item.get("skuId")
            or item.get("productID")
            or item.get("mpn")
            or item.get("gtin13")
            or offer.get("@id")
            or item.get("@id")
            or f"offer_{idx + 1}"
        )

        title = offer.get("name") or item.get("name")
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
        availability_raw = offer.get("availability") or item.get("availability")
        availability = _availability_from_raw(str(availability_raw)) if availability_raw else "unknown"

        variants.append(
            {
                "variant_id": str(variant_id),
                "title": str(title).strip() if title else None,
                "price_amount": _parse_price(price_raw) if price_raw else None,
                "price_currency": currency,
                "availability": availability,
            }
        )

        if len(variants) >= MAX_VARIANTS:
            break

    return variants


def _detect_currency_from_text(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("$"):
        return "USD"
    if text.startswith("€"):
        return "EUR"
    if text.startswith("£"):
        return "GBP"
    if text.startswith("¥"):
        return "JPY"
    # Fallback: match currency codes in text.
    m = re.search(r"\b([A-Z]{3})\b", text)
    return m.group(1) if m else None


def _extract_variants_from_data_attrs(html: str, fallback_currency: Optional[str], base_url: str) -> list[Dict[str, Any]]:
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
        if not variant_id or variant_id in seen:
            continue

        title = _extract_size_label_from_sku(sku) or sku.get("size") or sku.get("name") or sku.get("title")
        price_amount = sku.get("price_with_discount") or sku.get("price")
        if price_amount is None:
            price_amount = sku.get("price_with_discount_with_currency_code") or sku.get("price_with_currency_code")
        currency = (
            sku.get("price_currency")
            or sku.get("priceCurrency")
            or _detect_currency_from_text(sku.get("price_with_discount_with_currency_code"))
            or _detect_currency_from_text(sku.get("price_with_currency_code"))
            or fallback_currency
        )

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
                "price_amount": _parse_price(str(price_amount)) if price_amount is not None else None,
                "price_currency": str(currency).strip().upper() if currency else None,
                "availability": availability,
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


def _extract_jsonld_variants(parsed_objs: list[Any]) -> list[Dict[str, Any]]:
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
                variants += _offer_variants_from_node(node.get("offers"), product_name)
            elif "offer" in tset:
                variants += _offer_variants_from_node(node, product_name)

            if len(variants) >= MAX_VARIANTS:
                break
        if len(variants) >= MAX_VARIANTS:
            break

    normalized: list[Dict[str, Any]] = []
    for v in variants:
        vid = str(v.get("variant_id") or "").strip()
        if not vid or vid in seen:
            continue
        seen.add(vid)
        normalized.append(v)
        if len(normalized) >= MAX_VARIANTS:
            break

    return normalized


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


def _extract_jsonld_offer(parsed_objs: list[Any]) -> Dict[str, Any]:
    best: Dict[str, Any] = {}
    for root in parsed_objs:
        for node in _iter_jsonld_nodes(root):
            t = node.get("@type") if isinstance(node, dict) else None
            if isinstance(t, list):
                tset = {str(x).lower() for x in t}
            else:
                tset = {str(t).lower()} if t else set()
            if "product" not in tset and "offer" not in tset:
                continue

            name = node.get("name") if isinstance(node, dict) else None
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
            if isinstance(offers, list) and offers:
                offers = offers[0]
            price = None
            currency = None
            availability = None
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
                currency = offers.get("priceCurrency")
                availability = offers.get("availability")

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
                    "price_raw": str(price).strip() if price is not None else None,
                    "currency": str(currency).strip().upper() if currency else None,
                    "availability_raw": str(availability).strip() if availability else None,
                    # Optional review signal off the winning Product node — null when
                    # the page carries no aggregateRating (never fabricated).
                    "rating_value": rating_value,
                    "rating_count": rating_count,
                }

    best.pop("_score", None)
    return best


def _availability_from_raw(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    v = raw.lower()
    if "instock" in v or "in_stock" in v or "in stock" in v:
        return "in_stock"
    if "outofstock" in v or "out_of_stock" in v or "out of stock" in v:
        return "out_of_stock"
    return "unknown"


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


def _prefer_detailed_description(*, primary: Optional[str], detailed: Optional[str], fallback: Optional[str]) -> Optional[str]:
    structured = _normalize_description_text(primary)
    detailed_text = _normalize_description_text(detailed)
    fallback_text = _normalize_description_text(fallback)

    if detailed_text:
        if not structured:
            return detailed_text
        structured_lower = structured.lower()
        detailed_lower = detailed_text.lower()
        starts_with_structured = detailed_lower.startswith(structured_lower)
        materially_longer = len(detailed_text) >= max(len(structured) + 60, round(len(structured) * 1.35))
        looks_like_expanded = bool(re.search(r"\bthis set includes\b|\bproduct details\b|\n|•|\bto use\b", detailed_text, re.IGNORECASE))
        if starts_with_structured or (materially_longer and looks_like_expanded):
            return detailed_text

    return structured or fallback_text


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


async def _fetch_html(url: str) -> Tuple[str, str]:
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers={"User-Agent": DEFAULT_UA}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        # Read up to MAX_BODY_BYTES to keep latency predictable.
        body = resp.content[:MAX_BODY_BYTES]
        text = body.decode(resp.encoding or "utf-8", errors="replace")
        return text, content_type


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
    currency = meta("property:product:price:currency", "property:og:price:currency") or None

    parsed_jsonld = _parse_jsonld_texts(p.jsonld)
    jsonld = _extract_jsonld_offer(parsed_jsonld)
    jsonld_image_urls = _extract_jsonld_image_urls(parsed_jsonld, canonical)
    meta_image_urls = _extract_meta_image_urls(p, canonical)
    data_attr_image_urls = _extract_image_urls_from_data_attrs(html, canonical)
    dom_image_urls = _extract_dom_image_urls(p, canonical)
    variants_jsonld = _extract_jsonld_variants(parsed_jsonld)
    fallback_currency = (jsonld.get("currency") or currency or "").strip().upper() or None
    variants_data_attr = _extract_variants_from_data_attrs(html, fallback_currency, canonical)

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

    image_urls: list[str] = []
    idx_by_key: dict[str, int] = {}
    score_by_key: dict[str, float] = {}

    def add_image(raw: Any) -> None:
        if not raw:
            return
        u = str(raw).strip()
        if not u:
            return
        if u.startswith("//"):
            scheme = urlparse(canonical).scheme or urlparse(base_url).scheme or "https"
            u = f"{scheme}:{u}"
        if not u.startswith(("http://", "https://")):
            u = urljoin(canonical or base_url, u)
        if not u.startswith(("http://", "https://")):
            return
        key = _image_dedupe_key(u) or u
        score = _image_resolution_score(u)
        if key in idx_by_key:
            if score > score_by_key.get(key, -1.0):
                image_urls[idx_by_key[key]] = u
                score_by_key[key] = score
            return
        if len(image_urls) >= MAX_IMAGES:
            return
        idx_by_key[key] = len(image_urls)
        score_by_key[key] = score
        image_urls.append(u)

    add_image((jsonld.get("image_url") or "").strip())
    for url in jsonld_image_urls + meta_image_urls + data_attr_image_urls + dom_image_urls:
        add_image(url)

    # Filter tiny swatch/thumbnail assets when we have other images.
    filtered = [u for u in image_urls if not _looks_like_variant_label_image(u)]
    if filtered:
        image_urls = filtered[:MAX_IMAGES]

    # JSON-LD is preferred when it provides structured offers.
    out = {
        "canonical_url": canonical,
        "title": jsonld.get("title") or title,
        "description": _prefer_detailed_description(
            primary=jsonld.get("description"),
            detailed=detailed_description,
            fallback=description,
        ),
        "brand": jsonld.get("brand") or None,
        "image_url": (image_urls[0] if image_urls else None) or jsonld.get("image_url") or image,
        "image_urls": image_urls,
        "price_amount": _parse_price(jsonld.get("price_raw")) or _parse_price(price),
        "price_currency": (jsonld.get("currency") or currency or "").strip().upper() or None,
        "availability": _availability_from_raw(jsonld.get("availability_raw")),
        "evidence_provider": "jsonld"
        if jsonld.get("price_raw") or jsonld.get("title")
        else ("data_attr" if variants else ("og" if title or image else "manual")),
        "variants": variants,
    }
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


async def resolve_external_offer(*, market: str, url: str, force_refresh: bool = False) -> ExternalOfferSnapshot:
    market_norm = str(market or "US").upper()
    url_norm = _normalize_url(url)
    url_hash = _url_hash(url_norm)

    existing = await _get_snapshot_row(market_norm, url_hash)
    if existing and not force_refresh and not _is_stale(existing.get("last_checked_at")):
        return _row_to_snapshot(existing)

    # Try to refresh (best-effort). If refresh fails, return existing cached value.
    try:
        html, _ct = await _fetch_html(url_norm)
        extracted = _extract_from_html(url_norm, html)

        canonical_url = _normalize_url(extracted.get("canonical_url") or url_norm)
        domain = _domain(canonical_url) or _domain(url_norm)

        currency = (extracted.get("price_currency") or "").strip().upper() or None
        amount = extracted.get("price_amount")
        if currency is None:
            currency = "JPY" if market_norm == "JP" else "USD"

        rid = f"eo_{url_hash[:24]}"
        now = _now()
        now_db = _now_db()
        evidence = {
            "provider": extracted.get("evidence_provider") or "manual",
            "fetchedAt": now.isoformat(),
            "snapshotId": (existing or {}).get("id") or rid,
        }
        description = extracted.get("description")
        if isinstance(description, str) and description.strip():
            evidence["description"] = description.strip()
        variants = extracted.get("variants") or []
        if variants:
            evidence["variants"] = variants[:MAX_VARIANTS]
        image_urls = extracted.get("image_urls") or []
        if isinstance(image_urls, list):
            cleaned = [str(u).strip() for u in image_urls if isinstance(u, str) and str(u).strip()]
            if cleaned:
                evidence["image_urls"] = cleaned[:MAX_IMAGES]
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
    except Exception:
        if existing:
            return _row_to_snapshot(existing)
        raise

    # Fallback: should never happen, but keep function total.
    if existing:
        return _row_to_snapshot(existing)
    raise ValueError("FETCH_FAILED")
