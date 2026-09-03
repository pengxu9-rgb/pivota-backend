"""Pivota wire records to the blueprint's models.

Every reader here is tolerant: the gateway's records carry more fields than any
one consumer needs, and a field it lacks is ``None`` or a sensible default, never
a crash. Nothing here invents a figure: a price the record does not carry stays
absent, and a product with no stock signal is offered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from shopping_agent.types import (
    Disclosure,
    DisclosureRow,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductDetails,
)

from .ids import ProductRef, encode_product_id

_IN_STOCK_WORDS = {"in_stock", "instock", "available", "in stock", "limited"}
_OUT_OF_STOCK_WORDS = {"out_of_stock", "outofstock", "sold_out", "soldout", "unavailable", "out of stock"}


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    if isinstance(value, dict):
        # {"amount": 600, "currency": "USD"} in minor units, or {"value": 6.0}
        if "value" in value:
            return _float(value.get("value"))
        amount = _float(value.get("amount"))
        if amount is not None and isinstance(value.get("amount"), int):
            return amount / 100
        return amount
    return None


def in_stock_of(record: dict[str, Any]) -> bool:
    """True unless the record says otherwise. ``in_stock`` wins, then the
    availability word, then a non-positive inventory count."""
    flag = record.get("in_stock")
    if isinstance(flag, bool):
        return flag
    word = _str(record.get("availability"))
    if word:
        lowered = word.lower()
        if lowered in _OUT_OF_STOCK_WORDS:
            return False
        if lowered in _IN_STOCK_WORDS:
            return True
    quantity = record.get("inventory_quantity")
    if isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
        return quantity > 0
    return True


def _image(record: dict[str, Any]) -> str | None:
    image = _str(record.get("image_url"))
    if image:
        return image
    for key in ("images", "image_urls"):
        images = record.get(key)
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict):
                first = first.get("url")
            return _str(first)
    return None


def _category(record: dict[str, Any]) -> str | None:
    path = record.get("catalog_category_path")
    if isinstance(path, str) and path:
        return path
    parts = record.get("category_path")
    if isinstance(parts, list) and parts:
        return "/".join(str(p) for p in parts if p)
    return _str(record.get("category")) or _str(record.get("product_type"))


def _description(record: dict[str, Any]) -> str | None:
    value = record.get("description")
    if isinstance(value, dict):
        value = value.get("plain") or value.get("text")
    return _str(value)


def _short(text: str | None, limit: int = 240) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _attributes(record: dict[str, Any]) -> dict[str, str]:
    """A few facts the agent can filter and reason on. Merchant identity is one:
    on a multi-merchant index the agent must be able to say who sells it."""
    out: dict[str, str] = {}
    for key, label in (
        ("merchant_name", "merchant"),
        ("merchant_id", "merchant_id"),
        ("platform", "platform"),
        ("availability", "availability"),
        ("readiness_tier", "readiness_tier"),
        ("truth_tier", "truth_tier"),
    ):
        value = _str(record.get(key))
        if value:
            out[label] = value
    return out


def _option_values(variant: dict[str, Any]) -> dict[str, str]:
    options = variant.get("options")
    if isinstance(options, dict):
        return {str(k): str(v) for k, v in options.items() if v not in (None, "")}
    if isinstance(options, list):
        # Shopify's selectedOptions shape: [{"name": "Size", "value": "30ml"}]
        out: dict[str, str] = {}
        for entry in options:
            if isinstance(entry, dict) and entry.get("name") and entry.get("value") not in (None, ""):
                out[str(entry["name"])] = str(entry["value"])
        return out
    return {}


def _variant_record_id(variant: dict[str, Any]) -> str | None:
    return _str(variant.get("variant_id")) or _str(variant.get("id"))


def summary_from_record(record: dict[str, Any], ref: ProductRef, *, currency: str) -> Product:
    """A search row as a plain product. Search rows carry no variants, so a family
    is only known once ``get_product_details`` returns it; the backend refuses to
    cart a family it has not resolved."""
    price = _float(record.get("price"))
    return Product(
        product_id=encode_product_id(ref),
        title=_str(record.get("title")) or ref.product_id,
        brand=_str(record.get("brand")) or _str(record.get("vendor")),
        price=price if price is not None else 0.0,
        currency=_str(record.get("currency")) or currency,
        image_url=_image(record),
        category=_category(record),
        attributes=_attributes(record),
        in_stock=in_stock_of(record),
        short_description=_short(_description(record)),
    )


def details_from_record(record: dict[str, Any], ref: ProductRef, *, currency: str) -> ProductDetails:
    """The full record: a family with its variants when the record lists any with
    options, a plain product otherwise. A family's price is its lowest in-stock
    variant's; it is in stock while any variant is."""
    family_id = encode_product_id(ref.family)
    record_currency = _str(record.get("currency")) or currency
    raw_variants = record.get("variants")
    variants: list[Product] = []
    options: dict[str, list[str]] = {}
    if isinstance(raw_variants, list):
        for raw in raw_variants:
            if not isinstance(raw, dict):
                continue
            variant_id = _variant_record_id(raw)
            if not variant_id:
                continue
            values = _option_values(raw)
            for name, value in values.items():
                bucket = options.setdefault(name, [])
                if value not in bucket:
                    bucket.append(value)
            price = _float(raw.get("price"))
            variants.append(
                Product(
                    product_id=encode_product_id(ProductRef(ref.merchant_id, ref.product_id, variant_id)),
                    title=_str(raw.get("title")) or _str(record.get("title")) or variant_id,
                    brand=_str(record.get("brand")) or _str(record.get("vendor")),
                    price=price if price is not None else (_float(record.get("price")) or 0.0),
                    currency=_str(raw.get("currency")) or record_currency,
                    image_url=_str(raw.get("image_url")) or _image(record),
                    in_stock=in_stock_of(raw),
                    option_values=values,
                    variant_of=family_id,
                )
            )
    if not options:
        # Variants without options are not choices the customer makes; the record
        # is plain and the platform picks its default variant at checkout.
        variants = []
    in_stock_prices = [v.price for v in variants if v.in_stock and v.price > 0]
    price = min(in_stock_prices) if in_stock_prices else _float(record.get("price"))
    description = _description(record)
    specs: dict[str, str] = {}
    inci = _str(record.get("raw_ingredient_text_clean")) or _str(record.get("pdp_ingredients_raw"))
    if inci:
        specs["ingredients"] = inci
    sku = _str(record.get("sku"))
    if sku:
        specs["sku"] = sku
    return ProductDetails(
        product_id=family_id,
        title=_str(record.get("title")) or ref.product_id,
        brand=_str(record.get("brand")) or _str(record.get("vendor")),
        price=price if price is not None else 0.0,
        currency=record_currency,
        image_url=_image(record),
        category=_category(record),
        attributes=_attributes(record),
        in_stock=any(v.in_stock for v in variants) if variants else in_stock_of(record),
        short_description=_short(description),
        long_description=description,
        specs=specs,
        options=options,
        variants=variants,
    )


