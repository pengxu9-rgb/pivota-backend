'use strict';

var CustomObjectMgr = require('dw/object/CustomObjectMgr');
var Logger = require('dw/system/Logger');
var Site = require('dw/system/Site');
var Transaction = require('dw/system/Transaction');
var UUIDUtils = require('dw/util/UUIDUtils');

var OUTBOX_TYPE = 'PivotaTelemetryOutbox';
var MAX_LINE_ITEMS = 100;

function scalar(value) {
    if (value === null || value === undefined) {
        return null;
    }
    var normalized = String(value).trim();
    return normalized || null;
}

function moneyValue(money) {
    return money && money.available ? String(money.value) : null;
}

function paymentAmount(paymentInstrument) {
    var value = paymentInstrument && paymentInstrument.amount;
    if (typeof value === 'number' || typeof value === 'string') {
        return scalar(value);
    }
    return moneyValue(value);
}

function currencyCode(container) {
    if (container && container.currencyCode) {
        return scalar(container.currencyCode);
    }
    if (container && container.totalGrossPrice && container.totalGrossPrice.currencyCode) {
        return scalar(container.totalGrossPrice.currencyCode);
    }
    return null;
}

function lineItems(container) {
    var safe = [];
    var collection = container && container.productLineItems;
    var iterator = collection && collection.iterator ? collection.iterator() : null;
    while (iterator && iterator.hasNext() && safe.length < MAX_LINE_ITEMS) {
        var item = iterator.next();
        safe.push({
            id: scalar(item.UUID),
            product_id: scalar(item.productID),
            variant_id: scalar(item.productID),
            sku: scalar(item.productID),
            quantity: item.quantityValue,
            price: moneyValue(item.basePrice),
            total: moneyValue(item.adjustedGrossPrice)
        });
    }
    return safe;
}

/**
 * `context` carries the basket stitch key AND, for the settlement sweep, a
 * small set of explicit overrides. The sweep observes facts SFCC records
 * without firing anything, so it — unlike a shopper hook — knows the event's
 * own identity, time and amount and must be able to say them:
 *
 *   event_id    a DETERMINISTIC id (`order.paid:<orderNo>`,
 *               `refund.succeeded:<invoiceNumber>`). The receiver hashes it
 *               into the canonical event key, so a redelivery dedupes in the
 *               ledger instead of writing a second row. A shopper hook has no
 *               such natural key and keeps the random UUID.
 *   occurred_at the order's `lastModified`, the best time SFCC has for a
 *               settlement it never timestamps.
 *   amount      the settled figure (`null` for events that move no money).
 *   refund_id   the credit invoice number.
 *   status      the native status text to report.
 *   items       `false` to omit line items — an order's lines are not a
 *               refund's lines, and a settlement event does not need them.
 *
 * Anything absent keeps the hook-path behaviour, so the five shopper hooks are
 * unchanged by this.
 */
function buildEvent(eventType, container, paymentInstrument, context) {
    var isOrder = Boolean(container && (container.orderNo || container.orderToken));
    var isPayment = eventType.indexOf('payment.') === 0;
    var overrides = context || {};
    var stitchedBasketId = scalar(context && context.basket_id);
    var event = {
        event_id: scalar(overrides.event_id) || UUIDUtils.createUUID(),
        type: eventType,
        occurred_at: scalar(overrides.occurred_at) || new Date().toISOString(),
        site_id: Site.getCurrent().getID(),
        basket_id: isOrder ? stitchedBasketId : scalar(container && container.UUID),
        checkout_id: isOrder ? stitchedBasketId : scalar(container && container.UUID),
        order_id: isOrder ? scalar(container.orderNo) : null,
        payment_id: scalar(paymentInstrument && paymentInstrument.UUID),
        customer_id: scalar(container && container.customerNo),
        // Split-tender authorization must not be reported as the whole order
        // amount. Use only the request/instrument amount when it is available.
        amount: isPayment
            ? paymentAmount(paymentInstrument)
            : moneyValue(container && container.totalGrossPrice),
        currency: currencyCode(container),
        status: scalar(container && (container.status || container.statusDisplayValue)),
        items: overrides.items === false ? [] : lineItems(container)
    };
    if (overrides.amount !== undefined) {
        event.amount = scalar(overrides.amount);
    }
    if (overrides.status !== undefined) {
        event.status = scalar(overrides.status);
    }
    if (overrides.refund_id) {
        event.refund_id = scalar(overrides.refund_id);
    }
    return event;
}

/**
 * Puts one event into the local outbox, keyed on its `event_id`.
 *
 * The key can legitimately be OCCUPIED already, and `createCustomObject` raises
 * on a duplicate key. The settlement sweep mints DETERMINISTIC ids and the
 * drain keeps an undelivered row for up to seven days, so re-enqueuing an event
 * that is still sitting in the outbox is the normal shape of a Pivota outage:
 * the marker did not stick (or the cursor's overlap window came round again)
 * while the row was still undelivered. Without the lookup below that raised,
 * `safeEnqueue` returned false, the sweep counted the order failed and held its
 * cursor, and `MaxFailureLagHours` eventually ABANDONED it — during an outage,
 * which is precisely when it must not. A row already under this key carries the
 * same event, so finding one is success, not an error.
 *
 * The shopper hooks are unchanged in behaviour: their ids are random UUIDs, so
 * the lookup never finds anything.
 */
function enqueue(event) {
    var key = event.event_id;
    var payload = JSON.stringify(event);
    var existed = false;
    Transaction.wrap(function () {
        // Checked inside the transaction so the window between the read and
        // the create is as small as the platform allows. A genuinely
        // concurrent create still raises and is still caught by `safeEnqueue`;
        // nothing routine reaches that window, because the sweep is declared
        // `@supports-parallel-execution: false` and every other producer mints
        // a fresh UUID.
        if (CustomObjectMgr.getCustomObject(OUTBOX_TYPE, key)) {
            existed = true;
            return;
        }
        var outbox = CustomObjectMgr.createCustomObject(OUTBOX_TYPE, key);
        outbox.custom.payload = payload;
        outbox.custom.attempts = 0;
        outbox.custom.availableAt = new Date();
        var expiresAt = new Date();
        expiresAt.setTime(expiresAt.getTime() + 7 * 24 * 60 * 60 * 1000);
        outbox.custom.expiresAt = expiresAt;
    });
    if (existed) {
        Logger.debug('Pivota telemetry event {0} is already queued; nothing to enqueue', key);
    }
}

/**
 * Returns `true` when the event reached the outbox.
 *
 * The five shopper hooks ignore the return value — telemetry must never fail
 * or delay a shopper operation, so a failure is logged and swallowed either
 * way. The settlement sweep DOES read it: it writes its once-only marker on
 * the order only after a successful enqueue, and holds its cursor back when
 * one fails, so a failed enqueue is retried on the next tick instead of being
 * marked as delivered. An event already queued under the same key counts as
 * reaching the outbox — see `enqueue`.
 */
function safeEnqueue(eventType, container, paymentInstrument, context) {
    try {
        enqueue(buildEvent(eventType, container, paymentInstrument, context));
        return true;
    } catch (error) {
        // Telemetry must never fail or delay the shopper operation. The scheduled
        // drain and custom-object volume should be monitored separately.
        Logger.error('Pivota telemetry enqueue failed for {0}: {1}', eventType, error.message);
        return false;
    }
}

module.exports = {
    safeEnqueue: safeEnqueue,
    buildEvent: buildEvent
};
