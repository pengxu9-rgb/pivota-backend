'use strict';

var Telemetry = require('*/cartridge/scripts/pivota/Telemetry');

exports.beforePOST = function (basket) {
    try {
        request.custom.pivotaTelemetryBasketId = String(basket.UUID);
    } catch (error) {
        // Correlation is best effort and must never interrupt order submission.
    }
    Telemetry.safeEnqueue('checkout.submitted', basket);
};
