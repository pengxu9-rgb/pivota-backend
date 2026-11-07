"""The unconfirmed-dead hide (EXTERNAL_SEED_DEAD_UNCONFIRMED_HIDE): a served card on a host whose catalogue cannot
corroborate a 404 is hidden, reversibly, when its link answered 404/410 on two observations at least 24h apart with
no read since; the refresh batch applies it per host after the run, behind a host brake.

The pure rule, the writer (through a stateful fake that applies its UPDATE back to the row, so nights can be
replayed), the apply step and the batch. The real SQL runs in tests/test_external_seed_destination_liveness_postgres.py.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from services import destination_dead_suppression
from services import external_seed_destination_liveness as liveness

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
REPO = pathlib.Path(__file__).resolve().parents[1]
REASON = liveness.UNCONFIRMED_DEAD_REASON

HARD_404 = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, "http_404")
HARD_410 = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 410, None, "http_410")
LIVE = liveness.DestinationObservation(liveness.VERDICT_LIVE, 200, None)
OFF_PRODUCT = liveness.DestinationObservation(liveness.VERDICT_REDIRECTED_OFF_PRODUCT, 200, None, "landed_off_site")
CHALLENGE = liveness.DestinationObservation(liveness.VERDICT_UNVERIFIABLE, 403, None, "bot_challenge")
CORROBORATED_404 = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, "http_404", corroborated=True)


def _run(coro):
    return asyncio.run(coro)


def _row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "destination_checked_at": None, "destination_verdict": None, "destination_http_status": None,
        "destination_failure_streak": 0, "destination_corroborated_dead_at": None, "status": "active",
        "destination_dead_404_since": None, "last_crawled_at": None,
    }
    row.update(overrides)
    return row


class _SeedDb:
    """One seed row. The writer's UPDATE is applied back to it; every statement is kept."""

    def __init__(self, row: Dict[str, Any]) -> None:
        self.row = row
        self.statements: List[Tuple[str, Dict[str, Any]]] = []

    async def fetch_one(self, query, params=None):
        self.statements.append((query, dict(params or {})))
        return dict(self.row) if self.row is not None else None

    async def fetch_all(self, query, params=None):
        self.statements.append((query, dict(params or {})))
        return []

    async def execute(self, query, params=None):
        p = dict(params or {})
        self.statements.append((query, p))
        if "UPDATE external_product_seeds" not in query:
            return
        if p.get("reached_origin"):
            self.row["destination_verdict"] = p["verdict"]
            self.row["destination_http_status"] = p["http_status"]
            self.row["destination_checked_at"] = p["checked_at"]
        self.row["destination_failure_streak"] = p["streak"]
        self.row["destination_corroborated_dead_at"] = p["corroborated_dead_at"]
        self.row["destination_dead_404_since"] = p["dead_404_since"]


class _Suppression:
    """Records what the pending machinery was asked to do."""

    def __init__(self, monkeypatch, *, fail_for: Tuple[str, ...] = (), already: Tuple[str, ...] = (),
                 log: Optional[List[str]] = None) -> None:
        self.suppressed: List[Tuple[List[str], str]] = []
        self.lifted: List[Tuple[List[str], str]] = []
        self.fail_for = fail_for
        self.log = log if log is not None else []

        async def suppress_pending(keys, *, reason, stamp, db=None):
            keys = list(keys)
            self.log.append("suppress")
            if any(k in self.fail_for for k in keys):
                raise RuntimeError("write failed")
            self.suppressed.append((keys, reason))
            moved = [k for k in keys if k not in already]
            return {"products": moved, "offers": [f"offer::{k}" for k in moved]}

        async def lift_pending(keys, *, reason, db=None):
            self.lifted.append((list(keys), reason))
            return {"products": list(keys), "offers": []}

        async def mirror_keys(seed_id):
            return [f"prod::{seed_id}"]

        monkeypatch.setattr(destination_dead_suppression, "suppress_pending", suppress_pending)
        monkeypatch.setattr(destination_dead_suppression, "lift_pending", lift_pending)
        monkeypatch.setattr(liveness, "_mirror_product_keys", mirror_keys)


def _observe(monkeypatch, db: _SeedDb, observation, at: datetime, mode: str = "shadow", seed_id: str = "eps_1"):
    monkeypatch.setattr(liveness, "database", db)
    if mode == "off":
        monkeypatch.delenv(liveness.UNCONFIRMED_HIDE_ENV, raising=False)
    else:
        monkeypatch.setenv(liveness.UNCONFIRMED_HIDE_ENV, mode)
    return _run(liveness.record_destination_observation(seed_id, observation, now=at))