def variant_details(family: ProductDetails, variant_id: str) -> ProductDetails | None:
    """One variant, as the details tool returns it for a variant's id."""
    for variant in family.variants:
        if variant.product_id == variant_id:
            return ProductDetails(
                **variant.model_dump(),
                long_description=family.long_description,
                specs=family.specs,
            )
    return None


# -- decision block -> disclosure -----------------------------------------------------


def _lines(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for entry in value:
            if isinstance(entry, dict):
                entry = entry.get("text") or entry.get("claim") or entry.get("label")
            text = _str(entry)
            if text:
                out.append(text)
        return out
    return []


def disclosure_from_record(record: dict[str, Any], product_id: str) -> Disclosure | None:
    """Pivota's decision block (``get_product`` with ``include: ["decision"]``) as
    the facts box the agent may show. Authored here from the record; the model
    names the product and the rows come from this function alone. Attributed to
    Pivota, as the tool contract asks."""
    block = record.get("decision") or record.get("pivota_insights")
    if not isinstance(block, dict):
        return None
    rows: list[DisclosureRow] = []
    for text in _lines(block.get("why_it_stands_out"))[:4]:
        rows.append(DisclosureRow(label="Why it stands out", value=text))
    for text in _lines(block.get("best_for"))[:3]:
        rows.append(DisclosureRow(label="Best for", value=text))
    profile = _str(block.get("evidence_profile"))
    if profile:
        rows.append(DisclosureRow(label="Evidence profile", value=profile))
    if not rows:
        return None
    return Disclosure(
        title="Pivota Insights",
        product_id=product_id,
        rows=rows,
        sources=["Pivota decision layer"],
    )


# -- orders ----------------------------------------------------------------------------

_STATUS_WORDS: dict[str, OrderStatus] = {
    "processing": OrderStatus.PROCESSING,
    "pending": OrderStatus.PROCESSING,
    "paid": OrderStatus.PROCESSING,
    "confirmed": OrderStatus.PROCESSING,
    "unfulfilled": OrderStatus.PROCESSING,
    "partially_fulfilled": OrderStatus.PROCESSING,
    "fulfilled": OrderStatus.SHIPPED,
    "shipped": OrderStatus.SHIPPED,
    "in_transit": OrderStatus.SHIPPED,
    "out_for_delivery": OrderStatus.OUT_FOR_DELIVERY,
    "delivered": OrderStatus.DELIVERED,
    "delayed": OrderStatus.DELAYED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "voided": OrderStatus.CANCELLED,
    "return_initiated": OrderStatus.RETURN_INITIATED,
    "return_requested": OrderStatus.RETURN_INITIATED,
    "returned": OrderStatus.RETURN_INITIATED,
    "refunded": OrderStatus.REFUNDED,
    "partially_refunded": OrderStatus.REFUNDED,
}


def order_status_of(record: dict[str, Any]) -> OrderStatus:
    """Fulfillment wins over payment: a shipped order that was also paid is
    shipped. An unknown word is processing, the honest default for an order that
    exists."""
    for key in ("fulfillment_status", "status", "payment_status"):
        word = _str(record.get(key))
        if word and word.lower() in _STATUS_WORDS:
            return _STATUS_WORDS[word.lower()]
    return OrderStatus.PROCESSING


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = _str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def order_from_record(record: dict[str, Any], *, currency: str) -> Order | None:
    body = record.get("order") if isinstance(record.get("order"), dict) else record
    order_id = _str(body.get("order_id")) or _str(body.get("id"))
    if not order_id:
        return None
    items: list[OrderItem] = []
    raw_items = body.get("items") if isinstance(body.get("items"), list) else body.get("line_items")
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict):
            continue
        merchant_id = _str(raw.get("merchant_id")) or _str(body.get("merchant_id"))
        product_id = _str(raw.get("product_id"))
        variant_id = _str(raw.get("variant_id"))
        if merchant_id and product_id:
            ref = ProductRef(merchant_id, product_id, variant_id)
            agent_id = encode_product_id(ref)
            variant_of = encode_product_id(ref.family) if variant_id else None
        else:
            agent_id = product_id or variant_id or "unknown"
            variant_of = None
        items.append(
            OrderItem(
                product_id=agent_id,
                title=_str(raw.get("title")) or _str(raw.get("name")) or agent_id,
                quantity=int(raw.get("quantity") or 1),
                price=_float(raw.get("price")) or _float(raw.get("unit_price")) or 0.0,
                option_values=_option_values(raw),
                variant_of=variant_of,
            )
        )
    total = _float(body.get("total"))
    if total is None:
        for key in ("amounts", "pricing", "totals"):
            nested = body.get(key)
            if isinstance(nested, dict):
                total = _float(nested.get("total"))
                if total is not None:
                    break
    if total is None:
        total = round(sum(i.price * i.quantity for i in items), 2)
    placed = _datetime(body.get("placed_at")) or _datetime(body.get("created_at")) or datetime.now(UTC)
    return Order(
        order_id=order_id,
        status=order_status_of(body),
        placed_at=placed,
        items=items,
        total=total,
        currency=_str(body.get("currency")) or currency,
        estimated_delivery=_str(body.get("estimated_delivery")),
        tracking_url=_str(body.get("tracking_url")),
    )


# -- checkout ------------------------------------------------------------------------

_SESSION_ID_KEYS = ("session_id", "checkout_session_id", "id")
# The gateway's own precedence for a hosted page URL (HOSTED_CHECKOUT_URL_FIELDS in
# PIVOTA-Agent src/server.js): flat body first, then a nested checkout_session.
_HOSTED_URL_KEYS = ("hosted_url", "checkout_url", "url", "redirect_url")


def checkout_session_id_of(result: dict[str, Any]) -> str | None:
    for scope in (result, result.get("checkout_session"), result.get("session")):
        if isinstance(scope, dict):
            for key in _SESSION_ID_KEYS:
                value = _str(scope.get(key))
                if value:
                    return value
    return None


def hosted_checkout_url_of(result: dict[str, Any]) -> str | None:
    for scope in (result, result.get("checkout_session")):
        if isinstance(scope, dict):
            for key in _HOSTED_URL_KEYS:
                value = _str(scope.get(key))
                if value and value.startswith("https://"):
                    return value
    return None
