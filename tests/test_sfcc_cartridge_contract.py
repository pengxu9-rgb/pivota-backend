import json
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
CARTRIDGE = ROOT / "integrations" / "sfcc-cartridge"


def test_sfcc_cartridge_manifests_and_impex_are_parseable():
    hooks = json.loads(
        (CARTRIDGE / "int_pivota_telemetry/cartridge/scripts/hooks.json").read_text()
    )
    package = json.loads(
        (CARTRIDGE / "int_pivota_telemetry/package.json").read_text()
    )
    steptypes = json.loads(
        (CARTRIDGE / "int_pivota_telemetry/steptypes.json").read_text()
    )
    hook_names = {item["name"] for item in hooks["hooks"]}
    assert hook_names == {
        "dw.ocapi.shop.basket.afterPOST",
        "dw.ocapi.shop.basket.items.afterPOST",
        "dw.ocapi.shop.order.beforePOST",
        "dw.ocapi.shop.order.afterPOST",
        "dw.ocapi.shop.order.payment_instrument.afterPOST",
    }
    assert package["hooks"] == "./cartridge/scripts/hooks.json"
    steps = {
        step["@type-id"]: step for step in steptypes["step-types"]["script-module-step"]
    }
    assert set(steps) == {"custom.PivotaTelemetryDrain", "custom.PivotaSettlementSweep"}
    for step in steps.values():
        assert step["@supports-parallel-execution"] is False
        assert (CARTRIDGE / "int_pivota_telemetry" / step["module"].split("/", 1)[1]).exists()
    for relative in (
        "metadata/meta/custom-objecttype-definitions.xml",
        "metadata/meta/system-objecttype-extensions.xml",
        "metadata/services.xml",
        "metadata/jobs.xml",
    ):
        ElementTree.parse(CARTRIDGE / relative)

    jobs_text = (CARTRIDGE / "metadata/jobs.xml").read_text()
    readme = (CARTRIDGE / "README.md").read_text()
    assert 'job-id="PivotaTelemetryDrain-RefArch"' in jobs_text
    assert '<context site-id="RefArch"' in jobs_text
    assert "For **every connected site**" in readme
    assert "duplicate its complete `<job>` element" in readme


def test_sfcc_settlement_sweep_step_is_registered_as_a_job_and_a_step_type():
    """A step that exists only as a .js file never runs.

    SFCC resolves a job flow step through `steptypes.json` and needs a `<job>`
    to schedule it, so the sweep is only real when BOTH manifests name it and
    the module path in `steptypes.json` resolves to a file that exports the
    named function.
    """
    steptypes = json.loads((CARTRIDGE / "int_pivota_telemetry/steptypes.json").read_text())
    sweep = next(
        step
        for step in steptypes["step-types"]["script-module-step"]
        if step["@type-id"] == "custom.PivotaSettlementSweep"
    )
    assert sweep["module"] == (
        "int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js"
    )
    assert sweep["function"] == "execute"
    assert sweep["@supports-site-context"] is True
    parameters = {
        parameter["@name"]: parameter
        for parameter in sweep["parameters"]["parameter"]
    }
    assert set(parameters) == {"MaxOrders", "OverlapMinutes", "InitialLookbackHours"}
    assert parameters["MaxOrders"]["max-value"] == 5000

    jobs = ElementTree.parse(CARTRIDGE / "metadata/jobs.xml").getroot()
    namespace = {"j": "http://www.demandware.com/xml/impex/jobs/2015-07-01"}
    job_ids = {job.attrib["job-id"] for job in jobs.findall("j:job", namespace)}
    assert "PivotaSettlementSweep-RefArch" in job_ids
    step_types = {
        step.attrib["type"] for step in jobs.findall(".//j:step", namespace)
    }
    assert "custom.PivotaSettlementSweep" in step_types

    sweep_source = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js"
    ).read_text()
    assert "module.exports = {\n    execute: execute\n};" in sweep_source


def test_sfcc_cartridge_never_registers_the_payment_implementation_hooks():
    """`dw.order.payment.capture` / `.refund` are IMPLEMENTATION extension
    points: the merchant's PSP cartridge implements them to move the money, and
    the hook manager resolves a single implementation by cartridge-path order.
    A telemetry observer registered there could shadow the real processor and
    break capture, so this cartridge must never appear on either.
    """
    hooks_text = (
        CARTRIDGE / "int_pivota_telemetry/cartridge/scripts/hooks.json"
    ).read_text()
    hooks = json.loads(hooks_text)
    registered = {item["name"] for item in hooks["hooks"]}
    forbidden = {"dw.order.payment.capture", "dw.order.payment.refund"}
    assert not (registered & forbidden)
    # Not just the exact two names: nothing from the PaymentHooks family, under
    # any spelling, may be registered.
    for name in registered:
        assert "payment.capture" not in name
        assert "payment.refund" not in name
        assert "dw.order.hooks.PaymentHooks" not in name
    assert "capture" not in hooks_text
    readme = (CARTRIDGE / "README.md").read_text()
    assert "dw.order.payment.capture" in readme
    assert "dw.order.payment.refund" in readme