# --------------------------------------------------------------------------- the flag


@pytest.mark.parametrize(
    "raw,mode",
    [(None, "off"), ("", "off"), ("off", "off"), ("shadow", "shadow"), (" ON ", "on"), ("1", "off"), ("true", "off")],
)
def test_the_flag_reads_off_unless_it_names_a_mode(monkeypatch, raw: Optional[str], mode: str) -> None:
    if raw is None:
        monkeypatch.delenv(liveness.UNCONFIRMED_HIDE_ENV, raising=False)
    else:
        monkeypatch.setenv(liveness.UNCONFIRMED_HIDE_ENV, raw)
    assert liveness.unconfirmed_hide_mode() == mode


# --------------------------------------------------------------------------- the run clock (pure)


def test_a_hard_404_is_a_404_or_410_recorded_dead() -> None:
    assert liveness.is_hard_404(HARD_404) and liveness.is_hard_404(HARD_410)
    assert not liveness.is_hard_404(OFF_PRODUCT)
    assert not liveness.is_hard_404(CHALLENGE)
    assert not liveness.is_hard_404(LIVE)
    assert not liveness.is_hard_404(liveness.DestinationObservation(liveness.VERDICT_DEAD_404, None, None))


def test_the_first_hard_404_starts_the_run_now() -> None:
    assert liveness.next_dead_404_since(_row(), HARD_404, NOW) == NOW


def test_a_later_hard_404_keeps_the_run_start() -> None:
    start = NOW - timedelta(hours=30)
    row = _row(destination_verdict="dead_404", destination_http_status=404,
               destination_checked_at=NOW - timedelta(hours=2), destination_dead_404_since=start)
    assert liveness.next_dead_404_since(row, HARD_410, NOW) == start


@pytest.mark.parametrize("observation", [LIVE, OFF_PRODUCT], ids=["live", "redirected_off_product"])
def test_any_other_answer_from_the_origin_clears_the_run(observation) -> None:
    row = _row(destination_verdict="dead_404", destination_http_status=404, destination_dead_404_since=NOW)
    assert liveness.next_dead_404_since(row, observation, NOW + timedelta(hours=1)) is None


@pytest.mark.parametrize(
    "verdict,status",
    [("live", 200), ("redirected_off_product", 200), ("redirected_to_product", 200), ("dead_404", None)],
    ids=["live", "off_product", "to_product", "dead_without_a_status"],
)
def test_a_stored_start_under_a_verdict_that_is_not_a_hard_404_is_not_a_run(verdict: str, status: Any) -> None:
    """Review P2: a writer on an older image answers live without clearing the column. 404 -> live -> 404 must
    start a new run, not continue the old one (the ping-pong the rule excludes)."""
    t0 = NOW - timedelta(hours=25)
    row = _row(destination_verdict=verdict, destination_http_status=status,
               destination_checked_at=NOW - timedelta(hours=2), destination_dead_404_since=t0)
    since = liveness.next_dead_404_since(row, HARD_404, NOW)
    assert since == NOW
    assert liveness.unconfirmed_hide_candidate(row, HARD_404, NOW, since) is False


def test_an_unverifiable_observation_moves_nothing() -> None:
    start = NOW - timedelta(hours=30)
    assert liveness.next_dead_404_since(_row(destination_dead_404_since=start), CHALLENGE, NOW) == start
    assert liveness.next_dead_404_since(_row(), CHALLENGE, NOW) is None


@pytest.mark.parametrize("status", [404, 410])
def test_a_run_that_predates_the_column_starts_at_the_last_check(status: int) -> None:
    checked = NOW - timedelta(hours=26)
    row = _row(destination_verdict="dead_404", destination_http_status=status, destination_checked_at=checked)
    assert liveness.next_dead_404_since(row, HARD_404, NOW) == checked


@pytest.mark.parametrize(
    "row",
    [
        _row(destination_verdict="dead_404", destination_http_status=None, destination_checked_at=NOW - timedelta(days=2)),
        _row(destination_verdict="redirected_off_product", destination_http_status=200,
             destination_checked_at=NOW - timedelta(days=2)),
        _row(destination_verdict="live", destination_http_status=200, destination_checked_at=NOW - timedelta(days=2)),
        _row(destination_verdict="dead_404", destination_http_status=404, destination_checked_at=None),
    ],
    ids=["dead_without_a_status", "off_product", "live", "no_check"],
)
def test_no_legacy_start_without_a_stored_hard_404(row: Dict[str, Any]) -> None:
    assert liveness.next_dead_404_since(row, HARD_404, NOW) == NOW


