"""jobs/reap_agentic_purchase_poll.py — the poller, on SQLite.

WHAT IS REAL HERE. The same split the state machine's own arm draws, one layer up:

  REAL  the ledger — every claim, release, sweep and transition runs against a real database
        built by the production self-heal. Every assertion about a claim, a schedule or a swept
        row is read back off the row.
  REAL  the state machine — `advance` is the shipped function, not a stub, so "one run drives a
        purchase one step" means the real step really ran.
  REAL  the client's PURE half (allowlists, status vocabularies).
  FAKE  the client's six TRANSPORT functions, scripted per test, with an autouse fixture that
        makes an unpatched `httpx.AsyncClient` raise. NOTHING HERE HAS EVER ADDRESSED A
        reap.global OR prava.space HOST.

THE FIXTURES ARE IMPORTED, NOT COPIED. `FakeReap`, the payload constants and the `_start` helper
come from tests/test_reap_agentic_purchase.py — the same dialect, the same database fixture, the
same faked surface, so a second copy would be 300 lines that can drift. (The Postgres arm does
copy them, and that is the house convention there for a reason the module docstring of
tests/test_reap_agentic_purchase_postgres.py states: the dialect gate must not have its
collection depend on a file it is not collecting.)

EVERY GUARD IN THE JOB WAS MUTATED ONE AT A TIME AND CONFIRMED TO KILL AT LEAST ONE TEST BELOW;
the table is in the PR body.

    .venv/bin/python -m pytest tests/test_reap_agentic_purchase_poll.py
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.reap_agentic_ledger as ledger  # noqa: E402
import services.reap_agentic_client as rc  # noqa: E402
import services.reap_agentic_purchase as svc  # noqa: E402
import jobs.reap_agentic_purchase_poll as job  # noqa: E402
from pivota_log_capture import (  # noqa: E402
    capture_pivota_stdout,
    pivota_lines,
    root_as_in_prod,
)

# The shared fixtures, from the state machine's own SQLite arm. Same directory, same dialect gate.
from test_reap_agentic_purchase import (  # noqa: E402
    ADDRESS,
    CHECKOUT_COMPLETED,
    CONSENT,
    CHECKOUT_CREATED,
    EMAIL,
    ENROLLMENT_ACTIVE,
    ENROLLMENT_CREATED,
    ENROLLMENT_UUID,
    PII_STRINGS,
    QUOTE_200,
    RETURN_URL,
    Attribution,
    FakeReap,
    _ok,
    _resolved,
    _row,
)

pytestmark = pytest.mark.skipif(
    IS_POSTGRES,
    reason=(
        "the SQLite arm of the agentic purchase poller; the Postgres arm is "
        "tests/test_reap_agentic_purchase_poll_postgres.py"
    ),
)


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _db():
    """The tables the way PRODUCTION builds them — the self-heal, not a test-only CREATE TABLE."""
    if not database.is_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    await ensure_required_schema_light()
    yield


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)
    for dial in job.DIALS.values():
        monkeypatch.delenv(dial.env, raising=False)
    # The dial warnings are ONCE PER PROCESS by design (fix F8), so without this every test after
    # the first would be asserting against a warning the previous test already spent.
    job._reset_dial_warnings()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import httpx

    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a test reached the network; the Reap client must be faked")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden)


def _future_iso(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z"
    )


def _with_future_expiry(payload: dict) -> dict:
    """The imported payloads carry a FIXED `expiresAt` (2026-09-17) that is now in the past.

    The state machine's own arm never noticed, because nothing there runs `expire_overdue_purchases`
    — this file does, first thing in every run, and a hosted URL whose expiry has passed is
    exactly what that sweep terminates. So a purchase would reach `needs_enrollment` and be
    expired by the NEXT run, which is correct behaviour and a useless fixture. Re-stamped
    relative to now; the sweep's own tests set the clocks they need explicitly.
    """
    action = dict(payload.get("nextAction") or {})
    action["expiresAt"] = _future_iso()
    return dict(payload, nextAction=action)


@pytest.fixture
def reap(monkeypatch):
    """The six transport calls, scripted. Lifted verbatim from the state machine's arm — the
    class is imported, only the monkeypatching is local (a fixture cannot be imported)."""
    fake = FakeReap()
    fake.create_checkout = _ok(_with_future_expiry(CHECKOUT_CREATED))

    def _enrollment_for(**kwargs):
        # A DISTINCT partner enrollment id per call, AND A REAL UUID.
        #
        # Distinct because `reap_agentic_enrollments` has a unique index on `reap_enrollment_id`,
        # so the shared constant in the imported fixture is fine for the one-purchase tests it
        # was written for and a constraint violation the moment a BATCH of purchases for
        # different buyers enrols in one run — which is the normal case here.
        #
        # A UUID because the state machine's fix round now validates every partner id it stores
        # through `rc._path_id(..., uuid=True)`; our own `attempt_id` (an `re_…` ledger id) is
        # refused, and the purchase fails with `partner_id_malformed` instead of enrolling. uuid5
        # keeps it derived from the attempt id, so it is still unique per enrollment and stable
        # within a run.
        attempt_id = str(kwargs.get("attempt_id") or ENROLLMENT_UUID)
        return _ok(
            dict(
                _with_future_expiry(ENROLLMENT_CREATED),
                id=str(uuid.uuid5(uuid.NAMESPACE_OID, attempt_id)),
            )
        )

    fake.create_enrollment = _enrollment_for
    fake.get_enrollment = lambda **kwargs: _ok(dict(ENROLLMENT_ACTIVE, id=str(kwargs.get("id"))))

    def _install(name):
        async def _call(*args, **kwargs):
            if args:
                kwargs = dict(kwargs, id=args[0])
            fake.calls.append((name, kwargs))
            answer = fake._answer(name, kwargs)
            if inspect.isawaitable(answer):
                answer = await answer
            return answer

        monkeypatch.setattr(rc, name, _call)

    for name in FakeReap.NAMES:
        _install(name)
    return fake


@pytest.fixture(autouse=True)
def attribution(monkeypatch):
    import services.commerce_attribution_service as cas

    recorder = Attribution()
    monkeypatch.setattr(cas, "close_external_order_conversion", recorder)
    return recorder


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


async def _start(**over) -> str:
    kwargs = dict(
        agent_id="agent_one",
        agent_user_ref_hash="hash_alice",
        buyer_ref="bref_alice",
        row=_row(),
        buyer=svc.BuyerContact(email=EMAIL, shipping_address=dict(ADDRESS)),
        quantity=1,
        click_id="click_abc",
        return_url=RETURN_URL,
        # mig 233: REQUIRED on every lane now, not only cart_link. Passed by the helper so the
        # suites that are about something else keep testing that something else; the tests that
        # are about consent override it explicitly.
        consent_version=CONSENT,
    )
    kwargs.update(over)
    return await svc.start_purchase(**kwargs)


async def _get(purchase_id: str):
    return await ledger.get_purchase_internal(purchase_id)


async def _raw(sql: str, params: dict):
    return await database.execute(sql, params)


async def _claimed_by(purchase_id: str):
    row = await _get(purchase_id)
    return (row or {}).get("claimed_by")


async def _all_claims():
    rows = await database.fetch_all(
        "SELECT id, claimed_by FROM reap_agentic_purchases WHERE claimed_by IS NOT NULL", {}
    )
    return {str(r["id"]): r["claimed_by"] for r in rows}


async def _row_count() -> int:
    rows = await database.fetch_all("SELECT id FROM reap_agentic_purchases", {})
    return len(rows)


async def _states():
    rows = await database.fetch_all("SELECT id, state FROM reap_agentic_purchases", {})
    return {str(r["id"]): r["state"] for r in rows}


class _Clock:
    """A settable monotonic clock the TEST advances, standing in for `job._monotonic`.

    NOT a scripted list of return values. The first cut was one, and it broke the moment fix F5
    added a budget check inside `_sweep_until_drained`: the assertions then depended on exactly
    how many times the job happened to read the clock, which is an implementation detail no test
    should be pinned to. A settable clock lets a test say the thing it means -- "time passed
    while this row was at the partner" -- and stay true however many checks there are.
    """

    def __init__(self, monkeypatch, start: float = 0.0):
        self.t = start
        monkeypatch.setattr(job, "_monotonic", lambda: self.t)

    def spend_budget(self) -> None:
        """Jump past any plausible budget."""
        self.t += 9_999.0


async def _run(**kwargs):
    return await job.run_reap_agentic_purchase_poll(**kwargs)


# ══ 1. the gate ═══════════════════════════════════════════════════════════════════════════════


async def test_a_disabled_rail_makes_no_partner_call_but_still_sweeps(monkeypatch, reap):
    """FIX F3, AND IT IS A PII TEST, NOT A FEATURE-FLAG TEST.

    The first cut gated the WHOLE run, so with the rail off an abandoned purchase kept
    `buyer_email` and `shipping_address` for as long as the dial stayed off — measured across
    three consecutive ticks on a row 99,999 seconds old. The dial's job is to stop us TALKING TO
    A PARTNER. It is not permission to stop forgetting people.

    So: step 4 is skipped, no transport function is called, no claim is taken and no attempt is
    spent — and the expire sweep runs anyway."""
    abandoned = await _start(buyer_ref="bref_gone")
    live = await _start(buyer_ref="bref_here")
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'awaiting_approval', "
        "state_entered_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": abandoned},
    )
    before_live = await _get(live)

    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")
    report = await _run()

    assert report.skipped_disabled == 1
    # The gated half did nothing.
    assert report.claimed == 0 and report.advanced == 0 and report.errors == 0
    assert reap.calls == [], "a disarmed run reached the partner"
    # The ungated half did its job.
    assert report.expired == 1
    swept = await _get(abandoned)
    assert swept["state"] == "expired"
    assert swept["buyer_email"] is None and swept["shipping_address"] is None, (
        "the PII deadline did not run with the rail off — this is the F3 regression"
    )
    # And the row that was NOT overdue is untouched, including its attempt counter: a skipped
    # step 4 must not spend a claim.
    after_live = await _get(live)
    assert after_live["state"] == "resolving"
    assert after_live["attempts"] == before_live["attempts"]
    assert after_live["claimed_by"] is None
    assert after_live["buyer_email"] == EMAIL


async def test_a_credential_blip_does_not_stop_the_pii_deadline(monkeypatch, reap):
    """The other half of the gate, and the sharper half. `is_configured()` is both env vars or
    neither, so ONE unset variable disarmed the rail — and in the first cut that also stopped the
    expire sweep. A credential blip must not be able to extend how long we hold a buyer's
    address."""
    abandoned = await _start(buyer_ref="bref_gone")
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'awaiting_approval', "
        "state_entered_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": abandoned},
    )
    monkeypatch.delenv("REAP_API_KEY", raising=False)

    report = await _run()

    assert report.skipped_disabled == 1
    assert report.claimed == 0
    assert reap.calls == []
    assert report.expired == 1
    assert (await _get(abandoned))["buyer_email"] is None


async def test_the_requeue_and_fail_sweeps_also_run_with_the_rail_off(monkeypatch, reap):
    """The remaining two of the three ungated steps, so F3 is pinned for all of them rather than
    for the one that was easiest to write. Neither calls a partner: they are UPDATE statements in
    the ledger, which is exactly why they are safe to run unarmed."""
    dead = await _start(buyer_ref="bref_dead")
    exhausted = await _start(buyer_ref="bref_tired")
    await _raw(
        "UPDATE reap_agentic_purchases SET claimed_by = 'dead_pod', "
        "claimed_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": dead},
    )
    monkeypatch.setenv("REAP_AGENTIC_MAX_ATTEMPTS", "3")
    await _raw(
        "UPDATE reap_agentic_purchases SET attempts = 9 WHERE id = :i", {"i": exhausted}
    )
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")

    report = await _run()

    assert report.skipped_disabled == 1
    assert report.requeued == 1
    assert report.failed_exhausted == 1
    assert report.claimed == 0
    assert reap.calls == []
    failed = await _get(exhausted)
    assert failed["state"] == "failed" and failed["buyer_email"] is None
    assert (await _get(dead))["claimed_by"] is None


async def test_a_processing_row_is_never_swept_while_the_rail_is_off(monkeypatch, reap):
    """The safety argument F3 rests on, asserted rather than reasoned: the two ungated terminal
    sweeps cannot reach a row whose payment is in flight. `expire_overdue_purchases` names only
    the two waiting states, and `fail_exhausted_purchases` runs `include_processing=False` — so
    running them unarmed cannot write 'failed' or 'expired' over a live charge."""
    stuck = await _start(buyer_ref="bref_flight")
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'processing', attempts = 9999, "
        "state_entered_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": stuck},
    )
    monkeypatch.setenv("REAP_AGENTIC_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")

    report = await _run()

    assert report.expired == 0 and report.failed_exhausted == 0
    assert (await _get(stuck))["state"] == "processing"
    # And it is REPORTED, which is fix F6 — see its own test below.
    assert report.processing_over_attempts == 1


@pytest.mark.parametrize("value", ["ture", "", "2", "off", "no"])
async def test_the_gate_is_an_allowlist_not_a_denylist(monkeypatch, reap, value):
    """A typo must not ARM the rail. This is the state machine's own reader — reused, not
    reimplemented — and this test is what proves the poller really consults it."""
    await _start()
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", value)
    report = await _run()
    assert report.skipped_disabled == 1
    assert report.claimed == 0
    assert reap.calls == []


@pytest.mark.parametrize("pivota_env,base,armed", [
    ("staging", "https://prod.api.reap.global", False),
    ("staging", "https://mx.prod.api.reap.global", False),
    ("staging", "https://sg.prod.api.reap.global", False),
    ("staging", "https://attacker.sg.sandbox.api.reap.global", False),
    ("staging", "https://x.sandbox.api.reap.global", False),   # exact host, not a suffix
    ("development", "https://prod.api.reap.global", False),
    ("staging", "https://sandbox.api.reap.global", True),
    ("staging", "https://mx.sandbox.api.reap.global", True),
    ("staging", "https://sg.sandbox.api.reap.global", True),       # the SG demo host
    ("production", "https://prod.api.reap.global", True),       # production is unchanged
])
async def test_outside_production_step_4_runs_only_against_an_exact_sandbox_host(
    monkeypatch, reap, pivota_env, base, armed
):
    """Staging is a RESTORED COPY of production: an armed staging poller pointed at the real rail
    would claim production buyers' rows and enroll (Reap emails the buyer), quote and check out.
    Outside production, step 4 needs an exact sandbox host; the sweeps still run either way."""
    await _start()
    monkeypatch.setenv("PIVOTA_ENV", pivota_env)
    monkeypatch.setenv("REAP_API_BASE_URL", base)
    report = await _run()
    if armed:
        assert report.skipped_disabled == 0
        assert report.claimed == 1
    else:
        assert report.skipped_disabled == 1
        assert report.claimed == 0
        assert reap.calls == []


async def test_the_non_sandbox_refusal_is_one_error_through_the_pivota_logger(monkeypatch, reap):
    await _start()
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://prod.api.reap.global")
    with root_as_in_prod(), capture_pivota_stdout() as buf:
        await _run()
        await _run()
    lines = [ln for ln in pivota_lines(buf) if "not exactly a Reap sandbox host" in ln]
    assert len(lines) == 1, pivota_lines(buf)
    assert " ERROR - " in lines[0]
    assert "sk_test_key" not in buf.getvalue()


# ══ 2. one step, and no claim left behind ═════════════════════════════════════════════════════


async def test_one_run_drives_a_purchase_one_step_and_holds_no_claim(reap):
    purchase_id = await _start()

    report = await _run(worker_id="w1")

    assert report.claimed == 1
    assert report.advanced == 1
    assert report.errors == 0
    assert report.released == 0
    row = await _get(purchase_id)
    assert row["state"] == "needs_enrollment"
    assert row["claimed_by"] is None, "the worker still holds the lease after its own run"
    assert await _all_claims() == {}
    # ONE step, not a loop: the row moved once and the resolve ran once.
    assert len(reap.named("resolve_our_row")) == 1


async def test_a_second_run_continues_it(reap):
    purchase_id = await _start()
    await _run(worker_id="w1")
    assert (await _get(purchase_id))["state"] == "needs_enrollment"

    # `needs_enrollment` is a HUMAN-WAIT state: the state machine scheduled the next poll a full
    # interval out, so a second run finds nothing due until that passes. Bring it forward the way
    # the clock would, without touching the code under test.
    await _raw(
        "UPDATE reap_agentic_purchases SET next_poll_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": purchase_id},
    )
    report = await _run(worker_id="w2")

    assert report.claimed == 1 and report.advanced == 1
    assert (await _get(purchase_id))["state"] == "quoting"
    assert await _all_claims() == {}


async def test_a_human_wait_state_is_not_due_again_immediately(reap):
    """The corollary of the test above, stated on its own: the job does NOT override the state
    machine's schedule. `release_claim` is issued with no `next_poll_at`, so the 30s the machine
    chose for `needs_enrollment` survives the release."""
    purchase_id = await _start()
    await _run(worker_id="w1")
    scheduled = (await _get(purchase_id))["next_poll_at"]
    assert scheduled > datetime.now(timezone.utc) + timedelta(seconds=20)

    second = await _run(worker_id="w1")
    assert second.claimed == 0
    assert (await _get(purchase_id))["next_poll_at"] == scheduled


async def test_the_full_happy_path_reaches_completed_over_n_runs(reap, attribution):
    """resolving -> needs_enrollment -> quoting -> awaiting_approval -> completed, each step a
    separate RUN of the job, with the human-wait schedules brought forward between them."""
    purchase_id = await _start()
    seen = []
    for _ in range(6):
        report = await _run(worker_id="w1")
        state = (await _get(purchase_id))["state"]
        seen.append(state)
        if state in ledger.TERMINAL_STATES:
            break
        assert report.errors == 0
        await _raw(
            "UPDATE reap_agentic_purchases SET next_poll_at = '2020-01-01 00:00:00' "
            "WHERE id = :i",
            {"i": purchase_id},
        )

    assert seen[-1] == "completed", seen
    row = await _get(purchase_id)
    assert row["reap_order_id"] == "ord_991"
    # The terminal write emptied the PII and cleared the claim, in the same UPDATE.
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["claimed_by"] is None
    assert len(attribution.calls) == 1
    assert await _all_claims() == {}


async def test_a_terminal_row_is_never_claimed_so_a_run_over_one_is_empty(reap):
    purchase_id = await _start()
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'completed', "
        "next_poll_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": purchase_id},
    )
    report = await _run(worker_id="w1")
    assert report.claimed == 0 and report.advanced == 0 and report.errors == 0
    assert reap.calls == []


# ══ 3. an exception out of advance ════════════════════════════════════════════════════════════


class _Boom(RuntimeError):
    """Stands in for the driver exception the runbook names: a partner replaying a `checkout.id`
    we already stored raises `UniqueViolationError` straight out of the step."""


async def test_an_exception_is_counted_and_the_claim_released_with_the_error_backoff(
    monkeypatch, reap, caplog
):
    purchase_id = await _start()

    async def _raises(pid, worker):
        raise _Boom("uq_reap_agentic_purchases_checkout")

    monkeypatch.setattr(job.purchase_svc, "advance", _raises)
    monkeypatch.setenv("REAP_AGENTIC_ERROR_BACKOFF_SECONDS", "120")
    caplog.set_level(logging.DEBUG, logger="jobs.reap_agentic_purchase_poll")

    before = datetime.now(timezone.utc)
    report = await _run(worker_id="w1")

    assert report.errors == 1
    assert report.advanced == 0 and report.claimed == 1
    row = await _get(purchase_id)
    assert row["claimed_by"] is None, "the claim was not given back after the exception"
    # THE BACKOFF, not the state machine's table and not "leave it due now". A row left due after
    # a raising step is re-claimed on the very next tick, and `attempts` is incremented by the
    # claim — so a permanently raising row would walk itself to `attempts_exhausted` in seconds.
    assert row["next_poll_at"] >= before + timedelta(seconds=110)
    assert row["next_poll_at"] <= datetime.now(timezone.utc) + timedelta(seconds=130)
    # The TYPE and the id, never the message.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "_Boom" in logged
    assert "uq_reap_agentic_purchases_checkout" not in logged


async def test_one_raising_row_does_not_stop_the_batch(monkeypatch, reap):
    bad = await _start(buyer_ref="bref_bad")
    good_a = await _start(buyer_ref="bref_a")
    good_b = await _start(buyer_ref="bref_b")

    real_advance = job.purchase_svc.advance

    async def _selective(pid, worker):
        if pid == bad:
            raise _Boom("driver")
        return await real_advance(pid, worker)

    monkeypatch.setattr(job.purchase_svc, "advance", _selective)

    report = await _run(worker_id="w1")

    assert report.claimed == 3
    assert report.errors == 1
    assert report.advanced == 2
    states = await _states()
    assert states[bad] == "resolving"
    assert states[good_a] == "needs_enrollment"
    assert states[good_b] == "needs_enrollment"
    assert await _all_claims() == {}


async def test_an_exception_whose_release_finds_the_claim_gone_is_counted_not_raised(
    monkeypatch, reap
):
    """The narrow race the brief names: `advance` raised AND the claim had already moved. The
    release answers None, which is information, not a failure."""
    purchase_id = await _start()

    async def _raises_after_stealing(pid, worker):
        await _raw(
            "UPDATE reap_agentic_purchases SET claimed_by = 'someone_else' WHERE id = :i",
            {"i": pid},
        )
        raise _Boom("driver")

    monkeypatch.setattr(job.purchase_svc, "advance", _raises_after_stealing)

    report = await _run(worker_id="w1")

    assert report.errors == 1
    assert report.lost_claim == 1
    assert (await _get(purchase_id))["claimed_by"] == "someone_else"


# ══ 4. the sweeps ═════════════════════════════════════════════════════════════════════════════


async def test_a_stale_claim_from_a_dead_worker_is_requeued_and_picked_up_in_the_same_run(reap):
    """THE ORDER TEST. A row held by a worker that died is NOT claimable until the requeue frees
    it, so a run that claimed before requeueing would skip exactly the rows that most need
    attention — every tick, forever, on a pod that keeps dying."""
    purchase_id = await _start()
    await _raw(
        "UPDATE reap_agentic_purchases SET claimed_by = 'dead_pod', "
        "claimed_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": purchase_id},
    )

    report = await _run(worker_id="w1")

    assert report.requeued == 1
    assert report.claimed == 1, "the requeued row was not picked up in the same run"
    assert report.advanced == 1
    assert (await _get(purchase_id))["state"] == "needs_enrollment"
    assert await _all_claims() == {}


async def test_a_fresh_lease_is_not_requeued(reap):
    purchase_id = await _start()
    await _raw(
        "UPDATE reap_agentic_purchases SET claimed_by = 'live_pod', "
        "claimed_at = CURRENT_TIMESTAMP WHERE id = :i",
        {"i": purchase_id},
    )
    report = await _run(worker_id="w1")
    assert report.requeued == 0 and report.claimed == 0
    assert await _claimed_by(purchase_id) == "live_pod"


async def test_the_expire_sweep_runs_and_its_count_is_reported(reap):
    """The PII deadline. `attempts` is exempt in the two waiting states, so without this sweep a
    buyer who walked away keeps their address and email on the row indefinitely."""
    abandoned = await _start(buyer_ref="bref_gone")
    live = await _start(buyer_ref="bref_here")
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'awaiting_approval', "
        "state_entered_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": abandoned},
    )

    report = await _run(worker_id="w1")

    assert report.expired == 1
    row = await _get(abandoned)
    assert row["state"] == "expired"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["last_error_code"] == "hosted_url_expired"
    # The sweep ran BEFORE the claim, so the expired row was never claimed and never polled.
    assert report.claimed == 1
    assert (await _get(live))["state"] == "needs_enrollment"


async def test_the_fail_sweep_runs_and_its_count_is_reported(monkeypatch, reap):
    exhausted = await _start(buyer_ref="bref_tired")
    monkeypatch.setenv("REAP_AGENTIC_MAX_ATTEMPTS", "3")
    await _raw(
        "UPDATE reap_agentic_purchases SET attempts = 3 WHERE id = :i", {"i": exhausted}
    )

    report = await _run(worker_id="w1")

    assert report.failed_exhausted == 1
    assert report.claimed == 0, "the sweep must run before the claim"
    row = await _get(exhausted)
    assert row["state"] == "failed"
    assert row["last_error_code"] == "attempts_exhausted"
    assert row["buyer_email"] is None


async def test_a_processing_purchase_is_never_auto_failed_by_the_counter(monkeypatch, reap):
    """`include_processing=False`, always. A purchase in 'processing' has been APPROVED BY THE
    BUYER and its payment is in flight with Reap; auto-failing it on a counter writes 'failed'
    over a charge whose outcome we do not know. A HUMAN decides those — see the job's comment."""
    stuck = await _start(buyer_ref="bref_flight")
    monkeypatch.setenv("REAP_AGENTIC_MAX_ATTEMPTS", "1")
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'processing', attempts = 9999, "
        "reap_checkout_id = 'chk_7f3a' WHERE id = :i",
        {"i": stuck},
    )

    report = await _run(worker_id="w1")

    assert report.failed_exhausted == 0
    # It was polled instead, which is the point: the outcome comes from the partner, not a counter.
    assert report.claimed == 1
    assert (await _get(stuck))["state"] == "completed"


