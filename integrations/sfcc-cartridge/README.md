# Pivota SFCC telemetry cartridge

This cartridge adds native Salesforce B2C Commerce lifecycle events without
placing an external network call on the shopper request path.

```text
SCAPI / OCAPI hook  ─┐
                     ├─> PivotaTelemetryOutbox custom object (local, best effort)
PivotaSettlementSweep┘     -> PivotaTelemetryDrain scheduled job
  (scheduled job step)     -> signed Pivota event endpoint
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
authorization with settlement — that is what the sweep below is for.

## The settlement sweep

**SFCC fires nothing on settlement.** `Order.paymentStatus` (`NOTPAID` /
`PARTPAID` / `PAID`) is written by the merchant's payment-processor cartridge,
by an OMS integration, or by a Business Manager user, and no hook, no
OCAPI/SCAPI event and no notification accompanies the transition. The same is
true of `Order.status` becoming `CANCELLED`, and of a credit `Invoice`
appearing under an order. Without the sweep, the SFCC funnel stops at
`payment.authorized`.

So `PivotaSettlementSweep` (`jobs/SweepPivotaSettlements.js`) **looks**. Each
run walks the orders whose `lastModified` is at or after a persisted cursor,
ordered `lastModified asc`, bounded by `MaxOrders`, and enqueues:

| Condition | Event | Amount | Keyed on |
| --- | --- | --- | --- |
| `paymentStatus == PAID` | `order.paid` | `totalGrossPrice` | `order.paid:<orderNo>` |
| `status == CANCELLED` | `order.cancelled` | none | `order.cancelled:<orderNo>` |
| a credit `Invoice` whose cumulative refund ROSE | `refund.succeeded` | the **delta** since the last observation | `refund.succeeded:<invoiceNumber>:<cumulative>` |

`Invoice.refundedAmount` is **cumulative per invoice** — SFCC does not create a
second invoice for a second partial refund against the same one, it raises that
invoice's figure. So the sweep sends the **difference** since the last figure it
observed, under a key qualified by the new cumulative total, and stores that same
string (`<invoiceNumber>:<cumulative>`) as the once-only marker. A second partial
refund against one invoice is therefore a second ledger row rather than a lost
fact, the two deltas sum to the invoice's cumulative figure in Pivota's funnel,
and a redelivery of either still dedupes. A cumulative figure that **fell** is
skipped with a WARN and never emitted: money does not un-refund, and re-sending
a lower figure under a new key would add refunded money.

`order.paid` carries the **order total**, not a capture, and its `occurred_at`
is the order's **`lastModified`**, not the settlement instant — SFCC records no
settlement instant, and `lastModified` is the transition only when nothing
touched the order afterwards. Both facts are reported to Pivota:
`native_amount_semantics = order_total_gross` rides on the event, and
`docs/SFCC_TELEMETRY.md` states the time semantics.

No `payment.succeeded` is emitted per capture. Pivota's funnel counts
`payment.succeeded` and `order.paid` into one paid stage and takes the *maximum*
reported amount per order rather than summing captures, so a second event per
capture would add rows and no money.

### Once-only, in two independent layers

1. **Order custom attributes** — `pivotaPaidEmittedAt`,
   `pivotaCancelledEmittedAt` and `pivotaRefundedInvoices` (a `set-of-string` of
   invoice numbers, capped at 200), imported from
   `metadata/meta/system-objecttype-extensions.xml`. They are written **only
   after** the event reached the outbox, so a failed enqueue is retried on the
   next run instead of being recorded as delivered.
2. **A deterministic `event_id`** per fact, as in the table above. Pivota hashes
   it into the canonical event key and the ledger dedupes first-write-wins on
   that key. So a marker that is lost — cleared in Business Manager, restored
   from a backup, or aged out of the capped refund set — costs one redundant
   delivery, never a double count. The refund set keeps one marker per invoice
   (a new observation replaces that invoice's marker), so its 200 cap bounds
   refunded invoices per order, not observations of them.

Because those ids are deterministic, an outbox key can already be taken —
`createCustomObject` raises on a duplicate, and an undelivered row lives in the
outbox for up to seven days. `Telemetry.enqueue` therefore looks the key up
first and treats an existing row as success: re-enqueuing an event that is still
queued is the ordinary shape of a Pivota outage, and it must not be counted as
a failed order.

Writing a marker updates the order's `lastModified`, so an order the sweep has
just marked is re-examined on the following run and then skipped without a
write. That converges after one extra pass; it is not a loop.

The cursor lives in a `PivotaTelemetrySweepCursor` custom object (key
`settlement`) and is always stored **rewound by `OverlapMinutes`** (default 10),
so a run that dies mid-batch, or an order whose `lastModified` lands inside the
window just closed, is re-examined rather than lost. It never moves backwards,
and it never steps over an order whose enqueue failed in this run. With no
cursor yet, the first run reaches back `InitialLookbackHours` (default 24).

That custom type is declared **`<storage-scope>site</storage-scope>`**, the same
scope as the outbox it feeds, and the scope is load-bearing:
`OrderMgr.searchOrders` only ever sees the current site's orders, so a cursor
shared across sites would let one site's watermark hide another site's orders.
Because the scope is per site, the object id is the plain literal `settlement`
rather than being keyed on the site id.

### Why the failure clamp is bounded — `MaxFailureLagHours`

"Never steps over an order whose enqueue failed" is a stall if it is unbounded.
An order that fails on **every** run — bad data, an exception while the event is
built, an invoice member that throws — pins the cursor to its own `lastModified`
for good; because `MaxOrders` bounds each run, the sweep then re-reads the same
window forever and never reaches newer orders. The site's whole settlement
telemetry stops, and the only symptom is a `failed=` count in this job's log.

So `MaxFailureLagHours` (default 24, range 1–168) caps how far behind the newest
order a run observed the clamp may hold the cursor. Past that cap the failing
orders are **abandoned** — their `order.paid`, `order.cancelled` and
`refund.succeeded` events are never emitted and Pivota will never learn them —
and the job logs an **ERROR naming the abandoned order numbers** (up to 50 per
run) so they can be replayed by hand. The step also **fails** on such a run —
`Status(ERROR, 'ABANDONED')`, with the counts in the message — because Business
Manager notifies on a non-OK step status and on nothing else, and an ERROR log
line nobody reads is not a notice. The step is declared `enforce-restart="false"`
in `metadata/jobs.xml`, so a failed run does not block the next scheduled tick:
the sweep carries straight on from its advanced cursor. If the bound moves the
cursor while nothing was actually abandoned, the run logs a WARN and stays `OK`.

Watch that ERROR: it is the only notice that settlement facts were dropped.
Raise the parameter to give a flapping integration longer to recover; lower it
to keep the cursor moving on a site where a stall matters more than a few
orders.

### What the sweep must never do

**It does not register on `dw.order.payment.capture` or
`dw.order.payment.refund`.** Those are `dw.order.hooks.PaymentHooks`
*implementation* extension points: the merchant's PSP cartridge implements them
to actually move the money, and the hook manager resolves a single
implementation by cartridge-path order. A telemetry "observer" registered there
could shadow the real processor and break capture. Telemetry must never be able
to do that, so it observes state and never intercepts it.
`tests/test_sfcc_cartridge_contract.py` fails if either name ever appears in
`hooks.json`.

### Residual gap: refunds issued in the PSP dashboard

Refund records exist in SFCC only through the Order Management extension's
`dw.order.Invoice`. A merchant who refunds from Stripe's (or any PSP's)
dashboard leaves **no trace in SFCC at all**, so the sweep cannot see it. For
Stripe that residual is covered by Pivota's PSP webhook bridge
(`charge.refunded`), which lands under a different authority and is reconciled
against the platform's own figure. For every other processor it is an open gap:
the order stays `order.paid` in Pivota with no refund against it. A realm
without the Order Management extension has no invoice surface at all — the
sweep logs one INFO line per order and reports no refunds.

## Install

1. Upload `int_pivota_telemetry` and add it to the site's cartridge path.
2. In Business Manager, open **Global Preferences → Feature Switches** and
   enable **Salesforce Commerce API Hook Execution**.
3. Import `metadata/meta/custom-objecttype-definitions.xml` **and**
   `metadata/meta/system-objecttype-extensions.xml` in Business Manager
   (**Administration → Site Development → Import & Export**). The first
   declares the outbox and the sweep cursor; the second adds the three
   once-only markers to `Order`. Without the second import every marker write
   throws and the sweep re-enqueues the same events on every run — the ledger
   would still dedupe them, but the outbox would never stop growing.
4. Connect the SFCC store in Pivota, then call
   `POST /integrations/salesforce-commerce-cloud/{store_id}/telemetry/provision`.
   Save the returned secret; it is shown only when first generated or rotated.
5. Replace `RefArch`, the URL, and the password placeholders in
   `metadata/services.xml`, then import the service definition. The credential
   ID must be `pivota.telemetry.{site_id}`. Create one credential per connected
   site; the cartridge selects the current site's URL and signing secret at
   runtime. Communication logging intentionally remains disabled so event
   bodies and signatures are not written to service logs.
6. `metadata/jobs.xml` is a per-site template holding **two** jobs —
   `PivotaTelemetryDrain-RefArch` and `PivotaSettlementSweep-RefArch`.
   For **every connected site**, duplicate its complete `<job>` element,
   replace `RefArch` in both the job ID and `<context site-id>`, keeping copies
   inside the same `<jobs>` root. Import the combined file and schedule every
   site job. A site-scoped outbox is drained only by a job flow running in that
   exact site context, and the sweep reads its cursor from the same site scope.

   Recommended cadence: **drain every minute; sweep every five minutes, offset
   a few minutes from the drain** (e.g. the drain on the minute, the sweep at
   `:02`, `:07`, …). The two never conflict — the sweep only writes outbox rows
   and the drain sends and deletes them — but running them together wastes a
   window, and a sweep is far heavier than a drain. Raise `MaxOrders` before
   raising the frequency: a realm with a large order volume and a long backlog
   is better served by a bigger batch every five minutes than by a sweep that
   is still running when the next one starts (the job's hanging threshold is
   30 minutes).
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