# --------------------------------------------------------------------------- the candidate rule (pure)


def _candidate(observation=HARD_404, since: Any = NOW - timedelta(hours=24), **row: Any) -> bool:
    return liveness.unconfirmed_hide_candidate(_row(**row), observation, NOW, since)


def test_a_second_hard_404_a_day_into_the_run_is_a_candidate() -> None:
    assert _candidate() is True
    assert _candidate(HARD_410) is True
    assert _candidate(since=NOW - timedelta(days=9)) is True


def test_under_a_day_into_the_run_is_not() -> None:
    assert _candidate(since=NOW - timedelta(hours=23, minutes=59)) is False
    assert _candidate(since=NOW) is False


def test_a_read_since_the_run_began_is_not() -> None:
    start = NOW - timedelta(hours=30)
    assert _candidate(since=start, last_crawled_at=start + timedelta(hours=1)) is False
    assert _candidate(since=start, last_crawled_at=start) is False
    assert _candidate(since=start, last_crawled_at=start - timedelta(seconds=1)) is True


@pytest.mark.parametrize(
    "observation",
    [OFF_PRODUCT, CHALLENGE, LIVE, CORROBORATED_404],
    ids=["redirected_off_product", "challenge", "live", "corroborated"],
)
def test_only_an_uncorroborated_hard_404_is_a_candidate(observation) -> None:
    assert _candidate(observation) is False


def test_no_run_start_is_no_candidate() -> None:
    assert _candidate(since=None) is False


def test_naive_and_text_times_compare_as_utc() -> None:
    assert _candidate(since=(NOW - timedelta(hours=25)).replace(tzinfo=None)) is True
    assert _candidate(since=(NOW - timedelta(hours=25)).isoformat()) is True


# --------------------------------------------------------------------------- the writer


def test_the_clock_survives_a_refresh_that_re_stamps_the_check(monkeypatch) -> None:
    """The reason for the column: each 404 re-stamps destination_checked_at, so a gap measured from it would never
    reach 24h on a row retried twice a day. Measured from the run's start it does."""
    supp = _Suppression(monkeypatch)
    db = _SeedDb(_row())
    day0 = NOW - timedelta(hours=36)
    first = _observe(monkeypatch, db, HARD_404, day0)
    second = _observe(monkeypatch, db, HARD_404, day0 + timedelta(hours=12))
    third = _observe(monkeypatch, db, HARD_404, day0 + timedelta(hours=24))
    assert "unconfirmed_hide_candidate" not in first and "unconfirmed_hide_candidate" not in second
    assert third["unconfirmed_hide_candidate"] is True
    assert third["dead_404_since"] == day0.isoformat()
    assert db.row["destination_dead_404_since"] == day0
    assert db.row["destination_checked_at"] == day0 + timedelta(hours=24)
    assert supp.suppressed == [], "the writer decides; it never hides"


def test_the_writer_never_hides_even_with_the_flag_on(monkeypatch) -> None:
    supp = _Suppression(monkeypatch)
    db = _SeedDb(_row(destination_verdict="dead_404", destination_http_status=404,
                      destination_checked_at=NOW - timedelta(hours=2),
                      destination_dead_404_since=NOW - timedelta(days=3)))
    result = _observe(monkeypatch, db, HARD_404, NOW, mode="on")
    assert result["unconfirmed_hide_candidate"] is True
    assert supp.suppressed == []
    assert not any("catalog_products" in sql and "UPDATE" in sql for sql, _ in db.statements)


def test_off_decides_nothing_but_still_keeps_the_clock(monkeypatch) -> None:
    _Suppression(monkeypatch)
    db = _SeedDb(_row(destination_verdict="dead_404", destination_http_status=404,
                      destination_checked_at=NOW - timedelta(hours=2),
                      destination_dead_404_since=NOW - timedelta(days=3)))
    result = _observe(monkeypatch, db, HARD_404, NOW, mode="off")
    assert "unconfirmed_hide_candidate" not in result and "dead_404_since" not in result
    # Kept with the flag off, so a later flip has the real run start rather than starting the clock then.
    assert db.row["destination_dead_404_since"] == NOW - timedelta(days=3)


