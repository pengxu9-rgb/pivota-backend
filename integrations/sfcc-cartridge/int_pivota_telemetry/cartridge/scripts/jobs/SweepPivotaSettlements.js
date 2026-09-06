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
 *      `order.cancelled:<orderNo>`, `refund.succeeded:<invoiceNumber>`. The
 *      receiver hashes it into the canonical event key, and the ledger dedupes
 *      first-write-wins on that key. So an enqueue that succeeded while the
 *      marker write failed (or a marker that aged out of the capped refund set)
 *      costs a duplicate delivery, never a duplicate ledger row. The random
 *      UUID `Telemetry.buildEvent` mints for shopper hooks would NOT have that
 *      property, which is why these events override it.
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
var MAX_INVOICES_PER_ORDER = 50;
// A set-of-string cannot grow without bound. An order with more credit
// invoices than this loses its oldest markers; the deterministic refund event
// id (the invoice number) is what keeps that from double-counting.
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

function rememberRefund(order, markers, invoiceNumber) {
    var next = markers.concat([invoiceNumber]);
    if (next.length > MAX_REFUND_MARKERS) {
        next = next.slice(next.length - MAX_REFUND_MARKERS);
    }
    Transaction.wrap(function () {
        order.custom[REFUND_MARKER] = next;
    });
}

function markEmitted(order, attribute) {
    Transaction.wrap(function () {
        order.custom[attribute] = new Date();
    });
}

/**
 * The refunded amount of one invoice, or 0 when it is not a settled refund.
 *
 * `refundedAmount` is the money actually returned and is the primary reading;
 * it is trusted on its own, because a positive refunded amount IS the
 * settlement. Only the fallback — a credit-typed invoice whose money lives on
 * the (negative) grand total — additionally insists the invoice is PAID, since
 * a grand total alone says what the invoice is worth, not that it settled.
 */
function refundAmount(invoice) {
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

function isCreditInvoice(invoice) {
    var type = invoice && invoice.type ? String(invoice.type).toLowerCase() : '';
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
        if (!invoiceNumber || markers.indexOf(invoiceNumber) !== -1) {
            continue;
        }
        var amount = refundAmount(invoice);
        if (!(amount > 0)) {
            // Not a settled refund (or a credit invoice whose amount is zero).
            // A zero-amount refund.succeeded keyed on this invoice number would
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
        var enqueued = Telemetry.safeEnqueue('refund.succeeded', order, null, {
            event_id: 'refund.succeeded:' + invoiceNumber,
            occurred_at: occurredAt,
            amount: String(amount),
            refund_id: invoiceNumber,
            status: invoice.status ? String(invoice.status) : null,
            items: false
        });
        if (!enqueued) {
            complete = false;
            continue;
        }
        rememberRefund(order, markers, invoiceNumber);
        markers = markers.concat([invoiceNumber]);
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
            if (!complete) {
                counts.failed += 1;
                if (!firstFailureAt && lastModified) {
                    firstFailureAt = new Date(lastModified.getTime());
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

    // The cursor must never step over an order this run could not deliver.
    var effective = watermark;
    if (firstFailureAt && (!effective || firstFailureAt.getTime() < effective.getTime())) {
        effective = firstFailureAt;
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
        ' failed=' + counts.failed;
    logger.info(message);
    return new Status(Status.OK, 'OK', message);
}

module.exports = {
    execute: execute
};
