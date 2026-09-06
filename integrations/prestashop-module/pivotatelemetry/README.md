# Pivota Commerce Telemetry — PrestaShop module

PrestaShop has **no outbound webhooks**. There is no subscription API, no
signed delivery, no callback registry — the platform's extension point is a
*hook* that runs inside the shop's own PHP process. So Pivota ships the sender,
exactly as it does for Salesforce B2C (`integrations/sfcc-cartridge/`).

Compatible with PrestaShop 1.7 and 8. **The PHP in this directory is
unlinted** — it was authored on a machine with no `php` binary.

## What it does

| File | Role |
| --- | --- |
| `pivotatelemetry.php` | Module class. Installs the outbox table, registers three hooks, renders the configuration page. **Never opens a socket.** |
| `controllers/front/drain.php` | Cron-driven front controller. The only file that talks to the network. |

Three hooks are registered:

* `actionValidateOrder` — the order was created (params: `order`, `orderStatus`,
  `cart`, `customer`, `currency`).
* `actionOrderStatusPostUpdate` — the order's state changed (params:
  `newOrderStatus`, `oldOrderStatus`, `id_order`). **PostUpdate, not Update:**
  `OrderHistory` fires the `Update` variant *before* the new state is written,
  so there the order still carries the old `current_state` and the old
  `total_paid_real`.
* `actionOrderSlipAdd` — a credit slip (a refund) was created (params: `order`,
  `productList`, `qtyList`). The hook does not carry the slip itself, so the
  module reads back the newest `order_slip` row for that order.

Each hook writes **one row** into `ps_pivota_telemetry_outbox` and returns. A
Pivota outage therefore cannot slow down or break a checkout.

## What it never sends

No name, no e-mail, no telephone number, no postal or billing details, no
payment instrument, no product titles, no per-line refund detail. The payload
carries only: order id / reference / cart id / customer **id**, currency ISO
code, the resolved state key, four order-state booleans, the order totals, the
payment module's technical name, the two order timestamps, and — for a
refund — the credit slip's id, amounts and timestamp.

## Install

1. In the Pivota merchant console call
   `POST /integrations/prestashop/{store_id}/telemetry/ensure`. The response
   carries `endpoint`, `store_id` and — **only on the call that mints it** —
   `secret`. Copy the secret now; it is never shown again. If you lose it,
   call the same endpoint with `{"rotate": true}` for a new one. Minting and
   rotating are restricted to the merchant themselves and to Pivota admins;
   other staff can only see *whether* a store is provisioned.
2. Zip this `pivotatelemetry` directory and upload it in the back office under
   **Modules → Module Manager → Upload a module**, then install it.
   PrestaShop writes the module's `config.xml` itself during installation, so
   this directory does not ship one. A `logo.png` in this folder is optional
   and only affects the back-office listing.
3. Open the module's **Configure** page and paste the endpoint and the secret.
   There is no separate "store id" field: the store id is the last segment of
   the endpoint URL. The endpoint **must start with `https://`** — the page
   refuses anything else, because the body is signed but not encrypted and over
   http it (and a valid signature for it) would travel in cleartext. The secret
   field is a password input and its stored value is never rendered back —
   leaving it empty on a later save keeps the current secret.
4. The Configure page prints the **cron URL** (it carries a cron token minted
   at install; that token is *not* the signing secret). Add a cron line, e.g.

   ```
   */2 * * * * curl -fsS "https://your-shop.example/index.php?fc=module&module=pivotatelemetry&controller=drain&token=THE_CRON_TOKEN" > /dev/null
   ```

   The non-rewritten `index.php?fc=module&...` form is used deliberately: the
   rewritten `/module/pivotatelemetry/drain` form carries a language segment a
   cron line should not have to know.

## Delivery

Each POST carries at most **100 events**; a single cron run makes at most
**10 POSTs**. The body is

```json
{"events": [ ... ], "shop_url": "https://your-shop.example"}
```

and the headers are

```
X-Pivota-PrestaShop-Signature: sha256=<hex hmac(secret, timestamp + "." + body)>
X-Pivota-PrestaShop-Timestamp: <unix seconds>
X-Pivota-PrestaShop-Delivery-Id: <random per batch>
X-Pivota-PrestaShop-Shop-Url: https://your-shop.example
```

Pivota rejects a delivery whose timestamp is more than 300 seconds old, whose
signature does not verify, or whose shop URL host does not match the host the
store was connected with.

A 2xx deletes the delivered rows. Anything else keeps them, increments
`attempts`, backs off exponentially (15 s doubling to a 1 h ceiling) and stops
that run.

## Events that could not be delivered

After 20 failed attempts a row is **not deleted**. Its `status` column flips
from `pending` to `dead`, the drain never selects it again, and the shop's own
log (**Advanced Parameters → Logs**, source `PivotaTelemetry`) gets one error
line naming the event id, the order and the attempt count. The module's
**Configure** page shows a red banner counting the dead rows, so a shop that
was misconfigured for a week can see how much it lost rather than discovering a
short ledger in Pivota.

> **Gap, stated on purpose.** Nothing on the *receiver* side notices the
> silence: Pivota has no staleness signal for a PrestaShop store that stopped
> delivering, and the module has no self-service replay. Dead rows are replayed
> by Pivota support, from the rows still sitting in `ps_pivota_telemetry_outbox`
> with `status = 'dead'`. Fixing the endpoint or the secret does **not**
> resurrect them.

## Uninstall

Uninstalling is deliberately conservative, because the back office's **Reset**
button is uninstall followed by install:

* `ps_pivota_telemetry_outbox` is dropped **only when nothing is queued in it**.
  If pending events remain, the table is kept and a warning is logged, so a
  reinstall can still deliver them.
* `PIVOTA_TELEMETRY_ENDPOINT` and `PIVOTA_TELEMETRY_SECRET` are **kept**. A
  reset would otherwise force the merchant to rotate the secret in the Pivota
  console and paste it again.
* Only `PIVOTA_TELEMETRY_CRON_TOKEN` is deleted; install mints a new one. Update
  the cron line after a reinstall.
