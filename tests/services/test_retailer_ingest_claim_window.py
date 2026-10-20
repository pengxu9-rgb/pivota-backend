"""The drain claims no NEW stage between 03:00 and 06:30Z (configurable), nor within a lead before it (the
longest stage, an hour at least), so no stage is crawling when the window opens; a stage already running
finishes, and a refused claim takes no lease."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

import jobs.retailer_ingest_drain as drain
from db import retailer_ingest as ledger

DAY = datetime(2026, 10, 21, tzinfo=timezone.utc)
WINDOW = ledger.parse_claim_blackout(ledger.DEFAULT_CLAIM_BLACKOUT)


def at(hh, mm, ss=0):
    return DAY.replace(hour=hh, minute=mm, second=ss)


def test_the_default_window_is_three_to_half_past_six_utc():
    assert WINDOW == (180, 390)
    assert ledger.format_claim_blackout(WINDOW) == "03:00-06:30"
    assert ledger.format_claim_blackout(None) == "off"


@pytest.mark.parametrize("when, blocked", [
    (at(2, 59), False), (at(2, 59, 59), False), (at(3, 0), True), (at(3, 0, 1), True), (at(4, 30), True),
    (at(6, 29), True), (at(6, 29, 59), True), (at(6, 30), False), (at(6, 31), False), (at(0, 0), False),
    (at(23, 59), False), (at(12, 0), False),
])
def test_the_window_bounds(when, blocked):
    assert ledger.in_claim_blackout(when, WINDOW) is blocked


def test_the_window_is_judged_in_utc_whatever_the_instants_zone():
    pacific = timezone(timedelta(hours=-7))
    assert ledger.in_claim_blackout(datetime(2026, 10, 20, 20, 0, tzinfo=pacific), WINDOW)       # 03:00Z
    assert not ledger.in_claim_blackout(datetime(2026, 10, 20, 23, 30, tzinfo=pacific), WINDOW)  # 06:30Z


def test_no_window_blocks_nothing():
    assert not ledger.in_claim_blackout(at(4, 0), None)
    assert not ledger.in_claim_blackout(at(4, 0), None, 3600)


@pytest.mark.parametrize("when, blocked", [
    (at(1, 59, 59), False), (at(2, 0), True), (at(2, 59), True), (at(3, 0), True), (at(6, 29, 59), True),
    (at(6, 30), False), (at(12, 0), False),
])
def test_a_lead_refuses_claims_that_would_still_run_when_the_window_opens(when, blocked):
    assert ledger.in_claim_blackout(when, WINDOW, 3600) is blocked


def test_the_lead_is_counted_to_the_second():
    assert ledger.in_claim_blackout(at(2, 59, 30), WINDOW, 30)
    assert not ledger.in_claim_blackout(at(2, 59, 29), WINDOW, 30)


def test_the_lead_is_whole_seconds_and_wraps_midnight():
    assert ledger.in_claim_blackout(at(2, 30), WINDOW, 1800) and not ledger.in_claim_blackout(at(2, 29, 59), WINDOW, 1800)
    early = ledger.parse_claim_blackout("00:30-02:00")
    assert ledger.in_claim_blackout(DAY.replace(hour=23, minute=30) - timedelta(days=1), early, 3600)
    assert not ledger.in_claim_blackout(DAY.replace(hour=23, minute=29, second=59) - timedelta(days=1), early, 3600)
    assert not ledger.in_claim_blackout(at(2, 0), early, 3600)
    assert ledger.in_claim_blackout(at(3, 0), WINDOW, -50)  # a negative lead is no lead
    assert not ledger.in_claim_blackout(at(2, 59), WINDOW, -50)
    # a lead longer than the rest of the day refuses every claim rather than wrapping into a gap
    assert all(ledger.in_claim_blackout(at(h, 0), WINDOW, 10 * 86400) for h in range(24))


def test_a_window_may_wrap_midnight():
    window = ledger.parse_claim_blackout("23:30-01:15")
    assert window == (1410, 75)
    assert [ledger.in_claim_blackout(at(h, m), window) for h, m in
            ((23, 29), (23, 30), (0, 0), (1, 14), (1, 15), (12, 0))] == [False, True, True, True, False, False]


@pytest.mark.parametrize("raw, want", [
    ("03:00-06:30", (180, 390)), (" 02:00 - 06:30 ", (120, 390)), ("3:05-6:30", (185, 390)),
    ("off", None), ("OFF", None), ("none", None), ("disabled", None),
])
def test_parse(raw, want):
    assert ledger.parse_claim_blackout(raw) == want


@pytest.mark.parametrize("raw", ["", "03:00", "03:00-06:30-07:00", "3-6", "03:0-06:30", "24:00-06:30", "03:60-06:30",
                                 "03:00-03:00", "aa:bb-cc:dd", "03:00_06:30", "-1:00-06:30"])
def test_parse_refuses_anything_else(raw):
    with pytest.raises(ValueError):
        ledger.parse_claim_blackout(raw)


@pytest.mark.parametrize("raw, ok", [("06:30-03:00", False), ("01:00-13:01", False), ("01:00-13:00", True),
                                     ("20:00-08:00", True), ("20:00-08:01", False)])
def test_a_window_longer_than_twelve_hours_is_refused(raw, ok):
    if ok:
        assert ledger.parse_claim_blackout(raw)
    else:
        with pytest.raises(ValueError, match="longer than 12h"):
            ledger.parse_claim_blackout(raw)


def test_swapped_bounds_fall_back_to_the_default_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv(ledger.CLAIM_BLACKOUT_ENV, "06:30-03:00")
    with caplog.at_level(logging.WARNING, logger=ledger.__name__):
        assert ledger.claim_blackout_from_env() == (180, 390)
    assert any("longer than 12h" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("raw, want", [(None, (180, 390)), ("", (180, 390)), ("  ", (180, 390)),
                                       ("off", None), ("01:00-02:00", (60, 120)),
                                       ("garbage", (180, 390)), ("03:00-03:00", (180, 390)),
                                       ("06:30-03:00", (180, 390)), ("01:00-13:01", (180, 390))])
def test_the_env_value_and_a_typo_keeps_the_default_window(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv(ledger.CLAIM_BLACKOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(ledger.CLAIM_BLACKOUT_ENV, raw)
    assert ledger.claim_blackout_from_env() == want


class Script:
    """A clock that reads the given times in order."""

    def __init__(self, times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0)


class Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"a claim inside the window used db.{name}")


async def test_a_claim_inside_the_window_runs_no_statement_so_no_lease_is_taken():
    assert await ledger.claim_due_job(lease_seconds=60, db=Untouchable(), blackout=WINDOW, now=at(3, 0)) is None
    assert await ledger.claim_due_job(lease_seconds=60, db=Untouchable(), blackout=WINDOW, now=at(6, 29)) is None
    assert await ledger.claim_due_job(lease_seconds=60, db=Untouchable(), blackout=WINDOW, now=at(2, 0),
                                      lead_s=3600) is None


async def test_a_claim_outside_the_window_reaches_the_database():
    with pytest.raises(AssertionError, match="used db"):
        await ledger.claim_due_job(lease_seconds=60, db=Untouchable(), blackout=WINDOW, now=at(6, 30))
    with pytest.raises(AssertionError, match="used db"):
        await ledger.claim_due_job(lease_seconds=60, db=Untouchable(), blackout=WINDOW, now=at(2, 59))
    with pytest.raises(AssertionError, match="used db"):  # no window: unchanged behaviour
        await ledger.claim_due_job(lease_seconds=60, db=Untouchable(), now=at(4, 0))


async def _counts(**kw):
    return {"held": 1, "queued": 3}


@pytest.mark.parametrize("when, lead, claimed", [(at(2, 59), 0, True), (at(3, 0), 0, False), (at(6, 29), 0, False),
                                                 (at(6, 30), 0, True), (at(1, 59), 3600, True),
                                                 (at(2, 0), 3600, False)])
async def test_drain_once_claims_only_outside_the_window_and_always_reports_counts(monkeypatch, when, lead, claimed):
    claims = []

    async def claim(**kw):
        claims.append(kw)
        return None
    monkeypatch.setattr(drain.ledger, "claim_due_job", claim)
    monkeypatch.setattr(drain.ledger, "status_counts", _counts)
    out = await drain.drain_once(lease_seconds=60, db=object(), claim_blackout=WINDOW, now=lambda: when,
                                 claim_lead_s=lead)
    assert out["jobs"] == {"held": 1, "queued": 3}  # a held store still alerts from inside the window
    if claimed:
        assert out == {"outcome": "idle", "jobs": {"held": 1, "queued": 3}}
        assert claims[0]["blackout"] == WINDOW and claims[0]["now"] == when and claims[0]["lead_s"] == lead
    else:
        assert out["outcome"] == drain.CLAIM_WINDOW and out["claim_window"] == "03:00-06:30" and claims == []
        assert out["claim_lead_s"] == lead


async def test_the_last_claim_leaves_an_hour_before_the_window_and_the_stage_finishes(monkeypatch):
    """Claimed at 01:59 (an hour's lead), the stage ends at 02:20 and records its verdict; the loop then claims
    nothing more, so no stage is crawling at 03:00."""
    times = iter([at(1, 59), at(2, 20)])
    stages = []

    async def claim(**kw):
        return {"id": "rij_1", "status": "queued"}

    async def stage(job, *, db, time_left_s=None):
        stages.append(job["id"])
        return {"job_id": job["id"], "outcome": "clean", "status": "apply_due"}
    monkeypatch.setattr(drain.ledger, "claim_due_job", claim)
    monkeypatch.setattr(drain.ledger, "status_counts", _counts)
    monkeypatch.setattr(drain, "run_stage", stage)
    clock = iter([0.0, 0.0, 1260.0, 1260.0, 1260.0])
    summaries = await drain.drain_loop(lease_seconds=4200, budget_seconds=3000, db=object(),
                                       clock=lambda: next(clock), claim_blackout=WINDOW, now=lambda: next(times))
    assert [s["outcome"] for s in summaries] == ["clean", drain.CLAIM_WINDOW]
    assert stages == ["rij_1"]  # the in-flight stage ran to its end; nothing new was claimed
    assert summaries[1]["claim_lead_s"] == drain.DEFAULT_TASK_TIMEOUT_SECONDS == 3600


async def _leads(monkeypatch, *, task_timeout, stage_s):
    leads = []

    async def once(**kw):
        leads.append(kw["claim_lead_s"])
        return {"outcome": "clean", "jobs": {}}
    monkeypatch.setattr(drain, "drain_once", once)
    # stage 1 runs stage_s; stage 2 is claimed after it; the budget stops the loop there.
    await drain.drain_loop(lease_seconds=4200, budget_seconds=2 * stage_s + 1000, db=object(),
                           task_timeout_seconds=task_timeout, claim_blackout=WINDOW,
                           clock=Script([0, 0, stage_s, stage_s, stage_s + 1000]))
    return leads


async def test_the_lead_floor_is_the_task_timeout_and_moves_with_it(monkeypatch):
    assert await _leads(monkeypatch, task_timeout=3600, stage_s=900) == [3600, 3600]
    assert await _leads(monkeypatch, task_timeout=7200, stage_s=900) == [7200, 7200]


async def test_the_lead_is_the_longest_stage_seen_when_that_outlasts_the_task_timeout(monkeypatch):
    assert await _leads(monkeypatch, task_timeout=4000, stage_s=5000) == [4000, 5000]


async def test_without_a_window_the_loop_is_unchanged(monkeypatch):
    seen = []

    async def once(**kw):
        seen.append(kw)
        return {"outcome": "idle", "jobs": {}}
    monkeypatch.setattr(drain, "drain_once", once)
    await drain.drain_loop(lease_seconds=60, budget_seconds=0, db=object())
    assert seen[0]["claim_blackout"] is None


@pytest.mark.parametrize("env, window, timeout", [(None, (180, 390), None), ("off", None, "5400"),
                                                  ("02:00-06:30", (120, 390), "7200")])
def test_main_hands_the_env_window_and_timeout_to_the_loop(monkeypatch, capsys, env, window, timeout):
    seen = []

    class DB:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

    async def loop(**kw):
        seen.append((kw["claim_blackout"], kw["task_timeout_seconds"]))
        return []
    capsys.readouterr()
    if timeout is None:
        monkeypatch.delenv("RETAILER_INGEST_TASK_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("RETAILER_INGEST_TASK_TIMEOUT_SECONDS", timeout)
    lead = int(timeout or 3600)
    if env is None:
        monkeypatch.delenv(ledger.CLAIM_BLACKOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(ledger.CLAIM_BLACKOUT_ENV, env)
    monkeypatch.setenv("RETAILER_INGEST_DRAIN_ENABLED", "1")
    monkeypatch.setattr("sys.argv", ["drain"])
    monkeypatch.setattr(drain, "database", DB())
    monkeypatch.setattr(drain, "drain_loop", loop)
    assert drain.main() == 0
    assert seen == [(window, lead)]
    (config,) = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(drain.CONFIG_MARKER)]
    body = json.loads(config[len(drain.CONFIG_MARKER):])
    assert body["claim_window"] == ledger.format_claim_blackout(window) and body["claim_lead_floor_s"] == lead
    assert not config.startswith(drain.SUMMARY_MARKER)  # the held/failed log metrics count summary lines only


def test_a_claim_window_tick_prints_one_summary_line(monkeypatch, capsys):
    class DB:
        async def connect(self):
            return None

        async def disconnect(self):
            return None
    monkeypatch.setenv(ledger.CLAIM_BLACKOUT_ENV, "11:00-13:00")
    monkeypatch.setattr(drain, "_utcnow", lambda: at(12, 0))  # pinned inside it
    monkeypatch.setenv("RETAILER_INGEST_DRAIN_ENABLED", "1")
    monkeypatch.setattr("sys.argv", ["drain"])
    monkeypatch.setattr(drain, "database", DB())
    monkeypatch.setattr(drain.ledger, "status_counts", _counts)

    async def claim(**kw):
        raise AssertionError("claimed inside the window")
    monkeypatch.setattr(drain.ledger, "claim_due_job", claim)
    assert drain.main() == 0
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(drain.SUMMARY_MARKER)]
    body = json.loads(line[len(drain.SUMMARY_MARKER):])
    assert body["outcome"] == "claim_window" and body["jobs"] == {"held": 1, "queued": 3}