def test_sfcc_settlement_sweep_keys_refunds_on_the_invoice_and_only_sends_positive_money():
    sweep = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js"
    ).read_text()
    # Refunds are keyed on the credit invoice, never the order: two credit
    # invoices on one order must stay two refund rows.
    assert "'refund.succeeded:' + invoiceNumber" in sweep
    assert "refund_id: invoiceNumber" in sweep
    # …and `order.paid` / `order.cancelled` on the order, so a redelivery
    # dedupes in the ledger instead of writing a second row.
    assert "'order.paid:' + order.orderNo" in sweep
    assert "'order.cancelled:' + order.orderNo" in sweep

    # Only a positive settled amount is enqueued. A zero-amount money event
    # under a native id is a permanent shadow, because the ledger dedupes
    # first-write-wins on the key derived from that id.
    assert "if (!(amount > 0)) {" in sweep
    assert "amount is not positive" in sweep
    assert "if (total === null || total <= 0) {" in sweep
    assert "no positive total gross price" in sweep
    # The skip must not mark the order as emitted, or a corrected total could
    # never be reported.
    paid_skip = sweep.split("if (total === null || total <= 0) {", 1)[1].split("}", 1)[0]
    assert "markEmitted" not in paid_skip

    # An enqueue that failed must not be marked as delivered.
    assert "if (!enqueued) {" in sweep
    assert "markEmitted(order, PAID_MARKER);" in sweep
    assert "markEmitted(order, CANCELLED_MARKER);" in sweep

    telemetry = (
        CARTRIDGE / "int_pivota_telemetry/cartridge/scripts/pivota/Telemetry.js"
    ).read_text()
    # `safeEnqueue` has to REPORT success for the marker discipline above to
    # mean anything.
    assert "return true;" in telemetry
    assert "return false;" in telemetry


def test_sfcc_settlement_sweep_has_a_bounded_cursor_with_an_overlap_window():
    sweep = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js"
    ).read_text()
    assert "DEFAULT_OVERLAP_MINUTES = 10" in sweep
    # The stored cursor is the watermark REWOUND by the overlap, so a tick that
    # died mid-batch re-examines rather than skips.
    assert "watermark.getTime() - overlapMinutes * 60 * 1000" in sweep
    # …and never moves backwards.
    assert "stored.getTime() < next.getTime()" in sweep
    # An order this run could not deliver must not be stepped over.
    assert "firstFailureAt" in sweep
    assert "lastModified asc" in sweep
    assert "counts.scanned < maxOrders" in sweep
    assert "iterator.close()" in sweep
    # The step must never throw out of the job: every order is guarded and the
    # search itself is guarded.
    assert "catch (orderError)" in sweep
    assert "new Status(Status.OK, 'OK', message)" in sweep


def test_sfcc_once_only_markers_and_sweep_cursor_are_declared_in_the_impex():
    """The markers the sweep writes must exist as custom attributes, or every
    write throws and the sweep re-emits forever."""
    meta_namespace = {"m": "http://www.demandware.com/xml/impex/metadata/2006-10-31"}
    extensions = ElementTree.parse(
        CARTRIDGE / "metadata/meta/system-objecttype-extensions.xml"
    ).getroot()
    order_extension = extensions.find(
        "m:type-extension[@type-id='Order']", meta_namespace
    )
    assert order_extension is not None
    declared = {
        definition.attrib["attribute-id"]: definition.find("m:type", meta_namespace).text
        for definition in order_extension.findall(
            "m:custom-attribute-definitions/m:attribute-definition", meta_namespace
        )
    }
    assert declared == {
        "pivotaPaidEmittedAt": "datetime",
        "pivotaCancelledEmittedAt": "datetime",
        "pivotaRefundedInvoices": "set-of-string",
    }

    custom_types = ElementTree.parse(
        CARTRIDGE / "metadata/meta/custom-objecttype-definitions.xml"
    ).getroot()
    type_ids = {
        custom_type.attrib["type-id"]
        for custom_type in custom_types.findall("m:custom-type", meta_namespace)
    }
    assert type_ids == {"PivotaTelemetryOutbox", "PivotaTelemetrySweepCursor"}
    cursor = custom_types.find(
        "m:custom-type[@type-id='PivotaTelemetrySweepCursor']", meta_namespace
    )
    cursor_attributes = {
        definition.attrib["attribute-id"]
        for definition in cursor.findall(
            "m:attribute-definitions/m:attribute-definition", meta_namespace
        )
    }
    assert cursor_attributes == {"cursorAt", "updatedAt"}

    sweep = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js"
    ).read_text()
    for attribute in declared:
        assert attribute in sweep
    assert "CURSOR_TYPE = 'PivotaTelemetrySweepCursor'" in sweep

    readme = (CARTRIDGE / "README.md").read_text()
    assert "system-objecttype-extensions.xml" in readme


