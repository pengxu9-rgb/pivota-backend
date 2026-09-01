import assert from "node:assert/strict";
import test from "node:test";

import {mapShopifyEvent} from "../integrations/shopify-web-pixel/src/mapper.mjs";

test("maps cart event without copying customer or page data", () => {
  const mapped = mapShopifyEvent({
    id: "evt-1",
    name: "product_added_to_cart",
    timestamp: "2026-08-31T12:00:00Z",
    clientId: "client-1",
    context: {document: {location: {href: "https://shop.test/?email=private"}}},
    data: {cartLine: {quantity: 2, merchandise: {id: "gid://variant/2", product: {id: "gid://product/1"}}}},
  });
  assert.equal(mapped.event_type, "cart.item_added");
  assert.equal(mapped.session_id, "client-1");
  assert.deepEqual(mapped.metadata.native_line_items, [{product_id: "gid://product/1", variant_id: "gid://variant/2", quantity: 2}]);
  assert.equal(JSON.stringify(mapped).includes("private"), false);
});

test("checkout completed remains non-authoritative", () => {
  const mapped = mapShopifyEvent({
    id: "evt-2",
    name: "checkout_completed",
    clientId: "client-1",
    data: {checkout: {token: "checkout-token", order: {id: "order-secret"}, totalPrice: {amount: "99.00"}}},
  });
  assert.equal(mapped.event_type, "checkout.submitted");
  assert.equal(mapped.checkout_id, "checkout-token");
  assert.equal("order_id" in mapped, false);
  assert.equal("amount_cents" in mapped, false);
});

test("drops unsupported or unstitchable events", () => {
  assert.equal(mapShopifyEvent({name: "page_viewed", clientId: "client"}), null);
  assert.equal(mapShopifyEvent({name: "product_viewed", data: {}}), null);
});
