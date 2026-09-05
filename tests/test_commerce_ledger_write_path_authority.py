"""Trust provenance on the commerce ledger (migration 213).

Before this change the only record of WHO wrote a ledger row was `source`
and `surface`, both copied from the caller's payload. The merchant HMAC
collector accepts any value there, so a server-side collector could write
`source="stripe_webhook", surface="psp"` and the funnel would treat it as
the settlement authority. The four columns under test are stamped by the
ingress that authenticated the caller and never read from the event body.

The declaration tests come first for the reason test_audit_run_anonymous_claim
gives: `create_all` runs BEFORE migrations here, so the MODEL builds a fresh
database and the migration only fixes existing ones. Both must agree, and the
schema-guard self-heal must carry the same four columns for a deploy that
skips db/migrations/.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import databases
import pytest
from sqlalchemy import create_engine, select

from db.commerce_interactions import commerce_interaction_events, commerce_interactions
from db.database import metadata

REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = REPO_ROOT / "db" / "migrations" / "213_commerce_ledger_write_path_authority.sql"
_INDEX_MIGRATION = REPO_ROOT / "db" / "migrations" / "214_commerce_ledger_synthetic_index.sql"
_PROVENANCE_COLUMNS = ("write_path", "authority", "agent_identity_confidence", "synthetic")


# ---- 1. the three schema homes must agree ----------------------------------


def test_the_model_declares_the_four_provenance_columns():
    cols = commerce_interaction_events.c
    for name in _PROVENANCE_COLUMNS:
        assert name in cols, f"model is missing {name}"
    assert cols.write_path.nullable is True
    assert cols.authority.nullable is True
    assert cols.agent_identity_confidence.nullable is True
    # NOT NULL with a server default: a pre-migration row is "not a probe",
    # and the funnel's exclusion predicate must never see a NULL as truthy.
    assert cols.synthetic.nullable is False
    assert cols.synthetic.server_default is not None


def test_the_migration_agrees_with_the_model():
    sql = _MIGRATION.read_text()
    for name in _PROVENANCE_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {name}" in sql
    assert re.search(r"synthetic BOOLEAN NOT NULL DEFAULT FALSE", sql)
    # The partial index rolls out CONCURRENTLY in its own file. The runner
    # classifies a file by a regex over its WHOLE body, comments included, and
    # runs a match statement-at-a-time on autocommit; an ALTER TABLE in such
    # a file would lose its transaction. Ask the runner, not a substring.
    from db.sql_migrations import needs_autocommit

    assert needs_autocommit(sql) is False
    index_sql = _INDEX_MIGRATION.read_text()
    assert needs_autocommit(index_sql) is True
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_commerce_interaction_events_synthetic" in index_sql


def test_the_runtime_self_heal_carries_all_four_columns_and_no_index():
    guard = (REPO_ROOT / "db" / "schema_guard.py").read_text()
    for name in _PROVENANCE_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {name}" in guard
    assert "synthetic BOOLEAN NOT NULL DEFAULT FALSE" in guard
    # Same policy as migration 206: index rollout stays out of the startup
    # guard, whose transaction cannot run CONCURRENTLY.
    assert "idx_commerce_interaction_events_synthetic" not in guard


# ---- 2. the write-path contract is fixed on the server ----------------------


def test_every_production_write_path_has_exactly_one_authority():
    from services import merchant_event_ingest_service as service

    write_paths = set(service.WritePath.__args__)
    assert set(service.LEDGER_AUTHORITY_BY_WRITE_PATH) == write_paths
    assert set(service._ALLOWED_CONFIDENCE_BY_WRITE_PATH) == write_paths
    assert set(service.LEDGER_AUTHORITY_BY_WRITE_PATH.values()) <= set(
        service.LedgerAuthority.__args__
    )
    # Browser paths can only ever be observational; the PSP bridge is the one
    # settlement authority. A regression here is a trust regression.
    assert service.LEDGER_AUTHORITY_BY_WRITE_PATH["universal_web_collector"] == "observational"
    assert service.LEDGER_AUTHORITY_BY_WRITE_PATH["shopify_web_pixel"] == "observational"
    assert service.LEDGER_AUTHORITY_BY_WRITE_PATH["merchant_hmac_batch"] == "merchant"
    assert service.LEDGER_AUTHORITY_BY_WRITE_PATH["stripe_webhook"] == "psp"
    # The native store bridges speak for the PLATFORM, never for the merchant:
    # the row is what BigCommerce's own API said, not what a merchant claimed.
    assert service.LEDGER_AUTHORITY_BY_WRITE_PATH["bigcommerce_webhook"] == "platform"
    assert service._ALLOWED_CONFIDENCE_BY_WRITE_PATH["bigcommerce_webhook"] == frozenset(
        {"platform_asserted"}
    )


@pytest.mark.parametrize(
    "write_path, confidence",
    [
        ("universal_web_collector", "browser_observed"),
        ("shopify_web_pixel", "browser_observed"),
        ("merchant_hmac_batch", "merchant_asserted"),
        ("cafe24_webhook", "platform_asserted"),
        ("bigcommerce_webhook", "platform_asserted"),
        ("stripe_webhook", "platform_asserted"),
    ],
)
def test_each_ingress_may_assert_only_its_own_confidence(write_path, confidence):
    from services.merchant_event_ingest_service import resolve_ledger_authority

    assert resolve_ledger_authority(write_path, confidence)


@pytest.mark.parametrize(
    "write_path, confidence",
    [
        ("universal_web_collector", "platform_asserted"),
        ("shopify_web_pixel", "merchant_asserted"),
        ("merchant_hmac_batch", "platform_asserted"),
        ("cafe24_webhook", "browser_observed"),
        # No production ingress may claim a verified agent today. The tier
        # exists in the core; this is the guard that keeps it unissued until
        # an ingress actually authenticates the agent.
        ("merchant_hmac_batch", "verified"),
        ("stripe_webhook", "verified"),
        ("cafe24_webhook", "unknown"),
        ("bigcommerce_webhook", "merchant_asserted"),
        ("bigcommerce_webhook", "browser_observed"),
    ],
)
def test_a_mismatched_write_path_and_confidence_is_refused(write_path, confidence):
    from services.merchant_event_ingest_service import resolve_ledger_authority

    with pytest.raises(ValueError):
        resolve_ledger_authority(write_path, confidence)


def test_an_unknown_write_path_is_refused():
    from services.merchant_event_ingest_service import resolve_ledger_authority

    with pytest.raises(ValueError):
        resolve_ledger_authority("made_up_path", "platform_asserted")


def _production_ingest_calls():
    """Every `ingest_merchant_event_batch(...)` call under routes/ and services/.

    AST, not a regex: a ratchet that matches one syntactic form permits the
    others. Any call expression whose callee is named ingest_merchant_event_batch
    counts, however it is formatted or aliased through an attribute.
    """
    calls = []
    for directory in ("routes", "services"):
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == "ingest_merchant_event_batch":
                    calls.append((path.relative_to(REPO_ROOT), node))
    return calls


def test_every_production_ingest_call_names_a_literal_write_path():
    from services import merchant_event_ingest_service as service

    calls = _production_ingest_calls()
    # The definition itself is not a call; every real ingress is.
    assert len(calls) >= 12, [str(path) for path, _ in calls]
    allowed = set(service.WritePath.__args__)
    for path, node in calls:
        keywords = {kw.arg: kw.value for kw in node.keywords}
        assert "write_path" in keywords, f"{path}:{node.lineno} passes no write_path"
        value = keywords["write_path"]
        if isinstance(value, ast.Constant):
            literals = {value.value}
        elif isinstance(value, ast.IfExp):
            # `"a" if cond else "b"` — both arms must be literals.
            assert isinstance(value.body, ast.Constant) and isinstance(value.orelse, ast.Constant), (
                f"{path}:{node.lineno} write_path arms must be string literals"
            )
            literals = {value.body.value, value.orelse.value}
        else:
            raise AssertionError(
                f"{path}:{node.lineno} write_path must be a string literal, not {ast.dump(value)[:60]}"
            )
        assert literals <= allowed, f"{path}:{node.lineno} unknown write_path {literals}"
        assert "agent_identity_confidence" in keywords, (
            f"{path}:{node.lineno} passes no agent_identity_confidence"
        )


# ---- 3. the ledger stores the stamp, and the funnel reads it ---------------


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


def _refund_event(event_id: str, **overrides):
    event = {
        "event_id": event_id,
        "event_type": "refund.succeeded",
        "occurred_at": "2026-09-04T10:00:00Z",
        "platform": "shopify",
        "store_id": "store_a",
        "order_id": "ORDER_1",
        "refund_id": event_id,
        "amount_cents": 500,
        "currency": "USD",
        # What a merchant collector could claim to look like the PSP bridge.
        "source": "stripe_webhook",
        "surface": "psp",
    }
    event.update(overrides)
    return event


@pytest.mark.asyncio
async def test_ledger_row_carries_the_ingress_stamp_not_the_payload_claim(tmp_path, monkeypatch):
    from services.merchant_event_ingest_service import (
        MerchantEventBatch,
        ingest_merchant_event_batch,
    )

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "ledger-stamp")
    try:
        batch = MerchantEventBatch.model_validate(
            {"events": [_refund_event("re_forged", agent_id="claimed-agent")]}
        )
        batch.events[0].occurred_at = batch.events[0].occurred_at.replace(tzinfo=None)
        await ingest_merchant_event_batch(
            merchant_id="merchant-a",
            batch=batch,
            agent_identity_confidence="merchant_asserted",
            write_path="merchant_hmac_batch",
        )
        rows = [dict(row) for row in await test_database.fetch_all(select(commerce_interaction_events))]
        assert len(rows) == 1
        row = rows[0]
        # The caller's strings are kept as-is for diagnostics...
        assert row["source"] == "stripe_webhook"
        assert row["surface"] == "psp"
        # ...but the stamp says what the ingress actually was.
        assert row["write_path"] == "merchant_hmac_batch"
        assert row["authority"] == "merchant"
        assert row["agent_identity_confidence"] == "merchant_asserted"
        assert row["synthetic"] in (False, 0)
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_funnel_refund_authority_reads_the_stamp_over_the_source_string(tmp_path, monkeypatch):
    """A forged PSP claim and the real PSP report of the same order must not
    collapse into one authority bucket. Two buckets -> the funnel takes the
    max per authority rather than trusting the forged string."""
    from services.merchant_event_ingest_service import (
        MerchantEventBatch,
        ingest_merchant_event_batch,
    )
    import services.merchant_commerce_event_funnel_service as funnel_module

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "ledger-authority")
    try:
        forged = MerchantEventBatch.model_validate(
            {"events": [_refund_event("re_merchant_claim", amount_cents=900)]}
        )
        real = MerchantEventBatch.model_validate(
            {"events": [_refund_event("re_psp_real", amount_cents=500)]}
        )
        for batch in (forged, real):
            batch.events[0].occurred_at = batch.events[0].occurred_at.replace(tzinfo=None)
        await ingest_merchant_event_batch(
            merchant_id="merchant-a",
            batch=forged,
            agent_identity_confidence="merchant_asserted",
            write_path="merchant_hmac_batch",
        )
        await ingest_merchant_event_batch(
            merchant_id="merchant-a",
            batch=real,
            agent_identity_confidence="platform_asserted",
            write_path="stripe_webhook",
        )
        rows = [dict(row) for row in await test_database.fetch_all(select(commerce_interaction_events))]
        authorities = {funnel_module._refund_authority(row) for row in rows}
        assert authorities == {"merchant", "psp"}

        result = await funnel_module.get_merchant_commerce_event_funnel(
            merchant_id="merchant-a", group_by="store"
        )
        assert result.payload["available"] is True
        # max(merchant=900, psp=500) per order, not a single "psp" bucket of 900.
        assert result.payload["summary"]["refunded_amount_cents_by_currency"] == {"USD": 900}
    finally:
        await test_database.disconnect()


def test_legacy_rows_without_a_stamp_still_use_the_string_inference():
    import services.merchant_commerce_event_funnel_service as funnel_module

    assert funnel_module._refund_authority({"source": "stripe_webhook"}) == "psp"
    assert funnel_module._refund_authority({"surface": "psp"}) == "psp"
    assert funnel_module._refund_authority({"source": "cafe24_webhook"}) == "store"
    # And an explicit stamp wins even when the strings say otherwise.
    assert (
        funnel_module._refund_authority(
            {"authority": "merchant", "source": "stripe_webhook", "surface": "psp"}
        )
        == "merchant"
    )


@pytest.mark.asyncio
async def test_synthetic_batches_are_stamped_and_excluded_from_the_default_funnel(
    tmp_path, monkeypatch
):
    from services.merchant_event_ingest_service import (
        MerchantEventBatch,
        ingest_merchant_event_batch,
    )
    import services.merchant_commerce_event_funnel_service as funnel_module

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "ledger-synthetic")
    try:
        paid = {
            "event_type": "payment.succeeded",
            "occurred_at": "2026-09-04T10:00:00Z",
            "platform": "cafe24",
            "store_id": "store_a",
            "source": "cafe24_webhook",
            "surface": "merchant_storefront",
            "amount_cents": 1000,
            "currency": "USD",
        }
        real = MerchantEventBatch.model_validate(
            {"events": [{**paid, "event_id": "pay_real", "order_id": "ORDER_REAL"}]}
        )
        # Declared synthetic at batch level, on an ordinary surface: only the
        # column can exclude it.
        probe = MerchantEventBatch.model_validate(
            {
                "synthetic": True,
                "events": [{**paid, "event_id": "pay_probe", "order_id": "ORDER_PROBE"}],
            }
        )
        # The pre-flag canary shape: surface says ops_canary, batch says nothing.
        legacy_probe = MerchantEventBatch.model_validate(
            {
                "events": [
                    {
                        **paid,
                        "event_id": "pay_legacy_probe",
                        "order_id": "ORDER_LEGACY",
                        "surface": "ops_canary",
                    }
                ]
            }
        )
        for batch in (real, probe, legacy_probe):
            batch.events[0].occurred_at = batch.events[0].occurred_at.replace(tzinfo=None)
            await ingest_merchant_event_batch(
                merchant_id="merchant-a",
                batch=batch,
                agent_identity_confidence="platform_asserted",
                write_path="cafe24_webhook",
            )
        rows = {
            row["event_id"]: dict(row)
            for row in await test_database.fetch_all(select(commerce_interaction_events))
        }
        by_order = {row["payload"]["order_id"]: bool(row["synthetic"]) for row in rows.values()}
        assert by_order == {"ORDER_REAL": False, "ORDER_PROBE": True, "ORDER_LEGACY": True}

        result = await funnel_module.get_merchant_commerce_event_funnel(
            merchant_id="merchant-a", group_by="store"
        )
        summary = result.payload["summary"]
        assert summary["events_total"] == 1
        assert summary["paid_amount_cents_by_currency"] == {"USD": 1000}
        assert result.paid_order_ids == {"ORDER_REAL"}
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_batch_cannot_raise_its_own_standing(tmp_path, monkeypatch):
    """`synthetic` is the only provenance a caller may set, and only downward.
    Nothing in the body reaches write_path / authority / confidence."""
    from services.merchant_event_ingest_service import MerchantEventBatch
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MerchantEventBatch.model_validate(
            {"events": [_refund_event("re_x")], "authority": "psp"}
        )
    with pytest.raises(ValidationError):
        MerchantEventBatch.model_validate(
            {"events": [_refund_event("re_x")], "write_path": "stripe_webhook"}
        )
    with pytest.raises(ValidationError):
        MerchantEventBatch.model_validate(
            {"events": [{**_refund_event("re_x"), "authority": "psp"}]}
        )


# ---- 4. first-party writers (PR-0.2) ---------------------------------------


def test_first_party_write_paths_pair_only_with_their_own_confidence():
    from services.commerce_ledger_provenance import (
        LEDGER_AUTHORITY_BY_WRITE_PATH,
        ledger_provenance,
        resolve_ledger_authority,
    )

    assert ledger_provenance("agent_commerce_api", "verified") == {
        "write_path": "agent_commerce_api",
        "authority": "pivota",
        "agent_identity_confidence": "verified",
    }
    for path in ("surface_click_attribution", "commerce_attribution_edge", "surface_listing_registry"):
        assert LEDGER_AUTHORITY_BY_WRITE_PATH[path] == "pivota"
        assert ledger_provenance(path, "unknown")["authority"] == "pivota"
        # Nothing at these writers authenticates an agent; a claimed identity
        # must not be promoted by naming a first-party path.
        for confidence in ("browser_observed", "merchant_asserted", "platform_asserted", "verified"):
            with pytest.raises(ValueError):
                resolve_ledger_authority(path, confidence)
    # And the one verified issuer cannot be used to launder a weaker claim.
    for confidence in ("unknown", "browser_observed", "merchant_asserted", "platform_asserted"):
        with pytest.raises(ValueError):
            resolve_ledger_authority("agent_commerce_api", confidence)


def test_verified_is_issued_by_exactly_one_write_path():
    from services.commerce_ledger_provenance import _ALLOWED_CONFIDENCE_BY_WRITE_PATH

    issuers = sorted(
        path for path, allowed in _ALLOWED_CONFIDENCE_BY_WRITE_PATH.items() if "verified" in allowed
    )
    assert issuers == ["agent_commerce_api"]


def _direct_ledger_calls():
    """Every direct `record_commerce_event(...)` / `_best_effort(...)` call in
    routes/ and services/, excluding the two forwarders (the batch ingest and
    the best-effort wrapper) whose write_path is a validated variable."""
    forwarders = {
        Path("services/merchant_event_ingest_service.py"),
        Path("services/commerce_interaction_service.py"),
    }
    calls = []
    for directory in ("routes", "services"):
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT)
            if rel in forwarders:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in {"record_commerce_event", "record_commerce_event_best_effort"}:
                    calls.append((rel, node))
    return calls


def test_every_direct_ledger_write_splats_a_literal_provenance():
    from services.commerce_ledger_provenance import (
        WritePath,
        _ALLOWED_CONFIDENCE_BY_WRITE_PATH,
    )

    calls = _direct_ledger_calls()
    # 6 agent-commerce + 5 attribution + 2 listing-registry writers.
    assert len(calls) >= 13, sorted({str(p) for p, _ in calls})
    allowed_paths = set(WritePath.__args__)
    for path, node in calls:
        splats = [
            kw.value
            for kw in node.keywords
            if kw.arg is None
            and isinstance(kw.value, ast.Call)
            and getattr(kw.value.func, "id", getattr(kw.value.func, "attr", None)) == "ledger_provenance"
        ]
        assert len(splats) == 1, f"{path}:{node.lineno} must splat exactly one ledger_provenance(...)"
        args = splats[0].args
        assert len(args) == 2 and all(isinstance(a, ast.Constant) for a in args), (
            f"{path}:{node.lineno} ledger_provenance needs two string literals"
        )
        write_path, confidence = args[0].value, args[1].value
        assert write_path in allowed_paths, f"{path}:{node.lineno} unknown write_path {write_path}"
        assert confidence in _ALLOWED_CONFIDENCE_BY_WRITE_PATH[write_path], (
            f"{path}:{node.lineno} {write_path} may not assert {confidence}"
        )
        # No writer may also hand-type the stamp; the splat is the only source.
        typed = {kw.arg for kw in node.keywords if kw.arg in {"write_path", "authority", "agent_identity_confidence"}}
        assert not typed, f"{path}:{node.lineno} types provenance by hand: {typed}"


def test_direct_writers_are_partitioned_by_file():
    """The literal each file may use is part of the contract: a route must not
    borrow the attribution edge's path, nor the reverse."""
    expected = {
        Path("routes/agent_commerce.py"): {"agent_commerce_api"},
        Path("services/commerce_attribution_service.py"): {
            "surface_click_attribution",
            "commerce_attribution_edge",
        },
        Path("services/surface_listing_registry_service.py"): {"surface_listing_registry"},
    }
    seen: dict = {}
    for path, node in _direct_ledger_calls():
        for kw in node.keywords:
            if kw.arg is None and isinstance(kw.value, ast.Call):
                seen.setdefault(path, set()).add(kw.value.args[0].value)
    assert seen == expected, seen


