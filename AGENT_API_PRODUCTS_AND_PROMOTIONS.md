# Agent API: Product Details + Promotions (Shopify)

Base URL:

`https://api.pivota.cc/agent/v1`

Auth:

`X-API-Key: ak_live_...`

Notes:
- The canonical machine-readable API spec is `GET https://api.pivota.cc/agent/docs/openapi.json`.
- Promotions/discounts are best-effort previews; the final amount is confirmed at checkout.
- Shopify marketing rules (e.g. buy-X-get-Y / order discounts) are synced into Pivota’s promotions table best-effort; first-time previews may take longer while the sync warms.

---

## 1) Product details (resolve from Shopify variant_id)

Use this when your UI only has a Shopify `variant_id` (skuId), and you need full product details
(title, description, images, variants/options) without sending the user out to a retailer page.

```http
GET /agent/v1/products/merchants/{merchant_id}/variant/{variant_id}
X-API-Key: ak_live_...
```

Response shape (abridged):
```json
{
  "status": "success",
  "selected_variant_id": "52589898989907",
  "product": {
    "id": "10315409752403",
    "merchant_id": "merch_xxx",
    "title": "Product title",
    "description": "<p>...</p>",
    "vendor": "Brand",
    "product_type": "Category",
    "images": ["https://..."],
    "tags": ["tag1", "tag2"],
    "options": [{ "name": "Size", "values": ["S", "M", "L"] }],
    "variants": [
      {
        "variant_id": "52589898989907",
        "title": "Default",
        "price": 36.0,
        "sku": "SKU-123",
        "available": true,
        "inventory_quantity": 10,
        "selected": true
      }
    ]
  }
}
```

---

## 2) Quote preview (Shopify promotions/discounts)

Use this to surface Shopify discounts/promotions in your own UI:
- automatic discounts
- discount codes
- buy X get Y / order discounts
- shipping discounts (when address/delivery options provided)

> Many promotions only apply with full cart context (multiple items, quantities, shipping address, discount codes).
> If you preview a single item, you may see no discount even though checkout applies one later.
>
> If you recently changed discounts in Shopify Admin, allow some time for promotion sync to reflect in preview.

```http
POST /agent/v1/quotes/preview
X-API-Key: ak_live_...
Content-Type: application/json
```

Request:
```json
{
  "merchant_id": "merch_xxx",
  "items": [
    { "product_id": "10315409752403", "variant_id": "52589898989907", "quantity": 2 }
  ],
  "discount_codes": ["CODE_IF_ANY"],
  "shipping_address": { "country": "US", "postal_code": "10001" }
}
```

Key response fields:
- `pricing.discount_total`
- `promotion_lines[]` (labels + allocations)
- `line_items[]` (unit price original/effective, line discounts, compare-at savings)
