'use strict';

var CustomObjectMgr = require('dw/object/CustomObjectMgr');
var Logger = require('dw/system/Logger');
var Status = require('dw/system/Status');
var Transaction = require('dw/system/Transaction');
var Bytes = require('dw/util/Bytes');
var Service = require('*/cartridge/scripts/services/PivotaTelemetryService');

var OUTBOX_TYPE = 'PivotaTelemetryOutbox';
var MAX_BATCH_BYTES = 900000;

function boundedBatchSize(value) {
    var parsed = Number(value || 50);
    if (!isFinite(parsed)) {
        return 50;
    }
    return Math.max(1, Math.min(Math.floor(parsed), 100));
}

function loadBatch(limit) {
    var objects = [];
    var iterator = CustomObjectMgr.queryCustomObjects(
        OUTBOX_TYPE,
        'custom.availableAt <= {0}',
        'creationDate asc',
        new Date()
    );
    try {
        while (iterator.hasNext() && objects.length < limit) {
            objects.push(iterator.next());
        }
    } finally {
        iterator.close();
    }
    return objects;
}

function markRetry(objects) {
    Transaction.wrap(function () {
        objects.forEach(function (object) {
            var attempts = Number(object.custom.attempts || 0) + 1;
            var delaySeconds = Math.min(3600, Math.pow(2, Math.min(attempts, 10)) * 15);
            var availableAt = new Date();
            availableAt.setTime(availableAt.getTime() + delaySeconds * 1000);
            object.custom.attempts = attempts;
            object.custom.availableAt = availableAt;
        });
    });
}

function removeBatch(objects) {
    Transaction.wrap(function () {
        objects.forEach(function (object) {
            CustomObjectMgr.remove(object);
        });
    });
}

function selectPayload(objects) {
    var selectedObjects = [];
    var events = [];
    var rejected = false;
    for (var index = 0; index < objects.length; index += 1) {
        var object = objects[index];
        var event;
        try {
            event = JSON.parse(String(object.custom.payload));
        } catch (error) {
            markRetry([object]);
            rejected = true;
            Logger.error('Pivota telemetry outbox contains invalid JSON: {0}', error.message);
            continue;
        }
        var candidateEvents = events.concat([event]);
        var candidateBody = JSON.stringify({events: candidateEvents});
        if (new Bytes(candidateBody, 'UTF-8').length > MAX_BATCH_BYTES) {
            if (!events.length) {
                markRetry([object]);
                rejected = true;
                Logger.error('Pivota telemetry event exceeds the delivery byte limit: {0}', object.custom.ID);
                continue;
            }
            break;
        }
        events = candidateEvents;
        selectedObjects.push(object);
    }
    return {
        events: events,
        objects: selectedObjects,
        rejected: rejected
    };
}

function execute(parameters) {
    var objects = loadBatch(boundedBatchSize(parameters && parameters.BatchSize));
    if (!objects.length) {
        return new Status(Status.OK, 'OK', 'No Pivota telemetry events are due');
    }
    var now = new Date();
    var expired = objects.filter(function (object) {
        return object.custom.expiresAt && object.custom.expiresAt <= now;
    });
    if (expired.length) {
        removeBatch(expired);
        objects = objects.filter(function (object) {
            return expired.indexOf(object) === -1;
        });
    }
    if (!objects.length) {
        return new Status(Status.OK, 'OK', 'Removed expired Pivota telemetry events');
    }
    var payload = selectPayload(objects);
    if (!payload.events.length) {
        return new Status(
            payload.rejected ? Status.ERROR : Status.OK,
            payload.rejected ? 'ERROR' : 'OK',
            payload.rejected ? 'No deliverable Pivota events' : 'No Pivota events selected'
        );
    }
    if (!Service.send(payload.events)) {
        markRetry(payload.objects);
        return new Status(Status.ERROR, 'ERROR', 'Pivota telemetry delivery failed');
    }
    removeBatch(payload.objects);
    return new Status(
        Status.OK,
        'OK',
        'Delivered ' + payload.objects.length + ' Pivota events'
    );
}

module.exports = {
    execute: execute
};
