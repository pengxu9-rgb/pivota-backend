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

from db.catalog import catalog_merchants
from db.database import database
from services import store_lifecycle_service as svc
from tests.model_schema import ensure_model_tables


_MERCHANT_PREFIX = "slt"


async def _connect_if_needed() -> bool:
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    return was_connected


async def _ensure_schema() -> None:
    # catalog_merchants is DERIVED from db/catalog.py — see tests/model_schema.py.
    # The hand-written copy this replaces declared `indexable BOOLEAN DEFAULT TRUE`
    # where the model says `nullable=False`, which is precisely the laxer-fixture
    # defect that let a test assert `indexable IS NULL` and pass in isolation.
    await ensure_model_tables((catalog_merchants,))

    # merchant_stores has NO SQLAlchemy model — it is created by main.py:1461 and
    # widened by db/schema_guard.py — so there is nothing to derive it from and it
    # stays hand-written. NULLABILITY MIRRORS PRODUCTION EXACTLY. A laxer fixture
    # schema is not a convenience, it's a mask: this table is created by whichever
    # test module gets there first in a full-suite run, so a fixture that drops a
    # NOT NULL passes in isolation and fails the moment the real DDL wins the race.
    # That is exactly how the first push went green locally and red in CI (20
    # failures, `NOT NULL constraint failed: merchant_stores.name`). Keep every NOT
    # NULL, and have the seed helpers supply every one of them.
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_stores (
            store_id VARCHAR(50) PRIMARY KEY,
            merchant_id VARCHAR(50) NOT NULL,
            platform VARCHAR(50) NOT NULL,
            name VARCHAR(255) NOT NULL,
            domain VARCHAR(255),
            api_key TEXT,
            status VARCHAR(50) DEFAULT 'connected',
            connected_at TIMESTAMP,
            last_sync TIMESTAMP,
            product_count INTEGER DEFAULT 0,
            is_primary BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            upstream_probe_at TIMESTAMP,
            upstream_probe_status TEXT,
            upstream_probe_http_status INTEGER,
            upstream_probe_failures INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # The probe columns are added by migration 190 / schema_guard in the real
    # world; in a full-suite run another module may have created merchant_stores
    # from the pre-190 DDL, so add them here the same way schema_guard does.
    for column, coltype in (
        ("upstream_probe_at", "TIMESTAMP"),
        ("upstream_probe_status", "TEXT"),
        ("upstream_probe_http_status", "INTEGER"),
        ("upstream_probe_failures", "INTEGER NOT NULL DEFAULT 0"),
    ):
        try:
            await database.execute(f"ALTER TABLE merchant_stores ADD COLUMN {column} {coltype}")
        except Exception:
            pass  # already present


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
    connected_at: Optional[str] = None,
    probe_at: Optional[str] = None,
) -> None:
    await database.execute(
        """
        INSERT INTO merchant_stores
            (store_id, merchant_id, platform, name, domain, api_key, status,
             upstream_probe_failures, connected_at, upstream_probe_at)
        VALUES (:store_id, :merchant_id, :platform, :name, :domain, :api_key, :status,
                :failures, :connected_at, :probe_at)
        """,
        {
            "store_id": store_id,
            "merchant_id": merchant_id,
            "platform": platform,
            "name": f"Store {store_id}",
            "domain": domain,
            "api_key": '{"access_token": "shpat_test"}',
            "status": status,
            "failures": failures,
            "connected_at": connected_at,
            "probe_at": probe_at,
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


# ---------------------------------------------------------------------------
# 5. What adversarial review broke (all four were live-capable)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_raising_probe_cannot_abort_the_tick(monkeypatch: pytest.MonkeyPatch):
    """The poison-row bug. The Wix credential helpers RAISE on a blank site id
    rather than returning "", and never-probed rows sort FIRST — so one
    malformed store row aborted every tick before any other store was reached,
    and before the status sweep ran. APScheduler swallows it: the job was
    permanently inert while reporting nothing."""
    poison_merchant = f"{_MERCHANT_PREFIX}_poison"
    good_merchant = f"{_MERCHANT_PREFIX}_good"
    await _seed_merchant(poison_merchant, "active")
    await _seed_store(f"{poison_merchant}_s1", poison_merchant, "active", platform="wix", domain="")
    await _seed_merchant(good_merchant, "active")
    await _seed_store(f"{good_merchant}_s1", good_merchant, "disconnected")

    # The real Wix path — NOT a stub. This is the only test that executes a
    # probe implementation end to end; every other tick test stubs it, which is
    # precisely why this class survived the first round of tests.
    summary = await svc.run_store_lifecycle_reconciliation_tick()

    assert summary["outcomes"].get(svc.PROBE_NO_CREDENTIALS) == 1
    # `probed` counts UPSTREAM EVIDENCE, not rows walked. This row produced
    # none, and it must not inflate the correlated-failure breaker's
    # denominator — a fleet padded with malformed rows would otherwise
    # under-trigger the guard, which is the platform-mix sensitivity that moving
    # off `examined` exists to remove. (Mutation-checked: counting it here
    # leaves every other assertion in this file green.)
    assert summary["examined"] == 1
    assert summary["probed"] == 0
    # ...and the rest of the tick still ran: the sweep reached the other merchant.
    assert await _merchant_status(good_merchant) == "inactive"
    assert summary["status_sweep"]["examined"] >= 2


@pytest.mark.asyncio
async def test_probe_store_upstream_never_raises_on_malformed_wix_row():
    outcome, http_status = await svc.probe_store_upstream(
        {"store_id": "x", "platform": "wix", "domain": "   ", "api_key": "key"}
    )
    assert outcome == svc.PROBE_NO_CREDENTIALS
    assert http_status is None


@pytest.mark.asyncio
async def test_reconnect_clears_stale_failures(monkeypatch: pytest.MonkeyPatch):
    """The two-strike rule spans a RECONNECT, or it isn't a two-strike rule.

    No reconnect path clears the probe columns — they all write connected_at.
    So a store that failed once weeks ago, was reconnected, and then hits a
    single transient 401 (most likely right after a reconnect, in the
    token-refresh window) would otherwise be disconnected on its FIRST failure
    of the current connection."""
    merchant = f"{_MERCHANT_PREFIX}_stale"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(
        store_id,
        merchant,
        "active",
        failures=1,
        probe_at="2026-07-01 00:00:00",       # failure measured...
        connected_at="2026-07-15 00:00:00",   # ...before this reconnect
    )
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))

    await svc.run_store_lifecycle_reconciliation_tick()

    row = await _store_row(store_id)
    assert row["status"] == "active", "a reconnect must reset the strike count"
    assert row["upstream_probe_failures"] == 1
    assert await _merchant_status(merchant) == "active"


