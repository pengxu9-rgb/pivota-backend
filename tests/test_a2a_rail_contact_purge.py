"""jobs/a2a_rail_contact_purge: the buyer's email and address go within 72 h even with the rail's dials off.

Pinned here: dormant until its own flag; independent of A2A_RAIL_WORKER_ENABLED and the rail client;
purges rows past 71 h in any state without moving the state, keeps younger ones; bounded batches that
finish a backlog over runs; logs counts, never a value; registered on the worker as a cron at four fixed
minutes, with a deadline and run-now; and the schedule arithmetic keeps the 72 h promise.
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.a2a_rail_ledger as ledger  # noqa: E402
import jobs.a2a_rail_contact_purge as job  # noqa: E402

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="SQLite arm; the purge SQL's Postgres arm is pinned in test_a2a_rail_ledger_postgres.py")

EMAIL = "ada@example.test"
STREET = "900 Brannan St"
_RAIL_ENV = ("A2A_RAIL_WORKER_ENABLED", "A2A_RAIL_ENABLED", "A2A_RAIL_BASE_URL", "A2A_RAIL_API_KEY")


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    for table in ("agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    yield


@pytest.fixture(autouse=True)
def _rail_off(monkeypatch):
    """Every test runs with the rail's own dials and client OFF: the job must not need them."""
    for name in _RAIL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(job.ENABLED_ENV, raising=False)


async def _opened(hours_ago: float, state: str = "opened") -> str:
    row = await ledger.create_purchase(
        agent_id="agent_a2a", agent_user_ref_hash="h" * 64, merchant_domain="brand.example",
        checkout_url="https://brand.example/cart/123:1", currency="USD", our_price_minor=1500,
        product_name="Glow Serum", consent_version="v1", buyer_email=EMAIL,
        shipping_address={"addressLine1": STREET, "city": "San Francisco"})
    await database.execute(
        f"UPDATE a2a_rail_purchases SET state = :s, created_at = datetime('now', '-{int(hours_ago * 60)} minutes') "
        "WHERE id = :i", {"s": state, "i": row["id"]})
    return row["id"]


async def _rows():
    return {r["id"]: r for r in await database.fetch_all("SELECT * FROM a2a_rail_purchases")}


async def test_dormant_until_its_own_flag():
    old = await _opened(100)
    assert await job.run_a2a_rail_contact_purge() == {"skipped": "disabled"}
    assert (await _rows())[old]["buyer_email"] == EMAIL


@pytest.mark.parametrize("value", ["1", "true", "on", "yes", " TRUE "])
async def test_purges_with_the_rail_dials_and_client_off(monkeypatch, value):
    monkeypatch.setenv(job.ENABLED_ENV, value)
    paying = await _opened(71.5, state="paying")
    waiting = await _opened(80, state="needs_payment_method")
    young = await _opened(70)
    result = await job.run_a2a_rail_contact_purge()
    assert result == {"contact_purged": 2, "batches": 1}
    rows = await _rows()
    for pid, state in ((paying, "paying"), (waiting, "needs_payment_method")):
        assert rows[pid]["state"] == state, "the purge never moves the state"
        assert rows[pid]["buyer_email"] is None and rows[pid]["shipping_address"] is None
        assert rows[pid]["contact_purged_at"] is not None
    assert rows[young]["buyer_email"] == EMAIL and rows[young]["shipping_address"] is not None


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "enabled"])
async def test_anything_else_is_off(monkeypatch, value):
    monkeypatch.setenv(job.ENABLED_ENV, value)
    old = await _opened(100)
    assert await job.run_a2a_rail_contact_purge() == {"skipped": "disabled"}
    assert (await _rows())[old]["buyer_email"] == EMAIL


async def test_a_backlog_is_bounded_per_run_and_finishes_over_runs(monkeypatch):
    monkeypatch.setenv(job.ENABLED_ENV, "1")
    monkeypatch.setattr(job, "BATCH", 2)
    monkeypatch.setattr(job, "MAX_BATCHES", 2)
    for _ in range(5):
        await _opened(90)
    assert await job.run_a2a_rail_contact_purge() == {"contact_purged": 4, "batches": 2, "more": True}
    assert await job.run_a2a_rail_contact_purge() == {"contact_purged": 1, "batches": 1}
    assert all(r["buyer_email"] is None for r in (await _rows()).values())
    assert await job.run_a2a_rail_contact_purge() == {"contact_purged": 0, "batches": 1}


async def test_logs_counts_never_a_value(monkeypatch, caplog):
    monkeypatch.setenv(job.ENABLED_ENV, "1")
    pid = await _opened(90)
    with caplog.at_level(logging.DEBUG):
        await job.run_a2a_rail_contact_purge()
    text = caplog.text
    assert "contact_purged" in text
    for secret in (EMAIL, STREET, pid):
        assert secret not in text


def test_the_schedule_keeps_the_72_hour_promise():
    """Worst case: a row crosses 71 h just after a run; the next run is <= 15 min later. Even with
    two runs missed in a row it is purged before 72 h (the run itself is bounded at 120 s)."""
    minutes = [3, 18, 33, 48]
    gaps = [(b - a) % 60 for a, b in zip(minutes, minutes[1:] + minutes[:1])]
    assert max(gaps) == 15
    worst = job.PURGE_AGE_SECONDS + 3 * max(gaps) * 60 + 120
    assert worst < 72 * 3600, worst
    assert job.PURGE_AGE_SECONDS == ledger.CONTACT_RETENTION_SECONDS_DEFAULT - 3600 == 71 * 3600


class _Recorder:
    def __init__(self, *a, **k):
        self.added = []

    def add_job(self, func, *a, **k):
        self.added.append((k.get("id"), a, k, func))

    def start(self):
        pass

    def get_jobs(self):
        return []


async def test_registered_on_the_worker_as_a_cron_with_a_deadline_and_run_now(monkeypatch):
    import routes.admin_scheduler_jobs as admin
    import services.audit_scheduler as sched

    for k in ("AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT", "SCHEDULER_JOB_ALLOWLIST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "web")
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    rec = _Recorder()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: rec)
    await sched.start_scheduler()
    assert sched._BOOT_ERROR is None, sched._BOOT_ERROR
    found = [(a, k, fn) for jid, a, k, fn in rec.added if jid == "a2a_rail_contact_purge"]
    assert len(found) == 1, [jid for jid, *_ in rec.added]
    args, kwargs, fn = found[0]
    assert args == ("cron",)
    assert kwargs["minute"] == "3,18,33,48"
    assert kwargs["max_instances"] == 1 and kwargs["coalesce"] is True
    assert getattr(fn, "__wrapped_job_id__", None) == "a2a_rail_contact_purge"
    assert sched._JOB_RUN_DEADLINES["a2a_rail_contact_purge"] == 120
    assert "a2a_rail_contact_purge" in admin._RUNNABLE_JOB_IDS
