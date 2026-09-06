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
| **sweep**, credit `Invoice` with a positive refund | `refund.succeeded` | `refund.succeeded` | that invoice's refunded amount |
| **sweep**, any of the three with a non-positive amount | **nothing** — logged and skipped | — | — |

`order_ref` is always `salesforce_commerce_cloud:<orderNo>`.

### Event keys, and why they are deterministic

The shopper hooks mint a random UUID per event, because a hook fires once and
has no natural key. The sweep does not: it re-examines the same orders on every
tick, so it sends a **deterministic** `event_id`, which the mapper hashes into
the canonical event key:

| Fact | `event_id` |
| --- | --- |
| paid | `order.paid:<orderNo>` |
| cancelled | `order.cancelled:<orderNo>` |
| refund | `refund.succeeded:<invoiceNumber>` |

Two credit invoices on one order are therefore two rows, and a redelivery of
either is a duplicate rather than a second row.

### Once-only, in two independent layers

1. **Order custom attributes** — `pivotaPaidEmittedAt`,
   `pivotaCancelledEmittedAt`, `pivotaRefundedInvoices` (a `set-of-string`,
   capped at 200), shipped in
   `integrations/sfcc-cartridge/metadata/meta/system-objecttype-extensions.xml`.
   Written only **after** a successful enqueue, so a failed enqueue is retried
   next tick instead of being recorded as delivered. This is what stops the
   outbox growing.
2. **The deterministic event key above**, deduped first-write-wins by the
   ledger. This is what stops a *count* being wrong when layer 1 is lost — a
   marker cleared in Business Manager, a restore, an invoice number aged out of
   the capped set, or a marker write that failed after the enqueue succeeded.

Neither layer alone is enough, and neither is a substitute for the other.

### `order.paid`: what the amount and the time actually mean

* The amount is **`Order.totalGrossPrice`**, the order total — not a capture,
  not `PARTPAID`, not the PSP's settled figure. The event carries
  `native_amount_semantics = order_total_gross` so a divergence from the PSP's
  own number is diagnosable rather than invisible. (`order.paid` is the only
  event that carries this key; a refund's amount is the invoice's own.)
* `occurred_at` is the order's **`lastModified`**. SFCC records no settlement
  instant anywhere, so this is the best time available; it equals the transition
  only when nothing touched the order afterwards. This is stated here rather
  than in metadata because the shared
  `ALLOWED_MERCHANT_METADATA_KEYS` allowlist was deliberately not widened for
  it, as with BigCommerce and Wix.
* The amount is **frozen** at the first emission. The ledger dedupes
  first-write-wins on the event key, and that key is the order — so a later
  correction to `totalGrossPrice` does not update the recorded figure.

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
site-scoped, `cursorAt` + `updatedAt`).

* Orders are read `lastModified >= cursorAt`, ordered `lastModified asc`,
  bounded by `MaxOrders` (default 500, max 5000).
* The stored cursor is the run's watermark **rewound by `OverlapMinutes`**
  (default 10), so a tick that died mid-batch, a clock skew, or an order whose
  `lastModified` lands inside the window just closed is re-examined rather than
  lost. Re-examination is free: both once-only layers hold.
* It never moves backwards, and it never steps past an order whose enqueue
  failed in this run (the first such order clamps the watermark).
* With no cursor yet, the first run reaches back `InitialLookbackHours`
  (default 24).

Writing a once-only marker updates the order's `lastModified`, so an order the
sweep just marked is re-examined on the following tick and then skipped without
a write. That converges after one extra pass; it is not a loop.

## What the cartridge never sends

No name, no e-mail, no telephone number, no postal or billing details, no
payment instrument details, no authentication token, no product titles. The
sweep's events carry no line items at all (`items: []`): an order's lines are
not a refund's lines, and a settlement event does not need them. Metadata is
`native_event_name`, `native_status`, `native_site_id`, `native_line_items` (on
the shopper-hook events only), `native_amount_semantics` (on `order.paid` only)
and `webhook_delivery_id` — all already in
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
| `lastModified` is queryable in an `OrderMgr.searchOrders` query string, and `'lastModified asc'` is a valid sort | **ASSUMED** — stated in the programme brief; not re-verified against the search-attribute reference. If it is not queryable the sweep finds nothing and says `scanned=0`, which is a loud, safe failure |
| `Order.getInvoices()` returns a collection of `dw.order.Invoice` | **ASSUMED** — `Invoice` is an Order Management extension surface; the call is wrapped in `try/catch` and a realm without it logs one INFO line and reports no refunds |
| `Invoice.invoiceNumber`, `Invoice.status`, `Invoice.type`, `Invoice.grandTotal`, `Invoice.refundedAmount` | **ASSUMED** — same family. Absent members read as `undefined` and the invoice is skipped, never crash-emitted |
| `Invoice.INVOICE_TYPE_CREDIT` exists as a constant with that exact name | **NOT RELIED ON.** The author could not confirm this spelling (the type constants may be `TYPE_RETURN` / `TYPE_APPEASEMENT` / `TYPE_RETURN_CASE`), and a mis-named constant reads as `undefined`, which would match no invoice and silently report zero refunds. So the sweep classifies by lowercase substring of `String(invoice.type)` — `credit`, `return`, `appeasement` — and only as a FALLBACK: the primary rule is `refundedAmount > 0`, which needs no type at all |
| `Invoice.status` string contains `paid` when the invoice settled | **ASSUMED** — matched case-insensitively, and only on the fallback path; `refundedAmount > 0` is trusted on its own |
| A refund made in the PSP's own dashboard leaves no trace in SFCC | VERIFIED — SFCC learns of it only if an integration writes an invoice back. Documented gap, below |
| Custom attributes on `Order` require `metadata/meta/system-objecttype-extensions.xml` and a `Transaction` to write | VERIFIED — standard SFCC |
| Writing a custom attribute updates the object's `lastModified` | **ASSUMED** — the sweep is written to converge either way; if it does not, the order simply leaves the window sooner |
| A `set-of-string` custom attribute reads back as a native `Array` | **ASSUMED** — the sweep accepts an `Array` *or* a `dw.util.Collection` rather than betting on one |
| `CustomObjectMgr.getCustomObject` / `createCustomObject` / `queryCustomObjects`, `Transaction.wrap`, `dw.system.Status`, `dw.system.Logger` | VERIFIED — already used by the shipped drain job |
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
refunds=… skipped=… failed=…`) into the realm's own job log, where only the
merchant can see them.

**Nothing verifies the cartridge runs.** `tests/test_sfcc_cartridge_contract.py`
is text matching over unexecuted JavaScript. The first real execution of
`SweepPivotaSettlements.js` will be on a merchant's sandbox.
