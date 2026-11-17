"""Read storefront identity and US Shopify quote evidence without writing to the store."""

import json
from html.parser import HTMLParser
from urllib.parse import urlsplit


AJAX_LIMIT = 2_000_000
HTML_LIMIT = 3_000_000
JSONLD_LIMIT = 300_000
GTIN_KEYS = ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "barcode")


def _gtin(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) not in (8, 12, 13, 14) or not value.isascii() or not value.isdigit() or set(value) == {"0"}:
        return None
    total = sum(int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(reversed(value[:-1])))
    return value.zfill(14) if (total + int(value[-1])) % 10 == 0 else None


def _string(value):
    return value.strip()[:300] or None if isinstance(value, str) else None


def _native_id(value):
    if type(value) is int:
        value = str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit() and 0 < len(value) <= 24 and value[0] != "0":
        return value
    return None


def _values(headers, name):
    if hasattr(headers, "get_all"):
        return headers.get_all(name, ())
    result = []
    for key, value in headers.items():
        if key.lower() == name:
            result.extend(value if isinstance(value, (list, tuple)) else [value])
    return result


def _held(reason):
    return {"status": "held", "reason": reason}


def _shopify(parts, fetch):
    path = parts.path.rstrip("/")
    if len(path.split("/")) < 3 or not path.split("/")[-1] or path.split("/")[-2] != "products":
        return _held("unsupported_shopify_product_path")
    if not path.endswith(".js"):
        path += ".js"
    source = parts._replace(path=path, query="country=US", fragment="").geturl()
    headers, raw = fetch(source, AJAX_LIMIT)
    currencies = {value.partition(";")[0].strip().partition("=")[2].strip('"')
                  for value in _values(headers, "set-cookie")
                  if value.strip().startswith("cart_currency=")}
    if currencies != {"USD"}:
        return _held("missing_or_conflicting_usd_presentment_cookie")
    product = json.loads(raw)
    if not isinstance(product, dict) or not _native_id(product.get("id")) or not isinstance(product.get("variants"), list):
        return _held("invalid_shopify_product")
    variants = []
    seen = set()
    missing_gtin = 0
    for row in product["variants"]:
        if not isinstance(row, dict):
            continue
        variant_id = _native_id(row.get("id"))
        if not variant_id or variant_id in seen:
            return _held("invalid_or_duplicate_shopify_variant_id")
        seen.add(variant_id)
        barcodes = {_gtin(row[key]) for key in GTIN_KEYS if key in row} - {None}
        gtin = next(iter(barcodes)) if len(barcodes) == 1 else None
        if not gtin:
            missing_gtin += 1
        price = row.get("price")
        available = row.get("available")
        if type(price) is not int or price <= 0 or type(available) is not bool:
            continue
        variants.append({"variant_id": variant_id, "gtin": gtin, "price_minor": price, "in_stock": available,
                         "title": _string(row.get("title")), "sku": _string(row.get("sku"))})
    if not variants:
        return _held("no_explicit_shopify_price_and_availability")
    return {"status": "shopify_us_quote_observed", "source_url": source, "currency": "USD",
            "currency_proof": "product_cart_currency_cookie", "product_id": _native_id(product["id"]),
            "title": _string(product.get("title")), "vendor": _string(product.get("vendor")),
            "variants": variants, "source_variant_count": len(product["variants"]),
            "missing_gtin_count": missing_gtin,
            "invalid_quote_count": len(product["variants"]) - len(variants)}


def _products(data, depth=0):
    if depth > 8:
        return
    if isinstance(data, list):
        for entry in data[:200]:
            yield from _products(entry, depth + 1)
    elif isinstance(data, dict):
        types = data.get("@type", ())
        types = [types] if isinstance(types, str) else types
        if isinstance(types, (list, tuple)) and any(
            isinstance(value, str) and value.rsplit("/", 1)[-1] in {"Product", "ProductGroup"} for value in types
        ):
            yield data
        else:
            yield from _products(data.get("@graph"), depth + 1)


class _ProductMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reading = False
        self.product = None
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        script_type = dict(attrs).get("type") if tag == "script" else None
        self.reading = (not self.product and self.scripts < 40 and isinstance(script_type, str)
                        and script_type.split(";", 1)[0].strip().lower() == "application/ld+json")

    def handle_data(self, data):
        if not self.reading:
            return
        self.reading = False
        self.scripts += 1
        if len(data) > JSONLD_LIMIT:
            return
        try:
            self.product = next(_products(json.loads(data)), None)
        except (ValueError, RecursionError):
            return

    def handle_endtag(self, tag):
        if tag == "script":
            self.reading = False


def _rating_fields(data):
    if not isinstance(data, dict):
        return None
    result = {}
    for field in ("ratingValue", "ratingCount", "reviewCount", "bestRating", "worstRating"):
        value = data.get(field)
        if type(value) in (int, float) or isinstance(value, str):
            try:
                number = float(value)
            except (ValueError, OverflowError):
                continue
            if not 0 <= number <= 1_000_000_000:
                continue
            if field in {"ratingCount", "reviewCount"}:
                if number.is_integer():
                    result[field] = int(number)
            else:
                result[field] = number
    return result or None


def _public(parts, fetch):
    source = parts._replace(fragment="").geturl()
    _, raw = fetch(source, HTML_LIMIT)
    parser = _ProductMarkup()
    parser.feed(raw.decode("utf-8", errors="replace"))
    product = parser.product
    if not product:
        return _held("no_bounded_product_jsonld")
    offers = product.get("offers", [])
    offers = [offers] if isinstance(offers, dict) else offers
    currencies = set()
    if isinstance(offers, list):
        for offer in offers[:100]:
            currency = offer.get("priceCurrency") if isinstance(offer, dict) else None
            if isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isalpha():
                currencies.add(currency.upper())
    gtins = sorted({_gtin(product[key]) for key in GTIN_KEYS if key in product} - {None})
    return {"status": "public_product_metadata_observed", "source_url": source,
            "title": _string(product.get("name")), "sku": _string(product.get("sku")),
            "gtin": gtins[0] if len(gtins) == 1 else None, "explicit_gtins": gtins,
            "aggregate_rating": _rating_fields(product.get("aggregateRating")),
            "explicit_offer_currencies": sorted(currencies)}


def capture(item, fetch):
    """Return one read-only evidence snapshot; runtime owns host and redirect policy."""
    try:
        parts = urlsplit(item["url"])
        if parts.scheme != "https" or not parts.hostname or parts.netloc != parts.hostname or not parts.path:
            return _held("invalid_source_url")
        kind = item.get("source_kind")
        if kind == "shopify_ajax":
            return _shopify(parts, fetch)
        if kind == "public_pdp":
            return _public(parts, fetch)
        return _held("unsupported_source_kind")
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        return _held("unreadable_storefront_payload")