@pytest.mark.parametrize(
    "prior,connected_at,probe_at,expected",
    [
        (2, "2026-07-15 00:00:00", "2026-07-01 00:00:00", 0),  # reconnected after the failures
        (2, "2026-07-01 00:00:00", "2026-07-15 00:00:00", 2),  # failures are current
        (2, "2026-07-15 00:00:00", None, 2),                   # never probed
        (0, "2026-07-15 00:00:00", "2026-07-01 00:00:00", 0),
    ],
)
def test_effective_prior_failures(prior, connected_at, probe_at, expected):
    assert svc.effective_prior_failures(prior, connected_at, probe_at) == expected


def test_effective_prior_failures_falls_back_to_created_at():
    """`connected_at` is nullable. With no connect anchor at all, a stale count
    of unknown vintage would be trusted — so fall back to created_at, which the
    schema always stamps."""
    # created_at newer than the probe -> the count predates this connection.
    assert svc.effective_prior_failures(2, None, "2026-07-01 00:00:00", "2026-07-15 00:00:00") == 0
    # created_at older -> the count is current.
    assert svc.effective_prior_failures(2, None, "2026-07-15 00:00:00", "2026-07-01 00:00:00") == 2
    # Neither anchor: KEEP the count. Resetting every tick would mean the store
    # could never reach the threshold — trading a false disconnect for a
    # permanent leak, which is the wrong trade for this module.
    assert svc.effective_prior_failures(2, None, "2026-07-15 00:00:00", None) == 2


