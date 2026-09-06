import json
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
CARTRIDGE = ROOT / "integrations" / "sfcc-cartridge"
SWEEP = (
    CARTRIDGE / "int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js"
)


def _function_body(source: str, name: str) -> str:
    """The whole text of one top-level function: from its `function` keyword to
    the next top-level `function`.

    Slicing a branch with `split("}", 1)` does NOT work on this file — the first
    `}` inside `emitPaid`'s skip branch belongs to the `{0}` placeholder in the
    log format string, so the slice ended before the branch body and every
    `assert "..." not in slice` in it was vacuous. Cutting on the function
    boundary first, and on the branch's own terminator second, is what makes
    those assertions able to fail.
    """
    start = source.index("function %s(" % name)
    rest = source[start:]
    end = rest.find("\nfunction ")
    return rest if end == -1 else rest[:end]


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
    assert set(parameters) == {
        "MaxOrders",
        "OverlapMinutes",
        "InitialLookbackHours",
        "MaxFailureLagHours",
    }
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
    sweep = SWEEP.read_text()
    # Refunds are keyed on the credit invoice AND the cumulative figure that
    # invoice had reached, never on the order: two credit invoices on one order
    # must stay two refund rows, and two partial refunds against ONE invoice
    # must stay two rows as well.
    assert "'refund.succeeded:' + key" in sweep
    assert "refund_id: key," in sweep
    assert "var key = refundKey(invoiceNumber, cumulative);" in sweep
    # …and `order.paid` / `order.cancelled` on the order, so a redelivery
    # dedupes in the ledger instead of writing a second row.
    assert "'order.paid:' + order.orderNo" in sweep
    assert "'order.cancelled:' + order.orderNo" in sweep

    # Only a positive settled amount is enqueued. A zero-amount money event
    # under a native id is a permanent shadow, because the ledger dedupes
    # first-write-wins on the key derived from that id.
    assert "if (!(delta > 0)) {" in sweep
    assert "amount is not positive" in sweep
    assert "if (total === null || total <= 0) {" in sweep
    assert "no positive total gross price" in sweep

    # The zero-total skip must not mark the order as emitted, or a corrected
    # total could never be reported. Slice the WHOLE function first, then the
    # branch up to its own `return true;`.
    paid = _function_body(sweep, "emitPaid")
    assert "Telemetry.safeEnqueue('order.paid'" in paid
    assert "function emitCancelled" not in paid, "the emitPaid slice ran past its end"
    paid_skip = paid.split("if (total === null || total <= 0) {", 1)[1].split(
        "return true;", 1
    )[0]
    # Positive counterparts: the slice really is the skip branch's body…
    assert "no positive total gross price" in paid_skip
    assert "counts.skipped += 1;" in paid_skip
    # …so this can fail.
    assert "markEmitted" not in paid_skip

    # Same for both refund skip branches: neither enqueues and neither marks.
    refunds = _function_body(sweep, "emitRefunds")
    assert "Telemetry.safeEnqueue('refund.succeeded'" in refunds
    assert "function sweepOrder" not in refunds, "the emitRefunds slice ran past its end"
    not_positive = refunds.split("if (!(delta > 0)) {", 1)[1].split(
        "var key = refundKey(", 1
    )[0]
    assert "amount is not positive" in not_positive
    assert "counts.skipped += 1;" in not_positive
    assert "rememberRefund" not in not_positive
    assert "safeEnqueue" not in not_positive
    decreased = refunds.split("if (cumulative < previous) {", 1)[1].split(
        "var delta =", 1
    )[0]
    assert "counts.skipped += 1;" in decreased
    assert "rememberRefund" not in decreased
    assert "safeEnqueue" not in decreased

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


def test_sfcc_settlement_sweep_marks_an_order_only_after_the_enqueue():
    """Order matters, not just presence.

    "Written only AFTER a successful enqueue" is the whole of once-only layer 1.
    Marked FIRST, an enqueue that then failed would be recorded as delivered and
    the fact lost for good — and every `markEmitted` / `rememberRefund`
    assertion elsewhere in this file would still pass, because they only ask
    whether the call exists. So the position is pinned.
    """
    sweep = SWEEP.read_text()
    assert sweep.index("var enqueued = Telemetry.safeEnqueue('order.paid'") < sweep.index(
        "markEmitted(order, PAID_MARKER);"
    )
    assert sweep.index(
        "var enqueued = Telemetry.safeEnqueue('order.cancelled'"
    ) < sweep.index("markEmitted(order, CANCELLED_MARKER);")
    assert sweep.index("Telemetry.safeEnqueue('refund.succeeded'") < sweep.index(
        "markers = rememberRefund(order, markers, invoiceNumber, key);"
    )
    # And each marker write is guarded by the enqueue's own return value.
    assert sweep.count("if (!enqueued) {") == 3


