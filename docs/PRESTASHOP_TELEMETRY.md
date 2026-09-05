# PrestaShop commerce telemetry

PrestaShop has **no outbound webhooks**. No subscription API, no signed
delivery, no callback registry — the platform's only extension point is a hook
that runs inside the shop's own PHP process. So, exactly like Salesforce B2C,
Pivota ships the sender:

| Piece | Where |
| --- | --- |
| The module the merchant installs | `integrations/prestashop-module/pivotatelemetry/` |
| The receiver | `routes/prestashop_webhooks.py` (`POST /webhooks/prestashop/{store_id}`) |
| The mapper | `services/prestashop_event_adapter.py` |
| Secret provisioning | `POST /integrations/prestashop/{store_id}/telemetry/ensure` |
| Wire contract test | `tests/test_prestashop_module_contract.py` |

**The PHP is unlinted.** It was written on a machine with no `php` binary and
nothing in CI executes it. `tests/test_prestashop_module_contract.py` is the
only thing holding the two sides together, and it is a text-level test.

## Install (merchant)

1. Call `POST /integrations/prestashop/{store_id}/telemetry/ensure`. The
   response carries `endpoint`, `store_id`, `shop_url` and — **only on the call
   that mints it** — `secret`.
2. Zip `integrations/prestashop-module/pivotatelemetry/` and install it in the
   back office (**Modules → Module Manager → Upload a module**). PrestaShop
   writes the module's `config.xml` itself during installation, so the
   directory does not ship one; `logo.png` is optional.
3. Paste `endpoint`, `store_id` and `secret` into the module's **Configure**
   page.
4. Add the cron line the Configure page prints, e.g. every two minutes:

   ```
   */2 * * * * curl -fsS "https://shop.example/index.php?fc=module&module=pivotatelemetry&controller=drain&token=THE_CRON_TOKEN" > /dev/null
   ```

   The cron token is minted at install and is **not** the signing secret. The
   non-rewritten `index.php?fc=module&…` form is used because the rewritten
   `/module/pivotatelemetry/drain` form carries a language segment.

## The secret lifecycle

There is no OAuth handshake and no webhook API, so the secret has to reach a
human. That forces a different lifecycle from `ensure_bigcommerce_webhooks`,
which never returns its secret because the server installs the hooks itself:

| Call | Response |
| --- | --- |
| First (no secret stored) | `{endpoint, store_id, shop_url, secret, secret_provisioned: true, rotated: false}` |
| Every later call | `{endpoint, store_id, shop_url, secret_provisioned: true}` — **no secret** |
| `{"rotate": true}` | A new secret, returned once, `rotated: true`. The old one stops working immediately. |

The secret is written into the store's credential JSON in
`merchant_stores.api_key`, then **re-read** before it is returned: two
first-time calls can race and the merchant must paste the value the receiver
actually holds, not the one this request minted. `databases` + asyncpg reports
no rowcount from an `UPDATE`, so the re-read is also the only proof the write
landed. Nothing logs the secret. A merchant who loses it rotates; there is no
read-back path.

`merchant_connect_prestashop` stores the bare Webservice key as a plain string,
so a store connected before this existed is migrated to
`{"api_key": "<the bare key>", "webhook_secret": "…"}` rather than having its
key destroyed.

## The wire

```
POST /webhooks/prestashop/{store_id}
X-Pivota-PrestaShop-Signature: sha256=<hex hmac(secret, timestamp + "." + body)>
X-Pivota-PrestaShop-Timestamp: <unix seconds>
X-Pivota-PrestaShop-Delivery-Id: <random per batch>
X-Pivota-PrestaShop-Shop-Url: https://shop.example

{"events": [ … 1..100 … ], "shop_url": "https://shop.example"}
```

Auth chain, in order:

1. 1 MB body cap;
2. an **active** `platform = 'prestashop'` store row for `{store_id}`;
3. a `webhook_secret` in that row's credential JSON;
4. timestamp within ±300 s;
5. constant-time HMAC-SHA256 over `timestamp + "." + body`;
6. the JSON parses;
7. the shop-url **header** and the **signed body's** `shop_url` both resolve to
   the host of the store row's `domain`;
8. `identify` + the `platform` rate-limit tier;
9. 1–100 events, mapped, ingested with
   `write_path="prestashop_module"` / `agent_identity_confidence="platform_asserted"`
   → authority `platform`.

Steps 2–5 answer with one message, so a caller never learns which it hit.
Step 7 has its own message: it is a configuration error on a delivery that has
already proved it holds the secret. The header alone is not covered by the
signature, which is why the signed body must agree with it.

An unsupported hook is counted `ignored`; a malformed event is counted
`rejected` and its siblings still ingest. Both answer 2xx so the module's
outbox deletes the batch rather than retrying forever.

## Mapping

| Module event | Canonical | Amount |
| --- | --- | --- |
| `actionValidateOrder` | `order.created` | `total_paid_tax_incl` |
| `actionValidateOrder`, state paid | + `order.paid` | `total_paid_real`, falling back to `total_paid_tax_incl` when it is 0 |
| `actionOrderStatusPostUpdate`, `state_key = payment` or `state_flags.paid` | `order.paid` | as above |
| `actionOrderStatusPostUpdate`, `state_key = canceled` | `order.cancelled` | none |
| `actionOrderStatusPostUpdate`, `state_key = error` | `payment.failed` | none |
| `actionOrderStatusPostUpdate`, `state_key = refund` | **nothing** | — |
| `actionOrderStatusPostUpdate`, `shipped` / `delivered` / `other` | nothing | — |
| `actionOrderSlipAdd` | `refund.succeeded`, keyed on the slip id | `total_products_tax_incl + total_shipping_tax_incl` |

