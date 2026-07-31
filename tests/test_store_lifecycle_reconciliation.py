"""Store-lifecycle reconciliation (#1648 P1b + P1c).

These tests EXECUTE. Every assertion below is a DB re-read, not a call count —
this arc's dominant defect is the no-op behind a success signal (the closure's
first "successful" run was 831 swallowed exceptions), and a test that asserts
"we called execute()" would have passed for every one of them.

What is proved here:
  1. derive_merchant_status is safe corpus-wide — no store rows means "don't
     touch it". On prod (2026-07-31) 471 of 483 catalog_merchants have no
     merchant_stores row; a rule without that guard empties public search.
  2. The write-through moves active <-> inactive in both directions, and refuses
     to touch 'observed' (crawl-sourced brands store lifecycle has no say over).
  3. The probe classifier calls 401 hard and 403 NOT hard — disconnecting a live
     merchant over a missing scope would be a self-inflicted outage.
  4. Two strikes, not one: a single hard failure records but does not flip; the
     second flips the store AND the merchant. Transient outcomes neither
     advance nor reset the count.

The SQL here runs on SQLite. That proves the statements PARSE and that the
logic branches correctly; it does NOT prove the Postgres plan (see #1588). The
Postgres half is covered by executing the real functions read-only against prod
before merge, recorded in the PR.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from db.database import database
from services import store_lifecycle_service as svc


_MERCHANT_PREFIX = "slt"


async def _connect_if_needed() -> bool:
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    return was_connected


async def _ensure_schema() -> None:
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS merchant_stores (
            store_id TEXT PRIMARY KEY,
            merchant_id TEXT NOT NULL,
            platform TEXT,
            domain TEXT,
            name TEXT,
            api_key TEXT,
            status TEXT,
            is_primary BOOLEAN DEFAULT FALSE,
            connected_at TIMESTAMP,
            last_sync TIMESTAMP,
            upstream_probe_at TIMESTAMP,
            upstream_probe_status TEXT,
            upstream_probe_http_status INTEGER,
            upstream_probe_failures INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS catalog_merchants (
            merchant_id TEXT PRIMARY KEY,
            merchant_name TEXT,
            primary_platform TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            indexable BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """,
    ]
    for stmt in ddl:
        await database.execute(stmt)


async def _reset() -> None:
    await database.execute(
        "DELETE FROM merchant_stores WHERE merchant_id LIKE :p", {"p": f"{_MERCHANT_PREFIX}%"}
    )
    await database.execute(
        "DELETE FROM catalog_merchants WHERE merchant_id LIKE :p", {"p": f"{_MERCHANT_PREFIX}%"}
    )


async def _seed_merchant(merchant_id: str, status: str) -> None:
    await database.execute(
        """
        INSERT INTO catalog_merchants (merchant_id, merchant_name, status, indexable)
        VALUES (:merchant_id, :name, :status, 1)
        """,
        {"merchant_id": merchant_id, "name": merchant_id, "status": status},
    )


async def _seed_store(
    store_id: str,
    merchant_id: str,
    status: str,
    *,
    platform: str = "shopify",
    domain: str = "example.myshopify.com",
    failures: int = 0,
) -> None:
    await database.execute(
        """
        INSERT INTO merchant_stores
            (store_id, merchant_id, platform, domain, api_key, status, upstream_probe_failures)
        VALUES (:store_id, :merchant_id, :platform, :domain, :api_key, :status, :failures)
        """,
        {
            "store_id": store_id,
            "merchant_id": merchant_id,
            "platform": platform,
            "domain": domain,
            "api_key": '{"access_token": "shpat_test"}',
            "status": status,
            "failures": failures,
        },
    )


async def _merchant_status(merchant_id: str) -> Optional[str]:
    row = await database.fetch_one(
        "SELECT status FROM catalog_merchants WHERE merchant_id = :m", {"m": merchant_id}
    )
    return None if row is None else str(dict(row).get("status"))


async def _store_row(store_id: str) -> Dict[str, Any]:
    row = await database.fetch_one(
        "SELECT * FROM merchant_stores WHERE store_id = :s", {"s": store_id}
    )
    return dict(row) if row is not None else {}


@pytest.fixture(autouse=True)
async def _db():
    was_connected = await _connect_if_needed()
    await _ensure_schema()
    await _reset()
    try:
        yield
    finally:
        await _reset()
        if not was_connected:
            await database.disconnect()


# ---------------------------------------------------------------------------
# 1. The corpus-safety property
# ---------------------------------------------------------------------------


