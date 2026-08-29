'use strict';

var Telemetry = require('*/cartridge/scripts/pivota/Telemetry');

exports.afterPOST = function (basket) {
    Telemetry.safeEnqueue('basket.created', basket);
};