async def test_the_sweeps_loop_until_drained_and_stop_at_the_iteration_cap(monkeypatch, reap):
    """A sweep that keeps returning a FULL batch is drained by looping — and bounded by a cap, so
    a predicate a future migration makes permanently true is a warning, not an infinite loop
    inside a scheduled job."""
    calls = []

    async def _always_full(*, max_age_seconds, limit, enrollment_grace_seconds):
        calls.append(limit)
        # `await asyncio.sleep(0)` IS LOAD-BEARING, and not for the passing case. A coroutine
        # that never awaits anything does not yield to the event loop, so with the cap removed
        # the sweep loop spins forever and `asyncio.wait_for` BELOW CAN NEVER FIRE — the mutant
        # would hang CI instead of failing it. One yield makes the cancellation land, so the
        # mutant dies in 20 seconds. (Measured: without this, the mutant ran at 100% CPU until
        # an external kill.) The real ledger call awaits the driver, so this is also the more
        # faithful fake.
        await asyncio.sleep(0)
        return [f"id_{n}" for n in range(limit)]

    monkeypatch.setattr(job.ledger, "expire_overdue_purchases", _always_full)

    report = await asyncio.wait_for(_run(worker_id="w1"), timeout=20)

    # PINNED TO THE LITERALS, not to the constants (mutant M18). Comparing against
    # `job.MAX_SWEEP_ITERATIONS` asserts that the code equals itself: raising the cap to 10,000
    # -- which is the mutation that matters, because it is an unbounded loop with extra steps --
    # kept this test green. 20 and 200 are the reviewed values; changing them is a decision, and
    # this line is where it gets made.
    assert len(calls) == 20
    assert job.MAX_SWEEP_ITERATIONS == 20
    assert job.SWEEP_BATCH == 200
    assert report.expired == 20 * 200


