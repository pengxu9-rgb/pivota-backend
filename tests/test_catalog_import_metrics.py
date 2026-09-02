"""Import outcomes must be countable — `credentials_unavailable` above all.

jobs/catalog_import_worker emitted NO metrics. `catalog_import_drain_tick`
(#1964) is dormant behind CATALOG_IMPORT_DRAIN_ENABLED and, when armed, will
walk a backlog nothing has ever drained. Without a counter the two outcomes that
most need a human — a credential-resolution outage, and a backlog burning down
into dead rows — are indistinguishable from a quiet queue.

The value is read off the REAL prometheus collector, not a spy on the record
helper: a test that asserts "the helper was called" passes even when the
collector was never created, the labels are wrong, or the disabled-path `None`
shadowed it. Reading the counter proves the series an operator would alert on
actually moves.

Mutation-checked: deleting either `_record_import_outcome` call site, or
dropping the error_category label, turns a test below red.
"""

from __future__ import annotations

import os

import pytest

import jobs.catalog_import_worker as worker
from observability.reliability_metrics import catalog_import_task_total


def _metrics_explicitly_disabled() -> bool:
    return (os.getenv("PROMETHEUS_METRICS_ENABLED") or "true").strip().lower() != "true"


# A MISSING COLLECTOR IS A FAILURE, NOT A SKIP.
#
# The first version of this file called pytest.skip() whenever the collector was
# None. With PROMETHEUS_METRICS_ENABLED=false that skipped five of six tests —
# the entire alertable-series claim — and still exited 0. Nothing would have
# caught it: backend-test-sweep cannot fail on skips (it has 33 legitimate
# env-gated ones) and guards with a 12,000-test floor instead, so losing five is
# invisible.
#
# So the skip now honours ONLY an explicit opt-out. If metrics are supposed to be
# on and the collector is absent — prometheus_client failed to import, or
# _existing_collector returned None on the duplicate-registration branch — that
# is a real defect in the thing under test and this file says so out loud.
if catalog_import_task_total is None and not _metrics_explicitly_disabled():
    raise AssertionError(
        "catalog_import_task_total was not registered even though "
        "PROMETHEUS_METRICS_ENABLED is not false — the counter this file exists "
        "to cover does not exist"
    )

pytestmark = pytest.mark.skipif(
    _metrics_explicitly_disabled(),
    reason="PROMETHEUS_METRICS_ENABLED is explicitly false",
)


def _counter_value(connector: str, status: str, error_category: str) -> float:
    """Read the live child series through the PUBLIC registry API.

    `REGISTRY.get_sample_value` rather than the child's private `._value.get()`:
    prometheus_client is unpinned in requirements.txt, and a rename of client
    internals would surface as a confusing 0.0 rather than an error. It also
    returns None (not an exception) for a series that has never been
    incremented, so no bare `except` is needed — and a bare except here would
    turn a wrong LABEL NAME into a silent 0.0 on both sides of the assertion.
    """
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(
        "catalog_import_task_total",
        {"connector": connector, "status": status, "error_category": error_category},
    )
    return 0.0 if value is None else float(value)


@pytest.fixture
def claimed(monkeypatch):
    """Make the claim succeed without a DB, so the test is about the metric."""
    task = {
        "id": 7,
        "merchant_id": "m_metrics",
        "source_type": "connector",
        "connector": "shopify",
        "attempt": 1,
        "counts": {},
    }

    async def _claim_by_id(task_id):
        return dict(task)

    async def _claim_next():
        return dict(task)

    monkeypatch.setattr(worker, "claim_ready_task_by_id", _claim_by_id)
    monkeypatch.setattr(worker, "claim_next_ready_task", _claim_next)
    return task


async def test_a_credentials_failure_moves_the_alertable_series(monkeypatch, claimed):
    """The series the runbook names when the drain is first armed."""
    async def _record(task):
        return {
            "processed": True,
            "task_id": 7,
            "status": "retry_scheduled",
            "counts": {"error_category": "credentials_unavailable"},
        }

    monkeypatch.setattr(worker, "_process_import_task_record", _record)
    before = _counter_value("shopify", "retry_scheduled", "credentials_unavailable")

    await worker.process_next_import_task()

    after = _counter_value("shopify", "retry_scheduled", "credentials_unavailable")
    assert after == before + 1, "the credentials_unavailable series did not move"


async def test_the_by_id_entry_point_counts_too(monkeypatch, claimed):
    """The endpoint's BackgroundTask goes through this path, not the drain tick.
    Counting only the tick would undercount every interactive Sync."""
    async def _record(task):
        return {"processed": True, "task_id": 7, "status": "succeeded", "counts": {}}

    monkeypatch.setattr(worker, "_process_import_task_record", _record)
    before = _counter_value("shopify", "succeeded", "none")

    await worker.process_import_task_by_id(7)

    after = _counter_value("shopify", "succeeded", "none")
    assert after == before + 1


