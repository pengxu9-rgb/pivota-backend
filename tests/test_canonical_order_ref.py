"""One canonical order identity across every authority (migration 216).

The same purchase reaches the ledger from the Stripe PSP bridge, the store
platform's own webhook, the agent checkout and the attribution edge — and each
of them names the order with its own id. The funnel keyed paid amounts and
order counts on `(platform, store_id, order_id)`, so a Pivota-originated
Shopify order paid through Stripe counted its GMV twice: once under the Pivota
order id the Stripe event carried, once under the Shopify order id the
`orders/paid` webhook carried.

`order_ref` is `<namespace>:<id in that namespace's system of record>`. Every
authority that can recognise a Pivota-originated order emits the same
`pivota:<orders.order_id>`; an order placed on the storefront keeps
`<platform>:<native id>`.

Declaration tests come first, for the reason
tests/test_commerce_ledger_write_path_authority.py gives: `create_all` runs
BEFORE migrations, so the MODEL builds a fresh database while the migration
only fixes existing ones. Four homes must agree — model, migration, its down
file, and the schema-guard self-heal for a deploy that skips db/migrations/.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import databases
import pytest
from sqlalchemy import create_engine, select

from db.commerce_interactions import commerce_interaction_events, commerce_interactions
from db.database import metadata

REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = REPO_ROOT / "db" / "migrations" / "216_commerce_ledger_canonical_order_ref.sql"
_DOWN = REPO_ROOT / "db" / "migrations" / "down" / "216_commerce_ledger_canonical_order_ref_down.sql"

PIVOTA_ORDER_ID = "ord_1"
SHOPIFY_ORDER_ID = "6600123"
PIVOTA_REF = "pivota:ord_1"
STORE_ID = "store_a"
MERCHANT_ID = "merchant-a"
ORDER_TOTAL_CENTS = 4999


# ---- 1. the four schema homes must agree -----------------------------------


def test_the_model_declares_order_ref_on_both_ledger_tables():
    for table in (commerce_interactions, commerce_interaction_events):
        assert "order_ref" in table.c, f"{table.name} is missing order_ref"
        column = table.c.order_ref
        assert column.nullable is True
        assert column.type.length == 160
    # order_id is untouched: it stays the diagnostic record of what one
    # authority called the order.
    assert commerce_interactions.c.order_id.type.length == 128


def test_the_model_declares_the_store_scoped_unique_index():
    index = next(
        idx
        for idx in commerce_interactions.indexes
        if idx.name == "idx_commerce_interactions_order_ref_unique"
    )
    assert index.unique is True
    # Mirrors idx_commerce_interactions_order_id_unique: merchant, store scope,
    # then the reference itself.
    rendered = [str(expr) for expr in index.expressions]
    assert rendered[0].endswith("merchant_id")
    assert "coalesce" in rendered[1].lower() and "store_id" in rendered[1]
    assert rendered[2].endswith("order_ref")
    assert index.dialect_options["postgresql"]["where"] is not None


def test_the_migration_agrees_with_the_model():
    sql = _MIGRATION.read_text()
    assert sql.count("ADD COLUMN IF NOT EXISTS order_ref VARCHAR(160) NULL") == 2
    assert "ALTER TABLE commerce_interactions" in sql
    assert "ALTER TABLE commerce_interaction_events" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_interactions_order_ref_unique" in sql
    assert "(merchant_id, COALESCE(store_id, ''), order_ref)" in sql


def test_the_migration_is_not_classified_as_a_concurrent_index_build():
    """The runner regexes the WHOLE file, comments included.

    A prose mention of that keyword would put the two ALTER TABLEs on the
    autocommit path, where they lose their transaction. Ask the runner, not a
    substring — the file deliberately talks about the classifier.
    """
    from db.sql_migrations import needs_autocommit

    assert needs_autocommit(_MIGRATION.read_text()) is False
    assert needs_autocommit(_DOWN.read_text()) is False


def test_the_down_migration_removes_everything_the_migration_adds():
    down = _DOWN.read_text()
    assert down.count("DROP COLUMN IF EXISTS order_ref") == 2
    assert "DROP INDEX IF EXISTS idx_commerce_interactions_order_ref_unique" in down


def test_the_runtime_self_heal_carries_both_columns_and_the_unique_index():
    guard = (REPO_ROOT / "db" / "schema_guard.py").read_text()
    assert guard.count("ADD COLUMN IF NOT EXISTS order_ref VARCHAR(160) NULL") == 2
    # Unlike mig 214's index this one is built normally, so the guard must
    # carry it: without the unique index two authorities can both insert an
    # interaction for one canonical order and neither insert raises.
    assert "idx_commerce_interactions_order_ref_unique" in guard


# ---- 2. the ref rule itself ------------------------------------------------


@pytest.mark.parametrize(
    "namespace, native, expected",
    [
        ("pivota", "ord_1", "pivota:ord_1"),
        ("shopify", "6600123", "shopify:6600123"),
        ("WooCommerce", "44", "woocommerce:44"),
        ("salesforce commerce cloud", "abc", "salesforce_commerce_cloud:abc"),
    ],
)
def test_build_order_ref_namespaces_and_normalizes(namespace, native, expected):
    from services.commerce_order_ref import build_order_ref

    assert build_order_ref(namespace, native) == expected


@pytest.mark.parametrize("namespace, native", [("", "1"), ("shopify", ""), ("shopify", "a b")])
def test_build_order_ref_returns_none_rather_than_raising(namespace, native):
    """Every caller is on a best-effort telemetry path: a ref we cannot build
    must degrade to legacy order_id keying, never drop the event."""
    from services.commerce_order_ref import build_order_ref

    assert build_order_ref(namespace, native) is None


def test_an_over_long_ref_is_refused_rather_than_truncated():
    from services.commerce_order_ref import ORDER_REF_MAX_LENGTH, build_order_ref

    assert build_order_ref("shopify", "x" * (ORDER_REF_MAX_LENGTH - 8)) is not None
    assert build_order_ref("shopify", "x" * ORDER_REF_MAX_LENGTH) is None


# ---- 3. the ledger, end to end on SQLite -----------------------------------


async def _sqlite_ledger(tmp_path, monkeypatch, name: str):
    db_path = tmp_path / f"{name}.sqlite3"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(
        sync_engine,
        tables=[commerce_interactions, commerce_interaction_events],
        checkfirst=True,
    )
    sync_engine.dispose()
    test_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await test_database.connect()
    from services import commerce_interaction_service as interaction_service
    import services.merchant_commerce_event_funnel_service as funnel_module

    monkeypatch.setattr(interaction_service, "database", test_database)
    monkeypatch.setattr(interaction_service, "IS_POSTGRES", False)
    monkeypatch.setattr(funnel_module, "database", test_database)
    return test_database


def _naive_batch(batch):
    """SQLite's DATETIME binding refuses tz-aware values.

    The bridges build their batch internally, so the tzinfo strip the sibling
    ledger tests do by hand has to happen here, after the real adapter ran.
    """
    for event in batch.events:
        event.occurred_at = event.occurred_at.replace(tzinfo=None)
    return batch


def _pivota_order(**overrides: Any) -> Dict[str, Any]:
    order = {
        "order_id": PIVOTA_ORDER_ID,
        "merchant_id": MERCHANT_ID,
        "store_id": STORE_ID,
        "shopify_order_id": SHOPIFY_ORDER_ID,
        "metadata": {},
    }
    order.update(overrides)
    return order


def _shopify_paid_payload(*, marker: bool) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": int(SHOPIFY_ORDER_ID),
        "order_number": 1001,
        "financial_status": "paid",
        "currency": "USD",
        "total_price": "49.99",
        "processed_at": "2026-09-04T10:00:00Z",
        "updated_at": "2026-09-04T10:00:00Z",
    }
    if marker:
        # What Pivota's Shopify order writeback stamps.
        payload["note_attributes"] = [
            {"name": "pivota_order_id", "value": PIVOTA_ORDER_ID}
        ]
    return payload


async def _drive_stripe(monkeypatch) -> Dict[str, Any]:
    import services.psp_commerce_event_ingest as bridge

    async def _scope(order):
        return (STORE_ID, "shopify")

    real_map = bridge.map_stripe_webhook_event

    def _map(*args, **kwargs):
        return _naive_batch(real_map(*args, **kwargs))

    monkeypatch.setattr(bridge, "resolve_order_store_scope", _scope)
    monkeypatch.setattr(bridge, "map_stripe_webhook_event", _map)
    return await bridge.ingest_stripe_commerce_event_best_effort(
        event_type="payment_intent.succeeded",
        stripe_event_id="evt_stripe_1",
        event_created=1757000000,
        data={
            "id": "pi_1",
            "amount_received": ORDER_TOTAL_CENTS,
            "currency": "usd",
            "status": "succeeded",
        },
        order=_pivota_order(),
        signature_verified=True,
    )


async def _drive_shopify(
    monkeypatch, *, marker: bool, pivota_order_id: Optional[str], topic: str = "orders/paid"
) -> Dict[str, Any]:
    import services.shopify_commerce_event_ingest as bridge

    async def _store(*, merchant_id, shop_domain):
        return STORE_ID

    async def _pivota(*, merchant_id, shopify_order_id):
        return pivota_order_id

    real_map = bridge.map_shopify_webhook

    def _map(*args, **kwargs):
        return _naive_batch(real_map(*args, **kwargs))

    monkeypatch.setattr(bridge, "resolve_shopify_store_id", _store)
    monkeypatch.setattr(bridge, "resolve_pivota_order_id", _pivota)
    monkeypatch.setattr(bridge, "map_shopify_webhook", _map)
    return await bridge.ingest_shopify_commerce_event_best_effort(
        merchant_id=MERCHANT_ID,
        shop_domain="shop.example.com",
        topic=topic,
        payload=_shopify_paid_payload(marker=marker),
        webhook_id="wh_1",
        occurred_at=datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc),
        signature_verified=True,
    )


async def _ledger_rows(test_database) -> List[Dict[str, Any]]:
    return [
        dict(row) for row in await test_database.fetch_all(select(commerce_interaction_events))
    ]


async def _interactions(test_database) -> List[Dict[str, Any]]:
    return [dict(row) for row in await test_database.fetch_all(select(commerce_interactions))]


async def _one_purchase_two_authorities(tmp_path, monkeypatch, name, *, marker, lookup):
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, name)
    stripe_result = await _drive_stripe(monkeypatch)
    shopify_result = await _drive_shopify(
        monkeypatch, marker=marker, pivota_order_id=lookup
    )
    assert stripe_result["status"] == "accepted", stripe_result
    assert shopify_result["status"] == "accepted", shopify_result
    return test_database


@pytest.mark.asyncio
async def test_stripe_and_shopify_agree_on_one_order_ref_via_the_writeback_marker(
    tmp_path, monkeypatch
):
    import services.merchant_commerce_event_funnel_service as funnel_module

    test_database = await _one_purchase_two_authorities(
        tmp_path, monkeypatch, "ref-marker", marker=True, lookup=None
    )
    try:
        rows = await _ledger_rows(test_database)
        assert len(rows) == 2
        # Two authorities, two native order ids, ONE canonical identity.
        assert {row["order_ref"] for row in rows} == {PIVOTA_REF}
        # order_id is not an events column; it lives in the payload. The two
        # authorities still disagree about it, which is the whole point.
        assert {row["payload"]["order_id"] for row in rows} == {
            PIVOTA_ORDER_ID,
            SHOPIFY_ORDER_ID,
        }

        interactions = await _interactions(test_database)
        assert len(interactions) == 1, interactions
        assert interactions[0]["order_ref"] == PIVOTA_REF

        result = await funnel_module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID, group_by="store"
        )
        assert result.payload["available"] is True
        # The purchase is counted ONCE, not once per namespace.
        assert result.payload["summary"]["paid_amount_cents_by_currency"] == {
            "USD": ORDER_TOTAL_CENTS
        }
        # The bare Pivota id rides along for the wrapper's legacy overlap.
        assert result.paid_order_ids == {PIVOTA_REF, PIVOTA_ORDER_ID}
        assert len(result.paid_keys) == 1
        assert result.payload["summary"]["stages"]["paid"] == 1
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_the_same_purchase_converges_on_the_shopify_order_id_lookup_alone(
    tmp_path, monkeypatch
):
    """No click, and no writeback marker: an order written back before the
    marker existed. The ingest's orders.shopify_order_id lookup is the only
    thing that recognises it, and it must be enough."""
    import services.merchant_commerce_event_funnel_service as funnel_module

    test_database = await _one_purchase_two_authorities(
        tmp_path, monkeypatch, "ref-lookup", marker=False, lookup=PIVOTA_ORDER_ID
    )
    try:
        rows = await _ledger_rows(test_database)
        assert {row["order_ref"] for row in rows} == {PIVOTA_REF}
        assert not any(row.get("click_id") for row in rows)
        assert len(await _interactions(test_database)) == 1

        result = await funnel_module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID, group_by="store"
        )
        assert result.payload["summary"]["paid_amount_cents_by_currency"] == {
            "USD": ORDER_TOTAL_CENTS
        }
        # The bare Pivota id rides along for the wrapper's legacy overlap.
        assert result.paid_order_ids == {PIVOTA_REF, PIVOTA_ORDER_ID}
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_without_the_marker_or_the_lookup_the_two_authorities_still_double_count(
    tmp_path, monkeypatch
):
    """The negative counterpart: order_ref is what fixes this, not luck.

    A Shopify order that is NOT Pivota-originated keeps `shopify:<id>` and is
    a genuinely different purchase from `pivota:ord_1`, so two paid keys and
    two amounts is the CORRECT answer here.
    """
    import services.merchant_commerce_event_funnel_service as funnel_module

    test_database = await _one_purchase_two_authorities(
        tmp_path, monkeypatch, "ref-none", marker=False, lookup=None
    )
    try:
        rows = await _ledger_rows(test_database)
        assert {row["order_ref"] for row in rows} == {
            PIVOTA_REF,
            f"shopify:{SHOPIFY_ORDER_ID}",
        }
        result = await funnel_module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID, group_by="store"
        )
        assert result.paid_order_ids == {PIVOTA_REF, PIVOTA_ORDER_ID, f"shopify:{SHOPIFY_ORDER_ID}"}
        assert result.payload["summary"]["paid_amount_cents_by_currency"] == {
            "USD": ORDER_TOTAL_CENTS * 2
        }
    finally:
        await test_database.disconnect()


# ---- 4. the attribution edge and the platform adapter ----------------------


async def _drive_attribution_edge(monkeypatch, order_id: str) -> None:
    """Drive the real upsert_order_attribution_edge order.created writer.

    Only the edges table is faked (as tests/test_t2_2_external_conversion_closure
    does); the ledger write goes through the real
    record_commerce_event_best_effort into the SQLite ledger.
    """
    from services import commerce_attribution_service as svc

    class _EdgesDB:
        async def fetch_one(self, query: Any, values: Any = None):
            return None

        async def fetch_all(self, query: Any, values: Any = None):
            return []

        async def execute(self, query: Any, values: Any = None):
            return 0

    monkeypatch.setattr(svc, "database", _EdgesDB())
    await svc.upsert_order_attribution_edge(
        order_id=order_id,
        merchant_id=MERCHANT_ID,
        # An attribution signal, so the writer does not take the inferred
        # fallback (which would hit the faked edges DB for a click lookup).
        metadata={"pvt_click_id": "clk_abcdefgh", "platform": "shopify"},
    )


@pytest.mark.asyncio
async def test_the_attribution_edge_and_the_platform_adapter_count_one_order_created(
    tmp_path, monkeypatch
):
    """The double count P1-E names, on the order_created stage.

    The attribution edge writes `order.created` under the PIVOTA order id and
    with NO store_id; the Shopify adapter writes `order.created` under the
    SHOPIFY order id inside a store scope. Before order_ref those were two
    order keys for one purchase.
    """
    import services.merchant_commerce_event_funnel_service as funnel_module

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "ref-edge")
    try:
        await _drive_attribution_edge(monkeypatch, PIVOTA_ORDER_ID)
        result = await _drive_shopify(
            monkeypatch, marker=True, pivota_order_id=None, topic="orders/create"
        )
        assert result["status"] == "accepted", result

        rows = await _ledger_rows(test_database)
        assert [row["event_type"] for row in rows].count("order.created") == 2
        assert {row["order_ref"] for row in rows} == {PIVOTA_REF}
        # The edge carries no store scope at all; the adapter carries one.
        assert {(row.get("store_id") or "") for row in rows} == {"", STORE_ID}

        funnel = await funnel_module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID, group_by="store"
        )
        assert funnel.order_ids == {PIVOTA_REF, PIVOTA_ORDER_ID}
        # ONE order key for one purchase; two before order_ref existed.
        assert funnel.order_keys == {("order_ref", "order_ref", PIVOTA_REF)}
        # The `stages` counter counts INTERACTIONS, not orders, and the
        # attribution edge carries no store scope, so it is a second
        # interaction. order_ref lookups are store-scoped on purpose:
        # _merge_interactions refuses a cross-store merge, so an unscoped key
        # would turn this stitch into a raised error and a dropped event.
        assert funnel.payload["summary"]["stages"]["order_created"] == 2
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_storefront_order_keeps_the_platform_namespace(tmp_path, monkeypatch):
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "ref-storefront")
    try:
        await _drive_shopify(
            monkeypatch, marker=False, pivota_order_id=None, topic="orders/create"
        )
        rows = await _ledger_rows(test_database)
        assert [row["order_ref"] for row in rows] == [f"shopify:{SHOPIFY_ORDER_ID}"]
    finally:
        await test_database.disconnect()


# ---- 5. legacy rows are unchanged ------------------------------------------


def _legacy_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "event_id": "evt_legacy",
        "interaction_id": "int_legacy",
        "event_type": "order.paid",
        "platform": "shopify",
        "store_id": STORE_ID,
        "order_id": "LEGACY_1",
        "order_ref": None,
        "amount_cents": 1000,
        "currency": "USD",
    }
    row.update(overrides)
    return row


def test_rows_without_an_order_ref_aggregate_exactly_as_before():
    """The pre-216 keying, run through the post-216 code.

    Two authorities reporting one legacy order under the SAME native order id
    in the same store still collapse to one key and one amount, and two
    different native order ids still stay two.
    """
    import services.merchant_commerce_event_funnel_service as funnel_module

    rows = [
        _legacy_row(event_id="evt_a", event_type="payment.succeeded"),
        _legacy_row(event_id="evt_b", event_type="order.paid"),
        _legacy_row(event_id="evt_c", order_id="LEGACY_2", interaction_id="int_legacy_2"),
    ]
    funnel_module._attach_resolved_order_ids(rows)
    assert all(row["_resolved_order_is_ref"] is False for row in rows)

    accumulator = funnel_module._Accumulator()
    for row in rows:
        accumulator.add(row)
    assert accumulator.paid_order_ids == {"LEGACY_1", "LEGACY_2"}
    assert accumulator.paid_keys == {
        ("shopify", STORE_ID, "LEGACY_1"),
        ("shopify", STORE_ID, "LEGACY_2"),
    }
    assert accumulator.public_summary()["paid_amount_cents_by_currency"] == {"USD": 2000}


def test_a_legacy_row_and_a_ref_row_do_not_collide_on_the_same_native_id():
    """A native order id is only comparable inside its (platform, store); a
    canonical ref is comparable everywhere. Mixing the two scopes must not make
    an unrelated legacy order look like the canonical one."""
    import services.merchant_commerce_event_funnel_service as funnel_module

    rows = [
        _legacy_row(event_id="evt_legacy", order_id=PIVOTA_REF),
        _legacy_row(
            event_id="evt_ref",
            interaction_id="int_ref",
            order_id=PIVOTA_ORDER_ID,
            order_ref=PIVOTA_REF,
        ),
    ]
    funnel_module._attach_resolved_order_ids(rows)
    accumulator = funnel_module._Accumulator()
    for row in rows:
        accumulator.add(row)
    # The legacy row's order_id happens to READ like a ref; it is still keyed
    # in its own (platform, store) scope, because canonical-ness travels with
    # the row that declared an order_ref rather than being re-derived from the
    # shape of a string.
    assert accumulator.paid_keys == {
        ("shopify", STORE_ID, PIVOTA_REF),
        ("order_ref", "order_ref", PIVOTA_REF),
    }


def test_a_refund_carrying_only_a_payment_id_inherits_the_orders_ref():
    import services.merchant_commerce_event_funnel_service as funnel_module

    rows = [
        _legacy_row(
            event_id="evt_paid",
            event_type="payment.succeeded",
            order_id=PIVOTA_ORDER_ID,
            order_ref=PIVOTA_REF,
            payment_id="pi_1",
        ),
        _legacy_row(
            event_id="evt_refund",
            event_type="refund.succeeded",
            interaction_id="int_refund",
            order_id=None,
            order_ref=None,
            payment_id="pi_1",
            refund_id="re_1",
            amount_cents=400,
        ),
    ]
    funnel_module._attach_resolved_order_ids(rows)
    refund_row = rows[1]
    assert refund_row["_resolved_order_id"] == PIVOTA_REF
    assert refund_row["_resolved_order_is_ref"] is True


# ---- 6. who may claim a ref ------------------------------------------------


def _hmac_event(**overrides: Any) -> Dict[str, Any]:
    event = {
        "event_id": "evt_hmac_1",
        "event_type": "order.paid",
        "occurred_at": "2026-09-04T10:00:00Z",
        "order_id": "20260904-0000011",
    }
    event.update(overrides)
    return event


def _bind(event: Dict[str, Any], *, stores: Dict[str, str]):
    from services.merchant_event_ingest_service import MerchantEventBatch
    from services.merchant_event_store_binding import bind_batch_to_stores

    batch = MerchantEventBatch.model_validate({"events": [event]})
    return bind_batch_to_stores(batch, stores=stores)


def test_a_merchant_collector_may_not_claim_a_pivota_order_ref():
    """A forged `pivota:` ref would merge the collector's events into a
    Pivota-originated interaction it does not own, and the funnel would then
    read its amounts as that purchase's."""
    from services.merchant_event_store_binding import MerchantEventBindingError

    with pytest.raises(MerchantEventBindingError) as error:
        _bind(_hmac_event(order_ref=PIVOTA_REF), stores={"store_c": "cafe24"})
    assert error.value.status_code == 422
    assert "order_ref" in error.value.detail