def test_effective_prior_failures_on_aware_datetimes():
    """asyncpg hands both columns back as aware datetimes; the SQLite fixture
    only ever produces strings, so the type prod actually sees needs its own
    assertion."""
    from datetime import datetime, timezone

    older = datetime(2026, 7, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert svc.effective_prior_failures(2, newer, older) == 0
    assert svc.effective_prior_failures(2, older, newer) == 2
    # mixed shapes (one driver, one fixture) must still compare
    assert svc.effective_prior_failures(2, newer, "2026-07-01 00:00:00") == 0


@pytest.mark.asyncio
async def test_correlated_failure_rate_limits_but_still_drains(monkeypatch: pytest.MonkeyPatch):
    """Two strikes defends against INDEPENDENT transients. It does nothing
    against a common-mode failure — an expired SHOPIFY_API_VERSION, a
    platform-wide 401, a credential-store regression — where every store fails
    the same way and the whole fleet would drop out of public recall.

    But the breaker must RATE-LIMIT, not halt. Halting was the first design and
    it was a permanent inertness bug: a fleet of >=5 stores where most are
    GENUINELY gone (ops retiring three test rigs in an afternoon — Pivota's
    fleet is largely retired rigs) could never be reconciled. That is #1648
    reopened by the fix for #1648."""
    merchant = f"{_MERCHANT_PREFIX}_fleet"
    await _seed_merchant(merchant, "active")
    for i in range(6):
        # connected long before any probe — the normal shape, since every probe
        # stamps upstream_probe_at = now, so only a genuine reconnect can leave
        # connected_at newer than the last probe.
        await _seed_store(
            f"{merchant}_s{i}", merchant, "active", failures=1, connected_at="2019-01-01 00:00:00"
        )
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))
    monkeypatch.setenv("STORE_LIFECYCLE_PROBE_INTERVAL_SECONDS", "300")

    first = await svc.run_store_lifecycle_reconciliation_tick()

    # Rate-limited hard: one per tick, not six, and loudly withheld for the rest.
    assert len(first["disconnected"]) == 1
    assert len(first["withheld"]) == 5

    # ...but it DRAINS. Each subsequent probe cycle takes one more. Backdating
    # upstream_probe_at is how we advance past the 6h per-store probe interval;
    # in prod this is simply the next cycle, so a 6-store fleet reconciles in
    # ~6 cycles rather than never.
    for _ in range(8):
        await database.execute(
            "UPDATE merchant_stores SET upstream_probe_at = '2020-01-01 00:00:00' "
            "WHERE merchant_id = :m",
            {"m": merchant},
        )
        await svc.run_store_lifecycle_reconciliation_tick()

    statuses = [(await _store_row(f"{merchant}_s{i}"))["status"] for i in range(6)]
    assert all(s == svc.DISCONNECTED_STATUS for s in statuses), statuses
    assert await _merchant_status(merchant) == "inactive"


