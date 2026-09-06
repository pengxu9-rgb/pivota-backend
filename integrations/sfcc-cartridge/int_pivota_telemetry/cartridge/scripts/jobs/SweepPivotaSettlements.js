'use strict';

/**
 * PivotaSettlementSweep — the settlement/refund half of the SFCC funnel.
 *
 * SFCC B2C fires NOTHING on settlement. `Order.paymentStatus` is written by the
 * merchant's payment-processor cartridge, by an OMS integration, or by a
 * Business Manager user, and no hook, no OCAPI/SCAPI event and no notification
 * accompanies the transition. Same for `Order.status` becoming CANCELLED, and
 * same for a credit `Invoice` appearing under an order. The only way to observe
 * any of it is to look.
 *
 * So this job step LOOKS: it walks the orders modified since a persisted
 * cursor and emits the money facts it finds, into the same outbox the shopper
 * hooks use, so `DrainPivotaTelemetry` signs, batches and retries them
 * unchanged.
 *
 * What it deliberately does NOT do: register on `dw.order.payment.capture` or
 * `dw.order.payment.refund`. Those are `dw.order.hooks.PaymentHooks`
 * IMPLEMENTATION extension points — the merchant's PSP cartridge implements
 * them to actually move the money, and the hook manager resolves a single
 * implementation by cartridge-path order. A telemetry "observer" registered
 * there could shadow the real processor and break capture. Telemetry must never
 * be able to do that, so it observes state instead of intercepting it.
 *
 * Once-only has two independent layers:
 *
 *   1. Order custom attributes (`pivotaPaidEmittedAt`, `pivotaCancelledEmittedAt`,
 *      `pivotaRefundedInvoices`) written only AFTER a successful enqueue. This
 *      is what stops the sweep re-emitting on every tick.
 *   2. A DETERMINISTIC `event_id` per fact — `order.paid:<orderNo>`,
 *      `order.cancelled:<orderNo>`,
 *      `refund.succeeded:<invoiceNumber>:<cumulative>`. The receiver hashes it
 *      into the canonical event key, and the ledger dedupes first-write-wins on
 *      that key. So an enqueue that succeeded while the marker write failed (or
 *      a marker that aged out of the capped refund set) costs at most a
 *      redundant delivery, never a duplicate ledger row — and while the row is
 *      still in the local outbox it does not even cost that, because
 *      `Telemetry.enqueue` treats an occupied outbox key as success rather than
 *      raising on it. The random UUID `Telemetry.buildEvent` mints for shopper
 *      hooks would NOT have the dedupe property, which is why these events
 *      override it.
 *
 * Refunds are emitted as DELTAS, and that is the reason the refund key carries
 * a cumulative figure rather than the invoice number alone. `Invoice.refundedAmount`
 * is the money returned against that invoice SO FAR: a second partial refund
 * against the same invoice RAISES it instead of creating a second invoice. Keyed
 * on the invoice number alone, the second partial refund was permanently lost
 * twice over — the once-only marker skipped it, and even without the marker the
 * ledger deduped it against the first observation's key. So each tick compares
 * the invoice's current cumulative figure with the last one this cartridge
 * observed (stored in the marker) and enqueues the DIFFERENCE under a key
 * qualified by the new cumulative total. The funnel takes `max(amount)` per
 * `refund_id` and SUMS across distinct `refund_id`s inside one authority, so
 * `10.00` then `15.00` reports `25.00` refunded, and a redelivery of either
 * still dedupes.
 *
 * Emitting `order.paid` and NOT a per-capture `payment.succeeded` is a decision
 * taken against the funnel, not a shortcut. In
 * `services/merchant_commerce_event_funnel_service.py`,
 * `_PAID_EVENTS = {"payment.succeeded", "order.paid"}` feed one `paid` stage
 * set, and the paid amount is `max(current, amount_cents)` per resolved order —
 * captures do not sum. A second event per capture would therefore add no money
 * and no stage, only rows.
 */

var CustomObjectMgr = require('dw/object/CustomObjectMgr');
var Logger = require('dw/system/Logger');
var Order = require('dw/order/Order');
var OrderMgr = require('dw/order/OrderMgr');
var Status = require('dw/system/Status');
var Transaction = require('dw/system/Transaction');
var Telemetry = require('*/cartridge/scripts/pivota/Telemetry');

