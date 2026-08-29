'use strict';

var Telemetry = require('*/cartridge/scripts/pivota/Telemetry');

exports.afterPOST = function (order) {
    var basketId = null;
    try {
        basketId = request.custom.pivotaTelemetryBasketId || null;
    } catch (error) {
        // Correlation is best effort and must never interrupt order creation.
    }
    Telemetry.safeEnqueue('order.created', order, null, {basket_id: basketId});
};