def test_a_live_answer_clears_the_clock_and_lifts_both_reasons(monkeypatch) -> None:
    supp = _Suppression(monkeypatch)
    db = _SeedDb(_row(destination_verdict="dead_404", destination_http_status=404,
                      destination_checked_at=NOW - timedelta(hours=2),
                      destination_dead_404_since=NOW - timedelta(days=3)))
    result = _observe(monkeypatch, db, LIVE, NOW, mode="off")
    assert db.row["destination_dead_404_since"] is None
    assert supp.lifted == [(["prod::eps_1"], liveness.PENDING_SUPPRESSION_REASON), (["prod::eps_1"], REASON)]
    assert result["unconfirmed_lifted"] == 1


def test_the_ping_pong_through_an_old_writer_is_not_a_candidate(monkeypatch) -> None:
    """404 at T0, an old-image writer answers live WITHOUT clearing the column, 404 at T0+25h: not a candidate."""
    _Suppression(monkeypatch)
    db = _SeedDb(_row())
    t0 = NOW - timedelta(hours=25)
    _observe(monkeypatch, db, HARD_404, t0)
    db.row.update(destination_verdict="live", destination_http_status=200,
                  destination_checked_at=t0 + timedelta(hours=10))  # the old writer: since left at t0
    assert db.row["destination_dead_404_since"] == t0
    result = _observe(monkeypatch, db, HARD_404, NOW)
    assert "unconfirmed_hide_candidate" not in result
    assert db.row["destination_dead_404_since"] == NOW


@pytest.mark.parametrize("observation", [OFF_PRODUCT, HARD_404, CORROBORATED_404, CHALLENGE],
                         ids=["redirected_off_product", "hard_404", "corroborated_404", "challenge"])
def test_no_answer_that_is_not_alive_lifts_the_hide(monkeypatch, observation) -> None:
    supp = _Suppression(monkeypatch)
    db = _SeedDb(_row(destination_verdict="dead_404", destination_http_status=404,
                      destination_checked_at=NOW - timedelta(hours=2),
                      destination_dead_404_since=NOW - timedelta(days=3)))
    result = _observe(monkeypatch, db, observation, NOW, mode="on")
    assert supp.lifted == []
    assert "unconfirmed_lifted" not in result


def test_a_handleless_page_read_with_the_shape_rule_on_lifts_the_hide(monkeypatch) -> None:
    """Design R1: a hidden HANDLE-LESS row must be able to lift. Its 200 is `unverifiable` (no handle) unless the
    shape rule (EXTERNAL_SEED_HANDLELESS_READ=on) reads it; then the observation is live and lifts this reason."""
    from tests.test_external_seed_handleless_read import _decide

    off = _decide(mode="shadow")
    assert off.observation is not None and off.observation.verdict == liveness.VERDICT_UNVERIFIABLE
    on = _decide(mode="on")
    assert on.read is True and on.observation.verdict == liveness.VERDICT_LIVE

    stored = dict(destination_verdict="dead_404", destination_http_status=404,
                  destination_checked_at=NOW - timedelta(hours=2), destination_dead_404_since=NOW - timedelta(days=3))
    supp = _Suppression(monkeypatch)
    _observe(monkeypatch, _SeedDb(_row(**stored)), off.observation, NOW, mode="on")
    assert supp.lifted == [], "without the shape rule a handle-less 200 says nothing and lifts nothing"
    db = _SeedDb(_row(**stored))
    result = _observe(monkeypatch, db, on.observation, NOW, mode="on")
    assert (["prod::eps_1"], REASON) in supp.lifted and result["unconfirmed_lifted"] == 1
    assert db.row["destination_dead_404_since"] is None


def test_a_challenge_between_two_404s_neither_breaks_the_run_nor_lifts(monkeypatch) -> None:
    supp = _Suppression(monkeypatch)
    db = _SeedDb(_row())
    start = NOW - timedelta(hours=30)
    _observe(monkeypatch, db, HARD_404, start)
    _observe(monkeypatch, db, CHALLENGE, start + timedelta(hours=10))
    result = _observe(monkeypatch, db, HARD_404, NOW)
    assert result["unconfirmed_hide_candidate"] is True
    assert supp.lifted == []


def test_redirected_off_product_breaks_the_run(monkeypatch) -> None:
    _Suppression(monkeypatch)
    db = _SeedDb(_row())
    start = NOW - timedelta(hours=40)
    _observe(monkeypatch, db, HARD_404, start)
    _observe(monkeypatch, db, OFF_PRODUCT, start + timedelta(hours=10))
    result = _observe(monkeypatch, db, HARD_404, NOW)
    assert "unconfirmed_hide_candidate" not in result
    assert db.row["destination_dead_404_since"] == NOW