// The cursor id is the bare literal `settlement` — NOT keyed on the site id —
// and that is only correct because `PivotaTelemetrySweepCursor` is declared
// `<storage-scope>site</storage-scope>` in
// metadata/meta/custom-objecttype-definitions.xml, exactly like the
// `PivotaTelemetryOutbox` type this job feeds. A site-scoped custom object is
// resolved within the current site, so each site gets its own `settlement`
// row. That has to match the data: `OrderMgr.searchOrders` only ever sees the
// current site's orders, so a cursor shared across sites would let one site's
// watermark hide another site's orders. If the storage scope is ever changed
// to `organization`, this id MUST become
// 'settlement:' + require('dw/system/Site').getCurrent().getID().
// `tests/test_sfcc_cartridge_contract.py` pins the scope of both types.
var CURSOR_TYPE = 'PivotaTelemetrySweepCursor';
var CURSOR_ID = 'settlement';

var DEFAULT_MAX_ORDERS = 500;
var MAX_MAX_ORDERS = 5000;
// The cursor is always rewound by this much, so a tick that dies mid-batch,
// a clock skew between the app server and the database, or an order whose
// `lastModified` lands inside the window we just closed is re-examined rather
// than lost. Re-examination is free: the custom-attribute markers and the
// deterministic event ids both hold.
var DEFAULT_OVERLAP_MINUTES = 10;
var MAX_OVERLAP_MINUTES = 1440;
var DEFAULT_LOOKBACK_HOURS = 24;
var MAX_LOOKBACK_HOURS = 720;
// How far behind the newest order this run saw the failure clamp is allowed to
// hold the cursor. Past this, the failing order is abandoned so the sweep can
// reach newer orders again. See the comment at the clamp in `execute`.
var DEFAULT_MAX_FAILURE_LAG_HOURS = 24;
var MIN_MAX_FAILURE_LAG_HOURS = 1;
var MAX_MAX_FAILURE_LAG_HOURS = 168;
// The abandonment log names order numbers so support can replay them by hand.
// It is bounded so one catastrophic run cannot write an unbounded log line.
var MAX_REPORTED_FAILURES = 50;
var MAX_INVOICES_PER_ORDER = 50;
// A set-of-string cannot grow without bound. Each marker is
// `<invoiceNumber>:<last observed cumulative refunded amount>` and there is at
// most ONE per invoice — a new observation REPLACES that invoice's marker
// rather than appending — so this caps the number of refunded invoices per
// order, not the number of observations.
//
// An order that exceeds it loses its oldest markers. A re-observation of a lost
// marker at an UNCHANGED cumulative figure is harmless: the key is the same, so
// the ledger dedupes it. A lost marker whose invoice is refunded again AFTER
// the loss is the one residual — it reports the whole cumulative figure as the
// delta and over-reports that invoice. `MAX_INVOICES_PER_ORDER` reads only the
// first 50 invoices of an order per tick, so reaching 200 markers at all takes
// an invoice collection whose membership changes between ticks. Both bounds and
// what they leave behind are written down in docs/SFCC_TELEMETRY.md rather than
// papered over.
var MAX_REFUND_MARKERS = 200;

var PAID_MARKER = 'pivotaPaidEmittedAt';
var CANCELLED_MARKER = 'pivotaCancelledEmittedAt';
var REFUND_MARKER = 'pivotaRefundedInvoices';

// Invoice type tokens that mean "money going back to the shopper". Matched as
// lowercase substrings of `invoice.type` rather than against class constants:
// the credit/return/appeasement constant names are an Order Management
// extension surface this cartridge cannot import safely on a realm without
// it, and a missing constant would be `undefined` — which equals nothing and
// would silently match no invoice at all.
var CREDIT_TYPE_TOKENS = ['credit', 'return', 'appeasement'];

var logger = Logger.getLogger('pivota', 'settlementSweep');

function bounded(value, fallback, low, high) {
    var parsed = Number(value === null || value === undefined ? fallback : value);
    if (!isFinite(parsed)) {
        return fallback;
    }
    return Math.max(low, Math.min(Math.floor(parsed), high));
}

