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

function buildEvent(eventType, container, paymentInstrument, context) {
    var isOrder = Boolean(container && (container.orderNo || container.orderToken));
    var isPayment = eventType.indexOf('payment.') === 0;
    var stitchedBasketId = scalar(context && context.basket_id);
    var event = {
        event_id: UUIDUtils.createUUID(),
        type: eventType,
        occurred_at: new Date().toISOString(),
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
        items: lineItems(container)
    };
    return event;
}

function enqueue(event) {
    var key = event.event_id;
    var payload = JSON.stringify(event);
    Transaction.wrap(function () {
        var outbox = CustomObjectMgr.createCustomObject(OUTBOX_TYPE, key);
        outbox.custom.payload = payload;
        outbox.custom.attempts = 0;
        outbox.custom.availableAt = new Date();
        var expiresAt = new Date();
        expiresAt.setTime(expiresAt.getTime() + 7 * 24 * 60 * 60 * 1000);
        outbox.custom.expiresAt = expiresAt;
    });
}

function safeEnqueue(eventType, container, paymentInstrument, context) {
    try {
        enqueue(buildEvent(eventType, container, paymentInstrument, context));
    } catch (error) {
        // Telemetry must never fail or delay the shopper operation. The scheduled
        // drain and custom-object volume should be monitored separately.
        Logger.error('Pivota telemetry enqueue failed for {0}: {1}', eventType, error.message);
    }
}

module.exports = {
    safeEnqueue: safeEnqueue,
    buildEvent: buildEvent
};
