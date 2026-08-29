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
    step = steptypes["step-types"]["script-module-step"][0]
    assert step["@type-id"] == "custom.PivotaTelemetryDrain"
    assert step["@supports-parallel-execution"] is False
    for relative in (
        "metadata/meta/custom-objecttype-definitions.xml",
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