function isoOf(date) {
    try {
        if (date && typeof date.getTime === 'function') {
            return new Date(date.getTime()).toISOString();
        }
    } catch (error) {
        logger.error('Pivota sweep could not read a timestamp: {0}', error.message);
    }
    return new Date().toISOString();
}

function moneyValue(money) {
    return money && money.available ? Number(money.value) : null;
}

function enumText(value) {
    if (!value) {
        return null;
    }
    if (value.displayValue) {
        return String(value.displayValue);
    }
    return String(value);
}

function cursorObject() {
    var existing = CustomObjectMgr.getCustomObject(CURSOR_TYPE, CURSOR_ID);
    if (existing) {
        return existing;
    }
    var created = null;
    Transaction.wrap(function () {
        created = CustomObjectMgr.createCustomObject(CURSOR_TYPE, CURSOR_ID);
    });
    return created;
}

function readCursor(object, lookbackHours) {
    var stored = object && object.custom.cursorAt;
    if (stored && typeof stored.getTime === 'function') {
        return new Date(stored.getTime());
    }
    var fallback = new Date();
    fallback.setTime(fallback.getTime() - lookbackHours * 60 * 60 * 1000);
    return fallback;
}

function writeCursor(object, watermark, overlapMinutes) {
    if (!object || !watermark) {
        return null;
    }
    var next = new Date(watermark.getTime() - overlapMinutes * 60 * 1000);
    Transaction.wrap(function () {
        var stored = object.custom.cursorAt;
        // Never move backwards: a re-run with a smaller batch must not rewind
        // the cursor past work that already completed.
        if (!stored || stored.getTime() < next.getTime()) {
            object.custom.cursorAt = next;
        }
        object.custom.updatedAt = new Date();
    });
    return next;
}

function refundMarkers(order) {
    var raw = order.custom[REFUND_MARKER];
    var list = [];
    if (!raw) {
        return list;
    }
    // A set-of-string reads back as a native Array on some API versions and as
    // a dw.util.Collection on others. Accept either rather than betting.
    if (typeof raw.length === 'number' && typeof raw.iterator !== 'function') {
        for (var index = 0; index < raw.length; index += 1) {
            list.push(String(raw[index]));
        }
        return list;
    }
    if (typeof raw.iterator === 'function') {
        var iterator = raw.iterator();
        while (iterator.hasNext()) {
            list.push(String(iterator.next()));
        }
    }
    return list;
}

/**
 * A refund marker is `<invoiceNumber>:<cumulative refunded amount>`, and it is
 * also the event's `refund_id` — one string carrying both "which invoice" and
 * "how far this cartridge has already reported it".
 *
 * The amount half is formatted to two decimals so the SAME observation always
 * produces the SAME key: the key is what the ledger dedupes on, so `10.5` and
 * `10.50` must not be two different refunds.
 */
function stableAmount(value) {
    return Number(value).toFixed(2);
}

function refundKey(invoiceNumber, cumulative) {
    return invoiceNumber + ':' + stableAmount(cumulative);
}

/**
 * The highest cumulative refunded amount this cartridge has already reported
 * for `invoiceNumber`, or 0 when it has reported none.
 *
 * Split on the LAST colon: an invoice number may contain one, the formatted
 * amount never does. A marker in any other shape is ignored rather than
 * guessed at — the invoice-number-only marker format the first revision of
 * this job used never shipped (both revisions are in the same unreleased
 * change), so there is no migration to do, and a marker this cannot parse
 * would only cost a redundant re-observation.
 */
function observedCumulative(markers, invoiceNumber) {
    var highest = 0;
    for (var index = 0; index < markers.length; index += 1) {
        var marker = markers[index];
        var separator = marker.lastIndexOf(':');
        if (separator === -1 || marker.slice(0, separator) !== invoiceNumber) {
            continue;
        }
        var parsed = Number(marker.slice(separator + 1));
        if (isFinite(parsed) && parsed > highest) {
            highest = parsed;
        }
    }
    return highest;
}

function markersWithout(markers, invoiceNumber) {
    var kept = [];
    for (var index = 0; index < markers.length; index += 1) {
        var marker = markers[index];
        var separator = marker.lastIndexOf(':');
        if (separator !== -1 && marker.slice(0, separator) === invoiceNumber) {
            continue;
        }
        kept.push(marker);
    }
    return kept;
}

