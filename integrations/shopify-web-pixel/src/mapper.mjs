const EVENT_MAP = Object.freeze({
  product_viewed: "product.viewed",
  product_added_to_cart: "cart.item_added",
  product_removed_from_cart: "cart.item_removed",
  cart_viewed: "cart.updated",
  checkout_started: "checkout.started",
  checkout_contact_info_submitted: "checkout.submitted",
  checkout_shipping_info_submitted: "checkout.submitted",
  payment_info_submitted: "payment.attempted",
  checkout_completed: "checkout.submitted",
});

function scalar(value, max = 128) {
  if (value === null || value === undefined || typeof value === "object") return undefined;
  const text = String(value).trim();
  return text ? text.slice(0, max) : undefined;
}

function ids(data = {}) {
  const cartLine = data.cartLine || {};
  const merchandise = cartLine.merchandise || {};
  const product = merchandise.product || data.productVariant?.product || {};
  const checkout = data.checkout || {};
  const cart = data.cart || {};
  const metadata = [];
  if (product.id || merchandise.id || data.productVariant?.id) {
    metadata.push({
      product_id: scalar(product.id),
      variant_id: scalar(merchandise.id || data.productVariant?.id),
      quantity: Number.isFinite(Number(cartLine.quantity)) ? Number(cartLine.quantity) : undefined,
    });
  }
  return {
    cart_id: scalar(cart.id || checkout.cartToken),
    checkout_id: scalar(checkout.token),
    metadata: metadata.length ? {native_line_items: metadata} : undefined,
  };
}

export function mapShopifyEvent(event) {
  const eventType = EVENT_MAP[event?.name];
  if (!eventType) return null;
  const mappedIds = ids(event.data);
  const sessionId = scalar(event.clientId);
  if (!sessionId && !mappedIds.cart_id && !mappedIds.checkout_id) return null;
  return {
    event_id: `shopify_pixel:${scalar(event.id, 200) || `${event.name}:${event.seq || 0}`}`,
    event_type: eventType,
    occurred_at: scalar(event.timestamp, 64) || new Date().toISOString(),
    session_id: sessionId,
    ...mappedIds,
  };
}

export {EVENT_MAP};
