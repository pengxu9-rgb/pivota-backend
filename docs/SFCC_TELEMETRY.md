# Salesforce B2C Commerce (SFCC) commerce telemetry

SFCC has **no outbound commerce webhooks**. There is no subscription API for
order or payment lifecycle, no signed delivery, no callback registry — the
platform's extension points are OCAPI/SCAPI hooks and job steps that run inside
the merchant's own realm. So, exactly like PrestaShop, Pivota ships the sender:

| Piece | Where |
| --- | --- |
| The cartridge the merchant installs | `integrations/sfcc-cartridge/int_pivota_telemetry/` |
| The receiver | `routes/sfcc_events.py` (`POST /webhooks/salesforce-commerce-cloud/{store_id}`) |
| The mapper | `services/sfcc_event_adapter.py` |
| Secret provisioning | `POST /integrations/salesforce-commerce-cloud/{store_id}/telemetry/provision` |
| Wire contract test | `tests/test_sfcc_cartridge_contract.py` |
| Ledger end-to-end | `tests/test_sfcc_ledger_end_to_end.py` |

**The cartridge JavaScript is unlinted and unexecuted.** There is no SFCC
runtime in this repo and no `node` harness for it; nothing in CI runs a line of
it. `tests/test_sfcc_cartridge_contract.py` is a text-level test and is the only
thing holding the two sides together.

## Why a sweep exists at all

**SFCC fires nothing on settlement.** `Order.paymentStatus`
(`PAYMENT_STATUS_NOTPAID` / `PARTPAID` / `PAID`) is set by the merchant's
payment-processor cartridge, by an OMS integration, or by a Business Manager
user, and no hook, no OCAPI/SCAPI event and no notification accompanies the
transition. `Order.status` becoming `ORDER_STATUS_CANCELLED` is the same. A
credit `Invoice` appearing under an order is the same.

Before this change the SFCC funnel therefore stopped at `payment.authorized`:
five shopper hooks covered basket → checkout → order created → authorization,
and nothing at all said whether the money ever arrived.

The `PivotaSettlementSweep` job step
(`int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js`) closes
that by **observing state on a cursor** instead of waiting for an event. It
writes into the same `PivotaTelemetryOutbox` custom object the shopper hooks
use, so `DrainPivotaTelemetry` signs, batches, retries and deletes them
unchanged.

### What it must never do

It does **not** register on `dw.order.payment.capture` or
`dw.order.payment.refund`. Those are `dw.order.hooks.PaymentHooks`
*implementation* extension points: the merchant's PSP cartridge implements them
to actually move the money, and the hook manager resolves a single
implementation by cartridge-path order. A telemetry "observer" registered there
could shadow the real processor and break capture — a telemetry integration must
never be able to do that. `tests/test_sfcc_cartridge_contract.py` fails if
either name (or anything from that family) appears in `hooks.json`.

## The wire

```
POST /webhooks/salesforce-commerce-cloud/{store_id}
X-Pivota-SFCC-Signature: sha256=<hex hmac(secret, timestamp + "." + body)>
X-Pivota-SFCC-Timestamp: <unix seconds>
X-Pivota-SFCC-Delivery-Id: <random per batch>
X-Pivota-SFCC-Site-Id: <the connected site id>

{"events": [ … 1..100 … ]}
```

Auth chain, in order:

1. 1 MB body cap;
2. an **active** `platform = 'salesforce_commerce_cloud'` store row for
   `{store_id}`;
3. a `telemetry_signing_secret` **and** a `site_id` in that row's credential
   JSON;
4. the `X-Pivota-SFCC-Site-Id` header equals that `site_id`;
5. timestamp within ±300 s;
6. constant-time HMAC-SHA256 over `timestamp + "." + body`;
7. the JSON parses;
8. `identify` + the `platform` rate-limit tier;
9. 1–100 events, **each of which must also carry that same `site_id` inside the
   signed body**;
10. mapped, ingested with `write_path="sfcc_cartridge"` /
    `agent_identity_confidence="platform_asserted"` → authority `platform`.

Steps 2–3 answer with one message, so a caller never learns which it hit.

### Two hardening fixes in this change