def test_no_store_rows_means_do_not_touch():
    """The guard that keeps this off 471 of 483 prod merchants."""
    assert svc.derive_merchant_status([]) is None


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["active"], "active"),
        (["connected"], "active"),
        (["ACTIVE"], "active"),           # case-insensitive, as every store read is
        (["inactive", "connected"], "active"),  # ANY active store keeps the merchant on
        (["inactive"], "inactive"),
        (["disconnected", "deleted"], "inactive"),
        (["retired_test_rig"], "inactive"),     # the #1648 rig's own state
        ([None], "inactive"),
    ],
)
def test_derive_merchant_status(statuses: List[Optional[str]], expected: str):
    assert svc.derive_merchant_status(statuses) == expected


@pytest.mark.asyncio
async def test_write_through_skips_merchant_with_no_stores():
    merchant = f"{_MERCHANT_PREFIX}_seedlike"
    await _seed_merchant(merchant, "active")

    out = await svc.sync_catalog_merchant_status(merchant)

    assert out["changed"] is False
    assert out["skipped"] == "no_store_rows"
    assert await _merchant_status(merchant) == "active"


# ---------------------------------------------------------------------------
# 2. The write-through, both directions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_store_deactivated_flips_merchant_inactive():
    """The #1648 shape: every store gone, merchant still gating search open."""
    merchant = f"{_MERCHANT_PREFIX}_deactivated"
    await _seed_merchant(merchant, "active")
    await _seed_store(f"{merchant}_s1", merchant, "retired_test_rig")
    await _seed_store(f"{merchant}_s2", merchant, "disconnected")

    out = await svc.sync_catalog_merchant_status(merchant)

    assert out["changed"] is True
    assert await _merchant_status(merchant) == "inactive"


@pytest.mark.asyncio
async def test_reconnect_flips_merchant_back_active():
    merchant = f"{_MERCHANT_PREFIX}_reconnect"
    await _seed_merchant(merchant, "inactive")
    await _seed_store(f"{merchant}_s1", merchant, "disconnected")
    await _seed_store(f"{merchant}_s2", merchant, "active")

    out = await svc.sync_catalog_merchant_status(merchant)

    assert out["changed"] is True
    assert await _merchant_status(merchant) == "active"


@pytest.mark.asyncio
async def test_observed_merchant_is_never_rewritten():
    """'observed' is crawl-sourced content — 346 of 483 prod rows. Store
    lifecycle has no authority over it, in either direction."""
    merchant = f"{_MERCHANT_PREFIX}_observed"
    await _seed_merchant(merchant, "observed")
    await _seed_store(f"{merchant}_s1", merchant, "disconnected")

    out = await svc.sync_catalog_merchant_status(merchant)

    assert out["changed"] is False
    assert out["skipped"] == "unmanaged_status:observed"
    assert await _merchant_status(merchant) == "observed"


@pytest.mark.asyncio
async def test_write_through_is_idempotent():
    merchant = f"{_MERCHANT_PREFIX}_idem"
    await _seed_merchant(merchant, "active")
    await _seed_store(f"{merchant}_s1", merchant, "active")

    first = await svc.sync_catalog_merchant_status(merchant)
    second = await svc.sync_catalog_merchant_status(merchant)

    assert first["changed"] is False and first["skipped"] == "already_correct"
    assert second["changed"] is False
    assert await _merchant_status(merchant) == "active"


@pytest.mark.asyncio
async def test_missing_catalog_merchant_row_is_not_invented():
    merchant = f"{_MERCHANT_PREFIX}_norow"
    await _seed_store(f"{merchant}_s1", merchant, "disconnected")

    out = await svc.sync_catalog_merchant_status(merchant)

    assert out["skipped"] == "no_catalog_merchant_row"
    assert await _merchant_status(merchant) is None


@pytest.mark.asyncio
async def test_sweep_converges_merchants_no_hook_ever_touched():
    """The reason the sweep exists: a lifecycle writer that forgets the hook
    costs one tick of lag, not another three-week leak."""
    dead = f"{_MERCHANT_PREFIX}_sweep_dead"
    live = f"{_MERCHANT_PREFIX}_sweep_live"
    await _seed_merchant(dead, "active")
    await _seed_store(f"{dead}_s1", dead, "disconnected")
    await _seed_merchant(live, "active")
    await _seed_store(f"{live}_s1", live, "active")

    summary = await svc.reconcile_catalog_merchant_statuses()

    assert await _merchant_status(dead) == "inactive"
    assert await _merchant_status(live) == "active"
    assert {t["merchant_id"] for t in summary["transitions"]} >= {dead}


