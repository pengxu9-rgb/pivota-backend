# 字段映射表（PCS v0.1）

> 约定：`Shopify.*` 表示 Admin GraphQL 字段路径；`PCS.*` 表示 v0.1 JSON object 的路径。

---

## OPS@0.1（产品与政策）

| Shopify | PCS | 备注 |
|---|---|---|
| `shop.id` | `OPS.merchant.shop_gid` | |
| `shop.primaryDomain.host` | `OPS.merchant.shop_domain` | |
| `shop.refundPolicy.{url,title,body,updatedAt}` | `OPS.policies.refund_policy.*` | `body` -> `body_html`，并生成 `hash_sha256` |
| `Product.id` | `OPS.products[].product_gid` | |
| `Product.legacyResourceId` | `OPS.products[].product_legacy_id` | |
| `Product.title/handle/status/vendor/productType/tags` | `OPS.products[].*` | |
| `Product.updatedAt` | `OPS.products[].updated_at` | |
| `Product.metafields(namespace:"pcs")` | `OPS.products[].pcs_metafields` | 原样存 kv（或按定义强类型） |
| `ProductVariant.id` | `OPS.products[].variants[].variant_gid` | |
| `ProductVariant.legacyResourceId` | `OPS.products[].variants[].variant_legacy_id` | |
| `ProductVariant.sku/barcode/title` | `OPS.products[].variants[].*` | |
| `ProductVariant.price` | `OPS.products[].variants[].price.amount` | 货币来自 `Order.currencyCode` / shop currency；v0.1 在 OPS 中显式存 currency |
| `ProductVariant.compareAtPrice` | `OPS.products[].variants[].compare_at_price.amount` | 可空 |
| `InventoryItem.requiresShipping` | `OPS.products[].variants[].requires_shipping` | |
| `InventoryItem.harmonizedSystemCode` | `OPS.products[].variants[].customs.hs_code` | |
| `InventoryItem.countryCodeOfOrigin` | `OPS.products[].variants[].customs.country_of_origin` | |
| `InventoryItem.measurement.weight` | `OPS.products[].variants[].customs.weight_grams` | 单位换算到 grams |
| `InventoryLevel.location.id` | `OPS.products[].variants[].inventory.by_location[].location_gid` | |
| `InventoryLevel.available` | `OPS.products[].variants[].inventory.by_location[].available` | |
| `ProductVariant.metafields(namespace:"pcs")` | `OPS.products[].variants[].pcs_metafields` | |

---

## Ledger@0.1（订单/支付/退款/履约）

| Shopify | PCS | 备注 |
|---|---|---|
| `Order.id` | `Ledger.order.order_gid` | |
| `Order.name` | `Ledger.order.order_name` | |
| `Order.processedAt` | `Ledger.order.placed_at` | v0.1 用 processedAt 作为 placed |
| `Order.currencyCode` | `Ledger.order.currency` | |
| `Order.displayFinancialStatus` | `Ledger.order.shopify_financial_status` | |
| `Order.displayFulfillmentStatus` | `Ledger.order.shopify_fulfillment_status` | |
| `Order.totalPriceSet.shopMoney` | `Ledger.order.totals.total` | |
| `Order.subtotalPriceSet.shopMoney` | `Ledger.order.totals.subtotal` | |
| `Order.totalDiscountsSet.shopMoney` | `Ledger.order.totals.discount_total` | |
| `Order.totalShippingPriceSet.shopMoney` | `Ledger.order.totals.shipping_fee` | |
| `Order.totalTaxSet.shopMoney` | `Ledger.order.totals.tax` | |
| `Order.transactions.nodes[].id` | `Ledger.payments[].transaction_gid` | |
| `Order.transactions.nodes[].kind/status/gateway/authorizationCode/processedAt` | `Ledger.payments[].*` | `parentTransaction.id` -> `parent_transaction_gid` |
| `Order.transactions.nodes[].amountSet.shopMoney` | `Ledger.payments[].amount` | |
| `Order.fulfillments.nodes[].id` | `Ledger.fulfillments[].fulfillment_gid` | |
| `Order.fulfillments.nodes[].status/createdAt/deliveredAt` | `Ledger.fulfillments[].*` | |
| `Order.fulfillments.nodes[].trackingInfo[]` | `Ledger.fulfillments[].tracking[]` | |
| `Order.refunds.nodes[].id` | `Ledger.refunds[].refund_gid` | |
| `Order.refunds.nodes[].createdAt/note` | `Ledger.refunds[].*` | |
| `Order.refunds.nodes[].totalRefundedSet.shopMoney` | `Ledger.refunds[].total_refunded` | |
| `Order.refunds.nodes[].refundLineItems.nodes[]` | `Ledger.refunds[].line_items[]` | `restockType` 直存 |

---

## Disputes / Settlement（可选域）

| Shopify | PCS | 备注 |
|---|---|---|
| `ShopifyPaymentsDispute.id` | `Ledger.disputes[].shopify_dispute_gid` | 仅 Shopify Payments |
| `ShopifyPaymentsDispute.status/reason` | `Ledger.disputes[].*` | 状态需归一化到 `dispute_state` |
| `ShopifyPaymentsDispute.initiatedAt/evidenceDueBy/finalizedAt` | `Ledger.disputes[].*` | |
| `ShopifyPaymentsBalanceTransaction.*` | `Settlement facts`（DDL 表） | v0.1 在 DB 中存，不强塞进 Ledger JSON |