def test_a_legacy_dead_row_is_a_candidate_from_its_last_check(monkeypatch) -> None:
    """A row that sat at dead_404 before the column existed: its last check was a 404 of this run."""
    _Suppression(monkeypatch)
    db = _SeedDb(_row(destination_verdict="dead_404", destination_http_status=404,
                      destination_checked_at=NOW - timedelta(hours=25)))
    result = _observe(monkeypatch, db, HARD_404, NOW)
    assert result["unconfirmed_hide_candidate"] is True
    assert db.row["destination_dead_404_since"] == NOW - timedelta(hours=25)


def test_the_writer_reads_and_writes_the_column(monkeypatch) -> None:
    _Suppression(monkeypatch)
    db = _SeedDb(_row())
    _observe(monkeypatch, db, HARD_404, NOW)
    select = " ".join(db.statements[0][0].split())
    assert "destination_dead_404_since" in select and "last_crawled_at" in select
    assert "destination_http_status" in select
    update = next(" ".join(sql.split()) for sql, _ in db.statements if "UPDATE external_product_seeds" in sql)
    assert "destination_dead_404_since = :dead_404_since" in update


# --------------------------------------------------------------------------- the host brake


@pytest.mark.parametrize(
    "reached,dead,braked",
    [(4, 4, False), (5, 2, False), (5, 3, True), (6, 3, False), (6, 4, True), (100, 50, False), (100, 51, True),
     (0, 0, False)],
)
def test_the_brake(reached: int, dead: int, braked: bool) -> None:
    assert liveness.unconfirmed_host_braked({"reached_origin": reached, "dead_404": dead}) is braked


# --------------------------------------------------------------------------- counting one row


def _dr(verdict="dead_404", status=404, checked=True, candidate=False, since=None, lifted=0) -> Dict[str, Any]:
    out: Dict[str, Any] = {"verdict": verdict, "http_status": status,
                           "checked_at": NOW.isoformat() if checked else None, "unconfirmed_lifted": lifted}
    if candidate:
        out.update(unconfirmed_hide_candidate=True, dead_404_since=since or (NOW - timedelta(days=2)).isoformat())
    return out


def test_counting_one_rows_observation() -> None:
    by_host: Dict[str, Dict[str, Any]] = {}
    liveness.count_unconfirmed_observation(by_host, "shop.example", "a", _dr(candidate=True, since="S"))
    liveness.count_unconfirmed_observation(by_host, "shop.example", "b", _dr(verdict="live", status=200, lifted=1))
    liveness.count_unconfirmed_observation(by_host, "shop.example", "c", _dr(verdict="unverifiable", status=403, checked=False))
    liveness.count_unconfirmed_observation(by_host, "shop.example", "d", {"status": "not_observed"})
    liveness.count_unconfirmed_observation(by_host, "shop.example", "e", None)
    liveness.count_unconfirmed_observation(by_host, "", "f", _dr(verdict="redirected_off_product", status=200))
    assert by_host["shop.example"] == {"observed": 3, "reached_origin": 2, "dead_404": 1, "lifted": 1,
                                       "candidates": [("a", "S")]}
    assert by_host["unknown"]["dead_404"] == 0 and by_host["unknown"]["reached_origin"] == 1


# --------------------------------------------------------------------------- the apply step


def _host(candidates: List[Tuple[str, str]], reached: int = 10, dead: Optional[int] = None) -> Dict[str, Any]:
    return {"observed": reached, "reached_origin": reached, "dead_404": len(candidates) if dead is None else dead,
            "lifted": 0, "candidates": candidates}


class _RecheckDb:
    """Answers the apply step's re-check per seed id, and shadow's would-hide count (`unsuppressed` mirrors each)."""

    def __init__(self, rows: Dict[str, Optional[Dict[str, Any]]], *, unsuppressed: int = 1,
                 log: Optional[List[str]] = None) -> None:
        self.rows = rows
        self.queries: List[str] = []
        self.unsuppressed = unsuppressed
        self.log = log if log is not None else []

    def transaction(self):
        db = self

        class _Tx:
            async def __aenter__(self):
                db.log.append("begin")

            async def __aexit__(self, exc_type, exc, tb):
                db.log.append("rollback" if exc_type else "commit")
                return False

        return _Tx()

    async def fetch_one(self, query, params=None):
        self.queries.append(query)
        if "COUNT(*)" in query:
            return {"n": self.unsuppressed}
        self.log.append("recheck")
        return self.rows.get((params or {}).get("id"))

    async def fetch_all(self, query, params=None):
        return []

    async def execute(self, query, params=None):
        raise AssertionError("the apply step writes only through the pending machinery")