async def test_a_claim_that_did_not_happen_is_not_counted(monkeypatch):
    """A drain tick on an empty queue fires every 30s forever. Counting those
    would swamp the series and make a real failure rate unreadable.

    What delivers this is the EARLY RETURN in process_next_import_task, not a
    guard inside _record_import_outcome — that function is only ever reached
    with a processed result, so such a guard would be unreachable. An earlier
    draft had one, and deleting it left this test green, which is how the dead
    code was found.
    """
    async def _claim_nothing():
        return None

    monkeypatch.setattr(worker, "claim_next_ready_task", _claim_nothing)
    before = _counter_value("unknown", "unknown", "none")

    result = await worker.process_next_import_task()

    assert result["processed"] is False
    assert _counter_value("unknown", "unknown", "none") == before


async def test_a_metrics_failure_never_breaks_the_import(monkeypatch, claimed):
    """Observability is best-effort. A broken collector must not turn a
    succeeded import into a failed one."""
    async def _record(task):
        return {"processed": True, "task_id": 7, "status": "succeeded", "counts": {}}

    calls = []

    def _boom(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(worker, "_process_import_task_record", _record)
    monkeypatch.setattr(
        "observability.reliability_metrics.record_catalog_import_task", _boom
    )

    result = await worker.process_import_task_by_id(7)

    # Positive counterpart. Without it this test passes even if
    # _record_import_outcome stops being called at all — the swallow would look
    # load-bearing while covering nothing. It also pins that the function-body
    # `from observability.reliability_metrics import ...` re-reads the module
    # attribute, so this patch target is the one that actually resolves.
    assert calls, "the recorder was never invoked; the swallow proves nothing"
    assert result["status"] == "succeeded"


async def test_an_arbitrary_connector_cannot_become_a_label_value(monkeypatch):
    """Cardinality clamp. `platform_import_tasks.connector` is a plain
    String(100) with no CHECK and no enum; `schedule_import_task` takes a bare
    Optional[str]; and services/platform_onboarding_service.py CATCHES
    InvalidConnectorError and logs it WITHOUT resetting the value. So a merchant
    calling POST /platform/onboarding/register can put an arbitrary string in
    that column and loop to seed unlimited distinct values.

    The drain lane is scoped to connector='shopify' today, so those rows are
    never processed. But db/platform_import_tasks.py explicitly invites widening
    the lane, and the clamp is what makes this safe when it widens — it does not
    depend on the lane staying narrow.
    """
    task = {
        "id": 9,
        "merchant_id": "m_evil",
        "source_type": "connector",
        "connector": "attacker-supplied-" + "x" * 40,
        "attempt": 1,
        "counts": {},
    }

    async def _claim_by_id(task_id):
        return dict(task)

    async def _record(t):
        return {"processed": True, "task_id": 9, "status": "failed", "counts": {}}

    monkeypatch.setattr(worker, "claim_ready_task_by_id", _claim_by_id)
    monkeypatch.setattr(worker, "_process_import_task_record", _record)

    before_other = _counter_value("other", "failed", "none")
    before_raw = _counter_value(task["connector"], "failed", "none")

    await worker.process_import_task_by_id(9)

    assert _counter_value("other", "failed", "none") == before_other + 1
    assert _counter_value(task["connector"], "failed", "none") == before_raw, (
        "an arbitrary merchant-supplied string became a live label value"
    )


async def test_a_retrying_task_counts_once_per_attempt_not_once_per_task(monkeypatch, claimed):
    """The multiplier an alert has to account for, pinned so the docs stay true.

    ShopifyCredentialsUnavailableError is deliberately retryable, so one
    merchant hitting one credential outage emits a sample per attempt. An
    earlier version of this counter's docstring called these outcomes
    "terminal", which would have led whoever wrote the runbook to read six
    samples as six affected merchants.
    """
    async def _record(task):
        return {
            "processed": True,
            "task_id": 7,
            "status": "retry_scheduled",
            "counts": {"error_category": "credentials_unavailable"},
        }

    monkeypatch.setattr(worker, "_process_import_task_record", _record)
    before = _counter_value("shopify", "retry_scheduled", "credentials_unavailable")

    for _ in range(3):
        await worker.process_next_import_task()

    after = _counter_value("shopify", "retry_scheduled", "credentials_unavailable")
    assert after == before + 3, (
        "retries are counted per attempt; if this ever becomes +1 the docs and "
        "any alert built on them are wrong"
    )