def test_a_merchant_collector_may_name_its_own_platforms_order():
    batch = _bind(
        _hmac_event(order_ref="cafe24:20260904-0000011"), stores={"store_c": "cafe24"}
    )
    assert batch.events[0].order_ref == "cafe24:20260904-0000011"
    assert batch.events[0].platform == "cafe24"


def test_a_merchant_collector_may_not_claim_another_platforms_namespace():
    from services.merchant_event_store_binding import MerchantEventBindingError

    with pytest.raises(MerchantEventBindingError):
        _bind(_hmac_event(order_ref="shopify:123"), stores={"store_c": "cafe24"})


def test_an_absent_order_ref_is_still_accepted():
    batch = _bind(_hmac_event(), stores={"store_c": "cafe24"})
    assert batch.events[0].order_ref is None


@pytest.mark.parametrize("value", ["ord_1", "Shopify:1", "shopify:", "shopify:a b", ":1"])
def test_a_malformed_order_ref_is_refused_by_the_model(value):
    from pydantic import ValidationError

    from services.merchant_event_ingest_service import MerchantEventBatch

    with pytest.raises(ValidationError):
        MerchantEventBatch.model_validate({"events": [_hmac_event(order_ref=value)]})


def test_a_browser_collector_may_not_send_an_order_ref():
    from services.merchant_web_collector_service import (
        FORBIDDEN_WEB_EVENT_FIELDS,
        WebCollectorError,
        build_web_collector_batch,
    )

    assert "order_ref" in FORBIDDEN_WEB_EVENT_FIELDS
    with pytest.raises(WebCollectorError) as error:
        build_web_collector_batch(
            {
                "collector_token": "public",
                "events": [
                    {
                        "event_id": "evt_web_1",
                        "event_type": "product.viewed",
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "session_id": "sess_1",
                        "order_ref": PIVOTA_REF,
                    }
                ],
            },
            claims={
                "merchant_id": MERCHANT_ID,
                "store_id": STORE_ID,
                "platform": "shopify",
            },
        )
    assert error.value.status_code == 422
    assert "order_ref" in error.value.detail