async def test_a_partial_batch_ends_the_sweep_loop(monkeypatch, reap):
    calls = []

    async def _one_full_then_partial(*, max_attempts=None, limit, include_processing=False):
        calls.append(limit)
        return [f"id_{n}" for n in range(limit if len(calls) == 1 else 3)]

    async def _shim(max_attempts, *, limit, include_processing=False):
        return await _one_full_then_partial(limit=limit, include_processing=include_processing)

    monkeypatch.setattr(job.ledger, "fail_exhausted_purchases", _shim)

    report = await _run(worker_id="w1")

    assert len(calls) == 2
    assert report.failed_exhausted == job.SWEEP_BATCH + 3


# ══ 5. lost claims ════════════════════════════════════════════════════════════════════════════


async def test_a_row_the_sweep_terminated_mid_step_is_counted_as_lost_claim_not_raised(
    monkeypatch, reap
):
    """The interleaving `transition_as_holder` exists for: the UNFENCED bulk sweep terminates a
    row this worker holds WHILE its partner call is in flight. The worker's fenced write then
    answers None and `advance` reports `lost_claim` — which this job counts and moves past."""
    purchase_id = await _start()

    async def _sweep_underneath(**kwargs):
        # Runs while `_step_resolving` is awaiting the resolve, i.e. after it read the row and
        # before it writes. `fail_exhausted_purchases` is unfenced and clears the claim.
        moved = await ledger.fail_exhausted_purchases(1, limit=10)
        assert purchase_id in moved
        return _resolved()

    reap.resolve_our_row = _sweep_underneath

    report = await _run(worker_id="w1")

    assert report.lost_claim == 1
    assert report.errors == 0, "a lost claim is never an error"
    assert report.advanced == 0
    assert (await _get(purchase_id))["state"] == "failed"
    assert await _all_claims() == {}


async def test_a_row_terminated_before_the_step_reads_it_is_counted_as_terminal(
    monkeypatch, reap
):
    """The other half of the same race, and the base code's real answer for it: when the sweep
    lands BEFORE `advance` reads the row, `advance` sees a terminal state and returns
    `outcome="terminal"` — not `lost_claim`. Counted, not raised, either way."""
    purchase_id = await _start()
    real_claim = job.ledger.claim_due_purchases

    async def _claim_then_terminate(worker, *, limit):
        rows = await real_claim(worker, limit=limit)
        await ledger.fail_exhausted_purchases(1, limit=10)
        return rows

    monkeypatch.setattr(job.ledger, "claim_due_purchases", _claim_then_terminate)

    report = await _run(worker_id="w1")

    assert report.terminal == 1
    assert report.errors == 0
    assert reap.calls == [], "a terminal row makes no partner call"
    assert await _all_claims() == {}


# ══ 6. the budget ═════════════════════════════════════════════════════════════════════════════


async def test_an_exhausted_budget_claims_nothing(monkeypatch, reap):
    """The sweeps ate the run. Claiming now spends a claim — and, in three states, an ATTEMPT —
    on a row this run cannot service, walking it toward `attempts_exhausted` for free."""
    purchase_id = await _start()
    before = await _get(purchase_id)
    clock = _Clock(monkeypatch)

    # The budget must be spent INSIDE the run, not before it: `started` is the run's first read
    # of the clock, so a jump beforehand is invisible. Spending it in the first sweep is also the
    # scenario the test is named for.
    real_requeue = job.ledger.requeue_stale_claims

    async def _slow_requeue(*, lease_seconds, limit):
        moved = await real_requeue(lease_seconds=lease_seconds, limit=limit)
        clock.spend_budget()
        return moved

    monkeypatch.setattr(job.ledger, "requeue_stale_claims", _slow_requeue)

    report = await _run(worker_id="w1")

    assert report.claimed == 0 and report.advanced == 0
    assert reap.calls == []
    after = await _get(purchase_id)
    assert after["claimed_by"] is None
    assert after["attempts"] == before["attempts"], "a claim was spent on a row we did not poll"


async def test_the_budget_stops_the_batch_mid_way_and_gives_the_rest_back(monkeypatch, reap):
    """The budget elapses BECAUSE A ROW TOOK A LONG TIME, which is the only way it ever elapses
    in production. The rest of the batch is released UNTOUCHED — no partner call chain is started
    that the run deadline would cut in half."""
    ids = [await _start(buyer_ref=f"bref_{n}") for n in range(3)]
    clock = _Clock(monkeypatch)

    resolved = _resolved()

    def _slow_first_row(**kwargs):
        # The first row's resolve "takes" the whole budget. Every row after it is over.
        clock.spend_budget()
        return resolved

    reap.resolve_our_row = _slow_first_row

    report = await _run(worker_id="w1")

    assert report.claimed == 3
    assert report.advanced == 1, "the row already in flight must finish, not be abandoned"
    # `abandoned_budget`, NOT `released` (fix F8). "The partner was not ready" and "we ran out of
    # time" are different facts; an operator tuning the budget needs to tell them apart, and the
    # first cut reported both under one name.
    assert report.abandoned_budget == 2
    assert report.released == 0
    assert len(reap.named("resolve_our_row")) == 1, "a partner chain was started past the budget"
    assert await _all_claims() == {}
    states = await _states()
    assert sorted(states[i] for i in ids) == ["needs_enrollment", "resolving", "resolving"]


async def test_the_budget_also_stops_a_sweep_that_keeps_finding_work(monkeypatch, reap):
    """FIX F5. The iteration cap alone let a real backlog issue 20 statements per sweep with the
    budget already spent — measured at 40 across the two sweeps, against the live Postgres,
    after which the run had nothing left for the work it exists to do."""
    clock = _Clock(monkeypatch)
    calls = []

    async def _always_full(*, max_age_seconds, limit, enrollment_grace_seconds):
        calls.append(limit)
        clock.spend_budget()
        await asyncio.sleep(0)
        return [f"id_{n}" for n in range(limit)]

    monkeypatch.setattr(job.ledger, "expire_overdue_purchases", _always_full)

    report = await asyncio.wait_for(_run(worker_id="w1"), timeout=20)

    assert calls == [job.SWEEP_BATCH], "the sweep kept going after the budget was spent"
    assert report.expired == job.SWEEP_BATCH
    # And the budget then stops the claim too, rather than the run pretending it has time.
    assert report.claimed == 0


