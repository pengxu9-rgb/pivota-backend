"""The nightly refresh reads several hosts at once, never one host twice at once.

Measured 2026-09-27 (serial loop, image 8f7605b1e): 3,282 rows in the 3,300s budget, 2,220 origin
reads, and 1,014 rows spent on hosts that gave no answer at all (ichibanm.com 212). The served
population is ~20k, so a full pass took ~12 days against a 3-day launch target.

Pinned here, against the REAL batch with an injected refresher:
  * one worker IS the serial loop: same rows, same order;
  * N workers: a host never has two rows in flight, its rows keep their queue order, and more
    than one host really is in flight at once;
  * workers run in fresh contexts (databases 0.7.0 shares a Connection through a ContextVar);
  * the no-answer breaker: five connection/timeout/DNS/TLS rows in a row on one host stop the
    run asking it, any answer (even a 404) breaks the streak, and 0 disables it;
  * the budget still stops STARTING rows, with workers in flight;
  * where the time went, per host.
"""
from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import jobs.external_referral_refresh as job
import services.crawl_politeness as cp
import services.external_referral_readiness as err


def _ok() -> Dict[str, Any]:
    return {"status": "success", "price_refresh": {"status": "unchanged"}, "projection": {}}


def _no_answer(host: str, reason: str = "connection") -> Dict[str, Any]:
    text = {
        "connection": "snapshot_failed: ConnectError: All connection attempts failed",
        "timeout": "snapshot_failed: ReadTimeout: timed out",
    }[reason]
    return {"status": "degraded", "error": text, "domain": host}


def _http(host: str, code: int) -> Dict[str, Any]:
    return {"status": "degraded", "error": f"destination_unavailable: http {code}", "domain": host}


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    hosts: List[str],
    *,
    concurrency: Optional[int] = None,
    answers: Optional[Dict[str, List[Any]]] = None,
    unreachable_trip: str = "5",
    budget_seconds: Optional[float] = None,
    delay: float = 0.0,
):
    """Run the real batch over one row per entry of `hosts`, in queue order."""
    cp.reset_for_tests()
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_UNREACHABLE_TRIP", unreachable_trip)
    monkeypatch.delenv("EXTERNAL_REFERRAL_REFRESH_HOST_CONCURRENCY", raising=False)
    seed_ids = [f"eps_{i}" for i in range(len(hosts))]
    host_of = dict(zip(seed_ids, hosts))
    monkeypatch.setattr(
        err, "get_external_referral_refresh_candidate_seed_ids",
        lambda *a, **k: asyncio.sleep(0, result=seed_ids),
    )
    monkeypatch.setattr(
        err, "_fetch_refresh_candidate_hosts",
        lambda ids: asyncio.sleep(0, result={s: h for s, h in host_of.items() if h}),
    )
    queues = {h: list(v) for h, v in (answers or {}).items()}
    trace: Dict[str, Any] = {"started": [], "in_flight": {}, "max_per_host": 0, "max_total": 0}

    async def fake_refresh(seed_id: str, **kwargs) -> Dict[str, Any]:
        host = host_of[seed_id]
        trace["started"].append(seed_id)
        in_flight = trace["in_flight"]
        in_flight[host] = in_flight.get(host, 0) + 1
        trace["max_per_host"] = max(trace["max_per_host"], in_flight[host])
        trace["max_total"] = max(trace["max_total"], sum(in_flight.values()))
        try:
            await asyncio.sleep(delay)
            queue = queues.get(host)
            return queue.pop(0) if queue else _ok()
        finally:
            in_flight[host] -= 1

    summary = asyncio.run(
        err.run_external_referral_refresh_batch(
            refresh_seed_by_id=fake_refresh,
            limit=len(hosts),
            budget_seconds=budget_seconds,
            host_concurrency=concurrency,
        )
    )
    cp.reset_for_tests()
    return summary, trace, seed_ids


HOSTS = ["a.com", "a.com", "b.com", "a.com", "c.com", "b.com", "d.com", "a.com"]


def test_one_worker_is_the_serial_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    summary, trace, seed_ids = _drive(monkeypatch, HOSTS, concurrency=1, delay=0.001)
    assert trace["started"] == seed_ids, "queue order, row by row"
    assert trace["max_total"] == 1
    assert summary["host_concurrency"] == 1
    assert summary["refreshed"] == len(HOSTS)


def test_the_default_is_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    summary, trace, _ = _drive(monkeypatch, HOSTS, delay=0.001)
    assert summary["host_concurrency"] == 1 and trace["max_total"] == 1


