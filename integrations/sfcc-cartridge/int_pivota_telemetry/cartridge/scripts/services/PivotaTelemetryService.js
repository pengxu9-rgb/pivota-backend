'use strict';

var Encoding = require('dw/crypto/Encoding');
var LocalServiceRegistry = require('dw/svc/LocalServiceRegistry');
var Mac = require('dw/crypto/Mac');
var Site = require('dw/system/Site');
var UUIDUtils = require('dw/util/UUIDUtils');

var SERVICE_ID = 'pivota.telemetry.events';
var CREDENTIAL_PREFIX = 'pivota.telemetry.';

function send(events) {
    var body = JSON.stringify({events: events});
    var timestamp = String(Math.floor(Date.now() / 1000));
    var deliveryId = UUIDUtils.createUUID();
    var service = LocalServiceRegistry.createService(SERVICE_ID, {
        createRequest: function (svc) {
            var credential = svc.getConfiguration().getCredential();
            var secret = credential && credential.getPassword();
            if (!secret) {
                throw new Error('Pivota telemetry service signing secret is missing');
            }
            var digest = new Mac(Mac.HMAC_SHA_256).digest(timestamp + '.' + body, secret);
            svc.setRequestMethod('POST');
            // Service Framework defaults to BASIC. Explicit NONE prevents the
            // signing secret in the credential password from being sent as an
            // Authorization header in addition to the HMAC signature.
            svc.setAuthentication('NONE');
            svc.addHeader('Content-Type', 'application/json');
            svc.addHeader('X-Pivota-SFCC-Timestamp', timestamp);
            svc.addHeader('X-Pivota-SFCC-Signature', 'sha256=' + Encoding.toHex(digest));
            svc.addHeader('X-Pivota-SFCC-Delivery-Id', deliveryId);
            svc.addHeader('X-Pivota-SFCC-Site-Id', Site.getCurrent().getID());
            return body;
        },
        parseResponse: function (svc, client) {
            return {statusCode: client.statusCode};
        },
        filterLogMessage: function () {
            return '[Pivota telemetry payload redacted]';
        }
    });
    // A realm can host multiple sites, each connected to a distinct Pivota
    // store. Select the per-site URL/signing secret before this call.
    service.setCredentialID(CREDENTIAL_PREFIX + Site.getCurrent().getID());
    var result = service.call();
    return Boolean(
        result.status === 'OK' &&
        result.object &&
        result.object.statusCode >= 200 &&
        result.object.statusCode < 300
    );
}

module.exports = {
    send: send
};