async def test_an_abandoned_row_is_only_counted_once_its_claim_really_went_back(
    monkeypatch, reap
):
    """FIX F8, THE HALF THAT SURVIVED THE FIRST MUTATION SWEEP.

    `abandoned_budget` is counted AFTER the await, not before it. Counting before is the shape
    that reads fine and lies: the number says "these leases went back" while one of them is still
    held, and an operator reading a clean report has no reason to look at the `errors` line.

    Mutating the order survived every other test in this file, because no test made the release
    FAIL on a row the budget had abandoned. This is that test.
    """
    ids = [await _start(buyer_ref=f"bref_{n}") for n in range(3)]
    clock = _Clock(monkeypatch)
    resolved = _resolved()

    def _slow_first_row(**kwargs):
        clock.spend_budget()
        return resolved

    reap.resolve_our_row = _slow_first_row

    real_release = job.ledger.release_claim
    seen = {"n": 0}

    async def _fails_on_the_first_abandoned(pid, worker, *, next_poll_at=None):
        seen["n"] += 1
        # 1 = the release after the row that advanced; 2 = the first row the budget abandoned.
        if seen["n"] == 2:
            raise RuntimeError("pool blip")
        return await real_release(pid, worker, next_poll_at=next_poll_at)

    monkeypatch.setattr(job.ledger, "release_claim", _fails_on_the_first_abandoned)

    report = await _run(worker_id="w1")

    assert report.claimed == 3
    assert report.advanced == 1
    # TWO rows were reached by the budget branch, but only ONE of them actually gave its lease
    # back on that path. Counting before the await would say 2.
    assert report.abandoned_budget == 1, (
        "a row whose release raised was still counted as abandoned"
    )
    assert report.errors >= 1, "the failed release was not counted"
    # And the `finally` still recovers the one that failed, so the invariant holds either way —
    # which is exactly why the COUNT has to be the thing that tells the truth.
    assert await _all_claims() == {}
    assert len(reap.named("resolve_our_row")) == 1


async def test_a_budget_with_room_processes_the_whole_batch(monkeypatch, reap):
    """The control for the three above: with the clock inside the budget, nothing is skipped. An
    assertion that rows ARE abandoned is worthless without one that they are not always."""
    ids = [await _start(buyer_ref=f"bref_{n}") for n in range(3)]
    _Clock(monkeypatch)

    report = await _run(worker_id="w1")

    assert report.claimed == 3 and report.advanced == 3
    assert report.released == 0 and report.abandoned_budget == 0
    for purchase_id in ids:
        assert (await _get(purchase_id))["state"] == "needs_enrollment"


# ══ 7. the leftover-claims invariant ══════════════════════════════════════════════════════════


async def test_a_claim_that_survived_the_loop_is_released_and_counted(monkeypatch, reap):
    """THE BACKSTOP. A claim the loop forgot is a row nothing touches until the lease ages out —
    and in the two states with no attempt counter, that is a buyer's address and email held for
    the whole lease, on every tick. So it is CHECKED, with a query, not asserted in prose."""
    purchase_id = await _start()
    real_release = job.ledger.release_claim
    swallowed = []

    async def _swallow_once(pid, worker, *, next_poll_at=None):
        if pid == purchase_id and not swallowed:
            swallowed.append(pid)
            return await _get(pid)  # looks like a successful release; releases nothing
        return await real_release(pid, worker, next_poll_at=next_poll_at)

    monkeypatch.setattr(job.ledger, "release_claim", _swallow_once)

    report = await _run(worker_id="w1")

    assert report.advanced == 1
    assert report.errors == 1, "the surviving claim was not noticed"
    assert await _all_claims() == {}, "the surviving claim was noticed but not released"


async def test_the_leftover_check_only_looks_at_this_worker(reap):
    """It must not free somebody else's live lease. The query is conjunct on `claimed_by`, and
    this is the test that says so."""
    mine = await _start(buyer_ref="bref_mine")
    theirs = await _start(buyer_ref="bref_theirs")
    await _raw(
        "UPDATE reap_agentic_purchases SET claimed_by = 'other_worker', "
        "claimed_at = CURRENT_TIMESTAMP WHERE id = :i",
        {"i": theirs},
    )

    report = await _run(worker_id="w1")

    assert report.errors == 0
    assert await _claimed_by(theirs) == "other_worker"
    assert await _claimed_by(mine) is None


# ══ 7b. the claim leak (fix F1/F2) ════════════════════════════════════════════


async def test_a_cancelled_run_propagates_and_still_releases_every_claim(monkeypatch, reap):
    """THE DEADLINE PATH, AND THE TWO HALVES THAT MUST BOTH HOLD.

    The scheduler bounds every run with a deadline and cancels the task on expiry (issue #1754).
    The first cut had no `try/finally`, so a cancel on the 2nd of 4 advances left 3 rows claimed
    for a whole lease — and in the two waiting states that is a buyer's address and email held
    for the whole window.

    1. `CancelledError` MUST PROPAGATE. It is a `BaseException` precisely so cleanup handlers do
       not swallow it; a run that caught it and returned a report would defeat the watchdog the
       scheduler is built around. This is the assertion that kills mutant M7
       (`except Exception` -> `except BaseException`), which survived the first sweep.
    2. THE CLAIMS ARE RELEASED ANYWAY, by the `finally`. The runner gives a cancelled task a
       5-second grace window and calls `cancel()` once, so awaits in the `finally` complete.
    """
    ids = [await _start(buyer_ref=f"bref_{n}") for n in range(4)]
    seen = []
    real_advance = job.purchase_svc.advance

    async def _cancel_on_the_second(pid, worker):
        seen.append(pid)
        if len(seen) == 2:
            raise asyncio.CancelledError()
        return await real_advance(pid, worker)

    monkeypatch.setattr(job.purchase_svc, "advance", _cancel_on_the_second)

    with pytest.raises(asyncio.CancelledError):
        await _run(worker_id="w1")

    assert len(seen) == 2, "the run carried on past the cancellation"
    assert await _all_claims() == {}, (
        "a cancelled run left claims behind — this is the F1/F2 regression"
    )
    # The row that DID advance kept its progress; the rest are simply due again. Asserted as a
    # multiset, not positionally: `claim_due_purchases` orders by `(next_poll_at, id)` and the
    # ids are random, so which of the four went first is not something this test gets to decide.
    by_id = await _states()
    assert sorted(by_id[i] for i in ids) == [
        "needs_enrollment", "resolving", "resolving", "resolving"
    ]


async def test_a_cancelled_run_calls_its_claims_released_on_cancellation_not_a_bug(
    monkeypatch, reap, caplog
):
    """THE LOG LINE IS PART OF AN ALERT. A run cancelled mid-step — its deadline, or the service
    shutting down under a deploy — holds claims because it was interrupted. They are released
    exactly as a leftover is, but logged at WARNING as `released on cancellation`, which the
    failing-poller metric excludes; `STILL CLAIMED after the loop` is the bug's line and must not
    appear, or every deploy that lands mid-step pages (review of #2488, P1)."""
    ids = [await _start(buyer_ref=f"bref_{n}") for n in range(3)]

    async def _cancelled(pid, worker):
        raise asyncio.CancelledError()

    monkeypatch.setattr(job.purchase_svc, "advance", _cancelled)
    caplog.set_level(logging.DEBUG, logger="jobs.reap_agentic_purchase_poll")

    with pytest.raises(asyncio.CancelledError):
        await _run(worker_id="w1")

    assert await _all_claims() == {}
    records = [r for r in caplog.records if r.name == "jobs.reap_agentic_purchase_poll"]
    released = [r for r in records if "released on cancellation" in r.getMessage()]
    assert sorted(r.getMessage().split("purchase=")[1].split(" ")[0] for r in released) == sorted(ids)
    assert {r.levelno for r in released} == {logging.WARNING}
    assert not [r for r in records if "STILL CLAIMED" in r.getMessage()]
    assert not [r for r in records if r.levelno >= logging.ERROR]


async def test_a_claim_the_loop_forgot_is_still_logged_as_the_bug_it_is(monkeypatch, reap, caplog):
    """The other side of the split: no cancellation, so a claim found after the loop keeps the
    ERROR line and the `errors` count. The cancel wording must not leak onto this path."""
    purchase_id = await _start()
    real_release = job.ledger.release_claim
    swallowed = []

    async def _swallow_once(pid, worker, **kwargs):
        if pid == purchase_id and not swallowed:
            swallowed.append(pid)
            return await _get(pid)
        return await real_release(pid, worker, **kwargs)

    monkeypatch.setattr(job.ledger, "release_claim", _swallow_once)
    caplog.set_level(logging.DEBUG, logger="jobs.reap_agentic_purchase_poll")

    report = await _run(worker_id="w1")

    assert report.errors == 1
    bug = [r for r in caplog.records if "STILL CLAIMED after the loop" in r.getMessage()]
    assert len(bug) == 1 and bug[0].levelno == logging.ERROR
    assert purchase_id in bug[0].getMessage()
    assert "released on cancellation" not in caplog.text