# ---------------------------------------------------------------------------
# 3. Probe classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "http_status,expected",
    [
        (200, svc.PROBE_OK),
        (401, svc.PROBE_AUTH_FAILED),   # the uninstall signature
        (402, svc.PROBE_STORE_CLOSED),  # frozen shop
        (423, svc.PROBE_STORE_CLOSED),  # locked shop
        (404, svc.PROBE_STORE_CLOSED),  # shop no longer resolves
        (403, svc.PROBE_PERMISSION_DENIED),  # missing scope on a LIVE app
        (429, svc.PROBE_UNREACHABLE),
        (500, svc.PROBE_UNREACHABLE),
        (503, svc.PROBE_UNREACHABLE),
        (None, svc.PROBE_UNREACHABLE),  # timeout / DNS / TLS
    ],
)
def test_classify_shopify_probe(http_status: Optional[int], expected: str):
    assert svc.classify_shopify_probe(http_status) == expected


def test_shopify_403_is_not_a_deactivation_signal():
    """Explicit, because getting this wrong disconnects live merchants."""
    assert svc.classify_shopify_probe(403) not in svc.HARD_PROBE_STATUSES


@pytest.mark.parametrize(
    "http_status,expected",
    [
        (200, svc.PROBE_OK),
        (401, svc.PROBE_AUTH_FAILED),
        (403, svc.PROBE_PERMISSION_DENIED),
        (500, svc.PROBE_UNREACHABLE),
        (None, svc.PROBE_UNREACHABLE),
    ],
)
def test_classify_wix_probe(http_status: Optional[int], expected: str):
    assert svc.classify_wix_probe(http_status) == expected


@pytest.mark.parametrize(
    "outcome,prior,expected",
    [
        (svc.PROBE_OK, 5, 0),                 # success resets
        (svc.PROBE_AUTH_FAILED, 0, 1),
        (svc.PROBE_AUTH_FAILED, 1, 2),
        (svc.PROBE_STORE_CLOSED, 1, 2),
        (svc.PROBE_UNREACHABLE, 1, 1),        # holds — evidence of nothing
        (svc.PROBE_PERMISSION_DENIED, 1, 1),
        (svc.PROBE_NO_CREDENTIALS, 1, 1),
        (svc.PROBE_UNSUPPORTED_PLATFORM, 0, 0),
    ],
)
def test_next_failure_count(outcome: str, prior: int, expected: int):
    assert svc.next_failure_count(outcome, prior) == expected


def test_probe_timestamp_coercion_spans_both_drivers():
    """asyncpg returns a datetime, aiosqlite returns CURRENT_TIMESTAMP's string.
    Reading only one of them makes every SQLite row look never-probed — which is
    how the two-strike rule collapsed into two consecutive ticks before this
    existed."""
    from datetime import datetime, timezone

    aware = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)
    assert svc._coerce_probe_timestamp(aware) == aware
    # naive datetime (SQLite via a driver that does parse it) -> assumed UTC
    assert svc._coerce_probe_timestamp(datetime(2026, 7, 31, 9, 0)) == aware
    # the raw CURRENT_TIMESTAMP string aiosqlite hands back
    assert svc._coerce_probe_timestamp("2026-07-31 09:00:00") == aware
    assert svc._coerce_probe_timestamp("2026-07-31T09:00:00Z") == aware
    # never probed / unreadable -> due
    assert svc._coerce_probe_timestamp(None) is None
    assert svc._coerce_probe_timestamp("") is None
    assert svc._coerce_probe_timestamp("not-a-timestamp") is None
    assert svc._coerce_probe_timestamp(12345) is None


# ---------------------------------------------------------------------------
# 4. The tick: two strikes, and only then
# ---------------------------------------------------------------------------


def _stub_probe(outcome: str, http_status: Optional[int]):
    async def _probe(store, **kwargs) -> Tuple[str, Optional[int]]:
        return outcome, http_status

    return _probe


@pytest.mark.asyncio
async def test_one_hard_failure_records_but_does_not_flip(monkeypatch: pytest.MonkeyPatch):
    merchant = f"{_MERCHANT_PREFIX}_strike1"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(store_id, merchant, "active")
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))

    summary = await svc.run_store_lifecycle_reconciliation_tick()

    row = await _store_row(store_id)
    assert row["status"] == "active", "one 401 must never disconnect a live merchant"
    assert row["upstream_probe_failures"] == 1
    assert row["upstream_probe_status"] == svc.PROBE_AUTH_FAILED
    assert row["upstream_probe_http_status"] == 401
    assert row["upstream_probe_at"] is not None
    assert summary["disconnected"] == []
    assert await _merchant_status(merchant) == "active"


