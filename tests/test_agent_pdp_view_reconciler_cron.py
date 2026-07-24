"""Unit tests for ``jobs.agent_pdp_view_reconciler_cron``.

Covers the GLUE without standing up Postgres, in the FakeDb style of
test_catalog_row_trust_upserter.py: candidate ordering + per-run bound,
outcome counters that count writes-that-landed, per-key error isolation,
the drift count, and the post-pass threshold alarm. The view assembly
itself is the canonical refresh primitive (injected here as a fake) and is
covered by test_agent_pdp_view_refresh_helper.py.
"""

from __future__ import annotations

import logging

import pytest

from jobs.agent_pdp_view_reconciler_cron import (
    REFRESH_SOURCE,
    count_agent_pdp_view_drift,
    reconcile_agent_pdp_view,
    run_agent_pdp_view_reconcile_tick,
)


class FakeDb:
    """Same surface as the encode/databases async client (``fetch_one``,
    ``fetch_all``, ``execute``). Returns canned candidate rows in the order
    given — the reconciler must preserve that order — and honors the SQL's
    LIMIT :limit the way the real query truncates (a fake that returns
    everything would mask a silent-cap bug; see the upserter tests)."""

    def __init__(self, *, candidate_keys=None, drift_row=None):
        self._candidate_keys = list(candidate_keys or [])
        self._drift_row = drift_row
        self.fetch_all_calls: list[tuple[str, dict]] = []

    async def fetch_all(self, query, values=None):
        self.fetch_all_calls.append((query, dict(values or {})))
        assert "LIMIT :limit" in query
        limit = int((values or {}).get("limit"))
        return [{"content_key": ck} for ck in self._candidate_keys[:limit]]

    async def fetch_one(self, query, values=None):
        assert "missing_public" in query
        return self._drift_row

    async def execute(self, query, values=None):  # pragma: no cover — unused
        raise AssertionError("reconciler must never write directly; only the refresh primitive writes")


class RecordingRefresh:
    """Fake refresh primitive: records call order, returns/raises per key."""

    def __init__(self, *, outcomes=None):
        # outcomes: content_key -> True | False | Exception
        self._outcomes = outcomes or {}
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, content_key, *, refresh_source, db):
        self.calls.append((content_key, refresh_source))
        outcome = self._outcomes.get(content_key, True)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refreshes_candidates_in_caller_order():
    """Stalest-first candidate order is load-bearing — the pass must walk it
    verbatim, not re-sort."""
    db = FakeDb(candidate_keys=["ck_zzz_stalest", "ck_mmm", "ck_aaa_freshest"])
    refresh = RecordingRefresh()

    counters = await reconcile_agent_pdp_view(db=db, limit=10, refresh=refresh)

    assert [ck for ck, _ in refresh.calls] == ["ck_zzz_stalest", "ck_mmm", "ck_aaa_freshest"]
    assert counters == {"candidates": 3, "refreshed": 3, "skipped_no_row": 0, "errors": 0}


@pytest.mark.asyncio
async def test_refresh_source_is_stamped():
    db = FakeDb(candidate_keys=["ck_1"])
    refresh = RecordingRefresh()

    await reconcile_agent_pdp_view(db=db, limit=10, refresh=refresh)

    assert refresh.calls == [("ck_1", REFRESH_SOURCE)]


@pytest.mark.asyncio
async def test_limit_bounds_the_run_but_processes_every_fetched_key():
    """The per-run bound applies at the SELECT; every fetched key is
    processed — no inner truncation."""
    db = FakeDb(candidate_keys=[f"ck_{i:03d}" for i in range(7)])
    refresh = RecordingRefresh()

    counters = await reconcile_agent_pdp_view(db=db, limit=4, refresh=refresh)

    # FakeDb honored LIMIT 4; all 4 fetched keys were refreshed.
    assert counters["candidates"] == 4
    assert counters["refreshed"] == 4
    assert len(refresh.calls) == 4
    assert db.fetch_all_calls[0][1] == {"limit": 4}


@pytest.mark.asyncio
async def test_counts_landed_writes_not_attempts():
    """A refresh that returns False (assembler declined — no catalog row /
    too thin) is an attempt, not a landed write; it must be counted apart."""
    db = FakeDb(candidate_keys=["ck_ok", "ck_thin", "ck_ok2"])
    refresh = RecordingRefresh(outcomes={"ck_thin": False})

    counters = await reconcile_agent_pdp_view(db=db, limit=10, refresh=refresh)

    assert counters["refreshed"] == 2
    assert counters["skipped_no_row"] == 1
    assert counters["errors"] == 0