SINCE = (NOW - timedelta(days=2)).isoformat()


def _still(**overrides: Any) -> Dict[str, Any]:
    row = {"status": "active", "destination_verdict": "dead_404", "destination_http_status": 404,
           "destination_dead_404_since": NOW - timedelta(days=2), "last_crawled_at": None}
    row.update(overrides)
    return row


def _apply(monkeypatch, by_host, mode, rows=None, fail_for=(), already=(), unsuppressed=1):
    log: List[str] = []
    supp = _Suppression(monkeypatch, fail_for=fail_for, already=already, log=log)
    db = _RecheckDb(rows if rows is not None else {}, unsuppressed=unsuppressed, log=log)
    monkeypatch.setattr(liveness, "database", db)
    summary = _run(liveness.apply_unconfirmed_hides(by_host, mode=mode, now=NOW))
    return summary, supp, db


def test_shadow_writes_nothing_and_reports_what_on_would_hide(monkeypatch) -> None:
    by_host = {"a.example": _host([("s1", SINCE), ("s2", SINCE)]), "b.example": _host([], reached=3, dead=0)}
    summary, supp, db = _apply(monkeypatch, by_host, "shadow", rows={"s1": _still(), "s2": _still()})
    assert supp.suppressed == []
    assert all("COUNT(*)" in q and "FOR UPDATE" not in q for q in db.queries) and len(db.queries) == 2
    assert "begin" not in db.log
    assert summary["mode"] == "shadow"
    assert summary["totals"]["would_hide"] == 2 and "hidden_rows" not in summary["totals"]
    assert summary["top_hosts"]["a.example"] == {"observed": 10, "reached_origin": 10, "dead_404": 2,
                                                 "candidates": 2, "would_hide": 2, "would_hide_products": 2}


def test_shadow_counts_only_mirror_products_still_unsuppressed(monkeypatch) -> None:
    summary, _, _ = _apply(monkeypatch, {"a.example": _host([("s1", SINCE)])}, "shadow", unsuppressed=0)
    assert summary["totals"]["would_hide"] == 1 and "would_hide_products" not in summary["totals"]


def test_on_hides_each_unbraked_candidate_under_its_own_reason(monkeypatch) -> None:
    by_host = {"a.example": _host([("s1", SINCE), ("s2", SINCE)])}
    summary, supp, _ = _apply(monkeypatch, by_host, "on", rows={"s1": _still(), "s2": _still()})
    assert supp.suppressed == [(["prod::s1"], REASON), (["prod::s2"], REASON)]
    totals = summary["totals"]
    assert (totals["hidden_rows"], totals["hidden_products"], totals["hidden_offers"]) == (2, 2, 2)


def test_the_re_check_and_the_hide_share_one_transaction_with_the_row_locked(monkeypatch) -> None:
    summary, _, db = _apply(monkeypatch, {"a.example": _host([("s1", SINCE)])}, "on", rows={"s1": _still()})
    assert db.log == ["begin", "recheck", "suppress", "commit"]
    assert any("FOR UPDATE" in q for q in db.queries)


def test_a_refused_re_check_commits_without_hiding(monkeypatch) -> None:
    _, supp, db = _apply(monkeypatch, {"a.example": _host([("s1", SINCE)])}, "on",
                         rows={"s1": _still(destination_verdict="live")})
    assert db.log == ["begin", "recheck", "commit"] and supp.suppressed == []


def test_a_failed_hide_rolls_back_and_is_counted(monkeypatch) -> None:
    summary, _, db = _apply(monkeypatch, {"a.example": _host([("s1", SINCE)])}, "on", rows={"s1": _still()},
                            fail_for=("prod::s1",))
    assert db.log == ["begin", "recheck", "suppress", "rollback"]
    assert summary["totals"]["errors"] == 1


def test_a_hide_that_moves_nothing_is_not_counted_hidden(monkeypatch) -> None:
    summary, supp, _ = _apply(monkeypatch, {"a.example": _host([("s1", SINCE)])}, "on", rows={"s1": _still()},
                              already=("prod::s1",))
    assert supp.suppressed == [(["prod::s1"], REASON)]
    assert summary["totals"]["nothing_moved"] == 1
    assert "hidden_rows" not in summary["totals"] and "hidden_products" not in summary["totals"]