**Every constant-time comparison is on bytes.** `hmac.compare_digest` raises
`TypeError` when either `str` holds a non-ASCII code point, and Starlette
decodes header bytes as latin-1 — so a signature header of `sha256=\xe9…`
reached the comparison as a `str` it could not accept and became an
**unauthenticated 500** (recorded by the ingress envelope as `error`, not
`unauthenticated`). Three comparisons had the shape: the signature, the site-id
**header**, and the per-event `site_id` **inside the signed JSON**, which can
hold any code point at all. The signature compare encodes ASCII inside a `try`
(a hex digest is ASCII by construction, so a value that cannot encode cannot
match); the two site-id compares use UTF-8 bytes, which never raise and are
exact for any pair. Every malformed value is now the one 401.

**A batch whose every event was rejected reports `rejected`, not `ignored`.**
`TelemetryIngress.record_result` short-circuits on `status: "ignored"` and
records exactly one `ignored` event, so the `rejected` count vanished from the
metrics entirely. The route now returns the normal summary shape with
`accepted: 0` and `status: "rejected"` in that case, which makes the ingress
walk its accepted / duplicate / ignored / rejected fields. Same fix, same
reason, as `routes/prestashop_webhooks.py`.

An unsupported event type is still counted `ignored`; a malformed event is
counted `rejected` and its siblings still ingest. Both answer 2xx so the
cartridge's outbox deletes the batch rather than retrying forever.

## Mapping

| Source | Native event | Canonical | Amount |
| --- | --- | --- | --- |
| `dw.ocapi.shop.basket.afterPOST` | `basket.created` | `cart.created` | basket total |
| `dw.ocapi.shop.basket.items.afterPOST` | `basket.item_added` | `cart.item_added` | basket total |
| `dw.ocapi.shop.order.beforePOST` | `checkout.submitted` | `checkout.submitted` | basket total |
| `dw.ocapi.shop.order.afterPOST` | `order.created` | `order.created` | `totalGrossPrice` |
| `dw.ocapi.shop.order.payment_instrument.afterPOST` | `payment.authorized` / `payment.declined` | same | the **instrument** amount, never the order total (split tender) |
| **sweep**, `paymentStatus == PAID` | `order.paid` | `order.paid` | `Order.totalGrossPrice` |
| **sweep**, `status == CANCELLED` | `order.cancelled` | `order.cancelled` | none |
| **sweep**, credit `Invoice` whose cumulative refunded amount ROSE | `refund.succeeded` | `refund.succeeded` | the **delta** since the last observation of that invoice |
| **sweep**, any of the three with a non-positive amount (or a cumulative refund figure that fell) | **nothing** — logged and skipped | — | — |

`order_ref` is always `salesforce_commerce_cloud:<orderNo>`.

### `refund.succeeded` is a DELTA, and its key says how far it counted

`Invoice.refundedAmount` is **cumulative per invoice**: SFCC does not create a
second invoice for a second partial refund against the same one, it raises that
invoice's figure. Keyed on the invoice number alone — as the first revision of
this sweep was — a second partial refund was lost **twice over**: the once-only
marker skipped the invoice, and even with the marker gone the ledger deduped the
new event against the first observation's key.

So each tick compares the invoice's current cumulative figure against the last
one this cartridge observed for it, and:

* enqueues the **difference**, never the cumulative total, as the amount;
* keys the event on `<invoiceNumber>:<the new cumulative total>`, which is also
  its `refund_id`, so a second partial refund is a second ledger row and a
  redelivery of either is still a duplicate;
* stores that same string as the once-only marker, so the marker records both
  *which* invoice and *how far* it has been reported;
* skips — loudly, at WARN, emitting nothing — a cumulative figure that
  **fell**. A negative amount is refused by the mapper anyway, and re-sending
  the lower figure under a new key would *add* refunded money.

The funnel is what makes the deltas add up: it takes `max(amount)` per
`refund_id` and **sums across distinct `refund_id`s** inside one authority, so
`10.00` then `15.00` on one invoice reports `25.00` refunded, once.
`tests/test_sfcc_ledger_end_to_end.py` proves that through the real route and
the real funnel, redelivery included.

Each row carries `native_amount_semantics = invoice_cumulative_delta`, because a
delta read as an invoice total would over-report exactly the invoice this
scheme exists to handle.