def test_workers_never_read_one_host_twice_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    summary, trace, seed_ids = _drive(monkeypatch, HOSTS, concurrency=4, delay=0.01)
    assert trace["max_per_host"] == 1, "one row in flight per host, always"
    assert trace["max_total"] > 1, "and several hosts really are read at once"
    assert sorted(trace["started"]) == sorted(seed_ids), "every row, once"
    assert summary["refreshed"] == len(HOSTS)
    assert summary["attempted_count"] == len(HOSTS)
    for host in set(HOSTS):
        own = [s for s in trace["started"] if HOSTS[seed_ids.index(s)] == host]
        assert own == [s for s in seed_ids if HOSTS[seed_ids.index(s)] == host], (
            f"{host}'s rows keep their queue order"
        )


def test_the_earliest_queued_row_among_free_hosts_goes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Priority survives concurrency: the first rows started are the first free-host rows in the
    queue, not whichever host happens to be listed first."""
    hosts = ["a.com", "a.com", "a.com", "b.com", "c.com"]
    _summary, trace, _ = _drive(monkeypatch, hosts, concurrency=2, delay=0.01)
    assert trace["started"][:2] == ["eps_0", "eps_3"], "a's head, then b (a is busy)"


def test_workers_do_not_share_the_callers_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """databases 0.7.0 keeps its Connection in a ContextVar; a worker that inherited the batch
    task's context would share that one Connection and join a sibling's open transaction."""
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="fresh")
    seen: List[str] = []

    async def run(concurrency: int) -> None:
        marker.set("inherited")

        async def refresh(seed_id: str, **kwargs) -> Dict[str, Any]:
            seen.append(marker.get())
            return _ok()

        await err.run_external_referral_refresh_batch(
            refresh_seed_by_id=refresh, limit=2, host_concurrency=concurrency
        )

    monkeypatch.setattr(
        err, "get_external_referral_refresh_candidate_seed_ids",
        lambda *a, **k: asyncio.sleep(0, result=["eps_0", "eps_1"]),
    )
    monkeypatch.setattr(
        err, "_fetch_refresh_candidate_hosts",
        lambda ids: asyncio.sleep(0, result={"eps_0": "a.com", "eps_1": "b.com"}),
    )
    asyncio.run(run(2))
    assert seen == ["fresh", "fresh"]
    seen.clear()
    asyncio.run(run(1))
    assert seen == ["inherited", "inherited"], "one worker runs inline, as the serial loop did"


def test_a_host_that_never_answers_stops_costing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 09-27 shape: ichibanm.com refused 212 connections, one slot each."""
    hosts = ["ichibanm.com", "ok.com"] * 12
    summary, trace, seed_ids = _drive(
        monkeypatch, hosts, concurrency=1,
        answers={"ichibanm.com": [_no_answer("ichibanm.com")] * 12},
    )
    asked = [s for s in trace["started"] if hosts[seed_ids.index(s)] == "ichibanm.com"]
    assert len(asked) == 5, "trips on the 5th consecutive no-answer, never asks a 6th time"
    assert summary["skipped_for_unreachable_host"] == 7
    assert summary["unreachable_host_skips"] == {"ichibanm.com": 7}
    assert summary["host_unreachable_trip"] == 5
    # Not attempts: never handed to the refresher (so never stamped), out of the denominator.
    assert summary["attempted_count"] == 17
    assert summary["refreshed"] == 12, "every ok.com row was still read"
    assert summary["degraded_reason_counts"] == {"connection": 5}
    assert summary["unreachable_host_errors"] == {
        "ichibanm.com": "snapshot_failed: ConnectError: All connection attempts failed"
    }


def test_the_sample_error_carries_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = {"status": "degraded", "domain": "u.com",
              "error": "snapshot_failed: ConnectError for https://u.com/products/x?utm=1 refused"}
    summary, _trace, _ = _drive(monkeypatch, ["u.com"], concurrency=1, answers={"u.com": [answer]})
    assert summary["unreachable_host_errors"] == {"u.com": "snapshot_failed: ConnectError for <url> refused"}


def test_the_default_trip_does_not_stop_a_flaky_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """ichibanm.com, census 2026-09-28: ~55% of rows read, failures interleaved, 2,284 served
    seeds. The default (20) must keep reading it; five-in-a-row happens by chance on such a host."""
    pattern = [_no_answer("i.com")] * 6 + [_ok()] + [_no_answer("i.com")] * 7 + [_ok()]
    summary, trace, _ = _drive(
        monkeypatch, ["i.com"] * len(pattern), concurrency=1, unreachable_trip="",
        answers={"i.com": pattern},
    )
    assert summary["host_unreachable_trip"] == 20
    assert len(trace["started"]) == len(pattern)
    assert summary["skipped_for_unreachable_host"] == 0


def test_the_default_trip_still_stops_a_dead_host(monkeypatch: pytest.MonkeyPatch) -> None:
    summary, trace, _ = _drive(
        monkeypatch, ["dead.com"] * 30, concurrency=1, unreachable_trip="",
        answers={"dead.com": [_no_answer("dead.com")] * 30},
    )
    assert len(trace["started"]) == 20 and summary["unreachable_host_skips"] == {"dead.com": 10}