def test_sfcc_custom_object_metadata_pins_vendor_schema_requirements():
    namespace = {"m": "http://www.demandware.com/xml/impex/metadata/2006-10-31"}
    root = ElementTree.parse(
        CARTRIDGE / "metadata/meta/custom-objecttype-definitions.xml"
    ).getroot()
    key = root.find(".//m:key-definition", namespace)
    attempts = root.find(
        ".//m:attribute-definition[@attribute-id='attempts']", namespace
    )
    group = root.find(".//m:attribute-group", namespace)
    assert key is not None
    assert key.find("m:field-length", namespace).text == "256"
    assert key.find("m:max-length", namespace) is None
    assert attempts is not None
    assert attempts.find("m:type", namespace).text == "int"
    attempts_children = [child.tag.rsplit("}", 1)[-1] for child in attempts]
    assert attempts_children.index("min-value") < attempts_children.index("default-value")
    assert group is not None
    assert "ID" in {
        element.attrib["attribute-id"]
        for element in group.findall("m:attribute", namespace)
    }


def test_sfcc_cartridge_keeps_network_out_of_shopper_hooks_and_redacts_payload_logs():
    hook_sources = "\n".join(
        path.read_text()
        for path in (CARTRIDGE / "int_pivota_telemetry/cartridge/scripts/hooks").glob("*.js")
    )
    telemetry = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/pivota/Telemetry.js"
    ).read_text()
    service = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/services/PivotaTelemetryService.js"
    ).read_text()
    assert "LocalServiceRegistry" not in hook_sources
    assert "LocalServiceRegistry" not in telemetry
    assert "CustomObjectMgr.createCustomObject" in telemetry
    assert "paymentAmount(paymentInstrument)" in telemetry
    assert "safe.length < MAX_LINE_ITEMS" in telemetry
    assert "stitchedBasketId" in telemetry
    assert "HMAC_SHA_256" in service
    assert "timestamp + '.' + body" in service
    assert "setAuthentication('NONE')" in service
    assert "addHeader('Authorization'" not in service
    assert "[Pivota telemetry payload redacted]" in service
    for sensitive_key in ("email", "phone", "address", "card", "cookie"):
        assert sensitive_key not in telemetry.lower()


def test_sfcc_cartridge_drain_has_bounded_batches_retry_and_iterator_cleanup():
    drain = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/jobs/DrainPivotaTelemetry.js"
    ).read_text()
    assert "Math.min(Math.floor(parsed), 100)" in drain
    assert "iterator.close()" in drain
    assert "markRetry(objects)" in drain
    assert "CustomObjectMgr.remove(object)" in drain
    assert "Math.min(3600" in drain
    assert "dw/util/Bytes" in drain
    assert "MAX_BATCH_BYTES = 900000" in drain
    assert "new Bytes(candidateBody, 'UTF-8').length" in drain
    assert "Service.send(payload.events)" in drain
    assert "removeBatch(payload.objects)" in drain


def test_sfcc_cartridge_stitches_submitted_basket_to_created_order():
    hooks_root = CARTRIDGE / "int_pivota_telemetry/cartridge/scripts/hooks"
    submitted = (hooks_root / "CheckoutSubmitted.js").read_text()
    created = (hooks_root / "OrderCreated.js").read_text()
    assert not (hooks_root / "CheckoutStarted.js").exists()
    assert "request.custom.pivotaTelemetryBasketId" in submitted
    assert "safeEnqueue('checkout.submitted', basket)" in submitted
    assert "request.custom.pivotaTelemetryBasketId" in created
    assert "{basket_id: basketId}" in created


def test_sfcc_cartridge_uses_site_specific_service_credentials():
    service = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/services/PivotaTelemetryService.js"
    ).read_text()
    assert "service.setCredentialID(CREDENTIAL_PREFIX + Site.getCurrent().getID())" in service