def test_a_braked_host_hides_nothing(monkeypatch) -> None:
    by_host = {
        "redesign.example": _host([("s1", SINCE), ("s2", SINCE), ("s3", SINCE)], reached=5, dead=4),
        "one-gone.example": _host([("s4", SINCE)], reached=20, dead=1),
    }
    rows = {k: _still() for k in ("s1", "s2", "s3", "s4")}
    summary, supp, _ = _apply(monkeypatch, by_host, "on", rows=rows)
    assert supp.suppressed == [(["prod::s4"], REASON)]
    assert summary["top_hosts"]["redesign.example"]["braked"] == 3
    assert "would_hide" not in summary["top_hosts"]["redesign.example"]
    assert summary["totals"]["braked"] == 3 and summary["totals"]["braked_hosts"] == 1
    assert summary["totals"]["would_hide"] == 1


def test_the_brake_counts_in_shadow_too(monkeypatch) -> None:
    by_host = {"redesign.example": _host([("s1", SINCE)], reached=5, dead=5)}
    summary, supp, _ = _apply(monkeypatch, by_host, "shadow")
    assert summary["totals"] == {"braked": 1, "braked_hosts": 1, "candidates": 1, "dead_404": 5, "observed": 5,
                                 "reached_origin": 5}
    assert supp.suppressed == []


@pytest.mark.parametrize(
    "row",
    [
        None,
        _still(status="inactive"),
        _still(destination_verdict="live"),
        _still(destination_http_status=403),
        _still(destination_dead_404_since=None),
        _still(destination_dead_404_since=NOW - timedelta(hours=1)),
        _still(last_crawled_at=NOW - timedelta(hours=1)),
        _still(last_crawled_at=NOW - timedelta(days=2)),
    ],
    ids=["gone", "inactive", "went_live", "another_status", "run_cleared", "another_run", "read_since", "read_at_start"],
)
def test_a_candidate_that_moved_after_the_decision_is_not_hidden(monkeypatch, row) -> None:
    summary, supp, _ = _apply(monkeypatch, {"a.example": _host([("s1", SINCE)])}, "on", rows={"s1": row})
    assert supp.suppressed == []
    assert summary["totals"]["changed"] == 1


def test_an_unchanged_candidate_with_a_read_before_the_run_is_hidden(monkeypatch) -> None:
    row = _still(last_crawled_at=NOW - timedelta(days=5))
    summary, supp, _ = _apply(monkeypatch, {"a.example": _host([("s1", SINCE)])}, "on", rows={"s1": row})
    assert supp.suppressed == [(["prod::s1"], REASON)]


def test_a_candidate_without_its_run_start_is_not_hidden(monkeypatch) -> None:
    summary, supp, _ = _apply(monkeypatch, {"a.example": _host([("s1", None)])}, "on", rows={"s1": _still()})
    assert supp.suppressed == [] and summary["totals"]["changed"] == 1


def test_one_failing_row_never_costs_the_others_or_the_run(monkeypatch) -> None:
    by_host = {"a.example": _host([("s1", SINCE), ("s2", SINCE)])}
    summary, supp, _ = _apply(monkeypatch, by_host, "on", rows={"s1": _still(), "s2": _still()}, fail_for=("prod::s1",))
    assert supp.suppressed == [(["prod::s2"], REASON)]
    assert summary["totals"]["errors"] == 1 and summary["totals"]["hidden_rows"] == 1


def test_the_summary_lists_at_most_25_hosts_by_candidates_then_404s(monkeypatch) -> None:
    by_host = {f"h{i:02d}.example": _host([(f"s{i}", SINCE)] * (i % 4), reached=10, dead=i) for i in range(40)}
    summary, _, _ = _apply(monkeypatch, by_host, "shadow")
    hosts = list(summary["top_hosts"])
    assert len(hosts) == liveness.UNCONFIRMED_TOP_HOSTS == 25
    keys = [(-summary["top_hosts"][h].get("candidates", 0), -summary["top_hosts"][h].get("dead_404", 0)) for h in hosts]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------- the batch and the job line