async def test_a_raising_release_does_not_abandon_the_rest_of_the_batch(monkeypatch, reap):
    """The post-step `release_claim` sat BARE in the first cut, outside the per-row guard, so a
    pool blip on row 2 of 4 propagated out of the whole function and abandoned every lease behind
    it. It is now inside the guard, and the `finally` is the backstop for the row itself."""
    ids = [await _start(buyer_ref=f"bref_{n}") for n in range(4)]
    real_release = job.ledger.release_claim
    calls = {"n": 0}

    async def _raises_once(pid, worker, *, next_poll_at=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("pool blip")
        return await real_release(pid, worker, next_poll_at=next_poll_at)

    monkeypatch.setattr(job.ledger, "release_claim", _raises_once)

    report = await _run(worker_id="w1")

    assert report.claimed == 4
    assert report.advanced == 4, "rows behind the failing release were abandoned"
    assert report.errors >= 1, "the failed release was not counted"
    assert await _all_claims() == {}, "the finally did not recover the leaked claim"


async def test_a_raising_leftover_select_does_not_propagate_and_retries_once(monkeypatch, reap):
    """The leftover SELECT also sat bare. A transient failure there propagated out of the run —
    after the loop had already done its work — and every lease went with it. It now gets a second
    attempt of its own, inside the `finally`."""
    await _start()
    real_fetch_all = job.database.fetch_all
    attempts = {"n": 0}

    async def _fails_first(sql, values=None, *a, **k):
        if sql is job._LEFTOVER_CLAIMS_SQL:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("pool blip")
        return await real_fetch_all(sql, values, *a, **k)

    monkeypatch.setattr(job.database, "fetch_all", _fails_first)

    report = await _run(worker_id="w1")

    assert attempts["n"] == 2, "the leftover read was not retried"
    assert report.advanced == 1
    assert await _all_claims() == {}


async def test_a_leftover_read_that_fails_twice_is_survived_not_raised(monkeypatch, reap):
    """Two failures is the end of it: the run reports what it did and the requeue becomes the
    backstop. What must NOT happen is an exception escaping a scheduled job, because the runner
    records that as a failed run and the counts for the work that really happened are lost."""
    await _start()

    async def _always_fails(sql, values=None, *a, **k):
        if sql is job._LEFTOVER_CLAIMS_SQL:
            raise RuntimeError("pool down")
        return await job.database.__class__.fetch_all(job.database, sql, values, *a, **k)

    monkeypatch.setattr(job.database, "fetch_all", _always_fails)

    report = await _run(worker_id="w1")

    assert report.advanced == 1
    assert report.errors == 0, "a failed cleanup read is not a per-row error"


async def test_processing_rows_past_the_ceiling_are_counted_and_logged(monkeypatch, reap, caplog):
    """FIX F6. `fail_exhausted_purchases(include_processing=False)` deliberately refuses these
    rows — a payment is in flight and a counter must not terminate it — but the first cut
    reported NOTHING about them, so a stuck charge was invisible in the one report an operator
    reads. Measured on a row at attempts=100,005 that no count mentioned."""
    caplog.set_level(logging.ERROR, logger="jobs.reap_agentic_purchase_poll")
    stuck_a = await _start(buyer_ref="bref_a")
    stuck_b = await _start(buyer_ref="bref_b")
    healthy = await _start(buyer_ref="bref_ok")
    monkeypatch.setenv("REAP_AGENTIC_MAX_ATTEMPTS", "5")
    for n, pid in enumerate((stuck_a, stuck_b)):
        # DISTINCT checkout ids: `uq_reap_agentic_purchases_checkout` is a real partial unique
        # index, and two rows claiming one checkout is the very thing it exists to refuse.
        await _raw(
            "UPDATE reap_agentic_purchases SET state = 'processing', attempts = 100005, "
            "reap_checkout_id = :c WHERE id = :i",
            {"i": pid, "c": f"chk_stuck_{n}"},
        )

    report = await _run(worker_id="w1")

    assert report.processing_over_attempts == 2
    assert report.failed_exhausted == 0, "the counter must never terminate a payment in flight"
    assert "max_attempts" in caplog.text and "human" in caplog.text
    # A healthy row is not counted, which is the control: a count that is always 2 says nothing.
    assert (await _get(healthy))["state"] == "needs_enrollment"


async def test_no_processing_rows_past_the_ceiling_reports_zero(reap):
    """The control for the count above."""
    await _start()
    assert (await _run(worker_id="w1")).processing_over_attempts == 0


async def test_an_unhandled_outcome_is_counted_and_named(monkeypatch, reap, caplog):
    """MUTANT M17. The `else` branch — `advance` returning `missing`, or an outcome added to the
    service that nobody taught this job about — was never executed by any test, so deleting it
    (or folding it into `advanced`) survived. It is the branch that keeps a whole new outcome
    class from being invisible in the report."""
    caplog.set_level(logging.ERROR, logger="jobs.reap_agentic_purchase_poll")
    purchase_id = await _start()

    async def _missing(pid, worker):
        return svc.AdvanceResult(pid, outcome="missing", state="")

    monkeypatch.setattr(job.purchase_svc, "advance", _missing)

    report = await _run(worker_id="w1")

    assert report.errors == 1
    assert report.advanced == 0 and report.released == 0
    assert report.lost_claim == 0 and report.terminal == 0
    assert "unhandled outcome" in caplog.text and "missing" in caplog.text
    # The claim is still given back: an outcome we do not understand is not a reason to keep a
    # lease on somebody's purchase.
    assert await _all_claims() == {}


async def test_an_outcome_invented_after_this_job_was_written_is_also_counted(monkeypatch, reap):
    """The same branch through the case it really guards against: a FUTURE outcome. Written out
    separately from `missing` because the two fail differently — one is a bug in the ledger, the
    other is a package that moved without this one."""
    await _start()

    async def _from_the_future(pid, worker):
        return svc.AdvanceResult(pid, outcome="deferred_to_human", state="quoting")

    monkeypatch.setattr(job.purchase_svc, "advance", _from_the_future)

    report = await _run(worker_id="w1")

    assert report.errors == 1
    assert await _all_claims() == {}


# ══ 7c. what #2204's adoption round changed under us ══════════════════════════════


async def test_a_raising_step_now_records_why_on_the_row(monkeypatch, reap):
    """`release_claim` gained `last_error_code` in #2204's adoption round, and THIS is the path
    that needed it. Before, a step that RAISED wrote nothing at all: the exception type lived in
    one log line, so "why has this purchase been stuck in 'resolving' for an hour?" had no answer
    in the row a human would actually look at."""
    purchase_id = await _start()

    class _UniqueViolationError(RuntimeError):
        pass

    async def _raises(pid, worker):
        raise _UniqueViolationError("uq_reap_agentic_purchases_checkout")

    monkeypatch.setattr(job.purchase_svc, "advance", _raises)

    report = await _run(worker_id="w1")

    assert report.errors == 1
    row = await _get(purchase_id)
    assert row["claimed_by"] is None
    # LOWERCASED, because `release_claim` REFUSES a code outside `^[a-z0-9_:.-]{1,64}` rather than
    # folding it — a raise here would turn one bad row into a failed release.
    assert row["last_error_code"] == "poller_advance_raised:_uniqueviolationerror"


def test_the_error_code_this_job_writes_is_one_the_ledger_will_accept(monkeypatch):
    """The obligation `_require_error_code`'s docstring states explicitly: the pattern is
    LOWERCASE and the caller must fold. Asserted against the LEDGER'S OWN regex for a spread of
    exception type names, including ones with digits and underscores, so a future exception class
    cannot make a scheduled job raise inside its own error handler."""
    for type_name in (
        "UniqueViolationError", "ReadTimeout", "OSError", "_Boom", "Error2", "A" * 200,
    ):
        code = job.purchase_svc._error_code(f"poller_advance_raised:{type_name}")
        assert ledger._ERROR_CODE_RE.match(code), (type_name, code)
        # And it really is acceptable to the validator, not merely regex-shaped.
        assert ledger._require_error_code(code) == code


async def test_the_post_step_release_does_not_erase_the_services_own_reason(monkeypatch, reap):
    """THE REGRESSION THIS JOB IS ONE LINE AWAY FROM. #2204 now records a reason on its OWN
    no-progress releases (`_release(..., error_code=…)`). This job then issues an unconditional
    `release_claim` after every step — and passing a code there, or a bare assignment in the
    ledger, would overwrite the service's more specific reason on every ordinary tick.

    Driven through a REAL released step: an enrollment the partner still calls pending.
    """
    purchase_id = await _start()
    await _run(worker_id="w1")  # -> needs_enrollment
    await _raw(
        "UPDATE reap_agentic_purchases SET next_poll_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": purchase_id},
    )
    reap.get_enrollment = lambda **kw: _ok(
        dict(ENROLLMENT_ACTIVE, id=str(kw.get("id")), status="REQUIRES_ACTION", paymentMethod=None)
    )

    report = await _run(worker_id="w2")

    assert report.released == 1, report
    row = await _get(purchase_id)
    assert row["state"] == "needs_enrollment"
    assert row["claimed_by"] is None
    assert row["last_error_code"] == "enrollment_pending", (
        "the poller's own release overwrote the reason the state machine recorded"
    )


def test_processing_is_the_only_state_with_no_bound():
    """WHY `processing_over_attempts` EXISTS, derived from the ledger's SQL rather than asserted.

    Each pollable state is bounded by a clock, by a counter, or by nothing:

      * `_EXPIRE_SOURCE_STATES` — bounded by `expire_overdue_purchases`, i.e. a CLOCK. This is
        also the PII deadline: a state outside this list never has `buyer_email` or
        `shipping_address` nulled by the passage of time.
      * `_FAIL_EXHAUSTED_SOURCE_STATES` minus `_CLAIM_ATTEMPT_EXEMPT_STATES` minus 'processing'
        (which the poller always skips) — bounded by a COUNTER.

    The remainder is 'processing', and #2204's round-2 review measured what that costs: a row
    parked there and aged to 2020 still held the buyer's address and email. If a future change
    ever bounds it, this test fails and whoever made the change gets to decide what
    `processing_over_attempts` is still for.
    """
    bounded_by_clock = set(ledger._EXPIRE_SOURCE_STATES)
    bounded_by_counter = (
        set(ledger._FAIL_EXHAUSTED_SOURCE_STATES)
        - set(ledger._CLAIM_ATTEMPT_EXEMPT_STATES)
        - {"processing"}
    )
    unbounded = set(ledger._POLLABLE_STATES) - bounded_by_clock - bounded_by_counter

    assert unbounded == {"processing"}, unbounded
    # And the PII half of the claim, stated separately because it is the half that matters.
    assert "processing" not in ledger._EXPIRE_SOURCE_STATES


async def test_every_outcome_the_service_can_return_is_one_this_job_handles():
    """MERGED IS NOT RUNNING, applied to a vocabulary. The job counts four outcomes by name and
    routes everything else to `errors`. That is safe, but it is only INFORMATIVE while the two
    lists agree — so the service's own documented set is parsed out of `AdvanceResult`'s
    docstring and compared, and a new outcome added upstream lands here as a failing assertion
    rather than as a silent bump in `errors` months later.
    """
    import inspect
    import re as _re

    doc = inspect.getdoc(svc.AdvanceResult) or ""
    body = doc.split("`outcome` is one of:", 1)[-1]
    documented = set(_re.findall(r"^\s{4,}([a-z_]+)\s{2,}\S", body, _re.M))

    assert documented == {"advanced", "released", "lost_claim", "terminal", "missing"}, documented
    counted = {"advanced", "released", "lost_claim", "terminal"}
    assert counted < documented
    # 'missing' is the one the service can return that this job deliberately routes to `errors`.
    assert documented - counted == {"missing"}


# ══ 8. concurrency ════════════════════════════════════════════════════════════════════════════


async def test_two_concurrent_runs_never_advance_one_row_twice(reap):
    """`asyncio.gather` on the SHARED `databases` connection — this repo's known hazard, and the
    interleaving the claim fence exists for. On SQLite everything is serialised onto one
    connection, so what this proves is the guard under INTERLEAVING; the two-real-connections
    half is the Postgres arm's job."""
    purchase_id = await _start()

    a, b = await asyncio.gather(_run(worker_id="w1"), _run(worker_id="w2"))

    assert a.claimed + b.claimed == 1, (a, b)
    assert a.advanced + b.advanced == 1, (a, b)
    assert a.errors + b.errors == 0
    assert len(reap.named("resolve_our_row")) == 1, "the row was stepped twice"
    assert (await _get(purchase_id))["state"] == "needs_enrollment"
    assert await _all_claims() == {}


async def test_the_job_opens_no_transaction():
    """Property 6 of the ledger header: on Postgres `CURRENT_TIMESTAMP` is TRANSACTION-start
    time, so a poll loop inside one transaction compares every row against the clock as it was
    when the run began. The ledger dodges that with `clock_timestamp()`; the JOB must not
    reintroduce it. A source assertion, because there is no runtime signal for "did not".
    """
    import ast

    tree = ast.parse(Path(job.__file__).read_text(encoding="utf-8"))
    # The module DOCSTRING names `database.transaction()` in order to say the file does not use
    # it, so the search has to be over the code. Stripping it here rather than grepping for a
    # cleverer pattern keeps the assertion literal.
    source = ast.unparse(ast.Module(body=tree.body[1:], type_ignores=[]))
    assert "database.transaction" not in source
    assert "async with database" not in source
    # And the only `database.*` call it makes at all is the one read-only bookkeeping SELECT.
    # TWO call sites, both read-only and both over module-level SQL constants: the leftover
    # claims sweep and the `processing_over_attempts` count. Pinned by number so a third arrives
    # with a deliberate decision rather than by accident.
    assert source.count("database.") == 2, "the job reaches `database` a different number of times"
    assert source.count("database.fetch_all") == 1
    assert source.count("database.fetch_one") == 1
    assert "database.fetch_all(_LEFTOVER_CLAIMS_SQL" in source
    assert "database.fetch_one(_PROCESSING_OVER_ATTEMPTS_SQL" in source


# ══ 9. the dials ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("name", sorted(job.DIALS))
@pytest.mark.parametrize("bad", ["", "  ", "abc", "3.5", "-1", "999999999"])
def test_every_dial_falls_back_to_its_default_on_a_bad_value(monkeypatch, caplog, name, bad):
    dial = job.DIALS[name]
    monkeypatch.setenv(dial.env, bad)
    caplog.set_level(logging.WARNING)

    assert job._env_int(dial) == dial.default

    if bad.strip():
        # NEVER SILENT. A dial that fell back without saying so is a number an operator tuned and
        # watched do nothing.
        assert dial.env in caplog.text, f"the warning does not name {dial.env}"


