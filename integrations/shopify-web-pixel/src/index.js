import {register} from "@shopify/web-pixels-extension";
import {EVENT_MAP, mapShopifyEvent} from "./mapper.mjs";

register(({analytics, settings}) => {
  const endpoint = String(settings.endpoint || "").trim();
  const collectorToken = String(settings.collectorToken || "").trim();
  if (!endpoint || !collectorToken) return;

  Object.keys(EVENT_MAP).forEach((name) => {
    analytics.subscribe(name, (shopifyEvent) => {
      const event = mapShopifyEvent(shopifyEvent);
      if (!event) return;
      fetch(endpoint, {
        method: "POST",
        headers: {"Content-Type": "text/plain;charset=UTF-8"},
        body: JSON.stringify({collector_token: collectorToken, events: [event]}),
        keepalive: true,
      }).catch(() => {});
    });
  });
});