@pytest.mark.asyncio
async def test_one_poisoned_key_does_not_abort_the_pass():
    """Per-key isolation: an exception on one key is counted (and NOT as a
    landed write) and the remaining keys still converge — the #1574 lesson."""
    db = FakeDb(candidate_keys=["ck_first", "ck_poison", "ck_last"])
    refresh = RecordingRefresh(outcomes={"ck_poison": RuntimeError("boom")})

    counters = await reconcile_agent_pdp_view(db=db, limit=10, refresh=refresh)

    assert [ck for ck, _ in refresh.calls] == ["ck_first", "ck_poison", "ck_last"]
    assert counters == {"candidates": 3, "refreshed": 2, "skipped_no_row": 0, "errors": 1}


@pytest.mark.asyncio
async def test_empty_candidate_set_is_a_quiet_noop():
    db = FakeDb(candidate_keys=[])
    refresh = RecordingRefresh()

    counters = await reconcile_agent_pdp_view(db=db, limit=10, refresh=refresh)

    assert counters == {"candidates": 0, "refreshed": 0, "skipped_no_row": 0, "errors": 0}
    assert refresh.calls == []


@pytest.mark.asyncio
async def test_drift_count_sums_stale_and_missing():
    db = FakeDb(drift_row={"missing_public": 3, "stale": 5})
    drift = await count_agent_pdp_view_drift(db)
    assert drift == {"missing_public": 3, "stale": 5, "total": 8}


@pytest.mark.asyncio
async def test_drift_count_tolerates_no_row():
    db = FakeDb(drift_row=None)
    drift = await count_agent_pdp_view_drift(db)
    assert drift == {"missing_public": 0, "stale": 0, "total": 0}


# ---------------------------------------------------------------------------
# Tick wrapper: env gates + drift alarm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_error_logs_when_drift_exceeds_threshold(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_PDP_VIEW_DRIFT_ALERT_THRESHOLD", "10")
    db = FakeDb(candidate_keys=["ck_1"], drift_row={"missing_public": 7, "stale": 6})
    refresh = RecordingRefresh()

    with caplog.at_level(logging.INFO, logger="jobs.agent_pdp_view_reconciler_cron"):
        await run_agent_pdp_view_reconcile_tick(db=db, refresh=refresh)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "13" in errors[0].getMessage()  # total drift
    assert refresh.calls  # the pass still ran


@pytest.mark.asyncio
async def test_tick_stays_quiet_at_or_below_threshold(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_PDP_VIEW_DRIFT_ALERT_THRESHOLD", "13")
    db = FakeDb(candidate_keys=[], drift_row={"missing_public": 7, "stale": 6})
    refresh = RecordingRefresh()

    with caplog.at_level(logging.INFO, logger="jobs.agent_pdp_view_reconciler_cron"):
        await run_agent_pdp_view_reconcile_tick(db=db, refresh=refresh)

    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


@pytest.mark.asyncio
async def test_tick_disabled_via_env_kill_switch(monkeypatch):
    monkeypatch.setenv("AGENT_PDP_VIEW_RECONCILE_ENABLED", "false")
    db = FakeDb(candidate_keys=["ck_1"])
    refresh = RecordingRefresh()

    await run_agent_pdp_view_reconcile_tick(db=db, refresh=refresh)

    assert refresh.calls == []
    assert db.fetch_all_calls == []


@pytest.mark.asyncio
async def test_tick_swallows_db_failure(caplog):
    class BadDb:
        async def fetch_all(self, *_a, **_k):
            raise RuntimeError("db down")

        async def fetch_one(self, *_a, **_k):
            raise RuntimeError("db down")

    with caplog.at_level(logging.ERROR, logger="jobs.agent_pdp_view_reconciler_cron"):
        await run_agent_pdp_view_reconcile_tick(db=BadDb(), refresh=RecordingRefresh())

    # Logged, never raised into APScheduler.
    assert any("tick failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_invalid_limit_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AGENT_PDP_VIEW_RECONCILE_LIMIT", "not_a_number")
    db = FakeDb(candidate_keys=["ck_1"])
    refresh = RecordingRefresh()
    # drift query needs a row for the tick's post-pass count
    db._drift_row = {"missing_public": 0, "stale": 0}

    await run_agent_pdp_view_reconcile_tick(db=db, refresh=refresh)

    assert db.fetch_all_calls[0][1] == {"limit": 300}