def test_sfcc_settlement_sweep_emits_refund_deltas_under_a_sequence_qualified_key():
    """A SECOND partial refund against the SAME invoice must still land.

    `Invoice.refundedAmount` is cumulative per invoice: a second partial refund
    raises it rather than creating a second invoice. Keyed on the invoice number
    alone the second refund was lost twice over — the once-only marker skipped
    the invoice, and even with the marker gone the ledger deduped the event
    against the first observation's key. So the marker stores the cumulative
    figure that was reported, the amount sent is the DIFFERENCE, and the key
    carries the new cumulative total.
    """
    sweep = SWEEP.read_text()
    # The marker IS the refund id: `<invoiceNumber>:<cumulative>`.
    assert "return invoiceNumber + ':' + stableAmount(cumulative);" in sweep
    assert "return Number(value).toFixed(2);" in sweep
    # The last reported cumulative figure is parsed back out of the marker,
    # splitting on the LAST colon (an invoice number may contain one).
    assert "var separator = marker.lastIndexOf(':');" in sweep
    assert "function observedCumulative(markers, invoiceNumber)" in sweep
    # The amount on the wire is the delta, never the cumulative figure.
    assert "var delta = cumulative - previous;" in sweep
    assert "amount: stableAmount(delta)," in sweep
    assert "amount: stableAmount(cumulative)," not in sweep
    # A cumulative figure that DROPPED is skipped loudly and never emitted:
    # a negative amount is refused by the mapper, and re-sending the lower
    # figure under a new key would ADD refunded money.
    assert "if (cumulative < previous) {" in sweep
    assert "refunded amount fell from {2} to {3}" in sweep
    # One marker per invoice — a new observation replaces that invoice's marker
    # instead of appending, so the capped set bounds refunded INVOICES per
    # order rather than observations of them.
    assert "function markersWithout(markers, invoiceNumber)" in sweep
    assert "markersWithout(markers, invoiceNumber).concat([key])" in sweep
    assert "MAX_REFUND_MARKERS = 200" in sweep

    docs = (ROOT / "docs/SFCC_TELEMETRY.md").read_text()
    readme = (CARTRIDGE / "README.md").read_text()
    # The docs claimed the opposite before this change, in both files.
    assert "frozen at first observation" not in docs
    assert "frozen at first observation" not in readme
    for text in (docs, readme):
        assert "refund.succeeded:<invoiceNumber>:<cumulative>" in text
        assert "delta" in text.lower()


def test_sfcc_settlement_sweep_reports_an_abandonment_as_a_job_failure():
    """`Status.OK` is invisible.

    Business Manager fires a job notification on a step's non-OK status and on
    nothing else, so a run that permanently dropped settlement facts must not
    report OK — the ERROR line naming the orders would otherwise sit unread in
    the merchant's own job log, which nothing on the Pivota side reads either.
    """
    sweep = SWEEP.read_text()
    assert "return new Status(Status.ERROR, 'ABANDONED', message);" in sweep
    assert "if (abandonedOrders.length) {" in sweep
    assert "' abandoned=' + abandonedOrders.length" in sweep
    # …and the ERROR log fires only when something really was abandoned. An
    # empty list is not an abandonment and must not claim a permanent loss.
    override = sweep.split("if (effective.getTime() < lagBound.getTime()) {", 1)[1]
    guarded = override.split("if (abandonedOrders.length) {", 1)
    assert len(guarded) == 2, "the ABANDONED log must be guarded by a non-empty list"
    logged = guarded[1].split("} else {", 1)
    assert len(logged) == 2, "the empty case needs its own, quieter branch"
    assert "logger.error(" in logged[0]
    assert "ABANDONED" in logged[0]
    assert "logger.error(" not in logged[1].split("effective = lagBound;", 1)[0]
    assert "nothing was abandoned" in logged[1]

    # The status code is declared, or Business Manager shows an unknown code.
    steptypes = json.loads(
        (CARTRIDGE / "int_pivota_telemetry/steptypes.json").read_text()
    )
    sweep_step = next(
        step
        for step in steptypes["step-types"]["script-module-step"]
        if step["@type-id"] == "custom.PivotaSettlementSweep"
    )
    codes = {status["@code"] for status in sweep_step["status-codes"]["status"]}
    assert codes == {"OK", "ABANDONED", "ERROR"}

    # A failing status must not stop the next tick: the step is not restarted.
    jobs_text = (CARTRIDGE / "metadata/jobs.xml").read_text()
    assert (
        '<step step-id="PivotaSettlementSweep" type="custom.PivotaSettlementSweep" '
        'enforce-restart="false">' in jobs_text
    )
    readme = (CARTRIDGE / "README.md").read_text()
    assert 'enforce-restart="false"' in readme


