from __future__ import annotations

import pytest

from services.catalog_offer_writer_guard import (
    ORPHAN_NO_SKU,
    ZERO_OR_MISSING_PRICE,
    WriterAuditAccumulator,
    validate_catalog_offer_rows,
    write_writer_audit_log,
)


def _offer(offer_id: str, *, sku_key: str = "sku_1", list_price=10):
    return {
        "offer_id": offer_id,
        "sku_key": sku_key,
        "list_price": list_price,
    }


def test_offer_guard_rejects_zero_negative_and_missing_price() -> None:
    accepted, reasons, rejected = validate_catalog_offer_rows(
        [
            _offer("offer_zero", list_price=0),
            _offer("offer_negative", list_price=-1),
            _offer("offer_missing", list_price=None),
        ],
        existing_sku_keys={"sku_1"},
    )

    assert accepted == []
    assert reasons[ZERO_OR_MISSING_PRICE] == 3
    assert [row["offer_id"] for row in rejected] == [
        "offer_zero",
        "offer_negative",
        "offer_missing",
    ]


def test_offer_guard_rejects_orphan_sku_reference() -> None:
    accepted, reasons, rejected = validate_catalog_offer_rows(
        [_offer("offer_orphan", sku_key="missing_sku", list_price=25)],
        existing_sku_keys={"sku_1"},
    )

    assert accepted == []
    assert reasons == {ORPHAN_NO_SKU: 1}
    assert rejected[0]["reasons"] == [ORPHAN_NO_SKU]


def test_offer_guard_accepts_valid_positive_price_row() -> None:
    accepted, reasons, rejected = validate_catalog_offer_rows(
        [_offer("offer_valid", list_price=25)],
        existing_sku_keys={"sku_1"},
    )

    assert [row["offer_id"] for row in accepted] == ["offer_valid"]
    assert reasons == {}
    assert rejected == []


def test_offer_guard_mixed_batch_writes_only_valid_rows() -> None:
    accepted, reasons, rejected = validate_catalog_offer_rows(
        [
            _offer("offer_valid", sku_key="sku_1", list_price=25),
            _offer("offer_zero", sku_key="sku_1", list_price=0),
            _offer("offer_orphan", sku_key="missing_sku", list_price=25),
        ],
        existing_sku_keys={"sku_1"},
    )

    assert [row["offer_id"] for row in accepted] == ["offer_valid"]
    assert reasons == {ZERO_OR_MISSING_PRICE: 1, ORPHAN_NO_SKU: 1}
    assert [row["offer_id"] for row in rejected] == ["offer_zero", "offer_orphan"]


@pytest.mark.asyncio
async def test_writer_audit_log_records_counts_and_reasons() -> None:
    calls = []

    class FakeDb:
        async def execute(self, sql, values):
            calls.append({"sql": str(sql), "values": dict(values)})

    audit = WriterAuditAccumulator(writer_name="shopify_products_sync", batch_id="batch_1")
    audit.record_applied(2)
    audit.record_skips({ZERO_OR_MISSING_PRICE: 1, ORPHAN_NO_SKU: 3})

    await write_writer_audit_log(audit, actor="test_actor", db=FakeDb())

    assert len(calls) == 1
    values = calls[0]["values"]
    assert values["writer_name"] == "shopify_products_sync"
    assert values["batch_id"] == "batch_1"
    assert values["applied_rows"] == 2
    assert values["skipped_rows"] == 4
    assert '"zero_or_missing_price": 1' in values["reasons"]
    assert '"orphan_no_sku": 3' in values["reasons"]
    assert values["actor"] == "test_actor"
