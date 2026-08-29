# Pivota SFCC telemetry cartridge

This cartridge adds native Salesforce B2C Commerce lifecycle events without
placing an external network call on the shopper request path.

```text
SCAPI / OCAPI hook
  -> PivotaTelemetryOutbox custom object (local, best effort)
  -> PivotaTelemetryDrain scheduled job
  -> signed Pivota event endpoint
  -> canonical merchant commerce ledger
```

The included hooks cover:

| SFCC hook | Native event | Canonical event |
| --- | --- | --- |
| `dw.ocapi.shop.basket.afterPOST` | `basket.created` | `cart.created` |
| `dw.ocapi.shop.basket.items.afterPOST` | `basket.item_added` | `cart.item_added` |
| `dw.ocapi.shop.order.beforePOST` | `checkout.submitted` | `checkout.submitted` |
| `dw.ocapi.shop.order.afterPOST` | `order.created` | `order.created` |
| `dw.ocapi.shop.order.payment_instrument.afterPOST` | `payment.authorized` / `payment.declined` | same |

SFCC invokes these OCAPI hook names for the corresponding supported SCAPI
Shopper APIs as well. A SiteGenesis/SFRA checkout that places orders outside
those APIs must call `Telemetry.safeEnqueue(...)` from its existing post-action
hook or controller extension. Do not equate order creation or payment
authorization with settlement; emit `order.paid` or `payment.succeeded` only
from the merchant's authoritative capture/status integration.

## Install

1. Upload `int_pivota_telemetry` and add it to the site's cartridge path.
2. In Business Manager, open **Global Preferences → Feature Switches** and
   enable **Salesforce Commerce API Hook Execution**.
3. Import `metadata/meta/custom-objecttype-definitions.xml` in Business Manager.
4. Connect the SFCC store in Pivota, then call
   `POST /integrations/salesforce-commerce-cloud/{store_id}/telemetry/provision`.
   Save the returned secret; it is shown only when first generated or rotated.
5. Replace `RefArch`, the URL, and the password placeholders in
   `metadata/services.xml`, then import the service definition. The credential
   ID must be `pivota.telemetry.{site_id}`. Create one credential per connected
   site; the cartridge selects the current site's URL and signing secret at
   runtime. Communication logging intentionally remains disabled so event
   bodies and signatures are not written to service logs.
6. `metadata/jobs.xml` is a per-site template. For **every connected site**,
   duplicate its complete `<job>` element, replace `RefArch` in both the job ID
   and `<context site-id>`, and keep all copies inside the same `<jobs>` root.
   Import the combined file and schedule every site job (one minute is the
   recommended starting cadence). A site-scoped outbox is drained only by a
   job flow running in that exact site context.
7. Activate the code version so SFCC registers `steptypes.json` and the hooks.

The drain sends at most 100 events and at most 900,000 UTF-8 bytes per request.
It signs
`{unix_timestamp}.{raw_json_body}` with HMAC-SHA256 and supplies the connected
site ID. Pivota rejects signatures older than five minutes. Successful batches
are deleted; failures remain in the seven-day outbox with exponential backoff.

## Safety boundary

The cartridge allowlists commerce IDs, amounts, currency, status, and product
line identifiers. It does not retain names, email, phone, address, IP, cookies,
payment instrument details, or authentication tokens. Hook failures are logged
and swallowed so telemetry cannot block basket or checkout operations.

The Universal Web Collector remains the recommended source for product views,
searches, visitor/session identity, and storefront interactions not represented
by these platform hooks.