@pytest.mark.asyncio
async def test_record_commerce_event_refuses_a_hand_typed_authority_that_disagrees(
    tmp_path, monkeypatch
):
    from services.commerce_interaction_service import record_commerce_event

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "ledger-mismatch")
    try:
        with pytest.raises(ValueError):
            await record_commerce_event(
                event_type="checkout.created",
                metadata={"merchant_id": "merchant-a", "checkout_id": "chk_1"},
                source="agent_v2_commerce",
                write_path="agent_commerce_api",
                authority="psp",
                agent_identity_confidence="verified",
            )
        with pytest.raises(ValueError):
            await record_commerce_event(
                event_type="checkout.created",
                metadata={"merchant_id": "merchant-a", "checkout_id": "chk_1"},
                source="agent_v2_commerce",
                write_path="agent_commerce_api",
                agent_identity_confidence="browser_observed",
            )
        rows = await test_database.fetch_all(select(commerce_interaction_events))
        assert rows == [], "a refused stamp must not write a row"
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_agent_commerce_stamp_authenticates_the_agent_on_the_interaction(tmp_path, monkeypatch):
    """The whole point of the verified tier: a later browser claim for a
    different agent must not displace the identity the API-key ingress
    authenticated, and the stitched interaction carries that agent."""
    from services.commerce_ledger_provenance import ledger_provenance
    from services.commerce_interaction_service import record_commerce_event
    from services.merchant_event_ingest_service import (
        MerchantEventBatch,
        ingest_merchant_event_batch,
    )

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "ledger-verified")
    try:
        result = await record_commerce_event(
            event_type="checkout.created",
            metadata={
                "agent_id": "agent_real",
                "agent_identity_confidence": "verified",
                "merchant_id": "merchant-a",
                "platform": "shopify",
                "store_id": "store_a",
                "checkout_id": "chk_1",
                "order_id": "chk_1",
            },
            source="agent_v2_commerce",
            upstream_idempotency_key="checkout:chk_1",
            actor_type="agent",
            actor_id="agent_real",
            **ledger_provenance("agent_commerce_api", "verified"),
        )
        event = dict(
            await test_database.fetch_one(
                select(commerce_interaction_events).where(
                    commerce_interaction_events.c.event_id == result["event_id"]
                )
            )
        )
        assert event["write_path"] == "agent_commerce_api"
        assert event["authority"] == "pivota"
        assert event["agent_identity_confidence"] == "verified"
        assert event["actor_id"] == "agent_real"

        # A browser collector on the same checkout claims a different agent.
        claim = MerchantEventBatch.model_validate(
            {
                "events": [
                    {
                        "event_id": "pixel-1",
                        "event_type": "checkout.started",
                        "occurred_at": "2026-09-04T10:00:00Z",
                        "platform": "shopify",
                        "store_id": "store_a",
                        "checkout_id": "chk_1",
                        "agent_id": "agent_claimed",
                    }
                ]
            }
        )
        claim.events[0].occurred_at = claim.events[0].occurred_at.replace(tzinfo=None)
        await ingest_merchant_event_batch(
            merchant_id="merchant-a",
            batch=claim,
            agent_identity_confidence="browser_observed",
            write_path="shopify_web_pixel",
        )
        interactions = [dict(r) for r in await test_database.fetch_all(select(commerce_interactions))]
        assert len(interactions) == 1
        assert interactions[0]["agent_id"] == "agent_real"
        assert interactions[0]["metadata"]["agent_identity_confidence"] == "verified"
    finally:
        await test_database.disconnect()