def test_timeouts_count_as_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    summary, trace, _ = _drive(
        monkeypatch, ["slow.com"] * 7, concurrency=1,
        answers={"slow.com": [_no_answer("slow.com", "timeout")] * 7},
    )
    assert len(trace["started"]) == 5 and summary["unreachable_host_skips"] == {"slow.com": 2}


def test_any_answer_breaks_the_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 is an answer: the host is reachable, the product is not. Only consecutive rows with
    no answer at all trip the breaker."""
    flaky = [_no_answer("f.com")] * 4 + [_http("f.com", 404)] + [_no_answer("f.com")] * 4 + [_ok()]
    summary, trace, _ = _drive(monkeypatch, ["f.com"] * 10, concurrency=1, answers={"f.com": flaky})
    assert len(trace["started"]) == 10
    assert summary["skipped_for_unreachable_host"] == 0


def test_the_no_answer_breaker_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    summary, trace, _ = _drive(
        monkeypatch, ["x.com"] * 8, concurrency=1, unreachable_trip="0",
        answers={"x.com": [_no_answer("x.com")] * 8},
    )
    assert len(trace["started"]) == 8 and summary["skipped_for_unreachable_host"] == 0


def test_the_no_answer_breaker_holds_under_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = ["dead.com"] * 9 + ["a.com", "b.com", "c.com"]
    summary, trace, seed_ids = _drive(
        monkeypatch, hosts, concurrency=3, delay=0.001,
        answers={"dead.com": [_no_answer("dead.com")] * 9},
    )
    asked = [s for s in trace["started"] if hosts[seed_ids.index(s)] == "dead.com"]
    assert len(asked) == 5 and summary["unreachable_host_skips"] == {"dead.com": 4}
    assert summary["refreshed"] == 3


def test_the_budget_stops_starting_rows_with_workers_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(err, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    hosts = ["a.com", "b.com", "c.com", "d.com"] * 5
    seed_ids = [f"eps_{i}" for i in range(len(hosts))]
    monkeypatch.setattr(
        err, "get_external_referral_refresh_candidate_seed_ids",
        lambda *a, **k: asyncio.sleep(0, result=seed_ids),
    )
    monkeypatch.setattr(
        err, "_fetch_refresh_candidate_hosts",
        lambda ids: asyncio.sleep(0, result=dict(zip(seed_ids, hosts))),
    )
    started: List[str] = []

    async def refresh(seed_id: str, **kwargs) -> Dict[str, Any]:
        started.append(seed_id)
        await asyncio.sleep(0)
        clock["t"] += 10.0
        return _ok()

    summary = asyncio.run(err.run_external_referral_refresh_batch(
        refresh_seed_by_id=refresh, limit=len(hosts), budget_seconds=35, host_concurrency=2,
    ))
    assert summary["stopped_early"] is True
    assert summary["skipped_for_budget"] == len(hosts) - len(started)
    assert summary["rows_started"] == len(started)
    assert 4 <= len(started) <= 6, "a row or two past the stop, never a whole extra round"
    assert summary["refreshed"] == len(started)


def test_the_summary_says_where_the_time_went(monkeypatch: pytest.MonkeyPatch) -> None:
    summary, _trace, _ = _drive(
        monkeypatch, ["a.com", "a.com", "b.com"], concurrency=1,
        answers={"b.com": [_no_answer("b.com")]},
    )
    assert summary["host_seconds"]["a.com"]["rows"] == 2
    assert summary["host_seconds"]["a.com"]["origin_reads"] == 2
    assert summary["host_seconds"]["b.com"] == {
        "rows": 1, "origin_reads": 0, "degraded": 1, "seconds": summary["host_seconds"]["b.com"]["seconds"],
    }
    assert summary["rows_started"] == 3


@pytest.mark.parametrize(
    "explicit, env, expected",
    [(None, "", 1), (None, "4", 4), (2, "4", 2), (0, "", 1), (99, "", 8), (None, "junk", 1)],
)
def test_concurrency_resolution(monkeypatch: pytest.MonkeyPatch, explicit, env, expected) -> None:
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_CONCURRENCY", env)
    assert err._refresh_host_concurrency(explicit) == expected


def test_the_job_passes_host_concurrency_through(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    seen: Dict[str, Any] = {}

    async def fake_batch(**kwargs) -> Dict[str, Any]:
        seen.update(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", fake_batch)
    monkeypatch.setattr(job.database, "connect", lambda: asyncio.sleep(0))
    monkeypatch.setattr(job.database, "disconnect", lambda: asyncio.sleep(0))
    monkeypatch.setattr(
        "sys.argv", ["external_referral_refresh", "--limit", "12000", "--host-concurrency", "4"]
    )
    assert job.main() == 0
    assert seen["host_concurrency"] == 4 and seen["limit"] == 12000