function rememberRefund(order, markers, invoiceNumber, key) {
    // One marker per invoice: the new cumulative figure supersedes the old one
    // for that invoice, so the set grows with refunded INVOICES and not with
    // observations of them.
    var next = markersWithout(markers, invoiceNumber).concat([key]);
    if (next.length > MAX_REFUND_MARKERS) {
        next = next.slice(next.length - MAX_REFUND_MARKERS);
    }
    Transaction.wrap(function () {
        order.custom[REFUND_MARKER] = next;
    });
    return next;
}

function markEmitted(order, attribute) {
    Transaction.wrap(function () {
        order.custom[attribute] = new Date();
    });
}

/**
 * The CUMULATIVE refunded amount of one invoice, or 0 when it is not a settled
 * refund.
 *
 * `refundedAmount` is the money actually returned SO FAR and is the primary
 * reading; it is trusted on its own, because a positive refunded amount IS the
 * settlement. It is cumulative per INVOICE, not per refund — a second partial
 * refund against the same invoice raises it — which is why `emitRefunds` sends
 * the difference against the last figure it observed rather than this number.
 * Only the fallback — a credit-typed invoice whose money lives on the
 * (negative) grand total — additionally insists the invoice is PAID, since a
 * grand total alone says what the invoice is worth, not that it settled.
 */
function cumulativeRefundAmount(invoice) {
    var refunded = moneyValue(invoice && invoice.refundedAmount);
    if (refunded !== null && refunded > 0) {
        return refunded;
    }
    if (!isCreditInvoice(invoice)) {
        return 0;
    }
    var status = invoice && invoice.status ? String(invoice.status).toLowerCase() : '';
    if (status.indexOf('paid') === -1 || status.indexOf('not') !== -1) {
        return 0;
    }
    var grand = moneyValue(invoice.grandTotal);
    if (grand !== null && grand < 0) {
        return -grand;
    }
    return 0;
}

/**
 * Is this invoice one that returns money to the shopper?
 *
 * The property is read as `invoiceType` FIRST and `type` only second. On
 * `dw.order.Invoice` the accessor is `getInvoiceType()`, so the script-API
 * property is `invoiceType`; `type` is very likely `undefined` there. Reading
 * `type` alone therefore made this entire fallback dead code against a real
 * credit invoice — `cumulativeRefundAmount`'s grand-total path could never be
 * reached,
 * and the "skipped, amount is not positive" WARN that a support engineer would
 * use to notice a mis-shaped credit invoice could never fire. `type` is kept as
 * the second reading because it costs nothing and a realm that does surface it
 * still classifies. Both names, and their verification status, are in the table
 * in docs/SFCC_TELEMETRY.md.
 */
function isCreditInvoice(invoice) {
    var raw = invoice ? (invoice.invoiceType || invoice.type) : null;
    var type = raw ? String(raw).toLowerCase() : '';
    if (!type) {
        return false;
    }
    for (var index = 0; index < CREDIT_TYPE_TOKENS.length; index += 1) {
        if (type.indexOf(CREDIT_TYPE_TOKENS[index]) !== -1) {
            return true;
        }
    }
    return false;
}

function invoiceList(order) {
    var invoices = [];
    var collection = null;
    try {
        collection = order.getInvoices ? order.getInvoices() : null;
    } catch (error) {
        // `Invoice` belongs to the Order Management extension. A realm without
        // it has no refund surface at all; that is a documented gap, not an
        // error worth failing the job over.
        logger.info('Pivota sweep found no invoice surface on {0}: {1}', order.orderNo, error.message);
        return invoices;
    }
    var iterator = collection && collection.iterator ? collection.iterator() : null;
    while (iterator && iterator.hasNext() && invoices.length < MAX_INVOICES_PER_ORDER) {
        invoices.push(iterator.next());
    }
    return invoices;
}

