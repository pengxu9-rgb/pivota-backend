"""The Squarespace registration points, and the two literals nothing else pins.

`services/squarespace_ledger.py` spells its write paths as string LITERALS at
the ingest call, because tests/test_commerce_ledger_write_path_authority.py
requires that of every production ingest. A literal is exactly what a rename of
the module constant would leave behind, silently writing rows under a write path
the provenance table has never heard of — which `resolve_ledger_authority`
raises on, but only at RUNTIME, on a real delivery. This file closes that gap,
and covers the other registrations a new platform has to make.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ingest_write_path_literals() -> set[str]:
    """Every string constant passed as `write_path=` in squarespace_ledger.py."""
    tree = ast.parse(
        (REPO_ROOT / "services" / "squarespace_ledger.py").read_text(encoding="utf-8")
    )
    literals: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", None)
        )
        if name != "ingest_merchant_event_batch":
            continue
        for keyword in node.keywords:
            if keyword.arg != "write_path":
                continue
            value = keyword.value
            assert isinstance(value, ast.IfExp), ast.dump(value)[:80]
            for arm in (value.body, value.orelse):
                assert isinstance(arm, ast.Constant), ast.dump(arm)[:80]
                literals.add(arm.value)
    return literals


def test_the_hard_coded_write_paths_are_the_module_constants():
    from services.squarespace_ledger import (
        SQUARESPACE_RECONCILIATION_WRITE_PATH,
        SQUARESPACE_WEBHOOK_WRITE_PATH,
    )

    assert _ingest_write_path_literals() == {
        SQUARESPACE_WEBHOOK_WRITE_PATH,
        SQUARESPACE_RECONCILIATION_WRITE_PATH,
    }


def test_both_write_paths_are_ledger_vocabulary_with_the_platform_authority():
    from services.merchant_event_ingest_service import WritePath
    from services.squarespace_ledger import SQUARESPACE_REFUND_WRITE_PATHS
    from services.commerce_ledger_provenance import resolve_ledger_authority

    for path in _ingest_write_path_literals():
        assert path in set(WritePath.__args__)
        assert resolve_ledger_authority(path, "platform_asserted") == "platform"
    # The refund baseline reads across BOTH, and only those two.
    assert set(SQUARESPACE_REFUND_WRITE_PATHS) == _ingest_write_path_literals()


def test_the_refund_baseline_covers_every_squarespace_ingress():
    """If a third Squarespace ingress is ever added, the baseline must grow with
    it. Otherwise the new path reads a baseline of zero and re-records the whole
    cumulative refund total under a key the funnel sums with the others."""
    from services.commerce_ledger_provenance import LEDGER_AUTHORITY_BY_WRITE_PATH
    from services.squarespace_ledger import SQUARESPACE_REFUND_WRITE_PATHS

    declared = {
        path for path in LEDGER_AUTHORITY_BY_WRITE_PATH if path.startswith("squarespace")
    }
    assert declared == set(SQUARESPACE_REFUND_WRITE_PATHS)


# ---- the other registration points a new platform has to make --------------


def test_the_order_ref_namespace_round_trips():
    from services.commerce_order_ref import (
        build_order_ref,
        is_valid_order_ref,
        order_ref_namespace,
    )

    ref = build_order_ref("squarespace", "5e1f0b6a1c9d440000a1b2c3")
    assert ref == "squarespace:5e1f0b6a1c9d440000a1b2c3"
    assert is_valid_order_ref(ref)
    assert order_ref_namespace(ref) == "squarespace"


def test_a_merchant_collector_bound_to_a_squarespace_store_may_use_the_namespace():
    """`bind_batch_to_stores` refuses an `order_ref` whose namespace is not the
    connected store's platform. A platform whose namespace token did not match
    the stored `platform` string would refuse every one of that merchant's own
    events, and the failure would only show up for merchants using the HMAC
    collector alongside the native bridge."""
    from services.merchant_event_ingest_service import (
        MerchantCommerceEvent,
        MerchantEventBatch,
    )
    from services.merchant_event_store_binding import bind_batch_to_stores

    batch = MerchantEventBatch(
        events=[
            MerchantCommerceEvent(
                event_id="e1",
                event_type="order.paid",
                occurred_at="2026-09-01T10:00:00Z",
                platform="squarespace",
                store_id="store-sq",
                order_id="o1",
                order_ref="squarespace:o1",
                amount_cents=1000,
                currency="USD",
            )
        ]
    )

    bound = bind_batch_to_stores(batch, stores={"store-sq": "squarespace"})

    assert bound.events[0].platform == "squarespace"
    assert bound.events[0].order_ref == "squarespace:o1"


def test_the_source_registry_claims_telemetry_and_refuses_catalog_sync():
    """Claiming `catalog_pull` would make an EMPTY product sync report success.

    Nothing in this repo reads Squarespace's Products API; the integration is
    telemetry only, and the registry has to say so rather than stay silent.
    """
    from services.commerce_source_registry import (
        catalog_sync_blocker,
        get_commerce_source,
    )

    definition = get_commerce_source("squarespace")
    assert definition is not None
    assert definition.capabilities.catalog_pull is False
    blocker = catalog_sync_blocker("squarespace")
    assert blocker and "telemetry" in blocker.lower()


def test_the_order_number_metadata_key_is_allowed_and_not_read_as_sensitive():
    from services.merchant_event_ingest_service import (
        ALLOWED_MERCHANT_METADATA_KEYS,
        _is_sensitive_metadata_key,
    )

    assert "native_order_number" in ALLOWED_MERCHANT_METADATA_KEYS
    assert _is_sensitive_metadata_key("native_order_number") is False


def test_the_webhook_receiver_is_mounted_on_the_app():
    """A router that is written but never included is a route that 404s in
    production while every unit test passes."""
    import main

    paths = {getattr(route, "path", "") for route in main.app.routes}
    assert "/webhooks/squarespace/{store_id}" in paths
    assert "/integrations/squarespace/connect" in paths
    assert "/integrations/squarespace/{store_id}/webhooks/ensure" in paths
    assert "/integrations/squarespace/{store_id}/reconcile" in paths


# ---- the generalized baseline read ----------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("shoplazza_webhook", ["shoplazza_webhook"]),
        (["a", "b"], ["a", "b"]),
        (("a", "a", " b "), ["a", "b"]),
        ([], []),
        ("", []),
        (None, []),
        (123, []),
    ],
)
def test_the_write_path_scope_accepts_one_path_or_several(value, expected):
    from services.commerce_interaction_service import _write_path_scope

    assert _write_path_scope(value) == expected


async def test_an_empty_write_path_scope_reads_nothing():
    """The read must never widen to "every write path" when handed nothing —
    that would subtract a PSP's refunds from a platform's cumulative total."""
    from services.commerce_interaction_service import recorded_refund_amount_cents

    assert (
        await recorded_refund_amount_cents(
            merchant_id="m", store_id="s", order_ref="squarespace:o", write_path=[]
        )
        == 0
    )