def test_correlated_ratio_uses_probed_not_examined():
    """The denominator must exclude stores that produced NO upstream evidence.
    Counting no_credentials/unsupported_platform makes the guard's sensitivity a
    function of platform mix — malformed rows dilute it, a pure-Shopify fleet
    trips it — which is nobody's intended tuning knob."""
    # 13 examined, but only 6 actually probed, 4 of them hard -> correlated,
    # and the correlated cap (1) is already spent.
    correlated = {
        "examined": 13,
        "probed": 6,
        "outcomes": {svc.PROBE_AUTH_FAILED: 4, svc.PROBE_OK: 2, svc.PROBE_NO_CREDENTIALS: 7},
        "disconnected": [{"store_id": "a"}],
    }
    assert "correlated failure" in (svc._correlated_failure_breaker(correlated) or "")

    # The identical tick judged on `examined` (13) would NOT look correlated
    # (4*2 !> 13), so the 7 evidence-free rows would have bought the fleet a
    # pass straight through to the normal cap.
    on_examined = dict(correlated, probed=correlated["examined"])
    assert svc._correlated_failure_breaker(on_examined) is None


@pytest.mark.asyncio
async def test_per_tick_disconnect_cap(monkeypatch: pytest.MonkeyPatch):
    """Below the ratio breaker's fleet floor, the absolute cap still bounds the
    damage a single tick can do."""
    merchant = f"{_MERCHANT_PREFIX}_cap"
    await _seed_merchant(merchant, "active")
    for i in range(4):
        await _seed_store(f"{merchant}_s{i}", merchant, "active", failures=1)
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))
    monkeypatch.setenv("STORE_LIFECYCLE_MIN_FLEET_FOR_RATIO", "99")  # disable the ratio guard
    monkeypatch.setenv("STORE_LIFECYCLE_MAX_DISCONNECTS_PER_TICK", "2")

    summary = await svc.run_store_lifecycle_reconciliation_tick()

    assert len(summary["disconnected"]) == 2
    assert len(summary["withheld"]) == 2
    disconnected = [
        s for s in [await _store_row(f"{merchant}_s{i}") for i in range(4)]
        if s["status"] == svc.DISCONNECTED_STATUS
    ]
    assert len(disconnected) == 2


@pytest.mark.asyncio
async def test_outer_catch_survives_a_probe_that_escapes_its_own_handler(
    monkeypatch: pytest.MonkeyPatch,
):
    """The tick's own catch, not the probe's. Both exist because the inner one
    is the kind of thing a later refactor narrows; if it does, this is the only
    thing standing between one bad row and a permanently inert job."""
    merchant = f"{_MERCHANT_PREFIX}_outercatch"
    other = f"{_MERCHANT_PREFIX}_outerother"
    await _seed_merchant(merchant, "active")
    await _seed_store(f"{merchant}_s1", merchant, "active")
    await _seed_merchant(other, "active")
    await _seed_store(f"{other}_s1", other, "disconnected")

    async def _boom(store, **kwargs):
        raise RuntimeError("probe handler was narrowed and something got through")

    monkeypatch.setattr(svc, "probe_store_upstream", _boom)

    summary = await svc.run_store_lifecycle_reconciliation_tick()

    assert summary["outcomes"].get(svc.PROBE_NO_CREDENTIALS) == 1
    # The sweep still ran — that is the half that closes the leak.
    assert await _merchant_status(other) == "inactive"


@pytest.mark.asyncio
async def test_disconnect_refuses_when_the_stored_counter_is_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
):
    """The disconnect's `COALESCE(upstream_probe_failures,0) >= :threshold`
    clause. It guards the window where something else resets the counter between
    the probe and the write; without it the write trusts an in-memory number."""
    merchant = f"{_MERCHANT_PREFIX}_racecheck"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(store_id, merchant, "active", failures=1)
    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))

    real_record = svc._record_probe

    async def racing_record(**kwargs):
        # Probe says "threshold reached"...
        await real_record(**kwargs)
        # ...but a concurrent reconnect resets the stored counter first.
        await database.execute(
            "UPDATE merchant_stores SET upstream_probe_failures = 0 WHERE store_id = :s",
            {"s": kwargs["store_id"]},
        )
        return 2

    monkeypatch.setattr(svc, "_record_probe", racing_record)

    summary = await svc.run_store_lifecycle_reconciliation_tick()

    assert summary["disconnected"] == []
    assert (await _store_row(store_id))["status"] == "active"
    assert await _merchant_status(merchant) == "active"