# ---- 7. stitching --------------------------------------------------------


def test_order_ref_is_a_strong_store_scoped_lookup_key():
    from services import commerce_interaction_service as service

    assert "order_ref" in service._INTERACTION_LOOKUP_KEYS
    assert "order_ref" not in service._WEAK_INTERACTION_LOOKUP_KEYS
    assert "order_ref" in service._STORE_SCOPED_LOOKUP_KEYS
    assert "order_ref" in service._MERGEABLE_INTERACTION_FIELDS
    # Ahead of order_id: the two authorities agree on the ref and disagree on
    # the native id, so the ref must be the key that selects the winner.
    keys = list(service._INTERACTION_SELECTION_KEYS)
    assert keys.index("order_ref") < keys.index("order_id")
    # It is also part of the advisory-lock key set, so a concurrent pair of
    # bridge writes serializes on the canonical order.
    locks = service._stitch_advisory_lock_keys(
        MERCHANT_ID, {"merchant_id": MERCHANT_ID, "store_id": STORE_ID, "order_ref": PIVOTA_REF}
    )
    assert any("order_ref" in key for key in locks)


@pytest.mark.asyncio
async def test_two_different_order_refs_on_one_store_stay_two_interactions(
    tmp_path, monkeypatch
):
    from services.commerce_interaction_service import record_commerce_event

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "ref-distinct")
    try:
        for index, ref in enumerate((PIVOTA_REF, "pivota:ord_2")):
            await record_commerce_event(
                event_type="order.paid",
                occurred_at=datetime(2026, 9, 4, 10, index),
                upstream_idempotency_key=f"order:{ref}",
                merchant_id=MERCHANT_ID,
                platform="shopify",
                store_id=STORE_ID,
                order_ref=ref,
                order_id=f"native_{index}",
            )
        interactions = await _interactions(test_database)
        assert len(interactions) == 2
        assert {row["order_ref"] for row in interactions} == {PIVOTA_REF, "pivota:ord_2"}
    finally:
        await test_database.disconnect()