function emitPaid(order, occurredAt, counts) {
    if (order.custom[PAID_MARKER]) {
        return true;
    }
    var paymentStatus = order.paymentStatus;
    if (!paymentStatus || paymentStatus.value !== Order.PAYMENT_STATUS_PAID) {
        return true;
    }
    var total = moneyValue(order.totalGrossPrice);
    if (total === null || total <= 0) {
        // A zero-amount money event keyed on the order would permanently
        // shadow the real one: the ledger dedupes first-write-wins on the
        // event key. Skip loudly and leave the marker unset so a corrected
        // total is still emitted later.
        logger.warn(
            'Pivota sweep skipped order.paid for {0}: no positive total gross price',
            order.orderNo
        );
        counts.skipped += 1;
        return true;
    }
    var enqueued = Telemetry.safeEnqueue('order.paid', order, null, {
        event_id: 'order.paid:' + order.orderNo,
        // `lastModified` is the best available time. SFCC records no
        // settlement instant, so this is when the order was last touched —
        // which is the transition itself only when nothing else touched it
        // afterwards. Documented in docs/SFCC_TELEMETRY.md.
        occurred_at: occurredAt,
        amount: String(total),
        status: enumText(paymentStatus),
        items: false
    });
    if (!enqueued) {
        return false;
    }
    markEmitted(order, PAID_MARKER);
    counts.paid += 1;
    return true;
}

function emitCancelled(order, occurredAt, counts) {
    if (order.custom[CANCELLED_MARKER]) {
        return true;
    }
    var status = order.status;
    if (!status || status.value !== Order.ORDER_STATUS_CANCELLED) {
        return true;
    }
    var enqueued = Telemetry.safeEnqueue('order.cancelled', order, null, {
        event_id: 'order.cancelled:' + order.orderNo,
        occurred_at: occurredAt,
        // A cancellation moves no money. Sending the order total here would
        // put a figure on a row nothing sums, so it is left out.
        amount: null,
        status: enumText(status),
        items: false
    });
    if (!enqueued) {
        return false;
    }
    markEmitted(order, CANCELLED_MARKER);
    counts.cancelled += 1;
    return true;
}

function emitRefunds(order, occurredAt, counts) {
    var invoices = invoiceList(order);
    if (!invoices.length) {
        return true;
    }
    var markers = refundMarkers(order);
    var complete = true;
    for (var index = 0; index < invoices.length; index += 1) {
        var invoice = invoices[index];
        var invoiceNumber = invoice && invoice.invoiceNumber ? String(invoice.invoiceNumber) : null;
        if (!invoiceNumber) {
            continue;
        }
        var cumulative = cumulativeRefundAmount(invoice);
        var previous = observedCumulative(markers, invoiceNumber);
        if (cumulative < previous) {
            // Money does not un-refund. A cumulative figure that DROPPED is a
            // reversal, a correction or a mis-read, and there is nothing safe
            // to send: a negative amount is refused by the mapper, and
            // re-sending the lower figure under a new key would ADD refunded
            // money, because the funnel sums distinct refund ids inside one
            // authority. Say so and leave the higher marker standing.
            logger.warn(
                'Pivota sweep skipped refund.succeeded for order {0} invoice {1}: the cumulative ' +
                'refunded amount fell from {2} to {3}; nothing was emitted and the marker was kept',
                order.orderNo,
                invoiceNumber,
                stableAmount(previous),
                stableAmount(cumulative)
            );
            counts.skipped += 1;
            continue;
        }
        var delta = cumulative - previous;
        if (!(delta > 0)) {
            if (previous > 0) {
                // Nothing new since the last observation. This is the ordinary
                // steady state on every tick after the first, so it is silent.
                continue;
            }
            // Not a settled refund (or a credit invoice whose amount is zero).
            // A zero-amount refund.succeeded keyed on this invoice would
            // permanently shadow the real figure, so nothing is enqueued and
            // nothing is marked.
            if (isCreditInvoice(invoice)) {
                logger.warn(
                    'Pivota sweep skipped refund.succeeded for order {0} invoice {1}: amount is not positive',
                    order.orderNo,
                    invoiceNumber
                );
                counts.skipped += 1;
            }
            continue;
        }
        // The key carries the NEW cumulative total, so a second partial refund
        // on the same invoice is a second ledger row rather than a duplicate of
        // the first; the amount is the DELTA, so the two sum to the cumulative
        // figure instead of double-counting the first partial.
        var key = refundKey(invoiceNumber, cumulative);
        var enqueued = Telemetry.safeEnqueue('refund.succeeded', order, null, {
            event_id: 'refund.succeeded:' + key,
            occurred_at: occurredAt,
            amount: stableAmount(delta),
            refund_id: key,
            status: invoice.status ? String(invoice.status) : null,
            items: false
        });
        if (!enqueued) {
            complete = false;
            continue;
        }
        markers = rememberRefund(order, markers, invoiceNumber, key);
        counts.refunds += 1;
    }
    return complete;
}