@pytest.mark.asyncio
async def test_portal_status_patch_counts_as_a_reconnect(monkeypatch: pytest.MonkeyPatch):
    """Drives the REAL route — `PUT /merchant/integrations/store/{id}` — because
    it builds its UPDATE dynamically from `update_fields`, so the source gate
    above (which reads literal SQL) cannot see it. Confirmed by mutation:
    deleting the route's connected_at line left all 68 other tests green.

    The route used to write `status` alone, so a store rejoined the serving set
    carrying a failure count from its PREVIOUS connection — and the next single
    transient 401 disconnected it. That is B2(i), reproduced live in review.
    """
    from routes import manage_integrations

    merchant = f"{_MERCHANT_PREFIX}_patch"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(
        store_id,
        merchant,
        "inactive",
        failures=1,
        connected_at="2026-05-01 00:00:00",
        probe_at="2026-06-01 00:00:00",
    )

    # The route's own SQL runs for real against the fixture DB; only the auth
    # identity is supplied.
    result = await manage_integrations.update_store(
        store_id,
        {"status": "active"},
        {"role": "merchant", "merchant_id": merchant},
    )
    assert result["status"] == "success"

    reconnected = await _store_row(store_id)
    assert reconnected["status"] == "active"
    assert str(reconnected["connected_at"]) > "2026-06-01", (
        "re-entering the serving set must stamp connected_at, or the two-strike "
        "rule has nothing to tell this connection apart from the last one"
    )

    monkeypatch.setattr(svc, "probe_store_upstream", _stub_probe(svc.PROBE_AUTH_FAILED, 401))
    await svc.run_store_lifecycle_reconciliation_tick()

    row = await _store_row(store_id)
    assert row["status"] == "active", "one 401 after a portal reconnect must not disconnect"
    assert row["upstream_probe_failures"] == 1


@pytest.mark.asyncio
async def test_portal_status_patch_to_inactive_does_not_stamp_connected_at():
    """The mirror: leaving the serving set is not a reconnect. A blanket stamp
    would forge a connection time on a store being switched OFF, and hand it a
    fresh two-strike allowance it did not earn."""
    from routes import manage_integrations

    merchant = f"{_MERCHANT_PREFIX}_patchoff"
    store_id = f"{merchant}_s1"
    await _seed_merchant(merchant, "active")
    await _seed_store(
        store_id, merchant, "active", connected_at="2026-05-01 00:00:00"
    )

    await manage_integrations.update_store(
        store_id,
        {"status": "inactive"},
        {"role": "merchant", "merchant_id": merchant},
    )

    row = await _store_row(store_id)
    assert row["status"] == "inactive"
    assert str(row["connected_at"]).startswith("2026-05-01")