# ---- the wrapper's legacy overlap must still cancel a Pivota-originated order ----


def _ledger_row(event_id, event_type, *, order_ref, order_id, amount=4999, payment_id="pi_1"):
    return {
        "event_id": event_id,
        "interaction_id": "int_ord_1",
        "merchant_id": "merch_1",
        "platform": "shopify",
        "store_id": "store_a",
        "surface": "psp",
        "event_type": event_type,
        "order_ref": order_ref,
        "payload": {
            "order_id": order_id,
            "payment_id": payment_id,
            "amount_cents": amount,
            "currency": "USD",
        },
    }


@pytest.mark.asyncio
async def test_wrapper_overlap_dedupe_still_cancels_a_pivota_originated_order(monkeypatch):
    """`merchant_commerce_funnel_service` subtracts the ledger's order id sets
    from the legacy attribution/orders rows, which are keyed on the BARE Pivota
    order id. A ledger that reports only `pivota:ord_1` would stop cancelling
    `ord_1`, and observed_* would count one purchase twice: once as the ledger
    key, once as the uncancelled legacy row."""
    import services.merchant_commerce_event_funnel_service as event_module
    import services.merchant_commerce_funnel_service as wrapper

    rows = [
        _ledger_row("evt_stripe", "payment.succeeded", order_ref="pivota:ord_1", order_id="ord_1"),
        _ledger_row("evt_shopify", "order.paid", order_ref="pivota:ord_1", order_id="6600123"),
        _ledger_row("evt_refund", "refund.succeeded", order_ref="pivota:ord_1", order_id="ord_1", amount=100),
    ]

    async def fake_fetch(**_kwargs):
        return rows, False

    monkeypatch.setattr(event_module, "_fetch_event_rows", fake_fetch)
    result = await event_module.get_merchant_commerce_event_funnel(merchant_id="merch_1", group_by="store")
    # The canonical ref is the counting key...
    assert len(result.paid_keys) == 1
    # ...and the bare Pivota id is exposed for the legacy overlap.
    assert "ord_1" in result.order_ids
    assert "ord_1" in result.paid_order_ids
    assert "ord_1" in result.refund_order_ids

    async def no_rows(*_args, **_kwargs):
        return []

    async def legacy_edge_rows(*_args, **_kwargs):
        return [{"order_id": "ord_1", "latest_refund_id": "re_1"}]

    async def paid_order_rows(*_args, **_kwargs):
        return [{"order_id": "ord_1", "payment_status": "paid", "status": "paid"}]

    async def real_event_funnel(**kwargs):
        return await event_module.get_merchant_commerce_event_funnel(**kwargs)

    monkeypatch.setattr(wrapper, "_fetch_listing_rows", no_rows)
    monkeypatch.setattr(wrapper, "_fetch_click_rows", no_rows)
    monkeypatch.setattr(wrapper, "_fetch_edge_rows", legacy_edge_rows)
    monkeypatch.setattr(wrapper, "_fetch_order_rows", paid_order_rows)
    monkeypatch.setattr(wrapper, "get_merchant_commerce_event_funnel", real_event_funnel)

    funnel = await wrapper.get_merchant_commerce_funnel(merchant_id="merch_1", group_by="platform")
    assert funnel["summary"]["observed_order_conversion"] == 1
    assert funnel["summary"]["observed_paid_conversion"] == 1
    assert funnel["summary"]["observed_refunded_orders"] == 1
