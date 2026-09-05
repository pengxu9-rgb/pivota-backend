"""The synthetic-row retention sweep.

Nothing has ever deleted from `commerce_interaction_events` or
`commerce_interactions`, so the ops canary's eight-event chain accumulates on
every run. This sweep deletes probe rows and ONLY probe rows: `synthetic IS
TRUE` (migration 213's column, indexed by 214) plus the pre-column shape
`surface = 'ops_canary'`.

The two guarantees the tests below exist to hold:

* a real event is never deleted, whatever its age;
* an interaction is deleted only when NO event of any kind is left pointing at
  it — a mixed interaction (one real event, one synthetic) keeps its row AND
  its real event.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import databases
import pytest
from sqlalchemy import create_engine, select

from db.commerce_interactions import commerce_interaction_events, commerce_interactions
from db.database import metadata


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
FRESH = NOW - timedelta(days=1)


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _event(
    event_id: str,
    interaction_id: str,
    merchant_id: str,
    *,
    occurred_at: datetime,
    synthetic: bool = False,
    surface: str = "merchant_storefront",
) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "interaction_id": interaction_id,
        "merchant_id": merchant_id,
        "platform": "shopify",
        "store_id": "store_1",
        "surface": surface,
        "event_type": "order.paid",
        "occurred_at": _naive(occurred_at),
        "synthetic": synthetic,
        "payload": {"order_id": event_id},
    }


def _interaction(interaction_id: str, merchant_id: str, *, last: datetime) -> Dict[str, Any]:
    return {
        "interaction_id": interaction_id,
        "merchant_id": merchant_id,
        "platform": "shopify",
        "store_id": "store_1",
        "first_occurred_at": _naive(last),
        "last_occurred_at": _naive(last),
    }


# Two merchants, two ages, four shapes of row.
_EVENTS: List[Dict[str, Any]] = [
    # merchant a -------------------------------------------------------------
    _event("evt_a_real_old", "int_a_real", "merch_a", occurred_at=OLD),
    _event("evt_a_syn_old", "int_a_syn", "merch_a", occurred_at=OLD, synthetic=True),
    _event("evt_a_syn_fresh", "int_a_syn_fresh", "merch_a", occurred_at=FRESH, synthetic=True),
    # the pre-213 probe shape: surface says ops_canary, synthetic is false
    _event(
        "evt_a_legacy_old",
        "int_a_legacy",
        "merch_a",
        occurred_at=OLD,
        surface="ops_canary",
    ),
    # a MIXED interaction: one real event and one synthetic event
    _event("evt_a_mixed_real", "int_a_mixed", "merch_a", occurred_at=OLD),
    _event("evt_a_mixed_syn", "int_a_mixed", "merch_a", occurred_at=OLD, synthetic=True),
    # merchant b -------------------------------------------------------------
    _event("evt_b_syn_old", "int_b_syn", "merch_b", occurred_at=OLD, synthetic=True),
    _event("evt_b_real_old", "int_b_real", "merch_b", occurred_at=OLD),
]

_INTERACTIONS: List[Dict[str, Any]] = [
    _interaction("int_a_real", "merch_a", last=OLD),
    _interaction("int_a_syn", "merch_a", last=OLD),
    _interaction("int_a_syn_fresh", "merch_a", last=FRESH),
    _interaction("int_a_legacy", "merch_a", last=OLD),
    _interaction("int_a_mixed", "merch_a", last=OLD),
    _interaction("int_b_syn", "merch_b", last=OLD),
    _interaction("int_b_real", "merch_b", last=OLD),
]

# Old + swept by the sweep's own rule: synthetic OR surface=ops_canary, and
# occurred_at older than the cutoff.
_SWEPT_EVENT_IDS = {
    "evt_a_syn_old",
    "evt_a_legacy_old",
    "evt_a_mixed_syn",
    "evt_b_syn_old",
}
# Of those, the interactions left with nothing at all.
_SWEPT_INTERACTION_IDS = {"int_a_syn", "int_a_legacy", "int_b_syn"}


async def _ledger(tmp_path, monkeypatch, name: str):
    db_path = tmp_path / f"{name}.sqlite3"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(
        sync_engine,
        tables=[commerce_interactions, commerce_interaction_events],
        checkfirst=True,
    )
    with sync_engine.begin() as connection:
        connection.execute(commerce_interactions.insert(), _INTERACTIONS)
        connection.execute(commerce_interaction_events.insert(), _EVENTS)
    sync_engine.dispose()
    test_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await test_database.connect()
    import services.commerce_ledger_retention as retention

    monkeypatch.setattr(retention, "database", test_database)
    return test_database


async def _event_ids(test_database) -> set[str]:
    rows = await test_database.fetch_all(select(commerce_interaction_events.c.event_id))
    return {row._mapping["event_id"] for row in rows}


async def _interaction_ids(test_database) -> set[str]:
    rows = await test_database.fetch_all(select(commerce_interactions.c.interaction_id))
    return {row._mapping["interaction_id"] for row in rows}


@pytest.mark.asyncio
async def test_a_dry_run_reports_exact_counts_and_deletes_nothing(tmp_path, monkeypatch):
    import services.commerce_ledger_retention as retention

    test_database = await _ledger(tmp_path, monkeypatch, "sweep-dry")
    try:
        before_events = await _event_ids(test_database)
        before_interactions = await _interaction_ids(test_database)

        result = await retention.sweep_synthetic_events(older_than_days=7, now=NOW)

        assert result["dry_run"] is True
        assert result["events_deleted"] == len(_SWEPT_EVENT_IDS)
        assert result["interactions_deleted"] == len(_SWEPT_INTERACTION_IDS)
        assert result["oldest"] == OLD.isoformat()
        assert result["newest"] == OLD.isoformat()
        assert result["by_merchant"] == {
            "merch_a": {"events": 3, "interactions": 2},
            "merch_b": {"events": 1, "interactions": 1},
        }
        assert result["batches"] == 1
        assert result["truncated"] is False

        assert await _event_ids(test_database) == before_events
        assert await _interaction_ids(test_database) == before_interactions
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_apply_deletes_only_aged_probe_rows(tmp_path, monkeypatch):
    import services.commerce_ledger_retention as retention

    test_database = await _ledger(tmp_path, monkeypatch, "sweep-apply")
    try:
        result = await retention.sweep_synthetic_events(
            older_than_days=7, apply=True, now=NOW
        )

        assert result["dry_run"] is False
        assert result["events_deleted"] == len(_SWEPT_EVENT_IDS)
        assert result["interactions_deleted"] == len(_SWEPT_INTERACTION_IDS)

        surviving_events = await _event_ids(test_database)
        assert surviving_events == {row["event_id"] for row in _EVENTS} - _SWEPT_EVENT_IDS
        # A real event is never deleted, whatever its age.
        assert "evt_a_real_old" in surviving_events
        assert "evt_b_real_old" in surviving_events
        # A FRESH probe row is inside the retention horizon and stays.
        assert "evt_a_syn_fresh" in surviving_events

        surviving_interactions = await _interaction_ids(test_database)
        assert (
            surviving_interactions
            == {row["interaction_id"] for row in _INTERACTIONS} - _SWEPT_INTERACTION_IDS
        )
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_mixed_interaction_keeps_its_row_and_its_real_event(tmp_path, monkeypatch):
    """The guarantee that makes the sweep safe to run against real history."""
    import services.commerce_ledger_retention as retention

    test_database = await _ledger(tmp_path, monkeypatch, "sweep-mixed")
    try:
        dry = await retention.sweep_synthetic_events(older_than_days=7, now=NOW)
        # Even the dry run must not claim it: the rule is evaluated before the
        # delete as "no event the sweep would NOT delete".
        assert "int_a_mixed" not in _SWEPT_INTERACTION_IDS
        assert dry["interactions_deleted"] == len(_SWEPT_INTERACTION_IDS)

        await retention.sweep_synthetic_events(older_than_days=7, apply=True, now=NOW)

        assert "int_a_mixed" in await _interaction_ids(test_database)
        events = await _event_ids(test_database)
        assert "evt_a_mixed_real" in events
        assert "evt_a_mixed_syn" not in events
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_the_legacy_ops_canary_surface_is_swept_even_without_the_column(
    tmp_path, monkeypatch
):
    import services.commerce_ledger_retention as retention

    test_database = await _ledger(tmp_path, monkeypatch, "sweep-legacy")
    try:
        await retention.sweep_synthetic_events(older_than_days=7, apply=True, now=NOW)
        assert "evt_a_legacy_old" not in await _event_ids(test_database)
        assert "int_a_legacy" not in await _interaction_ids(test_database)
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_batch_size_one_produces_one_batch_per_row(tmp_path, monkeypatch):
    import services.commerce_ledger_retention as retention

    test_database = await _ledger(tmp_path, monkeypatch, "sweep-batches")
    try:
        dry = await retention.sweep_synthetic_events(
            older_than_days=7, batch_size=1, now=NOW
        )
        assert dry["batches"] == len(_SWEPT_EVENT_IDS)
        assert dry["events_deleted"] == len(_SWEPT_EVENT_IDS)

        applied = await retention.sweep_synthetic_events(
            older_than_days=7, batch_size=1, apply=True, now=NOW
        )
        assert applied["batches"] == len(_SWEPT_EVENT_IDS)
        assert applied["events_deleted"] == len(_SWEPT_EVENT_IDS)
        assert applied["interactions_deleted"] == len(_SWEPT_INTERACTION_IDS)
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_max_batches_stops_early_and_says_so(tmp_path, monkeypatch):
    import services.commerce_ledger_retention as retention

    test_database = await _ledger(tmp_path, monkeypatch, "sweep-maxbatches")
    try:
        result = await retention.sweep_synthetic_events(
            older_than_days=7, batch_size=1, max_batches=2, now=NOW
        )
        assert result["batches"] == 2
        assert result["events_deleted"] == 2
        assert result["truncated"] is True
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_second_run_is_idempotent_and_reports_zeros(tmp_path, monkeypatch):
    import services.commerce_ledger_retention as retention

    test_database = await _ledger(tmp_path, monkeypatch, "sweep-idempotent")
    try:
        await retention.sweep_synthetic_events(older_than_days=7, apply=True, now=NOW)
        again = await retention.sweep_synthetic_events(
            older_than_days=7, apply=True, now=NOW
        )

        assert again["events_deleted"] == 0
        assert again["interactions_deleted"] == 0
        assert again["batches"] == 0
        assert again["oldest"] is None
        assert again["newest"] is None
        assert again["by_merchant"] == {}
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_the_retention_report_counts_real_history_and_deletes_nothing(
    tmp_path, monkeypatch
):
    import services.commerce_ledger_retention as retention

    test_database = await _ledger(tmp_path, monkeypatch, "retention-report")
    try:
        before_events = await _event_ids(test_database)
        before_interactions = await _interaction_ids(test_database)

        report = await retention.report_ledger_retention(horizon_days=7)

        # Everything OLD is behind a 7-day horizon; the fresh probe row is not.
        assert report["horizon_days"] == 7
        assert report["events_total"] == 7
        assert report["by_merchant"]["merch_a"]["events"] == 5
        assert report["by_merchant"]["merch_b"]["events"] == 2
        assert report["by_merchant"]["merch_a"]["interactions"] == 4
        assert report["by_merchant"]["merch_b"]["interactions"] == 2
        assert report["oldest"] == OLD.isoformat()

        assert await _event_ids(test_database) == before_events
        assert await _interaction_ids(test_database) == before_interactions
    finally:
        await test_database.disconnect()


def test_the_sweep_script_is_dry_run_by_default():
    """The script's defaults must match the service's."""
    from services.commerce_ledger_retention import (
        DEFAULT_BATCH_SIZE,
        DEFAULT_OLDER_THAN_DAYS,
    )

    parsed = _parse_script_args([])
    assert parsed.apply is False
    assert parsed.older_than_days == DEFAULT_OLDER_THAN_DAYS
    assert parsed.batch_size == DEFAULT_BATCH_SIZE
    assert parsed.report_horizon_days is None

    applied = _parse_script_args(["--apply", "--older-than-days", "30", "--batch-size", "5"])
    assert applied.apply is True
    assert applied.older_than_days == 30
    assert applied.batch_size == 5

    report = _parse_script_args(["--report-horizon-days", "365"])
    assert report.report_horizon_days == 365
    assert report.apply is False


def _parse_script_args(argv):
    """Build the script's parser without running it."""
    import scripts.sweep_commerce_ledger_synthetic as script

    captured = {}

    def _capture(args):
        captured["args"] = args

        async def _noop():
            return 0

        return _noop()

    original = script._run
    script._run = _capture
    try:
        script.main(argv)
    finally:
        script._run = original
    return captured["args"]
