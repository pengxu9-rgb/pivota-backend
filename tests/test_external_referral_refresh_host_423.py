"""A host that answers HTTP 423 ("Locked") row after row stops costing rows for the rest of the run.

Measured 2026-11-02..05: one host answered 423 to all 621 rows the refresh asked of it over 39 runs
(0 origin reads, ~8 s a row). No breaker saw it: 423 is not a 429/503 or a challenge, and it is an
answer, so it is never "no answer".

Pinned here, against the REAL batch with an injected refresher:
  * three 423s in a row trip the host for the rest of the run; its remaining rows are skipped;
  * only a REAL answer breaks the streak: a read that reached the origin, or another HTTP status
    (a 404, a 403 challenge). A timeout, a pacer/robots refusal, a cached snapshot, an exception or
    an unprocessable seed leaves it alone, so 423s around them still add up;
  * the streak is per host, and a host another breaker already holds is left to that breaker;
  * the trip is per run: the next run asks the host again and needs three new 423s;
  * other hosts are untouched, serial or concurrent;
  * the skipped rows ARE stamped (`last_crawl_attempt_at` only), unlike every other host skip, and
    the other breakers' skips still are not;
  * the counters reach the summary and the PRICE_REFRESH line; skipped rows leave the yield's
    denominator like every other host skip.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

import jobs.external_referral_refresh as job
import services.crawl_politeness as cp
import services.external_referral_readiness as err


def _ok() -> Dict[str, Any]:
    return {"status": "success", "price_refresh": {"status": "unchanged"}, "projection": {}}


def _http(host: str, code: int) -> Dict[str, Any]:
    return {"status": "degraded", "error": f"destination_unavailable: http {code}", "domain": host}


def _timeout(host: str) -> Dict[str, Any]:
    return {"status": "degraded", "error": "snapshot_failed: ReadTimeout: timed out", "domain": host}


def _paced(host: str) -> Dict[str, Any]:
    return {"status": "degraded", "error": "snapshot_failed: CrawlDelayTooLong: crawl delay too long", "domain": host}


def _cached() -> Dict[str, Any]:
    return {**_ok(), "snapshot_from_cache": True}


def _no_answer(host: str) -> Dict[str, Any]:
    return {"status": "degraded", "error": "snapshot_failed: ConnectError: All connection attempts failed", "domain": host}


LOCKED = "locked.example"
OTHER = "other.example"


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    hosts: List[str],
    *,
    answers: Optional[Dict[str, List[Any]]] = None,
    concurrency: Optional[int] = 1,
    locked_trip: Optional[str] = None,
    stamp_raises: bool = False,
):
    """Run the real batch over one row per entry of `hosts`, in queue order. Returns the summary,
    the ids started, the seed ids, and the ids the 423 stamp was asked to write."""
    cp.reset_for_tests()
    monkeypatch.delenv("EXTERNAL_REFERRAL_REFRESH_HOST_CONCURRENCY", raising=False)
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_UNREACHABLE_TRIP", "5")
    if locked_trip is None:
        monkeypatch.delenv("EXTERNAL_REFERRAL_REFRESH_HOST_LOCKED_TRIP", raising=False)
    else:
        monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_LOCKED_TRIP", locked_trip)
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
    stamped: List[List[str]] = []

    class _FakeDb:
        async def execute(self, query: str, params: Dict[str, Any]) -> None:
            if stamp_raises:
                raise RuntimeError("db down")
            stamped.append([params[k] for k in sorted(params, key=lambda k: int(k[2:]))])
            _FakeDb.queries.append(query)

        queries: List[str] = []

    _FakeDb.queries = []
    monkeypatch.setattr(err, "database", _FakeDb())
    queues = {h: list(v) for h, v in (answers or {}).items()}
    started: List[str] = []

    async def fake_refresh(seed_id: str, **kwargs) -> Dict[str, Any]:
        started.append(seed_id)
        await asyncio.sleep(0)
        queue = queues.get(host_of[seed_id])
        item = queue.pop(0) if queue else _ok()
        if isinstance(item, Exception):
            raise item
        return item

    summary = asyncio.run(
        err.run_external_referral_refresh_batch(
            refresh_seed_by_id=fake_refresh, limit=len(hosts), host_concurrency=concurrency,
        )
    )
    cp.reset_for_tests()
    flat = [i for chunk in stamped for i in chunk]
    return summary, started, seed_ids, flat, _FakeDb.queries


def test_three_423s_in_a_row_skip_the_rest_of_the_hosts_rows_and_stamp_them(monkeypatch) -> None:
    summary, started, ids, stamped, queries = _drive(
        monkeypatch, [LOCKED] * 8, answers={LOCKED: [_http(LOCKED, 423)] * 8},
    )
    assert started == ids[:3]
    assert summary["skipped_for_host_423"] == 5
    assert summary["host_423_skips"] == {LOCKED: 5}
    assert summary["host_423_trip"] == 3
    assert stamped == ids[3:] and summary["host_423_stamped"] == 5
    assert all(q.startswith("UPDATE external_product_seeds SET last_crawl_attempt_at = NOW() WHERE id IN (") for q in queries)
    assert all("last_crawled_at" not in q for q in queries), "the rows stay stale; only the attempt clock moves"
    assert summary["degraded"] == 3 and summary["attempted_count"] == 3
    # The other host skips are untouched.
    assert (summary["skipped_for_host_backoff"], summary["skipped_for_unreachable_host"],
            summary["skipped_for_host_challenge"]) == (0, 0, 0)


@pytest.mark.parametrize(
    "pattern",
    [
        [423, 423, 200, 423, 423, 200, 423, 423],  # a read breaks the streak
        [423, 423, 404, 423, 423, 404, 423, 423],  # so does any other answer
        [423, 200, 423, 200, 423, 200, 423, 200],
        [423, 403, 423, 423],  # a 403 is an answer too
    ],
)
def test_mixed_answers_below_the_trip_never_trip(monkeypatch, pattern) -> None:
    answers = [_ok() if code == 200 else _http(LOCKED, code) for code in pattern]
    summary, started, ids, stamped, _q = _drive(monkeypatch, [LOCKED] * len(pattern), answers={LOCKED: answers})
    assert started == ids
    assert (summary["skipped_for_host_423"], summary["host_423_skips"], stamped) == (0, {}, [])
    assert summary["host_423_stamped"] == 0


def test_an_exception_neither_counts_nor_breaks_the_streak(monkeypatch) -> None:
    # A row that raises says nothing about the host: the 423s either side of it still add up to three.
    seq: List[Any] = [_http(LOCKED, 423), _http(LOCKED, 423), RuntimeError("boom"), _http(LOCKED, 423)]
    summary, started, ids, stamped, _q = _drive(monkeypatch, [LOCKED] * 6, answers={LOCKED: seq})
    assert started == ids[:4]
    assert (summary["skipped_for_host_423"], stamped) == (2, ids[4:])
    assert summary["failed"] == 1


def test_the_trip_is_per_run(monkeypatch) -> None:
    first, started1, ids, stamped1, _q = _drive(
        monkeypatch, [LOCKED] * 6, answers={LOCKED: [_http(LOCKED, 423)] * 6},
    )
    assert started1 == ids[:3] and first["skipped_for_host_423"] == 3
    # The next run asks again: a host that now answers is read in full...
    second, started2, ids2, stamped2, _q = _drive(monkeypatch, [LOCKED] * 6)
    assert started2 == ids2 and second["skipped_for_host_423"] == 0 and stamped2 == []
    # ...and one still locked needs three NEW 423s, not one.
    third, started3, ids3, _st, _q = _drive(
        monkeypatch, [LOCKED] * 6, answers={LOCKED: [_http(LOCKED, 423)] * 6},
    )
    assert started3 == ids3[:3] and third["skipped_for_host_423"] == 3


@pytest.mark.parametrize("concurrency", [1, 4])
def test_other_hosts_are_untouched(monkeypatch, concurrency) -> None:
    hosts = [LOCKED, OTHER] * 6
    summary, started, ids, stamped, _q = _drive(
        monkeypatch, hosts, answers={LOCKED: [_http(LOCKED, 423)] * 6}, concurrency=concurrency,
    )
    other_ids = [i for i, h in zip(ids, hosts) if h == OTHER]
    locked_ids = [i for i, h in zip(ids, hosts) if h == LOCKED]
    assert set(other_ids) <= set(started), "every row of the healthy host is read"
    assert [i for i in started if i in locked_ids] == locked_ids[:3]
    assert summary["host_423_skips"] == {LOCKED: 3} and stamped == locked_ids[3:]
    assert summary["refreshed"] == len(other_ids)


def test_the_trip_reads_its_env_and_zero_is_never_off(monkeypatch) -> None:
    summary, started, ids, _st, _q = _drive(
        monkeypatch, [LOCKED] * 8, answers={LOCKED: [_http(LOCKED, 423)] * 8}, locked_trip="5",
    )
    assert started == ids[:5] and summary["host_423_trip"] == 5
    summary, started, ids, _st, _q = _drive(
        monkeypatch, [LOCKED] * 8, answers={LOCKED: [_http(LOCKED, 423)] * 8}, locked_trip="0",
    )
    assert started == ids[:3] and summary["host_423_trip"] == 3


def test_the_other_breakers_skips_are_still_unstamped(monkeypatch) -> None:
    summary, started, ids, stamped, _q = _drive(
        monkeypatch, [OTHER] * 8, answers={OTHER: [_no_answer(OTHER)] * 8},
    )
    assert summary["skipped_for_unreachable_host"] == 3
    assert stamped == [] and summary["host_423_stamped"] == 0


def test_a_failed_stamp_costs_only_the_rotation(monkeypatch, caplog) -> None:
    summary, started, ids, stamped, _q = _drive(
        monkeypatch, [LOCKED] * 6, answers={LOCKED: [_http(LOCKED, 423)] * 6}, stamp_raises=True,
    )
    assert summary["skipped_for_host_423"] == 3 and summary["host_423_stamped"] == 0
    assert summary["status"] in ("success", "degraded")
    assert any("could not stamp 3 rows" in r.getMessage() for r in caplog.records)


def test_the_stamp_is_chunked_and_writes_only_the_attempt_clock(monkeypatch) -> None:
    calls: List[tuple] = []

    class _Db:
        async def execute(self, query, params):
            calls.append((query, dict(params)))

    monkeypatch.setattr(err, "database", _Db())
    ids = [f"eps_{i}" for i in range(1_201)] + ["", None]
    sent = asyncio.run(err._stamp_crawl_attempts(ids))
    assert sent == 1_201
    assert [len(p) for _q, p in calls] == [500, 500, 201]
    assert [v for _q, p in calls for v in p.values()] == ids[:1_201]
    for query, _p in calls:
        assert query.startswith("UPDATE external_product_seeds SET last_crawl_attempt_at = NOW() WHERE id IN (:id0")
        assert "last_crawled_at" not in query and "updated_at" not in query


def test_the_counters_reach_the_summary_line_and_price_refresh(monkeypatch) -> None:
    summary, *_ = _drive(monkeypatch, [LOCKED] * 5, answers={LOCKED: [_http(LOCKED, 423)] * 5})
    json.dumps(summary)
    line = job.price_refresh_line(summary)
    assert (line["skipped_for_host_423"], line["host_423_stamped"]) == (2, 2)
    # Skipped rows leave the yield's denominator, like every other host skip.
    assert summary["attempted_count"] == 3


# ---- controller review of be#2716 (2026-11-06): the reviewer's probes, ported ----------------------------


def test_two_hosts_each_below_the_trip_never_trip(monkeypatch) -> None:
    # Interleaved, each host answers 423 twice then reads: neither has three in a row (a streak counted
    # across hosts would trip on the third 423 of the run).
    hosts = [LOCKED, OTHER, LOCKED, OTHER, LOCKED, OTHER]
    summary, started, ids, stamped, _q = _drive(
        monkeypatch, hosts,
        answers={LOCKED: [_http(LOCKED, 423)] * 2 + [_ok()], OTHER: [_http(OTHER, 423)] * 2 + [_ok()]},
    )
    assert started == ids and summary["host_423_skips"] == {} and stamped == []


class _NotedOnce(dict):
    """A refresh result that, when the batch first reads it, records the answer in the pacing layer as
    the fetch would have (`crawl_politeness.note_response`)."""

    def __init__(self, base: Dict[str, Any], status: int, headers: Dict[str, str]) -> None:
        super().__init__(base)
        self._note = (status, headers)
        self._seen = False

    def get(self, key, default=None):
        if not self._seen:
            self._seen = True
            cp.note_response(f"https://{LOCKED}/products/x", self._note[0], headers=self._note[1])
        return super().get(key, default)


def test_a_423_challenge_is_left_to_the_challenge_breaker_and_not_stamped(monkeypatch) -> None:
    seq = [_NotedOnce(_http(LOCKED, 423), 423, {"cf-mitigated": "challenge"}) for _ in range(8)]
    summary, started, ids, stamped, _q = _drive(monkeypatch, [LOCKED] * 8, answers={LOCKED: seq})
    assert started == ids[:3]
    assert summary["skipped_for_host_challenge"] == 5
    assert summary["skipped_for_host_423"] == 0 and stamped == []


def test_a_host_the_429_breaker_holds_is_left_to_it_and_not_stamped(monkeypatch) -> None:
    # The pacing layer saw a 429 streak (e.g. robots.txt) on the same rows that answered 423: both trip on
    # the third row, and the 429 breaker, checked first, keeps the host. Its skips stay unstamped.
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_HOST_BLOCK_TRIP", "3")
    seq = [_NotedOnce(_http(LOCKED, 423), 429, {}) for _ in range(8)]
    summary, started, ids, stamped, _q = _drive(monkeypatch, [LOCKED] * 8, answers={LOCKED: seq})
    assert started == ids[:3]
    assert summary["skipped_for_host_backoff"] == 5
    assert summary["skipped_for_host_423"] == 0 and stamped == []


@pytest.mark.parametrize(
    "between,unreachable_skips",
    [
        (_timeout, 0),  # a timeout says nothing about the lock (and a 423 resets the no-answer streak)
        (_paced, 0),  # nor does a pacer or robots refusal
        (lambda host: _cached(), 0),  # nor a cached snapshot served as a success
    ],
)
def test_a_non_answer_between_423s_leaves_the_streak_alone(monkeypatch, between, unreachable_skips) -> None:
    seq = [_http(LOCKED, 423), between(LOCKED)] * 6
    summary, started, ids, stamped, _q = _drive(monkeypatch, [LOCKED] * 12, answers={LOCKED: seq})
    assert started == ids[:5], "423, x, 423, x, 423: three 423s in a row as far as the lock is concerned"
    assert (summary["skipped_for_host_423"], stamped) == (7, ids[5:])
    assert summary["skipped_for_unreachable_host"] == unreachable_skips


def test_an_unprocessable_seed_leaves_the_streak_alone(monkeypatch) -> None:
    seq: List[Any] = [
        _http(LOCKED, 423), _http(LOCKED, 423), HTTPException(status_code=404, detail="SEED_NOT_FOUND"),
        _http(LOCKED, 423),
    ]
    summary, started, ids, stamped, _q = _drive(monkeypatch, [LOCKED] * 6, answers={LOCKED: seq})
    assert summary["unprocessable"] == 1
    assert started == ids[:4] and (summary["skipped_for_host_423"], stamped) == (2, ids[4:])


def test_the_streak_does_not_carry_into_the_next_run(monkeypatch) -> None:
    # Two 423s end one run; the next run's single 423 must not make three.
    _drive(monkeypatch, [LOCKED] * 2, answers={LOCKED: [_http(LOCKED, 423)] * 2})
    summary, started, ids, stamped, _q = _drive(
        monkeypatch, [LOCKED] * 4, answers={LOCKED: [_http(LOCKED, 423)] + [_ok()] * 3},
    )
    assert started == ids and summary["skipped_for_host_423"] == 0 and stamped == []


def test_a_challenge_answer_between_423s_breaks_the_streak(monkeypatch) -> None:
    challenged = {"status": "degraded", "error": "destination_unavailable: cloudflare challenge page", "domain": LOCKED}
    assert err._degraded_reason_bucket(challenged["error"]) == "bot_challenge"
    seq = [_http(LOCKED, 423), _http(LOCKED, 423), challenged] * 3
    summary, started, ids, stamped, _q = _drive(monkeypatch, [LOCKED] * 9, answers={LOCKED: seq})
    assert started == ids and summary["skipped_for_host_423"] == 0 and stamped == []