def _batch(monkeypatch, mode: str, produced: Dict[str, Dict[str, Any]], hosts: Dict[str, str]):
    import services.external_referral_readiness as err

    if mode == "off":
        monkeypatch.delenv(liveness.UNCONFIRMED_HIDE_ENV, raising=False)
    else:
        monkeypatch.setenv(liveness.UNCONFIRMED_HIDE_ENV, mode)
    ids = list(produced)
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids",
                        lambda *a, **k: asyncio.sleep(0, result=ids))
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts", lambda _ids: asyncio.sleep(0, result=hosts))
    applied: List[Tuple[Dict[str, Any], str]] = []

    async def fake_apply(by_host, *, mode):
        applied.append((by_host, mode))
        return {"mode": mode, "totals": {"observed": sum(h["observed"] for h in by_host.values())}, "top_hosts": {}}

    monkeypatch.setattr(err, "apply_unconfirmed_hides", fake_apply)

    async def scripted_refresh(seed_id, **_kwargs):
        return produced[seed_id]

    summary = asyncio.run(err.run_external_referral_refresh_batch(refresh_seed_by_id=scripted_refresh, limit=10))
    return summary, applied


_PRODUCED = {
    # A 404 comes back degraded, with the writer's result as `destination_refresh`.
    "s404": {"status": "degraded", "error": "destination_unavailable: http 404", "domain": "shop.example",
             "destination_refresh": _dr(candidate=True, since=SINCE)},
    "sread": {"status": "success", "destination_refresh": _dr(verdict="live", status=200, lifted=1)},
}


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_the_batch_counts_every_outcome_and_applies_once_after_the_run(monkeypatch, mode: str) -> None:
    summary, applied = _batch(monkeypatch, mode, _PRODUCED, {"s404": "shop.example", "sread": "shop.example"})
    assert len(applied) == 1
    by_host, applied_mode = applied[0]
    assert applied_mode == mode
    assert by_host["shop.example"] == {"observed": 2, "reached_origin": 2, "dead_404": 1, "lifted": 1,
                                       "candidates": [("s404", SINCE)]}
    assert summary["unconfirmed_hide"]["mode"] == mode


def test_the_batch_with_the_flag_off_decides_and_applies_nothing(monkeypatch) -> None:
    import jobs.external_referral_refresh as job

    summary, applied = _batch(monkeypatch, "off", _PRODUCED, {"s404": "shop.example", "sread": "shop.example"})
    assert applied == []
    assert summary["unconfirmed_hide"] == {"mode": "off", "totals": {}, "top_hosts": {}}
    assert job.unconfirmed_hide_line(summary) is None


def test_the_batch_runs_the_real_apply_step_end_to_end_in_shadow(monkeypatch) -> None:
    import services.external_referral_readiness as err

    monkeypatch.setenv(liveness.UNCONFIRMED_HIDE_ENV, "shadow")
    ids = list(_PRODUCED)
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids",
                        lambda *a, **k: asyncio.sleep(0, result=ids))
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts",
                        lambda _ids: asyncio.sleep(0, result={i: "shop.example" for i in ids}))
    supp = _Suppression(monkeypatch)

    async def scripted_refresh(seed_id, **_kwargs):
        return _PRODUCED[seed_id]

    summary = asyncio.run(err.run_external_referral_refresh_batch(refresh_seed_by_id=scripted_refresh, limit=10))
    block = summary["unconfirmed_hide"]
    assert block["totals"]["would_hide"] == 1 and block["totals"]["lifted"] == 1
    assert supp.suppressed == []


def test_the_job_line_needs_a_mode_and_an_observation() -> None:
    import jobs.external_referral_refresh as job

    block = {"mode": "shadow", "totals": {"observed": 3}, "top_hosts": {}}
    assert job.unconfirmed_hide_line({"unconfirmed_hide": block}) == block
    assert job.unconfirmed_hide_line({"unconfirmed_hide": {"mode": "shadow", "totals": {}, "top_hosts": {}}}) is None
    assert job.unconfirmed_hide_line({"unconfirmed_hide": {"mode": "off", "totals": {"observed": 3}}}) is None
    assert job.unconfirmed_hide_line({}) is None


# --------------------------------------------------------------------------- where it may fire


def test_only_the_refresh_batch_applies_the_hide() -> None:
    """The single-seed refresh route and the sweep have no run context (no host brake): they lift, never hide."""
    out = subprocess.run(["git", "-C", str(REPO), "grep", "-n", r"apply_unconfirmed_hides(", "--", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    callers = {line.split(":", 1)[0] for line in out.splitlines() if not line.split(":", 1)[0].startswith("tests/")}
    assert callers == {"services/external_seed_destination_liveness.py", "services/external_referral_readiness.py"}
    readiness = (REPO / "services/external_referral_readiness.py").read_text()
    assert len(re.findall(r"await apply_unconfirmed_hides\(", readiness)) == 1