def test_sfcc_outbox_enqueue_treats_an_occupied_key_as_success():
    """`createCustomObject` raises on a duplicate key.

    The sweep's event ids are deterministic and the drain keeps an undelivered
    row for up to seven days, so re-enqueuing an event that is still in the
    outbox is the normal shape of a Pivota outage. Without a lookup first that
    raise made `safeEnqueue` return false, the sweep counted the order failed,
    held its cursor, and `MaxFailureLagHours` eventually abandoned it — during
    the outage.
    """
    telemetry = (
        CARTRIDGE / "int_pivota_telemetry/cartridge/scripts/pivota/Telemetry.js"
    ).read_text()
    assert "CustomObjectMgr.getCustomObject(OUTBOX_TYPE, key)" in telemetry
    assert telemetry.index(
        "CustomObjectMgr.getCustomObject(OUTBOX_TYPE, key)"
    ) < telemetry.index("CustomObjectMgr.createCustomObject(OUTBOX_TYPE, key)")
    # It is a SUCCESS, not a swallowed failure: nothing is thrown and nothing
    # is written, and the outcome is logged at debug rather than error.
    assert "existed = true;" in telemetry
    assert "Logger.debug(" in telemetry
    assert "already queued" in telemetry
    docs = (ROOT / "docs/SFCC_TELEMETRY.md").read_text()
    assert "occupied outbox key" in docs


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


def test_sfcc_settlement_sweep_bounds_the_failure_clamp_so_a_poison_order_cannot_stall_the_site():
    """`firstFailureAt` clamps the cursor to the first undeliverable order.

    Unbounded, that is a silent site-wide outage: an order that fails on EVERY
    tick — bad data, an exception inside `buildEvent` — pins the cursor to its
    own `lastModified` forever, and because `MaxOrders` bounds each run the
    sweep re-scans the same window and never reaches a newer order. Every later
    settlement, cancellation and refund for the site is lost, and the only
    symptom is a `failed=` count in the merchant's own job log.

    So the clamp is bounded by `MaxFailureLagHours`, and when the bound
    overrides the clamp the abandoned order numbers are logged at ERROR.
    """
    steptypes = json.loads((CARTRIDGE / "int_pivota_telemetry/steptypes.json").read_text())
    sweep_step = next(
        step
        for step in steptypes["step-types"]["script-module-step"]
        if step["@type-id"] == "custom.PivotaSettlementSweep"
    )
    parameters = {
        parameter["@name"]: parameter for parameter in sweep_step["parameters"]["parameter"]
    }
    # Registered, or the job cannot be tuned and the default is the only value.
    assert "MaxFailureLagHours" in parameters
    lag = parameters["MaxFailureLagHours"]
    assert lag["@type"] == "long"
    assert lag["default-value"] == 24
    assert lag["min-value"] == 1
    assert lag["max-value"] == 168
    assert lag["@required"] is False

    jobs_text = (CARTRIDGE / "metadata/jobs.xml").read_text()
    assert '<parameter name="MaxFailureLagHours">24</parameter>' in jobs_text

    sweep = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js"
    ).read_text()
    # The same 1..168 bound is enforced in code, not only declared in the manifest:
    # a job imported before this steptypes.json would otherwise pass anything.
    assert "DEFAULT_MAX_FAILURE_LAG_HOURS = 24" in sweep
    assert "MIN_MAX_FAILURE_LAG_HOURS = 1" in sweep
    assert "MAX_MAX_FAILURE_LAG_HOURS = 168" in sweep
    assert "parameters && parameters.MaxFailureLagHours" in sweep
    assert "DEFAULT_MAX_FAILURE_LAG_HOURS,\n        MIN_MAX_FAILURE_LAG_HOURS," in sweep

    # The bound is measured against the newest order OBSERVED, not against the
    # watermark: a run in which every order failed has no watermark, and that is
    # precisely the run that needs the bound.
    assert "newestSeen" in sweep
    assert (
        "var lagBound = new Date(newestSeen.getTime() - maxFailureLagHours * 60 * 60 * 1000);"
        in sweep
    )
    # …and the clamp still wins whenever it is INSIDE the bound, so an ordinary
    # transient failure is still retried rather than abandoned.
    assert "if (firstFailureAt && (!effective || firstFailureAt.getTime() < effective.getTime()))" in sweep

    # The override and its ERROR log must be the same branch — a log placed
    # outside it would never fire on the run that abandons an order.
    override = sweep.split("if (effective.getTime() < lagBound.getTime()) {", 1)
    assert len(override) == 2, "the lag bound must override the failure clamp"
    override_body = override[1].split("\n        }", 1)[0]
    assert "logger.error(" in override_body
    assert "ABANDONED" in override_body
    # The log names the orders, so support can replay them by hand.
    assert "abandonedOrders.join(', ')" in override_body
    assert "failure.orderNo" in override_body
    assert "effective = lagBound;" in override_body

    docs = (ROOT / "docs/SFCC_TELEMETRY.md").read_text()
    assert "MaxFailureLagHours" in docs
    assert "abandon" in docs.lower()
    readme = (CARTRIDGE / "README.md").read_text()
    assert "MaxFailureLagHours" in readme
    assert "abandon" in readme.lower()