`order_ref` is always `prestashop:<id_order>`: there is no PrestaShop order
writeback in this repo (`create_prestashop_order` does not exist), so every
order originated in the shop. `buyer_id` is `id_customer` when non-zero,
`cart_id` is `id_cart`, and metadata is `native_status` (the resolved state
key), `native_payment_method` (the payment module's technical name),
`native_amount_semantics` (which total a paid amount came from) and
`webhook_delivery_id` — all already in the shared allowlist; none was added.

### Why a `refund` state alone emits nothing

`PS_OS_REFUND` records that someone marked the order refunded. It carries no
amount, no per-refund identity, and can be set and unset. The **credit slip**
(`OrderSlip`, hook `actionOrderSlipAdd`) is the fact that carries money and its
own id. Emitting `refund.succeeded` for both would double-count every refund of
every order that also gets flipped to the refund state.

### Which OrderSlip amount is authoritative

`total_products_tax_incl + total_shipping_tax_incl`. Verified against
`src/Adapter/Order/Refund/OrderSlipCreator.php` on 8.2.x, which writes:

```php
$orderSlip->amount = $add_tax ? …total_products_tax_excl : …total_products_tax_incl;
$orderSlip->shipping_cost_amount = $orderSlip->total_shipping_tax_incl;
```

So `amount` is products-only (shipping never rides in it) and **tax-excluded on
the default path**, with nothing on the row saying which basis was used; it is
also overwritten wholesale by an operator-typed figure. It is not formally
`@deprecated`, but it is not the refund.

The one row shape where the totals are absent is
`OrderSlip::createPartialOrderSlip()`, which sets only `amount` and
`shipping_cost_amount` and leaves the four totals at 0. Core has no callers for
it anywhere in the 8.2.x tree, but a third-party module can, so a slip whose
totals are missing or sum to zero while `amount` is non-zero falls back to
`amount + shipping_cost_amount` rather than reporting a refund of nothing.

## What the module never sends

No name, no e-mail, no telephone number, no postal or billing details, no
payment instrument, no product titles, no per-line refund detail (the
`productList` / `qtyList` hook parameters are ignored). The payload carries only
order id / reference / cart id / customer **id**, currency ISO code, the
resolved state key, four order-state booleans, the order totals, the payment
module's technical name, the two order timestamps, and — for a refund — the
credit slip's id, amounts and timestamp.

## PrestaShop facts: verified vs not

Verified against the PrestaShop 8.2.x source and devdocs on 2026-09-05.

| Fact | Status |
| --- | --- |
| `actionValidateOrder` params `cart, order, customer, currency, orderStatus` | VERIFIED — `classes/PaymentModule.php` |
| `actionOrderStatusUpdate` / `PostUpdate` params `newOrderStatus, oldOrderStatus, id_order` | VERIFIED — `classes/order/OrderHistory.php`. `oldOrderStatus` is a third param; `Update` fires **before** the write |
| `actionOrderSlipAdd` params `order, productList, qtyList` | VERIFIED — `src/Adapter/Order/Refund/OrderSlipCreator.php`. The hook does **not** carry the slip, so the module reads the newest `order_slip` row back |
| `OrderState` booleans `paid, logable, invoice, shipped, delivery` | VERIFIED — `classes/order/OrderState.php` |
| `OrderState.template` is a boolean | **FALSE** — it is a multilang string. Not carried |
| `PS_OS_PAYMENT`, `PS_OS_CANCELED` (one L), `PS_OS_REFUND`, `PS_OS_ERROR`, `PS_OS_SHIPPING`, `PS_OS_DELIVERED` | VERIFIED — `install-dev/data/xml/configuration.xml` |
| `PS_OS_PAYMENT_ERROR` | **DOES NOT EXIST**. Never looked up |
| `Order` fields `id, reference, id_cart, id_customer, id_currency, current_state, valid, total_paid_tax_incl, total_paid_real, module, payment, date_add, date_upd` | VERIFIED — `classes/order/Order.php` (`id` from `ObjectModel`) |
| `OrderSlip` fields and the authoritative amount | VERIFIED — see above |
| `Currency::$iso_code` | VERIFIED — `classes/Currency.php` |
| Webservice resources `orders`, `order_histories`, `order_slip` (singular) | VERIFIED — `classes/webservice/WebserviceRequest.php` and devdocs |
| `install()` + `registerHook()`, `Configuration::updateValue/get`, front controllers in `controllers/front/`, both URL forms, `Tools::getValue` | VERIFIED — devdocs |
| `config.xml` auto-generated at install; `logo.png` recommended not required | VERIFIED — devdocs |
| `Db::getInstance()->execute()` / `->insert()` / `->executeS()` contract | **UNVERIFIED from devdocs** — used pervasively in core, but no devdocs page states it |
| `Tools::passwdGen()`, `Tools::getShopDomainSsl()`, `PrestaShopLogger::addLog()`, `HelperForm` | **UNVERIFIED from devdocs** — core helpers, not covered by the pages checked |

## Not built

**No reconciliation fallback.** A poller over the Webservice `order_histories`
and `order_slip` resources would be a *different* ingress (a replay is not a
signed live delivery) and would need its own `prestashop_reconciliation` write
path in `services/commerce_ledger_provenance.py`. It is not in this change. The
outbox already survives an outage — rows are kept, retried with backoff for up
to 20 attempts, and only then dropped — so the gap it would close is a shop
whose cron never ran for long enough to exhaust that budget.