**Two bounds, and the residual each leaves.** Each tick reads at most the first
**50 invoices** of an order, and the marker set holds at most one marker per
invoice (a new observation replaces that invoice's marker) capped at **200**.

* An order with more than 50 invoices has the rest ignored entirely — their
  refunds are never reported. Nothing warns about it.
* Because of that read cap the 200-marker cap is only reachable when the invoice
  collection's membership changes across ticks. If a marker ever *is* evicted,
  re-observing that invoice at an unchanged cumulative figure is free (same key,
  deduped); an invoice refunded *again* after its marker was evicted reports its
  whole cumulative figure as the delta and over-reports that invoice.

### Event keys, and why they are deterministic

The shopper hooks mint a random UUID per event, because a hook fires once and
has no natural key. The sweep does not: it re-examines the same orders on every
tick, so it sends a **deterministic** `event_id`, which the mapper hashes into
the canonical event key:

| Fact | `event_id` |
| --- | --- |
| paid | `order.paid:<orderNo>` |
| cancelled | `order.cancelled:<orderNo>` |
| refund | `refund.succeeded:<invoiceNumber>:<cumulative>` |

Two credit invoices on one order are therefore two rows, two partial refunds
against one invoice are also two rows, and a redelivery of any of them is a
duplicate rather than a second row.

### Once-only, in two independent layers

1. **Order custom attributes** — `pivotaPaidEmittedAt`,
   `pivotaCancelledEmittedAt`, `pivotaRefundedInvoices` (a `set-of-string` of
   `<invoiceNumber>:<cumulative>` markers, one per invoice, capped at 200),
   shipped in
   `integrations/sfcc-cartridge/metadata/meta/system-objecttype-extensions.xml`.
   Written only **after** a successful enqueue, so a failed enqueue is retried
   next tick instead of being recorded as delivered. This is what stops the
   outbox growing.
2. **The deterministic event key above**, deduped first-write-wins by the
   ledger. This is what stops a *count* being wrong when layer 1 is lost — a
   marker cleared in Business Manager, a restore, an invoice marker aged out of
   the capped set, or a marker write that failed after the enqueue succeeded.

Neither layer alone is enough, and neither is a substitute for the other.

A third thing follows from layer 2 and is easy to get wrong: because the ids are
deterministic, an **occupied outbox key** is normal. `CustomObjectMgr.createCustomObject`
*raises* on a duplicate key, and the drain keeps an undelivered row for up to
seven days — so re-enqueuing an event that is still queued (a marker that did
not stick, or the cursor's overlap window coming round) is the ordinary shape of
a Pivota outage. `Telemetry.enqueue` therefore looks the key up first and treats
an existing row as **success**, logged at debug. Without that, the re-enqueue
threw, the sweep counted the order failed, held its cursor, and
`MaxFailureLagHours` eventually abandoned it — during the outage.

### `order.paid`: what the amount and the time actually mean

* The amount is **`Order.totalGrossPrice`**, the order total — not a capture,
  not `PARTPAID`, not the PSP's settled figure. The event carries
  `native_amount_semantics = order_total_gross` so a divergence from the PSP's
  own number is diagnosable rather than invisible. (`refund.succeeded` carries
  the same key with `invoice_cumulative_delta`; no other SFCC event carries it.)
* `occurred_at` is the order's **`lastModified`**. SFCC records no settlement
  instant anywhere, so this is the best time available; it equals the transition
  only when nothing touched the order afterwards. This is stated here rather
  than in metadata because the shared
  `ALLOWED_MERCHANT_METADATA_KEYS` allowlist was deliberately not widened for
  it, as with BigCommerce and Wix.
* The amount is **frozen** at the first emission. The ledger dedupes
  first-write-wins on the event key, and that key is the order — so a later
  correction to `totalGrossPrice` does not update the recorded figure.

### Two funnel consequences worth stating

**A free (zero-total) order never reaches the paid stage.** `order.paid` is
skipped when `totalGrossPrice` is not positive — the zero-amount rule below —
so a 100%-discounted or zero-priced order stays at `order.created` in the funnel
for good, even though SFCC may well mark it `PAID`. It is not a lost settlement;
there is no money to report.

**A paid-then-cancelled order keeps its paid GMV.** `order.cancelled` moves no
money and carries none, and the funnel never subtracts on a cancellation — paid
amounts are `max` per order and refunds are counted only from
`refund.succeeded`. So an order that settled and was then cancelled still counts
its full amount in `paid_amount_cents_by_currency` unless a credit `Invoice`
appears for it. That is a pre-existing property of
`merchant_commerce_event_funnel_service`, not new here; SFCC is simply the first
platform where the sweep makes it reachable, because it is the first to report
both facts.

### Why no per-capture `payment.succeeded`

Read `services/merchant_commerce_event_funnel_service.py`:
`_PAID_EVENTS = {"payment.succeeded", "order.paid"}` feed one `paid` stage set,
and the money is `paid_amounts[currency][order_key] = max(current, amount_cents)`
per resolved order — captures do **not** sum. A second event per capture would
therefore contribute no money and no stage, only rows. `order.paid` alone is
what the funnel counts.

### Money events must carry a positive amount and a currency

`order.paid`, `payment.succeeded` and `refund.succeeded` are rejected
(`ValueError`, counted `rejected`, no ledger row) when the amount is missing,
zero or non-positive, or when there is no currency.

This is not fastidiousness. Dedupe is first-write-wins on the event key, and
that key is derived from the order or the credit invoice — so a
`refund.succeeded` carrying `amount_cents = 0` for invoice `INV-77` is not a
harmless under-report, it is a **permanent shadow**: the real 10.50 for that
same invoice can never be written afterwards. The currency rule has the same
shape: the funnel drops a money row with no currency, but the key is already
occupied by then. `tests/test_sfcc_ledger_end_to_end.py` proves the zero row
never lands and the later correct figure still does.

The cartridge enforces the same rule one step earlier — it logs and skips rather
than enqueuing, and does **not** write the once-only marker, so a corrected
total is still reported on a later tick.

### The cursor

Stored in a `PivotaTelemetrySweepCursor` custom object (key `settlement`,
`cursorAt` + `updatedAt`).

The custom type is declared **`<storage-scope>site</storage-scope>`**, the same
scope as `PivotaTelemetryOutbox`, and that is load-bearing rather than
incidental: `OrderMgr.searchOrders` only ever sees the **current site's** orders,
so a cursor shared across sites would let one site's watermark hide another
site's orders entirely. Because the scope is per site, the object id is the bare
literal `settlement` rather than being keyed on
`require('dw/system/Site').getCurrent().getID()`; if the scope is ever changed to
`organization`, the id must become site-keyed in the same commit.
`tests/test_sfcc_cartridge_contract.py` pins the scope of both custom types.

* Orders are read `lastModified >= cursorAt`, ordered `lastModified asc`,
  bounded by `MaxOrders` (default 500, max 5000).
* The stored cursor is the run's watermark **rewound by `OverlapMinutes`**
  (default 10), so a tick that died mid-batch, a clock skew, or an order whose
  `lastModified` lands inside the window just closed is re-examined rather than
  lost. Re-examination is free: both once-only layers hold.
* It never moves backwards, and it never steps past an order whose enqueue
  failed in this run (the first such order clamps the watermark) — **up to
  `MaxFailureLagHours`**, below.
* With no cursor yet, the first run reaches back `InitialLookbackHours`
  (default 24).

#### `MaxFailureLagHours`: why the failure clamp is bounded

The failure clamp on its own is a **silent site-wide outage waiting to happen**.
An order that fails on *every* tick — unreadable custom attributes, an exception
raised inside `buildEvent`, an invoice member that throws — pins the clamp, and
therefore the cursor, to its own `lastModified` for good. Each run then re-reads
the same window, and because `MaxOrders` bounds the run the sweep never reaches
the orders behind it. Every later settlement, cancellation and refund for the
whole site is lost, and the only symptom is a `failed=` count in the merchant's
own job log — which, per *Not built* below, nothing on the Pivota side reads.

So the clamp is bounded by a job parameter, `MaxFailureLagHours` (default 24,
clamped 1–168 in both `steptypes.json` and the code): the clamp may hold the
cursor at most that far behind the **newest order the run observed** — newest
observed, not the watermark, because a run in which every order failed has no
watermark and is exactly the run that needs the bound.

When the bound overrides the clamp, the failing orders are **abandoned**: their
`order.paid` / `order.cancelled` / `refund.succeeded` facts are never emitted,
and Pivota will never learn them. That loss is deliberate — a named loss beats a
silent stall — and it is logged at **ERROR** naming the order numbers (up to 50
per run) so support can replay them by hand. A transient failure well inside the
bound is still simply retried on the next tick.

The step also **returns `Status(ERROR, 'ABANDONED')`** on such a run (the code is
declared in `steptypes.json`, and the run's counts, including `abandoned=`, are
in the status message). Business Manager fires a job notification on a non-OK
step status and on nothing else, so reporting `OK` would leave that ERROR line
sitting in the merchant's own job log, which — per *Not built* below — nothing
on the Pivota side reads either. The step is declared `enforce-restart="false"`
in `metadata/jobs.xml`, so the next scheduled tick still runs normally.

If the bound moves the cursor while **no** recorded failure was actually behind
it, nothing was abandoned: that run logs a WARN instead, claims no loss, and
returns `OK`.

Writing a once-only marker updates the order's `lastModified`, so an order the
sweep just marked is re-examined on the following tick and then skipped without
a write. That converges after one extra pass; it is not a loop.

## What the cartridge never sends

No name, no e-mail, no telephone number, no postal or billing details, no
payment instrument details, no authentication token, no product titles. The
sweep's events carry no line items at all (`items: []`): an order's lines are
not a refund's lines, and a settlement event does not need them. Metadata is
`native_event_name`, `native_status`, `native_site_id`, `native_line_items` (on
the shopper-hook events only), `native_amount_semantics` (on `order.paid` and
`refund.succeeded`) and `webhook_delivery_id` — all already in
`ALLOWED_MERCHANT_METADATA_KEYS`; **the allowlist was not widened.**

## SFCC facts: verified vs assumed

"VERIFIED" here means: consistent with the B2C Commerce Script API as the author
knows it, and consistent with the rest of the cartridge, which has been running
in this repo since the hook-only phase. **Nothing in this table was executed.**
There is no SFCC runtime in this repo, no sandbox was called, and the vendor
documentation was not re-fetched while writing this change. Anything below
marked ASSUMED is a claim a merchant install will be the first thing to test.

| Fact | Status |
| --- | --- |
| SFCC fires no hook/event/notification on a `paymentStatus` or `status` transition | VERIFIED — there is no such extension point in `dw.order.hooks.*` or the OCAPI/SCAPI hook list |
| `Order.paymentStatus` is an `EnumValue`; `PAYMENT_STATUS_NOTPAID` / `PARTPAID` / `PAID` are `Order` constants | VERIFIED — `dw.order.Order` |
| `Order.status` is an `EnumValue`; `ORDER_STATUS_CANCELLED` is an `Order` constant | VERIFIED — `dw.order.Order` |
| `Order.totalGrossPrice` / `currencyCode` / `orderNo` / `lastModified` | VERIFIED — `dw.order.Order`, `lastModified` from `dw.object.ExtensibleObject`/`PersistentObject` |
| `dw.value.Money` has `available` and `value` | VERIFIED — `dw.value.Money` |
| `dw.order.hooks.PaymentHooks` defines `dw.order.payment.capture(invoice)` / `.refund(invoice)` as IMPLEMENTATION extension points resolved by cartridge-path order | VERIFIED — the reason this cartridge must not register on them |
| `OrderMgr.searchOrders(query, sort, args…)` returns a `SeekableIterator` that must be `close()`d | VERIFIED — `dw.order.OrderMgr` |
| `lastModified` is queryable in an `OrderMgr.searchOrders` query string, and `'lastModified asc'` is a valid sort | **ASSUMED** — stated in the programme brief; not re-verified against the search-attribute reference. If the attribute is not queryable the call **throws**: `searchOrders` is inside the guarded block, so the step logs `Pivota settlement sweep failed:` and returns `Status.ERROR` without advancing its cursor. Loud and safe, but it is a job failure, not a quiet `scanned=0` |
| `Order.getInvoices()` returns a collection of `dw.order.Invoice` | **ASSUMED** — `Invoice` is an Order Management extension surface; the call is wrapped in `try/catch` and a realm without it logs one INFO line and reports no refunds |
| `Invoice.invoiceNumber`, `Invoice.status`, `Invoice.grandTotal`, `Invoice.refundedAmount` | **ASSUMED** — same family. Absent members read as `undefined` and the invoice is skipped, never crash-emitted |
| `Invoice.invoiceType` is the invoice's type property | **ASSUMED, and read FIRST** — the documented accessor is `getInvoiceType()`, so the script-API property is `invoiceType`. Not executed, hence assumed |
| `Invoice.type` is *also* a readable type property | **ASSUMED FALSE / very likely `undefined`** — there is no `getType()` on `dw.order.Invoice`. It is read only as the SECOND choice, which costs nothing if it is absent. An earlier revision of this cartridge read `type` **alone**, which made the whole credit-invoice fallback dead code against a real credit invoice: `cumulativeRefundAmount`'s grand-total path was unreachable and the "skipped, amount is not positive" WARN could never fire |
| `Invoice.INVOICE_TYPE_CREDIT` exists as a constant with that exact name | **NOT RELIED ON.** The author could not confirm this spelling (the type constants may be `TYPE_RETURN` / `TYPE_APPEASEMENT` / `TYPE_RETURN_CASE`), and a mis-named constant reads as `undefined`, which would match no invoice and silently report zero refunds. So the sweep classifies by lowercase substring of `String(invoice.invoiceType \|\| invoice.type)` — `credit`, `return`, `appeasement` — and only as a FALLBACK: the primary rule is `refundedAmount > 0`, which needs no type at all |
| `Invoice.status` string contains `paid` when the invoice settled | **ASSUMED** — matched case-insensitively, and only on the fallback path; `refundedAmount > 0` is trusted on its own |
| A refund made in the PSP's own dashboard leaves no trace in SFCC | VERIFIED — SFCC learns of it only if an integration writes an invoice back. Documented gap, below |
| Custom attributes on `Order` require `metadata/meta/system-objecttype-extensions.xml` and a `Transaction` to write | VERIFIED — standard SFCC |
| Writing a custom attribute updates the object's `lastModified` | **ASSUMED** — the sweep is written to converge either way; if it does not, the order simply leaves the window sooner |
| A `set-of-string` custom attribute reads back as a native `Array` | **ASSUMED** — the sweep accepts an `Array` *or* a `dw.util.Collection` rather than betting on one |
| A `set-of-string` custom attribute can be WRITTEN as a native JS `Array` (`order.custom.pivotaRefundedInvoices = ['INV-1:10.00']`) | **ASSUMED** — the read side is defensive, the write side is not: it assigns a plain `Array`. If the platform demands a `dw.util.Collection` (or an `ArrayList`) instead, every refund-marker write throws inside its `Transaction`, `sweepOrder` counts the order failed, and the refunds are re-enqueued on every tick — deduped by the ledger, but the outbox never settles and `MaxFailureLagHours` eventually abandons the order. First thing to check on a merchant sandbox |
| `String(enumValue)` on an `EnumValue` yields its VALUE, not its display value | **ASSUMED** — used only on `Invoice.status` in the credit-invoice fallback, which asks whether the lowercased text contains `paid` and not `not`. If it yields a localized display value instead, that fallback stops classifying and the invoice is skipped with the "amount is not positive" WARN; the primary `refundedAmount > 0` path does not read status at all. `Order.paymentStatus` / `Order.status` are compared on `.value` against `Order` constants, never on text, so they are unaffected |
| `CustomObjectMgr.getCustomObject` / `createCustomObject` / `queryCustomObjects`, `Transaction.wrap`, `dw.system.Status`, `dw.system.Logger` | VERIFIED — already used by the shipped drain job |
| `CustomObjectMgr.createCustomObject` raises when the key is already taken | VERIFIED — the reason `Telemetry.enqueue` looks the key up first and treats an existing row as success |
| A job step's `Status` code other than `OK` is what Business Manager notifies on | **ASSUMED** — the basis for returning `Status(ERROR, 'ABANDONED')`; the notification itself is configured per job in Business Manager and cannot be asserted from here |
| A job step's `parameters` reach `execute` as an object keyed by `@name` | VERIFIED — already relied on by `PivotaTelemetryDrain`'s `BatchSize` |

## Not built

**No PSP-dashboard refund coverage.** Refund records exist in SFCC only through
the Order Management extension's `Invoice`. A merchant who refunds from Stripe's
(or any PSP's) dashboard leaves nothing for the sweep to see. For Stripe that
residual is covered by the PSP terminal-event bridge (`charge.refunded`), which
lands under a different authority and is reconciled against the platform's
figure by `_refunded_amounts_by_currency`. For every other processor it is an
open gap: the order stays `order.paid` in Pivota with no refund against it.

**No partial-settlement reporting.** `PAYMENT_STATUS_PARTPAID` emits nothing.
Emitting `order.paid` there would freeze the order's key at a partial figure and
make the full settlement unwritable — the same permanent-shadow shape the
zero-amount rule exists to prevent. A partly-settled order is invisible to the
funnel until it reaches `PAID`.

**No receiver-side staleness signal.** Nothing on the Pivota side notices an
SFCC store whose sweep stopped running, whose cursor is stuck, or whose job was
never scheduled. The cartridge logs its counts (`scanned=… paid=… cancelled=…
refunds=… skipped=… failed=… abandoned=…`) into the realm's own job log, where
only the merchant can see them. The one exception is an abandonment: that also
makes the step report `Status(ERROR, 'ABANDONED')`, which Business Manager can
notify the merchant on — still the merchant, not Pivota.

**Nothing verifies the cartridge runs.** `tests/test_sfcc_cartridge_contract.py`
is text matching over unexecuted JavaScript. The first real execution of
`SweepPivotaSettlements.js` will be on a merchant's sandbox.