@pytest.mark.parametrize("name", sorted(job.DIALS))
def test_every_dials_bounds_are_accepted_at_the_edges(monkeypatch, name):
    dial = job.DIALS[name]
    monkeypatch.setenv(dial.env, str(dial.minimum))
    assert job._env_int(dial) == dial.minimum
    monkeypatch.setenv(dial.env, str(dial.maximum))
    assert job._env_int(dial) == dial.maximum


async def test_a_lease_below_the_ledgers_floor_falls_back_instead_of_raising(monkeypatch, reap):
    """THE BOUNDS ARE NOT DECORATION. `requeue_stale_claims(lease_seconds=5)` is a ValueError out
    of the ledger — the ledger refuses a silently-raised floor on purpose — so a dial this module
    let through unvalidated would not be a bad setting, it would be an exception out of a
    scheduled job on every tick."""
    await _start()
    monkeypatch.setenv("REAP_AGENTIC_LEASE_SECONDS", "5")

    report = await _run(worker_id="w1")

    assert report.errors == 0 and report.claimed == 1


def test_the_lease_floor_is_above_the_slowest_step_not_the_ledgers_minimum(monkeypatch):
    """FIX F4, AND THE NUMBER IS DERIVED, NOT PICKED.

    The ledger's own floor is 30 s and this module used to inherit it. A lease shorter than one
    step means a LIVE worker gets its own row requeued underneath it: the requeue frees the
    lease, a second worker claims the same purchase, and both call the partner. The fence stops
    the double WRITE; nothing stops the second QUOTE, which is a real request against a
    merchant's commerce layer.

    The worst realistic `quoting` step, re-derived against the client's per-path read timeouts
    after #2204's fix round:

        resolve_our_row   rc.MAX_SEARCH_ATTEMPTS searches + one variant read, 25 s each -> 100 s
        request_quote     35 s
        create_checkout   35 s
                                                                                   ---- 170 s

    So the floor is 180 and the default is 300. Asserted against the CLIENT'S OWN constants, so
    that a future timeout change makes this fail rather than quietly invalidating the floor.
    """
    lease = job.DIALS["lease_seconds"]
    worst_resolve = (rc.MAX_SEARCH_ATTEMPTS + 1) * rc._DEFAULT_TIMEOUT_S
    worst_quoting = worst_resolve + 2 * rc._QUOTE_TIMEOUT_S

    assert worst_quoting == 170.0, (
        f"the slowest step is now {worst_quoting}s; re-derive the lease floor and the job's "
        "_JOB_RUN_DEADLINES entry"
    )
    assert lease.minimum == 180
    assert lease.minimum > worst_quoting
    assert lease.default == 300
    # And a value between the ledger's floor and ours is REFUSED by us, which is the whole point.
    monkeypatch.setenv(lease.env, "60")
    assert job._env_int(lease) == 300


async def test_a_claim_batch_above_the_ledgers_cap_falls_back_instead_of_raising(
    monkeypatch, reap
):
    """The same shape at the other end: `_sweep_limit` caps `limit` at 500 and raises above it."""
    await _start()
    monkeypatch.setenv("REAP_AGENTIC_CLAIM_BATCH", "5000")

    report = await _run(worker_id="w1")

    assert report.errors == 0 and report.claimed == 1


async def test_a_max_age_below_the_ledgers_floor_falls_back_instead_of_raising(monkeypatch, reap):
    await _start()
    monkeypatch.setenv("REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS", "1")

    report = await _run(worker_id="w1")

    assert report.errors == 0 and report.claimed == 1


async def test_the_claim_batch_dial_really_bounds_the_batch(monkeypatch, reap):
    for n in range(5):
        await _start(buyer_ref=f"bref_{n}")
    monkeypatch.setenv("REAP_AGENTIC_CLAIM_BATCH", "2")

    report = await _run(worker_id="w1")

    assert report.claimed == 2


def test_the_registration_interval_dial_is_read_at_registration(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_POLL_INTERVAL_SECONDS", "45")
    assert job.job_interval_seconds() == 45
    monkeypatch.setenv("REAP_AGENTIC_POLL_INTERVAL_SECONDS", "0")
    assert job.job_interval_seconds() == 30


def test_the_dials_are_read_per_run_not_at_import(monkeypatch):
    """Every value is an `os.getenv` at call time. A flag sampled once at startup keeps reporting
    the value it had then, which on this rail means an operator turning something off and
    watching nothing change."""
    dial = job.DIALS["claim_batch"]
    monkeypatch.setenv(dial.env, "7")
    assert job._env_int(dial) == 7
    monkeypatch.setenv(dial.env, "9")
    assert job._env_int(dial) == 9


def test_a_bad_dial_warns_once_per_process_not_once_per_tick(monkeypatch, caplog):
    """FIX F8. A bad dial is a STANDING condition, not an event. At a 30 s interval the first cut
    logged the same warning 2,880 times a day, which is exactly how a real warning becomes
    something an operator filters out."""
    caplog.set_level(logging.WARNING, logger="jobs.reap_agentic_purchase_poll")
    dial = job.DIALS["claim_batch"]
    monkeypatch.setenv(dial.env, "nonsense")

    for _ in range(5):
        assert job._env_int(dial) == dial.default

    warnings = [r for r in caplog.records if dial.env in r.getMessage()]
    assert len(warnings) == 1, f"warned {len(warnings)} times"
    assert "once per process" in warnings[0].getMessage()


def test_each_dial_gets_its_own_warning(monkeypatch, caplog):
    """The control for the test above: "once" must mean once PER VARIABLE, not one warning ever.
    A shared flag would silence the second misconfigured dial entirely."""
    caplog.set_level(logging.WARNING, logger="jobs.reap_agentic_purchase_poll")
    for name in ("claim_batch", "max_attempts"):
        monkeypatch.setenv(job.DIALS[name].env, "nonsense")
        job._env_int(job.DIALS[name])

    named = {r.getMessage().split()[1] for r in caplog.records}
    assert "REAP_AGENTIC_CLAIM_BATCH" in named
    assert "REAP_AGENTIC_MAX_ATTEMPTS" in named


# ══ 10. PII ═══════════════════════════════════════════════════════════════════════════════════


async def test_no_pii_reaches_the_logs_or_the_report_on_any_path(monkeypatch, reap, caplog):
    """Every path in one test, because the interesting failure is a path nobody thought to check:
    the happy path, a raising step, a swept row, a lost claim and a leftover claim."""
    # Scoped to THIS RAIL'S loggers. A bare `set_level(DEBUG)` turns the root logger on, and the
    # aiosqlite/databases drivers then log every statement WITH ITS BOUND PARAMETERS — which on
    # an INSERT into this table is the buyer's email and address. That is the driver's debug
    # echo, present for every table in the repo and out of scope here; what is in scope is what
    # THIS JOB and the state machine it drives write.
    for name in ("jobs.reap_agentic_purchase_poll", "services.reap_agentic_purchase"):
        caplog.set_level(logging.DEBUG, logger=name)
    reports = []

    happy = await _start(buyer_ref="bref_happy")
    abandoned = await _start(buyer_ref="bref_gone")
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'awaiting_approval', "
        "state_entered_at = '2020-01-01 00:00:00' WHERE id = :i",
        {"i": abandoned},
    )

    async def _raises(pid, worker):
        raise _Boom(f"{EMAIL} {ADDRESS['addressLine1']}")

    with capture_pivota_stdout() as pivota_buffer:
        reports.append(await _run(worker_id="w1"))
        monkeypatch.setattr(job.purchase_svc, "advance", _raises)
        await _raw(
            "UPDATE reap_agentic_purchases SET next_poll_at = '2020-01-01 00:00:00' WHERE id = :i",
            {"i": happy},
        )
        reports.append(await _run(worker_id="w2"))

    # `getMessage()` applies the %-args, which is what a handler would write. Both halves matter:
    # a PII value could arrive as the format string OR as an argument.
    records = [r for r in caplog.records if r.name.startswith(("jobs.reap", "services.reap"))]
    assert records, "the scoped loggers captured nothing; this test would pass vacuously"
    # The per-run report line goes through the pivota logger (propagate=False), so caplog never
    # sees it; its handler's output joins the blob so the line this rail relies on stays in scope.
    report_lines = [line for line in pivota_lines(pivota_buffer) if "reap_agentic_poll: " in line]
    assert len(report_lines) == 2, report_lines
    blob = "\n".join([r.getMessage() for r in records] + report_lines)
    for secret in PII_STRINGS:
        assert secret not in blob, f"{secret!r} reached a log record"
        for report in reports:
            assert secret not in repr(report)
    for report in reports:
        assert all(isinstance(v, int) for v in vars(report).values())


async def test_the_report_carries_only_integers(reap):
    await _start()
    report = await _run(worker_id="w1")
    fields = vars(report)
    assert set(fields) == {
        "requeued", "expired", "failed_exhausted", "processing_over_attempts",
        "stuck_over_age", "contact_retention_blocked", "checkout_needs_human", "claimed", "advanced", "released", "abandoned_budget",
        "lost_claim", "terminal", "errors", "skipped_disabled", "duration_ms",
    }
    assert all(isinstance(v, int) for v in fields.values())
    with pytest.raises(Exception):
        report.claimed = 99  # frozen


