'use strict';

var Telemetry = require('*/cartridge/scripts/pivota/Telemetry');

exports.afterPOST = function (order, paymentInstrument, successfullyAuthorized) {
    Telemetry.safeEnqueue(
        successfullyAuthorized ? 'payment.authorized' : 'payment.declined',
        order,
        paymentInstrument
    );
};
