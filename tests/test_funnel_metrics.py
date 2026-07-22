"""WS-4 funnel metrics — compute_funnel_metrics over a seeded funnel story.

Seeds (window = July 2026):
  m1: funnel signup (ai-readiness-audit) -> ran 1 audit, succeeded in 20m
  m2: funnel signup -> ran 2 audits (allowance exhausted) -> subscribed
  m3: organic signup (no source) -> never ran an audit
  m4: pre-window registration -> excluded from cohort but its 2 in-window
      runs still count toward allowance-exhaustion
  m5: funnel signup -> audit still running (started, not succeeded)
  deleted merchant -> excluded entirely

SQLite fixture mirrors tests/test_billing_charged_iff_delivered.py.
"""

import os
import tempfile
from datetime import datetime, timedelta

import pytest

import services.funnel_metrics_service as fms

WINDOW_START = datetime(2026, 7, 1)
WINDOW_END = datetime(2026, 8, 1)


@pytest.fixture
async def sqlite_db(monkeypatch):
    from databases import Database

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(f"sqlite:///{tmp.name}")
    await db.connect()
    await db.execute("""
        CREATE TABLE merchant_onboarding (
          merchant_id TEXT PRIMARY KEY,
          signup_source TEXT,
          status TEXT NOT NULL DEFAULT 'approved',
          created_at TIMESTAMP NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE merchant_audit_runs (
          run_id TEXT PRIMARY KEY,
          merchant_id TEXT NOT NULL,
          subject_type TEXT,
          status TEXT NOT NULL,
          requested_at TIMESTAMP NOT NULL,
          completed_at TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE user_subscriptions (
          id INTEGER PRIMARY KEY,
          merchant_id TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TIMESTAMP NOT NULL
        )
    """)
    monkeypatch.setattr(fms, "database", db)
    yield db
    await db.disconnect()
    os.unlink(tmp.name)


async def _seed(db):
    day = datetime(2026, 7, 10, 9, 0, 0)

    async def reg(mid, source, created, status="approved"):
        await db.execute(
            "INSERT INTO merchant_onboarding (merchant_id, signup_source, status, created_at) "
            "VALUES (:m, :s, :st, :c)",
            {"m": mid, "s": source, "st": status, "c": created},
        )

    async def run(rid, mid, status, requested, completed=None):
        await db.execute(
            "INSERT INTO merchant_audit_runs "
            "(run_id, merchant_id, subject_type, status, requested_at, completed_at) "
            "VALUES (:r, :m, 'merchant_url', :s, :q, :c)",
            {"r": rid, "m": mid, "s": status, "q": requested, "c": completed},
        )

    await reg("m1", "ai-readiness-audit", day)
    await run("r1", "m1", "succeeded", day + timedelta(minutes=5), day + timedelta(minutes=20))

    await reg("m2", "ai-readiness-audit", day + timedelta(days=1))
    await run("r2a", "m2", "succeeded", day + timedelta(days=1, minutes=10), day + timedelta(days=1, minutes=40))
    await run("r2b", "m2", "succeeded", day + timedelta(days=2), day + timedelta(days=2, minutes=30))
    await db.execute(
        "INSERT INTO user_subscriptions (merchant_id, status, created_at) "
        "VALUES ('m2', 'active', :c)",
        {"c": day + timedelta(days=3)},
    )

    await reg("m3", None, day + timedelta(days=2))

    # Pre-window registration; in-window runs still exhaust the allowance.
    await reg("m4", None, datetime(2026, 6, 1))
    await run("r4a", "m4", "succeeded", day, day + timedelta(minutes=30))
    await run("r4b", "m4", "running", day + timedelta(days=1))

    await reg("m5", "ai-readiness-audit", day + timedelta(days=3))
    await run("r5", "m5", "running", day + timedelta(days=3, minutes=8))

    await reg("mdel", "ai-readiness-audit", day, status="deleted")

    # Failed runs never count toward exhaustion.
    await run("r1x", "m1", "failed", day + timedelta(days=4))


@pytest.mark.asyncio
async def test_funnel_metrics_story(sqlite_db):
    await _seed(sqlite_db)
    out = await fms.compute_funnel_metrics(since=WINDOW_START, until=WINDOW_END)

    # Registrations: m1, m2, m3, m5 (m4 pre-window, mdel deleted).
    assert out["registrations"]["total"] == 4
    assert out["registrations"]["audit_funnel"] == 3
    assert out["registrations"]["by_source"] == {
        "ai-readiness-audit": 3,
        "(none)": 1,
    }

    # Activation: m1, m2, m5 started; m1 + m2 have a succeeded report.
    assert out["activation"]["cohort_first_audit_started"] == 3
    assert out["activation"]["cohort_first_audit_succeeded"] == 2
    assert out["activation"]["funnel_cohort_first_audit_started"] == 3

    # Allowance exhaustion: m2 (2 succeeded) and m4 (succeeded + running —
    # running counts, failed doesn't).
    assert out["quota"]["merchants_free_allowance_exhausted_in_window"] == 2

    # Upgrades: m2 subscribed, and it came from the funnel cohort.
    assert out["upgrades"]["subscriptions_created"] == 1
    assert out["upgrades"]["from_audit_funnel_cohort"] == 1

    # Time-to-first-value: m1 = 20m (reg 09:00 -> report 09:20), m2 = 40m
    # (reg day+1 09:00 -> report day+1 09:40) -> n=2, p50 = 30m.
    ttfv = out["time_to_first_value_minutes"]
    assert ttfv["n"] == 2
    assert ttfv["p50"] == pytest.approx(30.0, abs=0.2)
    assert ttfv["avg"] == pytest.approx(30.0, abs=0.2)

    # Conversion rates.
    assert out["conversion"]["registration_to_first_audit"] == round(3 / 4, 4)
    assert out["conversion"]["first_audit_to_succeeded_report"] == round(2 / 3, 4)
    assert out["conversion"]["registration_to_paid"] == round(1 / 4, 4)


@pytest.mark.asyncio
async def test_funnel_metrics_empty_window(sqlite_db):
    out = await fms.compute_funnel_metrics(
        since=datetime(2030, 1, 1), until=datetime(2030, 2, 1)
    )
    assert out["registrations"]["total"] == 0
    assert out["conversion"]["registration_to_first_audit"] is None
    assert out["time_to_first_value_minutes"]["n"] == 0
    assert out["time_to_first_value_minutes"]["p50"] is None