def test_every_store_activation_writer_stamps_connected_at():
    """Repo-wide invariant, not a unit test — `effective_prior_failures` keys the
    two-strike rule on `connected_at`, so a writer that moves a store INTO the
    serving set without stamping it silently re-opens the disconnect-on-first-
    401 hole. Four writers did exactly that. This is the gate that stops the
    fifth: it reads the source, so it fails at PR time rather than in prod.

    Scanned as text on purpose. A behavioural test would have to stand up each
    route's auth and platform mocks, and the thing worth protecting is a
    one-line property of the SQL.
    """
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]

    # Widened after review enumerated the first version's blind spots: it only
    # matched `UPDATE merchant_stores SET` (so an alias hid a writer), only the
    # literal 'active'/'connected' or exactly `:status` (so any other bind name
    # hid one), and only non-recursive routes/ + services/ (so jobs/ and
    # scripts/ were invisible). Any SET-body reaching a WHERE/ON CONFLICT/end
    # now counts, and the status match is generic.
    # \Z (end of input) is a terminator so a SET with no WHERE still yields a
    # body — that shape updates EVERY store row, which is the last one that
    # should slip past. Over-capture here can only produce a false POSITIVE,
    # which a human then checks; a missed statement is silent.
    statement = re.compile(
        r"UPDATE\s+merchant_stores\b[^;]*?\bSET\b(.*?)(?:\bWHERE\b|\bRETURNING\b|\"\"\"|'''|;|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    # (?<!\w) so `upstream_probe_status = ...` is not read as a lifecycle write.
    activation = re.compile(
        r"""(?<!\w)status\s*=\s*(?:'(?:active|connected)'|"(?:active|connected)"|:\w*status\w*)""",
        re.IGNORECASE,
    )

    offenders = []
    statements_seen = 0
    for directory in ("routes", "services", "jobs", "scripts"):
        for path in sorted((repo / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            for match in statement.finditer(source):
                statements_seen += 1
                body = match.group(1)
                if not activation.search(body):
                    continue
                if re.search(r"(?<!\w)connected_at\s*=", body):
                    continue
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(repo)}:{line}")

    # The regexes below are self-tested, but nothing proved the LOOP opened a
    # file. Pointing the scan at mistyped directories, or dropping jobs/ and
    # scripts/, left this gate at zero coverage and still green — the same
    # "matches nothing is also green" failure one level up. The real tree yields
    # 41; floor it well under that so ordinary churn doesn't trip it.
    assert statements_seen >= 30, (
        f"the gate scanned only {statements_seen} UPDATE merchant_stores "
        "statements — check the directory list; it may be scanning nothing"
    )

    assert offenders == [], (
        "these UPDATEs move a store into the serving set without stamping "
        "connected_at, which lets a failure count from the PREVIOUS connection "
        f"disconnect the new one on its first transient 401: {offenders}"
    )

    # A source gate that matches nothing is also green. These are the shapes
    # review proved the FIRST version of this regex was blind to — each must be
    # flagged, or "no offenders" means "no eyes".
    must_flag = [
        "UPDATE merchant_stores SET status = 'active' WHERE store_id = :s",
        "UPDATE merchant_stores ms SET status = 'active' WHERE ms.store_id = :s",   # aliased
        'UPDATE merchant_stores SET status = "active" WHERE store_id = :s',         # double-quoted
        "UPDATE merchant_stores SET status = :new_status WHERE store_id = :s",      # other bind name
        "UPDATE merchant_stores SET status = 'ACTIVE' WHERE store_id = :s",         # case
        "UPDATE merchant_stores SET status = 'connected', name = :n WHERE id = :s",
        "UPDATE merchant_stores SET status = 'active'",                             # no WHERE
    ]
    for snippet in must_flag:
        found = statement.search(snippet)
        assert found and activation.search(found.group(1)), f"gate is blind to: {snippet}"

    must_not_flag = [
        "UPDATE merchant_stores SET upstream_probe_status = :status WHERE store_id = :s",
        "UPDATE merchant_stores SET status = 'inactive' WHERE store_id = :s",
        "UPDATE merchant_stores SET status = 'active', connected_at = CURRENT_TIMESTAMP WHERE id = :s",
    ]
    for snippet in must_not_flag:
        found = statement.search(snippet)
        body = found.group(1) if found else ""
        flagged = bool(activation.search(body)) and not re.search(
            r"(?<!\w)connected_at\s*=", body
        )
        assert not flagged, f"gate false-positives on: {snippet}"


@pytest.mark.asyncio
async def test_a_dead_probe_half_is_reported_not_disguised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A broken due-store query yields examined=0 probed=0 — byte-identical to a
    quiet, correct tick. The summary line is this job's ONLY inertness detector,
    so it has to carry the error, or the detector reports health while the thing
    it detects is happening. (Found when `created_at` joined the SELECT: on a
    schema without that column the probe half is dead forever and the tick still
    printed 'complete'.)"""
    merchant = f"{_MERCHANT_PREFIX}_deadhalf"
    await _seed_merchant(merchant, "active")
    await _seed_store(f"{merchant}_s1", merchant, "disconnected")

    async def broken_fetch(limit):
        raise RuntimeError("no such column: created_at")

    monkeypatch.setattr(svc, "_fetch_due_stores", broken_fetch)

    import logging

    with caplog.at_level(logging.INFO, logger="services.store_lifecycle_service"):
        summary = await svc.run_store_lifecycle_reconciliation_tick()

    assert summary["examined"] == 0
    assert summary["error"] == "RuntimeError", "the tick must RECORD which half died"

    # ...and SAY so in the summary line. Nothing consumes the return value —
    # APScheduler discards it — so this log line is the entire detector, and
    # asserting on the dict alone would let it print health forever. (Mutation:
    # dropping probe_error from the line left every other assertion green.)
    completions = [
        r.getMessage() for r in caplog.records if "tick complete" in r.getMessage()
    ]
    assert completions, "the tick must always emit its summary line"
    assert "probe_error=RuntimeError" in completions[-1], completions[-1]

    # The other half still converges — a dead probe must not take the
    # write-through down with it.
    assert summary["status_sweep"]["changed"] == 1
    assert await _merchant_status(merchant) == "inactive"


@pytest.mark.asyncio
async def test_a_dead_sweep_half_is_also_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """The other half. `probe_error` and `sweep_error` were added together and
    only one was asserted — so the write-through could die silently while the
    line still read as a healthy tick."""
    import logging

    async def broken_sweep():
        return {"examined": 0, "changed": 0, "transitions": [], "error": "OperationalError"}

    monkeypatch.setattr(svc, "reconcile_catalog_merchant_statuses", broken_sweep)

    with caplog.at_level(logging.INFO, logger="services.store_lifecycle_service"):
        await svc.run_store_lifecycle_reconciliation_tick()

    completions = [
        r.getMessage() for r in caplog.records if "tick complete" in r.getMessage()
    ]
    assert completions, "the tick must always emit its summary line"
    assert "sweep_error=OperationalError" in completions[-1], completions[-1]


def test_created_at_is_covered_by_schema_guard():
    """`created_at` became load-bearing when it joined the due-store SELECT.
    Railway does not run db/migrations/, so schema_guard is the only thing that
    puts a column in prod — and a column missing from REQUIRED_SCHEMA fails
    SILENTLY (dead probe half) instead of at /health.

    Asserts the heal reaches THE merchant_stores STATEMENT, not that the string
    exists somewhere in a 1,700-line file: moving the ALTER onto an unrelated
    table passed the string-presence version while merchant_stores never got
    the column.
    """
    from db.schema_guard import REQUIRED_SCHEMA

    required = {spec.table: spec.columns for spec in REQUIRED_SCHEMA}
    assert "created_at" in required["merchant_stores"]


@pytest.mark.asyncio
async def test_fast_mode_heal_adds_created_at_to_merchant_stores(monkeypatch):
    """Prod skips heavy startup init, so `ensure_required_schema_light` is the
    ONLY thing that lands these columns. Same shape as the merchant_psps test
    that already guards this failure mode for another table."""
    from db import schema_guard as sg

    executed: List[str] = []

    class DummyDB:
        async def execute(self, query):
            executed.append(str(query))

    async def noop_connect():
        return None

    monkeypatch.setattr(sg, "IS_POSTGRES", True)
    monkeypatch.setattr(sg, "IS_SQLITE", False)
    monkeypatch.setattr(sg, "database", DummyDB())
    monkeypatch.setattr(sg, "_ensure_database_connected", noop_connect)

    await sg.ensure_required_schema_light()

    store_stmt = next(
        s for s in executed
        if "ALTER TABLE IF EXISTS merchant_stores" in s and "upstream_probe_failures" in s
    )
    assert "ADD COLUMN IF NOT EXISTS created_at" in store_stmt
    for column in (
        "upstream_probe_at",
        "upstream_probe_status",
        "upstream_probe_http_status",
        "upstream_probe_failures",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in store_stmt


def test_sqlite_heal_types_match_the_real_schema():
    """A heal that disagrees with the real schema is worse than no heal — it
    makes a self-healed DB behave unlike prod for the first executing test that
    touches it. Without its map entry, `created_at` falls to the 'TEXT' default
    and nothing errors; the divergence is just silent. (This file's own comment
    warns about exactly that, citing catalog_merchants.indexable.)"""
    import re
    from pathlib import Path

    guard = (Path(__file__).resolve().parents[1] / "db" / "schema_guard.py").read_text(
        encoding="utf-8"
    )
    for column, expected in (
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("upstream_probe_at", "TIMESTAMP"),
        ("upstream_probe_status", "TEXT"),
        ("upstream_probe_http_status", "INTEGER"),
        ("upstream_probe_failures", "INTEGER NOT NULL DEFAULT 0"),
    ):
        found = re.search(
            rf'\("merchant_stores",\s*"{column}"\):\s*"([^"]+)"', guard
        )
        assert found, f"no SQLite heal type for merchant_stores.{column}"
        assert found.group(1) == expected, (
            f"merchant_stores.{column} heals as {found.group(1)!r}, "
            f"migration 190 / main.py says {expected!r}"
        )


@pytest.mark.asyncio
async def test_kill_switch_also_stops_the_route_write_through(monkeypatch: pytest.MonkeyPatch):
    """The switch has to cover the hooks too — they write the same
    public-recall gate the job does."""
    merchant = f"{_MERCHANT_PREFIX}_switchhook"
    await _seed_merchant(merchant, "active")
    await _seed_store(f"{merchant}_s1", merchant, "disconnected")
    monkeypatch.setenv("STORE_LIFECYCLE_RECONCILE_ENABLED", "false")

    out = await svc.sync_catalog_merchant_status(merchant)

    assert out["skipped"] == "disabled"
    assert await _merchant_status(merchant) == "active"


@pytest.mark.asyncio
async def test_probe_does_not_persist_credentials(monkeypatch: pytest.MonkeyPatch):
    """A health probe must not rewrite api_key. persist_refresh replaces the
    WHOLE credential blob from the snapshot the tick read, so a merchant
    reconnecting inside the probe window would lose their fresh tokens."""
    captured: Dict[str, Any] = {}

    async def fake_resolve(**kwargs):
        captured.update(kwargs)
        return None, {}

    import services.shopify_access_token_service as tok

    monkeypatch.setattr(tok, "resolve_shopify_admin_access_token", fake_resolve)

    outcome, _ = await svc.probe_store_upstream(
        {
            "store_id": "s1",
            "platform": "shopify",
            "domain": "example.myshopify.com",
            "api_key": '{"access_token": "shpat_x"}',
        }
    )

    assert captured.get("persist_refresh") is False
    assert outcome == svc.PROBE_NO_CREDENTIALS  # no token resolved -> not evidence


@pytest.mark.asyncio
async def test_unknown_future_merchant_status_is_left_alone():
    """`catalog_merchants.status` is NOT NULL (migration 058), so NULL cannot
    occur — but a status this module has never heard of can, and it must be
    treated like 'observed': recorded, not written."""
    merchant = f"{_MERCHANT_PREFIX}_futurestatus"
    await _seed_merchant(merchant, "quarantined_2027")
    await _seed_store(f"{merchant}_s1", merchant, "disconnected")

    out = await svc.sync_catalog_merchant_status(merchant)

    assert out["changed"] is False
    assert out["skipped"] == "unmanaged_status:quarantined_2027"
    assert await _merchant_status(merchant) == "quarantined_2027"


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