async def test_the_default_worker_id_is_unique_per_run():
    """It is the CLAIM FENCE, not a label. Two runs sharing one would each be able to release the
    other's live lease, and one run's leftover-claims check would 'recover' rows the other is
    mid-partner-call on."""
    ids = {job._worker_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(part.strip() for part in next(iter(ids)).split(":"))


# ══ 10b. the proof line ═══════════════════════════════════════════════════════════════════════
#
# Measured in prod 2026-09-23: /__scheduler_health showed 155 ok runs of this job and Cloud
# Logging held ZERO `reap_agentic_poll:` report lines. The report went through
# `logging.getLogger(__name__)`; nothing configures root in this process, root sits at WARNING,
# and INFO is dropped at the logger. caplog could not see that — it hangs on root and turns the
# level down — so this reads the pivota handler's OWN stream with root where prod has it, and
# FAILS on a module-logger emit.

_PROOF_LINE = re.compile(r"\[[^\]]+\] INFO - reap_agentic_poll: PollReport\(.*\)$")


async def test_the_report_line_lands_on_pivota_stdout_at_info_with_root_at_warning(reap):
    await _start()
    with root_as_in_prod(), capture_pivota_stdout() as out:
        report = await _run(worker_id="w1")
    lines = pivota_lines(out)
    proof = [line for line in lines if _PROOF_LINE.fullmatch(line)]
    assert len(proof) == 1, f"expected exactly one report line on the pivota handler, got {lines!r}"
    assert proof[0].endswith(f"reap_agentic_poll: {report!r}")
    assert report.claimed == 1, "the run did real work; the line is not a no-op tick's"
    for secret in PII_STRINGS:
        assert secret not in proof[0]


async def test_the_report_line_does_not_depend_on_the_root_logger(reap):
    """With root wide open and a handler on it, the report line must NOT show up there: reaching
    root means propagating from a module logger, the exact path prod drops."""

    class _Sink(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    root, sink = logging.getLogger(), _Sink()
    saved = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(sink)
    try:
        with capture_pivota_stdout() as out:
            await _run(worker_id="w1")
    finally:
        root.removeHandler(sink)
        root.setLevel(saved)
    assert any(_PROOF_LINE.fullmatch(line) for line in pivota_lines(out))
    assert not any(m.startswith("reap_agentic_poll: PollReport(") for m in sink.messages), (
        "the report line propagated to root, i.e. it went through the module logger"
    )


# ══ 10c. the stuck count ══════════════════════════════════════════════════════════════════════
#
# `stuck_over_age` is what the "purchase stuck" alert reads off the report line. WHICH rows count
# is the ledger's definition and is tested there (tests/test_reap_agentic_ledger.py); what is
# tested here is the job's half: it reads on armed runs ONLY, after the loop, with the sweep's own
# windows, and a read that fails says NOT COUNTED rather than zero.


async def _park(purchase_id: str, state: str, entered_seconds_ago: int, **cols) -> None:
    """Put a row in `state`, aged, and NOT DUE — so the run under test reports on it without
    stepping it."""
    sets = ", ".join(f"{name} = :{name}" for name in cols)
    await _raw(
        "UPDATE reap_agentic_purchases SET state = :s, "
        f"state_entered_at = datetime('now', '-{int(entered_seconds_ago)} seconds'), "
        "next_poll_at = datetime('now', '+3600 seconds')"
        + (", " + sets if sets else "")
        + " WHERE id = :i",
        dict(cols, s=state, i=purchase_id),
    )


async def test_an_armed_run_reports_the_purchases_stuck_past_their_deadline(reap):
    approved = await _start(buyer_ref="bref_paid")
    quoting = await _start(buyer_ref="bref_quote")
    fresh = await _start(buyer_ref="bref_fresh")
    await _park(approved, "processing", job.STUCK_AFTER_SECONDS + 5, reap_checkout_id="chk_stuck")
    await _park(quoting, "quoting", job.STUCK_AFTER_SECONDS + 5)
    await _park(fresh, "processing", job.STUCK_AFTER_SECONDS - 60, reap_checkout_id="chk_fresh")

    report = await _run(worker_id="w1")

    assert report.stuck_over_age == 2
    assert report.errors == 0
    assert job.STUCK_AFTER_SECONDS == 1800, (
        "the alert policy in infra/gcp/setup_monitoring.sh says 30 minutes; change them together"
    )


async def test_an_armed_run_with_nothing_stuck_reports_zero_not_the_sentinel(reap):
    await _start()
    report = await _run(worker_id="w1")
    assert report.stuck_over_age == 0
    assert report.stuck_over_age != job.NOT_COUNTED


def test_a_report_nobody_filled_in_says_not_counted():
    """The dataclass default is the sentinel too: a `PollReport` built anywhere without a count
    must not claim that nothing is stuck."""
    assert job.PollReport().stuck_over_age == job.NOT_COUNTED == -1


async def test_a_buyer_still_on_a_live_hosted_page_is_not_reported_stuck(reap):
    """Fifty minutes into `awaiting_approval` with a page that has not expired: waiting on a
    person, with a deadline of its own that has not passed. The same age in `processing` is the
    alert."""
    waiting = await _start(buyer_ref="bref_wait")
    await _park(waiting, "awaiting_approval", 3000, reap_checkout_id="chk_wait")
    await _raw(
        "UPDATE reap_agentic_purchases SET hosted_url_expires_at = datetime('now', '+600 seconds') "
        "WHERE id = :i",
        {"i": waiting},
    )
    assert (await _run(worker_id="w1")).stuck_over_age == 0

    await _raw("UPDATE reap_agentic_purchases SET state = 'processing' WHERE id = :i", {"i": waiting})
    assert (await _run(worker_id="w1")).stuck_over_age == 1


async def test_the_stuck_count_is_read_after_the_loop(reap):
    """A row this very tick moves is not stuck: it was 'resolving' for an hour, the run steps it
    to 'needs_enrollment', and the state clock restarts. A read placed before the claim loop
    reports 1 here and pages for a purchase that has just started moving."""
    purchase_id = await _start()
    await _raw(
        "UPDATE reap_agentic_purchases SET state_entered_at = datetime('now', '-3600 seconds') "
        "WHERE id = :i",
        {"i": purchase_id},
    )
    assert await ledger.count_stuck_purchases(stuck_after_seconds=job.STUCK_AFTER_SECONDS) == 1

    report = await _run(worker_id="w1")

    assert report.advanced == 1
    assert (await _get(purchase_id))["state"] == "needs_enrollment"
    assert report.stuck_over_age == 0


def _watch_the_stuck_read(monkeypatch) -> list:
    """Every call to the stuck-count read, recorded. A LIST, not a raising stub: the job wraps
    the read in `except Exception`, so a stub that raised would be swallowed into `errors` and a
    test that only looked at the report would pass with the read issued."""
    calls = []

    async def _recorded(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(ledger, "count_stuck_purchases", _recorded)
    return calls


async def test_a_disarmed_run_reconciles_and_reports_stuck_checkouts(monkeypatch, reap):
    """Disarming stops new work while reconciliation and its alerts remain observable."""
    stuck = await _start()
    await _park(stuck, "processing", 99999, reap_checkout_id="chk_dark")
    reads = _watch_the_stuck_read(monkeypatch)
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")

    with root_as_in_prod(), capture_pivota_stdout() as out:
        report = await _run()

    assert len(reads) == 1, "disarmed reconciliation must retain monitoring"
    assert report.skipped_disabled == 1
    assert report.stuck_over_age == 0
    assert report.errors == 0
    assert [line for line in pivota_lines(out) if "PollReport(" in line]


async def test_an_unconfigured_client_counts_only_exposed_stuck_rows(monkeypatch, reap):
    await _start()
    reads = _watch_the_stuck_read(monkeypatch)
    monkeypatch.delenv("REAP_API_KEY", raising=False)
    report = await _run()
    assert len(reads) == 1 and reads[0]["reconciliation_only"] is True
    assert report.skipped_disabled == 1
    assert report.stuck_over_age == 0


async def test_the_non_sandbox_refusal_still_counts_exposed_stuck_rows(monkeypatch, reap):
    await _start()
    reads = _watch_the_stuck_read(monkeypatch)
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://prod.api.reap.global")
    report = await _run()
    assert len(reads) == 1 and reads[0]["reconciliation_only"] is True
    assert report.skipped_disabled == 1
    assert report.stuck_over_age == 0


async def test_a_failed_stuck_read_is_not_counted_not_zero_and_the_run_still_reports(
    monkeypatch, reap, caplog
):
    """A count that could not be taken must not read as "nobody is stuck". The field keeps the
    sentinel — which the stuck metric's `[1-9]` cannot match — `errors` goes up so the
    poller-failing alert covers the tick, and the report line is still printed."""
    await _start()

    async def _boom(**kwargs):
        raise RuntimeError(f"row content: {EMAIL}")

    monkeypatch.setattr(ledger, "count_stuck_purchases", _boom)
    caplog.set_level(logging.ERROR, logger="jobs.reap_agentic_purchase_poll")

    with capture_pivota_stdout() as out:
        report = await _run(worker_id="w1")

    assert report.stuck_over_age == job.NOT_COUNTED
    assert report.errors == 1
    assert report.advanced == 1, "the diagnostic failing must not undo the run's real work"
    assert "could not count stuck purchases (error_type=RuntimeError)" in caplog.text
    assert EMAIL not in caplog.text, "the exception MESSAGE reached a log line"
    (line,) = [ln for ln in pivota_lines(out) if "PollReport(" in ln]
    assert "stuck_over_age=-1" in line and "errors=1" in line


async def test_an_over_budget_run_still_counts_the_stuck_purchases(monkeypatch, reap):
    """THE SCENARIO REVIEW MEASURED (#2488 round 2). A payment 90 minutes in `processing`, and
    three ticks in a row that each spend the whole run budget on the one row they step. The
    previous cut SKIPPED step 5 on a spent budget: every one of those reports read
    `stuck_over_age=-1, errors=0`, with `abandoned_budget=0`, and nothing paged — the stuck alert
    was blind exactly when the poller was slowest. The count is now taken on every armed run
    that reaches it, bounded by its own timeout rather than by the budget."""
    paid = await _start(buyer_ref="bref_paid")
    await _park(paid, "processing", 90 * 60, reap_checkout_id="chk_stuck")
    clock = _Clock(monkeypatch)
    resolved = _resolved()

    def _slow_row(**kwargs):
        clock.spend_budget()  # the one row this run steps uses the whole budget
        return resolved

    reap.resolve_our_row = _slow_row

    for tick in range(3):
        await _start(buyer_ref=f"bref_slow_{tick}")
        with capture_pivota_stdout() as out:
            report = await _run(worker_id=f"w{tick}")
        assert report.advanced == 1 and report.abandoned_budget == 0
        assert report.duration_ms >= 9_000_000, "the run was not over budget when it reached step 5"
        assert report.stuck_over_age == 1, f"tick {tick}: an over-budget run did not count"
        assert report.errors == 0
        (line,) = [ln for ln in pivota_lines(out) if "PollReport(" in ln]
        assert "stuck_over_age=1," in line


async def test_a_run_inside_its_budget_issues_the_stuck_read_once(monkeypatch, reap):
    await _start()
    reads = _watch_the_stuck_read(monkeypatch)
    _Clock(monkeypatch)
    report = await _run(worker_id="w1")
    assert len(reads) == 1
    assert report.stuck_over_age == 0


async def test_a_count_that_times_out_is_an_error_not_a_silent_sentinel(
    monkeypatch, reap, caplog
):
    """The count has a clock of its own. When it runs out the read is CANCELLED and awaited —
    nothing is left running behind the run — the field says NOT COUNTED, and `errors` goes up:
    failing to count is a failure and pages as one. `-1` with `errors=0` must not be possible on
    a run that printed a report."""
    await _start()
    seen = {}

    async def _hangs(**kwargs):
        seen["started"] = True
        try:
            await asyncio.sleep(8)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise
        finally:
            seen["finished"] = True

    monkeypatch.setattr(ledger, "count_stuck_purchases", _hangs)
    monkeypatch.setattr(job, "STUCK_COUNT_TIMEOUT_SECONDS", 0.05)
    caplog.set_level(logging.ERROR, logger="jobs.reap_agentic_purchase_poll")

    began = asyncio.get_running_loop().time()
    with capture_pivota_stdout() as out:
        report = await _run(worker_id="w1")
    elapsed = asyncio.get_running_loop().time() - began

    assert seen == {"started": True, "cancelled": True, "finished": True}, (
        "the timed-out read was not cancelled and awaited"
    )
    assert elapsed < 5, f"the run waited {elapsed:.1f}s on a 0.05s timeout"
    assert report.stuck_over_age == job.NOT_COUNTED
    assert report.errors == 1
    assert report.advanced == 1, "the diagnostic timing out must not undo the run's real work"
    assert "could not count stuck purchases (error_type=TimeoutError)" in caplog.text
    (line,) = [ln for ln in pivota_lines(out) if "PollReport(" in ln]
    assert "stuck_over_age=-1," in line and ", errors=1," in line


def test_the_count_timeout_fits_inside_the_run_deadline():
    """The arithmetic the constant's comment states, held against the numbers it depends on:
    the budget, the slowest step, this timeout, and the DB layer's own bound on handing a
    connection back after a cancelled statement. If any of them moves, re-derive."""
    from db.database import DB_POOL_CHECKOUT_TIMEOUT_SECONDS
    from services.audit_scheduler import _JOB_RUN_DEADLINES

    worst = (
        job.DIALS["poll_budget_seconds"].default
        + svc.QUOTING_STEP_BUDGET_S
        + job.STUCK_COUNT_TIMEOUT_SECONDS
        + DB_POOL_CHECKOUT_TIMEOUT_SECONDS
    )
    assert job.STUCK_COUNT_TIMEOUT_SECONDS == 10
    assert worst <= _JOB_RUN_DEADLINES["reap_agentic_purchase_poll"] - 30, worst


async def test_a_run_cancelled_during_the_count_still_propagates(monkeypatch, reap):
    """`wait_for` turns ITS OWN timeout into an `Exception`. A cancellation of the RUN — the
    deadline, a shutdown — is not one, and must keep travelling through step 5 as it does
    through step 4."""
    await _start()

    async def _cancelled(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ledger, "count_stuck_purchases", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _run(worker_id="w1")
    assert await _all_claims() == {}


async def test_the_stuck_read_is_given_the_windows_the_expire_sweep_was_given(monkeypatch, reap):
    """A waiting row's deadline is the sweep's own. Two readers of the two dials would be two
    rules, so the job hands the count exactly what it handed the sweep."""
    await _start()
    monkeypatch.setenv("REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS", "7200")
    monkeypatch.setenv("REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS", "240")
    seen = {}
    real_expire, real_count = ledger.expire_overdue_purchases, ledger.count_stuck_purchases

    async def _expire(**kwargs):
        seen["expire"] = kwargs
        return await real_expire(**kwargs)

    async def _count(**kwargs):
        seen["count"] = kwargs
        return await real_count(**kwargs)

    monkeypatch.setattr(ledger, "expire_overdue_purchases", _expire)
    monkeypatch.setattr(ledger, "count_stuck_purchases", _count)

    await _run(worker_id="w1")

    assert seen["count"] == {
        "stuck_after_seconds": job.STUCK_AFTER_SECONDS,
        "reconciliation_only": False,
        "max_age_seconds": 7200,
        "enrollment_grace_seconds": 240,
    }
    assert seen["count"]["max_age_seconds"] == seen["expire"]["max_age_seconds"]
    assert seen["count"]["enrollment_grace_seconds"] == seen["expire"]["enrollment_grace_seconds"]


# ══ 11. registration ══════════════════════════════════════════════════════════════════════════


class _RecordingScheduler:
    def __init__(self, *a, **k):
        self.added = []

    def add_job(self, func, *a, **k):
        self.added.append((k.get("id"), func, a, k))

    def start(self):
        pass

    def get_jobs(self):
        return []


async def _start_scheduler(monkeypatch, **env):
    import services.audit_scheduler as sched
    # Scheduling does not execute Stripe jobs. Prevent an unrelated Stripe SDK
    # constructor from creating its HTTP client while the no-network guard is active.
    import stripe
    from unittest.mock import MagicMock
    monkeypatch.setattr(stripe, "StripeClient", MagicMock())

    for k in ("AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT", "PIVOTA_ENV"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    rec = _RecordingScheduler()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: rec)
    await sched.start_scheduler()
    return sched, rec


async def test_the_job_is_registered_through_add_job_with_the_right_trigger(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_POLL_INTERVAL_SECONDS", "45")
    sched, rec = await _start_scheduler(monkeypatch, RAILWAY_SERVICE_NAME="web")

    entries = [e for e in rec.added if e[0] == "reap_agentic_purchase_poll"]
    assert len(entries) == 1, "registered zero or twice"
    _, func, args, kwargs = entries[0]
    assert args == ("interval",)
    assert kwargs["seconds"] == 45
    # `max_instances=1` EXPLICITLY: two overlapping runs would stack partner call chains on a
    # partner that rate-limits.
    assert kwargs["max_instances"] == 1
    assert kwargs["coalesce"] is True
    assert kwargs["misfire_grace_time"] == 45
    # Through `_add_job`, so it carries the isolating wrapper (issue #1754).
    assert getattr(func, "__wrapped_job_id__", None) == "reap_agentic_purchase_poll"


async def test_the_job_has_a_run_deadline_above_its_budget_plus_the_slowest_step(monkeypatch):
    """The deadline must clear budget + ONE WHOLE STEP, because the budget stops the job STARTING
    work, not the row already in flight.

    The first cut asserted `deadline > budget + 60` on an inherited "~40 s slowest step". That
    figure was wrong for `_step_quoting` after #2204's fix round: the real worst realistic step is
    170 s (see `test_the_lease_floor_is_above_the_slowest_step_not_the_ledgers_minimum`), so
    240 + 60 = 300 < 360 was arithmetic that happened to pass rather than a bound that held.
    """
    import services.audit_scheduler as sched

    deadline = sched._JOB_RUN_DEADLINES["reap_agentic_purchase_poll"]
    budget = job.DIALS["poll_budget_seconds"].default
    worst_step = (rc.MAX_SEARCH_ATTEMPTS + 1) * rc._DEFAULT_TIMEOUT_S + 2 * rc._QUOTE_TIMEOUT_S

    assert deadline > budget + worst_step, (deadline, budget, worst_step)
    assert deadline == 600
    # The isolation test's own ceiling, restated here so a raise lands in the right place.
    assert 0 < deadline <= 14400


async def test_the_job_is_not_registered_when_the_queue_worker_gate_is_false(monkeypatch):
    """The claim has no environment filter and this job spends a buyer's card, so it runs only
    where the queue-worker gate says so. (Written when prod and staging shared one Postgres and a
    staging service would have poached production purchases; each now has its own instance.)"""
    sched, rec = await _start_scheduler(monkeypatch, RAILWAY_SERVICE_NAME="web-staging")
    assert rec.added == []


async def test_registration_is_inert_with_the_dial_off(monkeypatch, reap):
    """The gate is INSIDE the job, so the job registers on a service where the rail is off — and
    running it does nothing. There is deliberately no second gate at registration."""
    await _start()
    sched, rec = await _start_scheduler(
        monkeypatch, RAILWAY_SERVICE_NAME="web", REAP_AGENTIC_ENABLED="0"
    )
    assert any(e[0] == "reap_agentic_purchase_poll" for e in rec.added)
    assert (await _run()).skipped_disabled == 1
    assert reap.calls == []


def test_the_job_id_is_force_runnable_and_pausable_by_an_operator():
    """The brief assumed routes/admin_scheduler_jobs.py worked for a new id without changes. IT
    DOES NOT — both run-now and pause/resume are ALLOWLISTS, and an id absent from them gets a
    403. So the id was added to both, and this test is what stops a rename silently taking the
    operator's levers away again."""
    from routes.admin_scheduler_jobs import _MANAGEABLE_JOB_IDS, _RUNNABLE_JOB_IDS

    assert "reap_agentic_purchase_poll" in _RUNNABLE_JOB_IDS
    assert "reap_agentic_purchase_poll" in _MANAGEABLE_JOB_IDS


# ══ 2026-09-30: the enrollment grace reaches the sweep ════════════════════════════════════════


async def _waiting_on_an_enrollment_link(seconds_past: int) -> str:
    purchase_id = await _start()
    await _raw(
        "UPDATE reap_agentic_purchases SET state = 'needs_enrollment', "
        "hosted_url_expires_at = datetime('now', :shift) WHERE id = :i",
        {"shift": f"-{int(seconds_past)} seconds", "i": purchase_id},
    )
    return purchase_id


async def test_the_job_leaves_a_needs_enrollment_row_alone_inside_the_grace(monkeypatch, reap):
    """Staging 2026-09-30: the sweep ran 28 s after the link expired and 19 s after Reap had
    turned the enrollment ACTIVE. With the job passing the grace, that row survives the sweep.
    The rail is OFF so the only thing that can move the row is the sweep itself."""
    purchase_id = await _waiting_on_an_enrollment_link(28)
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")
    report = await _run(worker_id="w1")
    assert report.expired == 0
    assert (await _get(purchase_id))["state"] == "needs_enrollment"


async def test_the_job_reads_the_grace_dial_from_the_state_machine(monkeypatch, reap):
    """ONE reader: `REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS=0` is Reap's exact expiry again, and
    the job hands exactly the service's value to the ledger."""
    monkeypatch.setenv(svc.REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS_ENV, "0")
    purchase_id = await _waiting_on_an_enrollment_link(28)
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")
    seen = []
    real = job.ledger.expire_overdue_purchases

    async def _spy(**kwargs):
        seen.append(kwargs["enrollment_grace_seconds"])
        return await real(**kwargs)

    monkeypatch.setattr(job.ledger, "expire_overdue_purchases", _spy)
    report = await _run(worker_id="w1")
    assert seen and set(seen) == {0}
    assert report.expired == 1
    assert (await _get(purchase_id))["state"] == "expired"


@pytest.mark.parametrize("pause", ["off", "malformed_scope"])
@pytest.mark.parametrize("queued_state", ["resolving", "needs_enrollment", "quoting"])
async def test_create_pause_keeps_precheckout_queued_and_reconciles_exposed_checkout(monkeypatch, reap, pause, queued_state):
    import db.reap_agentic_ledger as ledger
    queued = await _start(buyer_ref="bref_queue_pause")
    await _raw("UPDATE reap_agentic_purchases SET state=:state WHERE id=:id", {"state": queued_state, "id": queued})
    exposed = await _start(buyer_ref="bref_checkout_pause")
    await _raw("UPDATE reap_agentic_purchases SET state='awaiting_approval', reap_checkout_id='chk_paused', next_poll_at=:due WHERE id=:id",
               {"id": exposed, "due": datetime.now(timezone.utc) - timedelta(seconds=1)})
    if pause == "off":
        monkeypatch.setenv("REAP_AGENTIC_CREATE_ENABLED", "0")
    else:
        monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", "malformed")
    report = await _run()
    assert report.skipped_disabled == 1
    assert (await ledger.get_purchase_internal(queued))["state"] == queued_state
    assert (await ledger.get_purchase_internal(queued))["attempts"] == 0
    assert (await ledger.get_purchase_internal(exposed))["state"] == "completed"
    assert [name for name, _ in reap.calls] == ["get_checkout"]


@pytest.mark.parametrize("queued_state", ["resolving", "needs_enrollment", "quoting"])
async def test_narrowed_pilot_scope_prevents_queued_provider_work_but_reads_exposed_checkout(monkeypatch, reap, queued_state):
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    import json
    import db.reap_agentic_ledger as ledger
    queued = await _start(buyer_ref="bref_queue_scope")
    exposed = await _start(buyer_ref="bref_checkout_scope")
    await _raw("UPDATE reap_agentic_purchases SET state=:state WHERE id=:id", {"state": queued_state, "id": queued})
    await _raw("UPDATE reap_agentic_purchases SET state='awaiting_approval', reap_checkout_id='chk_scope', next_poll_at=:due WHERE id=:id",
               {"id": exposed, "due": datetime.now(timezone.utc) - timedelta(seconds=1)})
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps({
        "agent_ids": ["different-agent"], "merchant_domains": ["brand.example"], "markets": ["US"],
        "product_keys": ["pk_1"], "quantities": [1]}))
    await _run()
    pending = await ledger.get_purchase_internal(queued)
    assert pending["state"] == queued_state
    assert pending["last_error_code"] is None
    assert pending["attempts"] == 0
    assert (await ledger.get_purchase_internal(exposed))["state"] == "completed"
    assert [name for name, _ in reap.calls] == ["get_checkout"]


async def test_matching_pilot_scope_allows_queued_provider_progress(monkeypatch, reap):
    import json
    import db.reap_agentic_ledger as ledger
    purchase = await _start()
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps({
        "agent_ids": ["agent_one"], "merchant_domains": ["brand.example"], "markets": ["US"],
        "product_keys": ["pk_1"], "quantities": [1]}))
    await _run()
    assert (await ledger.get_purchase_internal(purchase))["state"] == "needs_enrollment"
    assert "create_enrollment" in [name for name, _ in reap.calls]