def test_sfcc_settlement_sweep_reads_the_invoice_type_under_its_real_property_name():
    """`dw.order.Invoice`'s accessor is `getInvoiceType()`, so the script-API
    property is `invoiceType`. Reading only `type` — very likely `undefined` —
    made the credit-invoice fallback dead code: the grand-total refund path was
    unreachable and the "skipped, amount is not positive" WARN could never fire
    for a real credit invoice.
    """
    sweep = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js"
    ).read_text()
    # `invoiceType` is the real name and is read FIRST; `type` is the fallback.
    assert "invoice.invoiceType || invoice.type" in sweep
    assert "invoice.type || invoice.invoiceType" not in sweep
    # The token matching is kept — the class constants are an Order Management
    # surface this cartridge must not import, and a mis-named constant is
    # `undefined`, which matches nothing.
    assert "CREDIT_TYPE_TOKENS = ['credit', 'return', 'appeasement']" in sweep
    assert "type.indexOf(CREDIT_TYPE_TOKENS[index]) !== -1" in sweep
    # The old, wrong-only read must be gone.
    assert "invoice && invoice.type ? String(invoice.type).toLowerCase()" not in sweep

    docs = (ROOT / "docs/SFCC_TELEMETRY.md").read_text()
    # Both names are listed with their verification status.
    assert "`Invoice.invoiceType`" in docs
    assert "getInvoiceType()" in docs


def test_sfcc_sweep_cursor_is_scoped_to_the_site_like_the_outbox_it_feeds():
    """`OrderMgr.searchOrders` only ever sees the current site's orders, so the
    cursor must be per site too. It is site-SCOPED (like `PivotaTelemetryOutbox`)
    rather than site-KEYED, and the bare id `settlement` is only correct because
    of that — so the scope is pinned here.
    """
    namespace = {"m": "http://www.demandware.com/xml/impex/metadata/2006-10-31"}
    root = ElementTree.parse(
        CARTRIDGE / "metadata/meta/custom-objecttype-definitions.xml"
    ).getroot()
    scopes = {
        custom_type.attrib["type-id"]: custom_type.find("m:storage-scope", namespace).text
        for custom_type in root.findall("m:custom-type", namespace)
    }
    assert scopes == {
        "PivotaTelemetryOutbox": "site",
        "PivotaTelemetrySweepCursor": "site",
    }

    sweep = (
        CARTRIDGE
        / "int_pivota_telemetry/cartridge/scripts/jobs/SweepPivotaSettlements.js"
    ).read_text()
    assert "CURSOR_ID = 'settlement'" in sweep
    # The bare id is deliberate and the reason is written down next to it, so a
    # later scope change cannot silently make one site's cursor hide another's.
    assert "storage-scope" in sweep
    assert "dw/system/Site" in sweep

    # The step runs in site context, which is what makes that scope resolvable
    # at all — an organization-context step would find no site cursor.
    steptypes = json.loads((CARTRIDGE / "int_pivota_telemetry/steptypes.json").read_text())
    sweep_step = next(
        step
        for step in steptypes["step-types"]["script-module-step"]
        if step["@type-id"] == "custom.PivotaSettlementSweep"
    )
    assert sweep_step["@supports-site-context"] is True
    assert sweep_step["@supports-organization-context"] is False


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