function sweepOrder(order, counts) {
    var occurredAt = isoOf(order.lastModified);
    var complete = true;
    complete = emitPaid(order, occurredAt, counts) && complete;
    complete = emitCancelled(order, occurredAt, counts) && complete;
    complete = emitRefunds(order, occurredAt, counts) && complete;
    return complete;
}

function execute(parameters) {
    var maxOrders = bounded(parameters && parameters.MaxOrders, DEFAULT_MAX_ORDERS, 1, MAX_MAX_ORDERS);
    var overlapMinutes = bounded(
        parameters && parameters.OverlapMinutes,
        DEFAULT_OVERLAP_MINUTES,
        1,
        MAX_OVERLAP_MINUTES
    );
    var lookbackHours = bounded(
        parameters && parameters.InitialLookbackHours,
        DEFAULT_LOOKBACK_HOURS,
        1,
        MAX_LOOKBACK_HOURS
    );
    var maxFailureLagHours = bounded(
        parameters && parameters.MaxFailureLagHours,
        DEFAULT_MAX_FAILURE_LAG_HOURS,
        MIN_MAX_FAILURE_LAG_HOURS,
        MAX_MAX_FAILURE_LAG_HOURS
    );
    var counts = {scanned: 0, paid: 0, cancelled: 0, refunds: 0, skipped: 0, failed: 0};
    var cursor;
    var since;
    try {
        cursor = cursorObject();
        since = readCursor(cursor, lookbackHours);
    } catch (error) {
        logger.error('Pivota settlement sweep could not read its cursor: {0}', error.message);
        return new Status(Status.ERROR, 'ERROR', 'Pivota settlement sweep could not read its cursor');
    }

    var watermark = null;
    var firstFailureAt = null;
    // The newest `lastModified` this run OBSERVED, successful or not. The lag
    // bound below is measured against this rather than against `watermark`,
    // because a run in which every order failed has no watermark at all — and
    // that is exactly the run that most needs the bound.
    var newestSeen = null;
    var failures = [];
    var abandonedOrders = [];
    var iterator = null;
    try {
        iterator = OrderMgr.searchOrders('lastModified >= {0}', 'lastModified asc', since);
        while (iterator.hasNext() && counts.scanned < maxOrders) {
            var order = iterator.next();
            counts.scanned += 1;
            var lastModified = order.lastModified;
            var complete = false;
            try {
                complete = sweepOrder(order, counts);
            } catch (orderError) {
                // One unreadable order must not end the sweep. `complete` is
                // still false, so the branch below counts and clamps it — do
                // NOT count it here as well.
                logger.error(
                    'Pivota settlement sweep failed on order {0}: {1}',
                    order && order.orderNo,
                    orderError.message
                );
            }
            if (lastModified && (!newestSeen || lastModified.getTime() > newestSeen.getTime())) {
                newestSeen = new Date(lastModified.getTime());
            }
            if (!complete) {
                counts.failed += 1;
                if (!firstFailureAt && lastModified) {
                    firstFailureAt = new Date(lastModified.getTime());
                }
                if (failures.length < MAX_REPORTED_FAILURES) {
                    failures.push({
                        orderNo: order && order.orderNo ? String(order.orderNo) : '(unknown)',
                        at: lastModified ? new Date(lastModified.getTime()) : null
                    });
                }
                continue;
            }
            if (lastModified && (!watermark || lastModified.getTime() > watermark.getTime())) {
                watermark = new Date(lastModified.getTime());
            }
        }
    } catch (error) {
        logger.error('Pivota settlement sweep failed: {0}', error.message);
        return new Status(Status.ERROR, 'ERROR', 'Pivota settlement sweep failed: ' + error.message);
    } finally {
        if (iterator && iterator.close) {
            iterator.close();
        }
    }

    // The cursor must never step over an order this run could not deliver…
    var effective = watermark;
    if (firstFailureAt && (!effective || firstFailureAt.getTime() < effective.getTime())) {
        effective = firstFailureAt;
    }
    // …but that clamp cannot be unbounded, or one poison order STALLS THE SITE.
    //
    // An order that fails on every single tick — unreadable custom attributes,
    // an exception raised inside `buildEvent`, an invoice member that throws —
    // pins `firstFailureAt`, and therefore the cursor, to its own
    // `lastModified` forever. Each run then re-reads the same window, and
    // because `MaxOrders` bounds the run, the sweep never reaches the newer
    // orders behind it. Every later settlement, cancellation and refund for the
    // whole site is silently lost, and the only symptom is a `failed=` count in
    // the merchant's own job log, which nothing on the Pivota side reads.
    //
    // So the clamp may hold the cursor at most `MaxFailureLagHours` behind the
    // newest order this run observed. Beyond that the failing orders are
    // ABANDONED — their facts are never emitted — and the abandonment is a loud
    // ERROR naming the order numbers, because a silent stall is worse than a
    // named loss that support can replay by hand.
    if (newestSeen && effective) {
        var lagBound = new Date(newestSeen.getTime() - maxFailureLagHours * 60 * 60 * 1000);
        if (effective.getTime() < lagBound.getTime()) {
            for (var failureIndex = 0; failureIndex < failures.length; failureIndex += 1) {
                var failure = failures[failureIndex];
                if (!failure.at || failure.at.getTime() < lagBound.getTime()) {
                    abandonedOrders.push(failure.orderNo);
                }
            }
            if (abandonedOrders.length) {
                logger.error(
                    'Pivota settlement sweep ABANDONED order(s) so the cursor could move past ' +
                    'them: {0}. They failed every attempt for longer than MaxFailureLagHours={1}h; ' +
                    'their settlement, cancellation and refund events were NEVER emitted and must ' +
                    'be replayed by hand. {2} order(s) failed this run in total; at most {3} order ' +
                    'numbers are listed here.',
                    abandonedOrders.join(', '),
                    maxFailureLagHours,
                    counts.failed,
                    MAX_REPORTED_FAILURES
                );
            } else {
                // The bound moved the cursor without any recorded failure being
                // behind it, so NOTHING was abandoned. Claiming a permanent loss
                // here would be false, and reporting ERROR would raise a Business
                // Manager job-failure notification for a run that dropped nothing.
                logger.warn(
                    'Pivota settlement sweep advanced its cursor to the MaxFailureLagHours={0}h ' +
                    'bound; no failing order was behind the bound, so nothing was abandoned. ' +
                    '{1} order(s) failed this run and are still retried.',
                    maxFailureLagHours,
                    counts.failed
                );
            }
            effective = lagBound;
        }
    }
    try {
        if (effective) {
            writeCursor(cursor, effective, overlapMinutes);
        }
    } catch (error) {
        // A cursor that did not advance costs a repeated scan, not lost events.
        logger.error('Pivota settlement sweep could not persist its cursor: {0}', error.message);
    }

    var message =
        'Pivota settlement sweep: scanned=' + counts.scanned +
        ' paid=' + counts.paid +
        ' cancelled=' + counts.cancelled +
        ' refunds=' + counts.refunds +
        ' skipped=' + counts.skipped +
        ' failed=' + counts.failed +
        ' abandoned=' + abandonedOrders.length;
    if (abandonedOrders.length) {
        // Settlement facts were permanently dropped. Business Manager notifies
        // on a job step's non-OK status and on nothing else, so reporting OK
        // here would leave the ERROR line above sitting unread in the realm's
        // job log — which, per docs/SFCC_TELEMETRY.md, nothing on the Pivota
        // side reads either. The step is declared `enforce-restart="false"` in
        // metadata/jobs.xml, so the next scheduled tick still runs.
        logger.info(message);
        return new Status(Status.ERROR, 'ABANDONED', message);
    }
    logger.info(message);
    return new Status(Status.OK, 'OK', message);
}

module.exports = {
    execute: execute
};