@pytest.mark.asyncio
async def test_second_hard_failure_flips_store_and_merchant(monkeypatch: pytest.MonkeyPatch):
    """The whole point: the missed uninstall webhook, caught by pulling."""
    merchant = f"{_MERCHANT_PREFIX}_strike2"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(store_id, merchant, "active", failures=1)
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))

    summary = await svc.run_store_lifecycle_reconciliation_tick()

    row = await _store_row(store_id)
    assert row["status"] == svc.DISCONNECTED_STATUS
    assert row["upstream_probe_failures"] == 2
    assert [d["store_id"] for d in summary["disconnected"]] == [store_id]
    # ...and the public-search gate closes with it. This is the leg that was
    # missing entirely: catalog_merchants.status was a column nothing wrote.
    assert await _merchant_status(merchant) == "inactive"


@pytest.mark.asyncio
async def test_transient_failure_never_flips_however_often(monkeypatch: pytest.MonkeyPatch):
    merchant = f"{_MERCHANT_PREFIX}_transient"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(store_id, merchant, "active", failures=1)
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_UNREACHABLE, 503))
    monkeypatch.setenv("STORE_LIFECYCLE_PROBE_INTERVAL_SECONDS", "300")

    for _ in range(3):
        await svc.run_store_lifecycle_reconciliation_tick()

    row = await _store_row(store_id)
    assert row["status"] == "active"
    assert row["upstream_probe_failures"] == 1, "transient probes must not accumulate"
    assert await _merchant_status(merchant) == "active"


@pytest.mark.asyncio
async def test_success_resets_the_counter(monkeypatch: pytest.MonkeyPatch):
    merchant = f"{_MERCHANT_PREFIX}_recover"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(store_id, merchant, "active", failures=1)
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_OK, 200))

    await svc.run_store_lifecycle_reconciliation_tick()

    row = await _store_row(store_id)
    assert row["upstream_probe_failures"] == 0
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_inactive_stores_are_never_probed(monkeypatch: pytest.MonkeyPatch):
    """Probing is for stores we BELIEVE are connected. Anything else is spend
    with no decision attached to it."""
    merchant = f"{_MERCHANT_PREFIX}_notdue"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "inactive")
    await _seed_store(store_id, merchant, "disconnected")
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))

    summary = await svc.run_store_lifecycle_reconciliation_tick()

    assert summary["probed"] == 0
    assert (await _store_row(store_id))["upstream_probe_at"] is None


@pytest.mark.asyncio
async def test_probe_interval_suppresses_a_second_probe(monkeypatch: pytest.MonkeyPatch):
    merchant = f"{_MERCHANT_PREFIX}_interval"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(store_id, merchant, "active")
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))

    first = await svc.run_store_lifecycle_reconciliation_tick()
    second = await svc.run_store_lifecycle_reconciliation_tick()

    assert first["probed"] == 1
    assert second["probed"] == 0, "the 6h interval must gate re-probes"
    assert (await _store_row(store_id))["upstream_probe_failures"] == 1


@pytest.mark.asyncio
async def test_kill_switch_stops_all_writes(monkeypatch: pytest.MonkeyPatch):
    merchant = f"{_MERCHANT_PREFIX}_killswitch"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(store_id, merchant, "active", failures=1)
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))
    monkeypatch.setenv("STORE_LIFECYCLE_RECONCILE_ENABLED", "false")

    summary = await svc.run_store_lifecycle_reconciliation_tick()

    assert summary["enabled"] is False
    assert summary["probed"] == 0
    assert (await _store_row(store_id))["status"] == "active"


@pytest.mark.asyncio
async def test_no_credentials_is_not_upstream_evidence(monkeypatch: pytest.MonkeyPatch):
    """A store mid-reconnect has no token. That says nothing about whether the
    merchant uninstalled — and the uninstall webhook itself NULLs api_key, so
    treating it as evidence would double-count that path."""
    merchant = f"{_MERCHANT_PREFIX}_notoken"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(store_id, merchant, "active", failures=1)
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_NO_CREDENTIALS, None))

    await svc.run_store_lifecycle_reconciliation_tick()

    row = await _store_row(store_id)
    assert row["status"] == "active"
    assert row["upstream_probe_failures"] == 1
