"""The POLLER for the Reap agentic purchase rail (WP3).

This is the thing that makes WP2b's state machine *run*. It owns no SQL beyond two read-only
bookkeeping SELECTs, no HTTP, and no transitions — it is a LOOP and a set of BOUNDS:

    db/reap_agentic_ledger.py        claims, releases, and the three bulk sweeps.
    services/reap_agentic_purchase.py `advance(purchase_id, worker_id)` — ONE fenced step on a row
                                      this job has ALREADY CLAIMED.
    services/audit_scheduler.py       registers `reap_agentic_purchase_poll` through `_add_job`.

── THE ORDER OF ONE RUN, AND WHY IT IS THAT ORDER ───────────────────────────────────────────

  1. `requeue_stale_claims`     free the leases of workers that died mid-step.       ALWAYS
  2. `expire_overdue_purchases` the PII deadline for the two states that wait on a PERSON. ALWAYS
  3. `fail_exhausted_purchases` the attempt ceiling for the states where a claim means we TRIED.
                                                                                     ALWAYS
  3b. count the 'processing' rows at or over the attempt ceiling — READ ONLY.         ALWAYS
  4. `claim_due_purchases` → `advance` → `release_claim`, one row at a time.  ONLY WHEN ARMED

THE SWEEPS RUN BEFORE THE CLAIM, AND THAT IS THE WHOLE POINT OF THE ORDER. Claiming first means
this run takes a lease on rows that steps 1–3 were about to recover or terminate, and then spends
a partner round trip on each of them before finding out. Worse for step 1: a row whose previous
worker died is NOT claimable until the requeue frees it, so a claim-first run would skip exactly
the rows that most need attention and leave them for the next tick — every tick, forever, on a
pod that keeps dying. Sweep first, then claim what is left.

── WHAT THE DIAL GATES, AND WHAT IT MUST NOT ────────────────────────────────────────────────

`REAP_AGENTIC_ENABLED` (plus a configured client) gates STEP 4 AND ONLY STEP 4.

The first cut gated the whole run, and that was a PII RETENTION BUG, measured: with the rail off,
an `awaiting_approval` row 99,999 seconds old kept `buyer_email` and `shipping_address` across
three consecutive ticks, because the expire sweep never ran. The gate is
`is_enabled() and is_configured()`, so a CREDENTIAL BLIP — one unset env var — had the same
effect as an operator disabling the feature: the deadline stopped.

Steps 1–3 are safe to run unarmed and that is checkable rather than asserted:

  * none of them calls a partner. They are three UPDATE statements in db/reap_agentic_ledger.py.
  * none of them can touch a row whose payment is in flight. `expire_overdue_purchases` names
    only 'needs_enrollment' and 'awaiting_approval'; `fail_exhausted_purchases` runs with
    `include_processing=False`, and both state lists are parsed out of the SQL that enforces
    them (`ledger._states_in`), so this is not a claim about a comment.
  * `requeue_stale_claims` only clears a dead worker's lease. It writes no state.

So the rule is: THE DIAL STOPS US TALKING TO A PARTNER. It does not stop us keeping our promises
about a buyer's data. A rail an operator has switched off must still forget people.

── THE INVARIANT THIS JOB GUARANTEES ────────────────────────────────────────────────────────

AFTER STEP 4, THIS WORKER HOLDS NO CLAIMS — including when step 4 did not finish.

That last clause is the whole of fix F1/F2. The first cut had no `try/finally`, and two measured
paths leaked claims for a full lease on both dialects:

  * the scheduler's DEADLINE cancelling the run on the 2nd of 4 advances left 3 rows claimed;
  * a raising `release_claim`, or a raising leftover SELECT, propagated out and left ALL rows
    claimed.

A leaked claim is not merely untidy. `requeue_stale_claims` will not touch it for
`REAP_AGENTIC_LEASE_SECONDS`, and the two states with no attempt counter
('needs_enrollment', 'awaiting_approval') sit there holding a buyer's address and email for that
whole window — the same retention hole as the gate bug, by a different route.

So: the row loop AND the leftover cleanup live in one `try/finally`, the cleanup runs in the
`finally` (so it runs on a cancel too), and it gets a second attempt of its own. The cleanup is
bounded by `REAP_AGENTIC_CLAIM_BATCH` fenced UPDATEs, which fits comfortably inside the runner's
5-second cancellation grace window (`services.scheduler_job_runner.DEFAULT_CANCEL_GRACE_SECONDS`);
the runner calls `task.cancel()` exactly once and then waits, so awaits inside this `finally`
complete normally.

`except Exception`, NEVER `except BaseException`. `asyncio.CancelledError` is a `BaseException`
on modern Python precisely so that cleanup handlers do not swallow it: a run that caught a
deadline cancellation and carried on would defeat the watchdog the whole scheduler is built
around (#1754). It propagates, and the `finally` still releases every claim on the way out.
`test_a_cancelled_run_propagates_and_still_releases_every_claim` pins both halves.

The claim is NOT released by every path through `advance`, which is why the release here is
unconditional rather than conditional on the outcome:

    advanced to a NON-TERMINAL state   the claim is STILL HELD — `_TRANSITION_SQL` clears
                                       `claimed_by` only on a terminal target.
    advanced to a TERMINAL state       cleared by the same UPDATE that wrote the state.
    released                           the service gave the lease back itself, with a schedule.
    lost_claim / terminal / missing     nothing of ours to give back.

Releasing again is a fenced no-op, so one unconditional `release_claim` per row is correct for
all five and cannot be got wrong by a future outcome nobody updated this list for. It is issued
WITHOUT `next_poll_at`, so the schedule the service chose (`_next_poll_for`, or `_release`'s
backoff table) survives — this job never overrides the state machine's own cadence, except on the
exception path below, where the state machine did not get to choose one.

── ROWS ARE PROCESSED SEQUENTIALLY, AND ONE STEP IS SLOW ────────────────────────────────────

One partner call chain at a time per worker. Reap is slow and rate-limits; `asyncio.gather` over
a batch would multiply the concurrent load by `REAP_AGENTIC_CLAIM_BATCH` against a partner we do
not control, on a rail that spends a buyer's own card.

THE SLOWEST SINGLE STEP IS 'quoting', AND THE NUMBER IS BIGGER THAN IT LOOKS. Re-derived against
`services/reap_agentic_purchase._step_quoting` as it stands, not inherited:

    rc.resolve_our_row   up to `rc.MAX_SEARCH_ATTEMPTS` (3) `products/search` calls plus one
                         `products/variant`, each with a 25 s read timeout   →  100 s
    rc.request_quote     35 s read timeout (13–16 s measured across nine merchants)
    rc.create_checkout   35 s read timeout
                                                                            ────────
                                                              worst realistic   170 s

plus three `_still_ours` re-reads the fix round added, which are local statements. The STRICT
bound is four times that: `services/reap_agentic_client` passes httpx a BARE FLOAT timeout
(`httpx.AsyncClient(timeout=timeout)`), and httpx spreads a bare float across connect, read,
write AND pool as that value EACH — the same trap `agent_card_revocation_sweep`'s deadline note
in services/audit_scheduler.py describes. Typical is ~30–40 s; 170 s is the number to size
bounds against, and ~680 s is the number to remember before calling any bound here a guarantee.

Everything downstream follows from that one figure: the lease FLOOR (180), the run deadline
(600 = the 240 s budget + 170 s worst step + margin for the sweeps), and the standing caveat
that the deadline is a backstop against a WEDGE, not a promise that a batch completes.

── THIS JOB OPENS NO TRANSACTION, DELIBERATELY ──────────────────────────────────────────────

Property 6 of the ledger's module header: on Postgres `CURRENT_TIMESTAMP` is TRANSACTION-start
time, so a poll loop wrapped in one transaction would compare every row against the clock as it
was when the run began. The ledger dodges that with `clock_timestamp()`, but the sweeps' own
bounded-UPDATE-plus-loop shape also depends on each statement committing before the next one
selects; and on `databases==0.7.0` a transaction here would additionally serialise the whole run
onto one connection's lock. There is no `database.transaction()` in this file and a test asserts
that from the source.

── PII ──────────────────────────────────────────────────────────────────────────────────────

`PollReport` carries COUNTS AND A DURATION. No purchase ids, no buyer refs, no agent ids, nothing
from a row. Purchase ids appear in DEBUG logs (per-row progress) and in the ERROR lines that an
operator needs to act on. No log line in this file interpolates a row: `buyer_email` and
`shipping_address` are read by `advance` and never returned to it.

── WHAT THIS JOB HAS NOT DONE ───────────────────────────────────────────────────────────────

Talked to a partner. The client is the only thing that can, the dial defaults OFF, and the worker
service that would run this is deployed separately — see docs/runbooks/reap_agentic_purchase.md.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Union

import db.reap_agentic_ledger as ledger
import services.reap_agentic_client as rc
import services.reap_agentic_purchase as purchase_svc
from db.database import database
from utils.logger import logger as operator_logger

logger = logging.getLogger(__name__)

# THE PER-RUN REPORT GOES THROUGH THE "pivota" LOGGER, NOT THE MODULE LOGGER. Measured in prod on
# 2026-09-23: /__scheduler_health showed 155 ok runs of this job and Cloud Logging held ZERO
# `reap_agentic_poll:` report lines. Nothing configures the root logger in this process, so it
# sits at WARNING and a module logger's INFO is dropped at the logger. `utils.logger` carries its
# own INFO level and stdout handler (propagate=False). Only the report line goes this way; the
# per-row errors and dial warnings stay on the module logger, where WARNING and above still land.
# See jobs/merchant_purchasability_sweep.py for the longer note and why root is left alone.

__all__ = [
    "DIALS",
    "PollReport",
    "job_interval_seconds",
    "run_reap_agentic_purchase_poll",
]


# ── the dials ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Dial:
    """One integer env dial: its name, its code default, and its bounds.

    A TABLE rather than eight `_env_int` calls with inline numbers, so that the bounds are data a
    test can iterate. `test_every_dial_falls_back_to_its_default_on_a_bad_value` walks `DIALS` and
    feeds each one six bad values; a dial added without bounds is then a test failure rather than
    a value nobody checks.
    """

    env: str
    default: int
    minimum: int
    maximum: int


#: Every dial, read PER RUN and never cached at import. The rail's own switch
#: (`REAP_AGENTIC_ENABLED`) is not here: it is a boolean and it belongs to the state machine,
#: which already owns the one authoritative reader (`services.reap_agentic_purchase.is_enabled`).
#:
#: The bounds are not decoration. `REAP_AGENTIC_LEASE_SECONDS` below the ledger's 30 is a
#: ValueError out of `requeue_stale_claims` (the ledger refuses a silently-raised floor), and
#: `REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS` below 60 is a ValueError out of
#: `expire_overdue_purchases` — so a dial this module let through unvalidated would not be a bad
#: setting, it would be an exception out of a scheduled job on every tick. The minimums here are
#: therefore AT OR ABOVE the ledger's own floors on purpose, and in one case well above.
DIALS: Dict[str, _Dial] = {
    # Registration-time only: the interval `_add_job` is given. Changing it needs a restart,
    # which is why it is the one dial that is not read inside the run.
    "poll_interval_seconds": _Dial("REAP_AGENTIC_POLL_INTERVAL_SECONDS", 30, 5, 3600),
    # Rows claimed per run. Capped at 100 because every row is a SERIAL partner call chain whose
    # worst realistic length is 170 s — a batch of 500 could not finish inside any sane budget
    # and would simply hold 500 leases.
    "claim_batch": _Dial("REAP_AGENTIC_CLAIM_BATCH", 10, 1, 100),
    # THE FLOOR IS 180, NOT THE LEDGER'S 30, AND THAT IS FIX F4. A lease shorter than one step
    # means a LIVE worker gets its own row requeued underneath it: the requeue frees the lease,
    # a second worker claims the same purchase, and both call the partner. The fence still stops
    # the double WRITE — only one transition wins — but nothing stops the second QUOTE, and a
    # quote is a real request against a merchant's commerce layer.
    #
    # 180 is the worst realistic 'quoting' step (170 s, derived in the module header) rounded up.
    # The default 300 is ~1.8x it. Both are BELOW the strict httpx four-phase bound of ~680 s,
    # which no floor can sensibly cover; the residual risk is a duplicated partner call on a
    # pathologically slow row, not a duplicated charge.
    "lease_seconds": _Dial("REAP_AGENTIC_LEASE_SECONDS", 300, 180, 3600),
    # The PII deadline for `needs_enrollment` / `awaiting_approval`. The ledger's floor is 60.
    "hosted_max_age_seconds": _Dial("REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS", 3600, 60, 2592000),
    # Attempts are counted at CLAIM and only in resolving/quoting/processing, so this is a count
    # of tries, not of ticks. The ledger's floor is 1.
    "max_attempts": _Dial("REAP_AGENTIC_MAX_ATTEMPTS", 50, 1, 10000),
    # How long a row waits after `advance` RAISED. Not the state machine's backoff table — this
    # is the path where the state machine did not get to choose, so the poller must.
    "error_backoff_seconds": _Dial("REAP_AGENTIC_ERROR_BACKOFF_SECONDS", 120, 1, 3600),
    # Wall-clock budget for one run. It stops the job STARTING new work; a row already in flight
    # runs to its own completion, so a run can legitimately exceed this by one step. The job's
    # entry in `_JOB_RUN_DEADLINES` is sized as budget + that one step.
    "poll_budget_seconds": _Dial("REAP_AGENTIC_POLL_BUDGET_SECONDS", 240, 10, 3600),
}

#: Rows per bulk-sweep statement. Not a dial: the ledger caps `limit` at 500 and the reason the
#: sweeps are bounded at all is a lock window on a Postgres prod and staging SHARE — that is a
#: property of the deployment, not something an operator should be tuning from an env var.
SWEEP_BATCH = 200

#: Hard ceiling on how many times a sweep's "loop until fewer than `limit` come back" may go
#: round. Without it, a sweep whose predicate a future migration makes permanently true (a column
#: default that re-qualifies every row it just wrote, say) is an infinite loop inside a scheduled
#: job — which on this scheduler means the run burns its whole deadline and `max_instances=1`
#: skips every tick behind it. 200 * 20 = 4,000 rows per sweep per run, and a backlog larger than
#: that drains over the following ticks, which is the same answer every bounded sweep in this
#: repo gives.
MAX_SWEEP_ITERATIONS = 20


#: Dial names already warned about IN THIS PROCESS. A bad dial is a STANDING condition, not an
#: event: at a 30 s interval the first cut logged the same warning 2,880 times a day, which is
#: how a real warning becomes something an operator filters out. Warned once, at the level that
#: means "somebody must fix this", and then silent. Per-process rather than per-run because the
#: env does not change under a running process without a restart — and a restart clears this.
_WARNED_DIALS: Set[str] = set()


def _env_int(dial: _Dial) -> int:
    """One dial's value, or its default WITH A WARNING NAMING THE VARIABLE (once per process).

    NEVER RAISES AND NEVER SILENTLY ZEROES. This runs inside a scheduled job on a rail whose
    whole point is to be safe when it is misconfigured: a typo in
    `REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS` must not take the PII deadline out of service, and
    reading `""` as 0 would hand `expire_overdue_purchases` a value it raises on — the same
    outage by a quieter route.

    `int(...)` is used here rather than the ledger's strict `_require_int` because the input is a
    STRING from the environment, where "300" is the only way to express 300. The bounds check
    afterwards is what the strictness is for.
    """
    raw = (os.getenv(dial.env) or "").strip()
    if not raw:
        return dial.default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _warn_once(
            dial.env,
            "reap_agentic_poll: %s is not an integer (%r); using the default %d "
            "(this is logged once per process)",
            dial.env,
            raw,
            dial.default,
        )
        return dial.default
    if value < dial.minimum or value > dial.maximum:
        _warn_once(
            dial.env,
            "reap_agentic_poll: %s=%d is outside %d..%d; using the default %d "
            "(this is logged once per process)",
            dial.env,
            value,
            dial.minimum,
            dial.maximum,
            dial.default,
        )
        return dial.default
    return value


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _WARNED_DIALS:
        return
    _WARNED_DIALS.add(key)
    logger.warning(message, *args)


def _reset_dial_warnings() -> None:
    """Test seam. Named for what it is rather than hidden behind a monkeypatch of a private set,
    so a test that needs the warning again asks for it in one readable line."""
    _WARNED_DIALS.clear()


def job_interval_seconds() -> int:
    """The registration interval. Read ONCE, by `services.audit_scheduler.start_scheduler`.

    Separated from the per-run dials because it is a different kind of value: an APScheduler
    trigger is fixed when the job is added, so changing this needs a process restart. Saying so
    with a function of its own is cheaper than a comment nobody reads at the call site.
    """
    return _env_int(DIALS["poll_interval_seconds"])


# ── the report ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PollReport:
    """What one run did. COUNTS ONLY — no ids, no rows, no PII. See the module header.

    `skipped_disabled` is 1 when STEP 4 was skipped because the rail is disarmed. It is NOT "the
    run did nothing": the three sweeps above it ran anyway, and their counts are real. It is a
    COUNT rather than a boolean so that a caller summing reports across ticks can see "step 4 has
    been inert for 400 runs", which is the question an operator actually asks after arming
    something and seeing nothing happen.

    `errors` is the only count that should ever page anyone. `processing_over_attempts` is the
    one that should ever WAKE anyone.

    'processing' IS THE ONLY POLLABLE STATE WITH NO BOUND AT ALL, and that is derived from the
    ledger's own SQL rather than asserted here — see
    `test_processing_is_the_only_state_with_no_bound`, which parses
    `_EXPIRE_SOURCE_STATES` / `_FAIL_EXHAUSTED_SOURCE_STATES` / `_CLAIM_ATTEMPT_EXEMPT_STATES`
    out of the statements that enforce them. `expire_overdue_purchases` does not name it, so it
    has NO PII DEADLINE; `fail_exhausted_purchases` skips it, so the counter never terminates it.
    #2204's round-2 review measured exactly what that costs: a row parked in 'processing' and
    aged to 2020 still held `buyer_email` and `shipping_address`. That fix removed the one path
    which parked rows there deliberately; it did not, and could not, bound the state.

    So this count is not a nicety. It is the ONLY signal that a buyer's payment is stuck in
    flight — and, with it, their address and email. A number that does not fall needs a person.
    The first cut reported nothing at all for those rows: measured at attempts=100,005 on a row
    no count mentioned.
    """

    requeued: int = 0
    expired: int = 0
    failed_exhausted: int = 0
    processing_over_attempts: int = 0
    claimed: int = 0
    advanced: int = 0
    released: int = 0
    abandoned_budget: int = 0
    lost_claim: int = 0
    terminal: int = 0
    errors: int = 0
    skipped_disabled: int = 0
    duration_ms: int = 0


_COUNTS = (
    "requeued",
    "expired",
    "failed_exhausted",
    "processing_over_attempts",
    "claimed",
    "advanced",
    "released",
    "abandoned_budget",
    "lost_claim",
    "terminal",
    "errors",
    "skipped_disabled",
)

#: Every purchase this worker still holds a lease on, IN ANY STATE. A MODULE-LEVEL CONSTANT, not
#: an f-string: tests/test_repo_sql_prepare_postgres.py resolves a `database.*` first argument
#: only when it is a literal or a module-level name, and a statement on a live path that nothing
#: ever PREPAREs is the #1588 shape this repo already paid for once.
#:
#: THERE IS NO STATE FILTER, AND THAT IS THE SAME FIX `requeue_stale_claims` CARRIES. A claim
#: left on a TERMINAL row is garbage to clear, not work to protect, and filtering it out would
#: make it unclearable forever (the ledger measured exactly that: a 99,999-second-old claim on a
#: completed purchase freed 0 rows). Clearing it cannot resurrect anything, because
#: `_CLAIM_PURCHASE_SQL` carries its own state conjunct.
#:
#: One statement for both dialects — no timestamp, no cast, no interval, so nothing here needs
#: the dialect split every other statement on this rail carries.
_LEFTOVER_CLAIMS_SQL = """
    SELECT id FROM reap_agentic_purchases
     WHERE claimed_by = :worker_id
"""

#: Rows whose payment is in flight AND whose attempt counter is past the ceiling — the exact set
#: `fail_exhausted_purchases(include_processing=False)` refuses to terminate, on purpose. READ
#: ONLY. Reported so that the refusal is visible rather than silent: the alternative to a count
#: is an operator discovering a stuck charge when the buyer complains.
#:
#: 'processing' also has NO PII DEADLINE — `expire_overdue_purchases` does not name it — so these
#: rows hold `buyer_email` and `shipping_address` for as long as they sit there. See `PollReport`.
_PROCESSING_OVER_ATTEMPTS_SQL = """
    SELECT COUNT(*) AS stuck FROM reap_agentic_purchases
     WHERE state = 'processing'
       AND attempts >= :max_attempts
"""


# ── small helpers ────────────────────────────────────────────────────────────────────────────

#: Indirected so a test can make the budget elapse without sleeping. MONOTONIC, not wall clock:
#: the budget must survive an NTP step, and it must not be affected by the injectable `now` —
#: those two are different clocks answering different questions, and conflating them would let a
#: caller that froze `now` for a schedule assertion also disable the budget.
_monotonic: Callable[[], float] = time.monotonic


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_now(now: Union[None, datetime, Callable[[], datetime]]) -> Callable[[], datetime]:
    """Normalise the `now` parameter to a CALLABLE.

    Both forms are accepted because both are natural at a call site: a test pinning one instant
    passes a `datetime`, a test advancing a clock passes a factory. A fixed datetime is wrapped
    rather than refused — refusing it would be a correctness rule with no correctness behind it.
    """
    if now is None:
        return _default_now
    if callable(now):
        return now
    fixed = now
    return lambda: fixed


def _worker_id() -> str:
    """`<hostname>:<pid>:<8 hex>`.

    UNIQUE PER RUN, and the uuid is why. Two runs of this job in one process — a scheduled tick
    overlapping an operator's `run-now`, which `max_instances=1` prevents for the scheduler but
    not across the admin path — would otherwise share a worker id, and the claim fence
    (`claimed_by = :worker_id`) would stop distinguishing them: each run's `release_claim` could
    free the other's live lease, and the leftover-claims cleanup at the end of one run would
    "recover" rows the other is mid-partner-call on.

    The hostname and pid are there for the operator reading `claimed_by` in a database, not for
    correctness. `socket.gethostname()` is a container name on this platform, not a buyer or a
    person; it is the same value every other lease-taking job in this repo writes.
    """
    try:
        host = socket.gethostname() or "unknown"
    except Exception:  # noqa: BLE001 — a hostname lookup must never fail a poll run
        host = "unknown"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def _sweep_until_drained(
    call: Callable[[int], Any], limit: int, label: str, budget_spent: Callable[[], bool]
) -> int:
    """Run a bounded bulk sweep until it returns fewer than `limit` ids, the cap is hit, or the
    RUN BUDGET IS SPENT.

    Both sweeps return THE IDS THEY MOVED, and "fewer than `limit`" is the only honest way to ask
    whether the backlog is drained — the ledger's statements have no count to report otherwise
    (`execute()` of an UPDATE answers None on asyncpg; see property 1 of the ledger header).

    THE BUDGET CHECK IS FIX F5 AND IT IS NOT COSMETIC. Without it the cap was the only bound, so
    a backlog could run 20 statements here and 20 more in the next sweep — measured at 40
    statements issued with the budget already spent, against a Postgres prod and staging share,
    after which the run had nothing left for the work it exists to do. The cap still stands
    behind it: the budget bounds a REAL backlog, the cap bounds a sweep that has stopped
    converging.

    The ids are counted and DROPPED. They are purchase ids, so they are not PII, but the report
    carries counts and a caller that wanted a list would be one refactor away from logging it.
    """
    total = 0
    for iteration in range(MAX_SWEEP_ITERATIONS):
        if budget_spent():
            logger.warning(
                "reap_agentic_poll: %s stopped after %d rows — the run budget was spent "
                "mid-sweep; the rest drains on the next tick",
                label,
                total,
            )
            return total
        moved = await call(limit)
        total += len(moved)
        if len(moved) < limit:
            return total
        logger.info(
            "reap_agentic_poll: %s swept a full batch of %d (iteration %d); looping",
            label,
            limit,
            iteration + 1,
        )
    logger.warning(
        "reap_agentic_poll: %s hit the %d-iteration cap after %d rows; the rest drains on the "
        "next tick",
        label,
        MAX_SWEEP_ITERATIONS,
        total,
    )
    return total


async def _release_leftovers(worker: str) -> int:
    """Release every claim this worker still holds. Returns how many there were.

    RUNS IN A `finally`, INCLUDING ON A CANCEL, so it is written to survive things going wrong
    rather than to be elegant:

      * TWO ATTEMPTS at the SELECT. The first cut had one, outside any guard, so a pool blip
        there abandoned every lease the run was holding. One retry is the difference between "a
        transient error costs a lease" and "a transient error costs a whole lease window of a
        buyer's PII".
      * EACH RELEASE IS GUARDED ON ITS OWN. One row that will not release must not strand the
        rows behind it — the same rule jobs/agent_card_revocation_sweep.py states as rule 3.
      * `except Exception`, never `BaseException`: a cancellation must keep travelling.

    Anything it finds is a bug in the loop above, which is why the caller counts the result under
    `errors`. But it is RELEASED first: leaving a row stuck for a whole lease to make a point
    helps nobody.
    """
    leftovers: List[Any] = []
    for attempt in (1, 2):
        try:
            leftovers = list(await database.fetch_all(_LEFTOVER_CLAIMS_SQL, {"worker_id": worker}))
            break
        except Exception as exc:  # noqa: BLE001 — see the docstring
            logger.error(
                "reap_agentic_poll: could not read this worker's leftover claims "
                "(attempt %d/2, error_type=%s)",
                attempt,
                type(exc).__name__,
            )
    else:
        logger.error(
            "reap_agentic_poll: GAVE UP reading leftover claims; any lease this run still holds "
            "is now the requeue's problem and will sit for a full lease window"
        )
        return 0

    released = 0
    for leftover in leftovers:
        purchase_id = str(leftover["id"])
        logger.error(
            "reap_agentic_poll: purchase=%s was STILL CLAIMED after the loop; releasing it. "
            "This is a bug in the poller, not in the ledger",
            purchase_id,
        )
        try:
            await ledger.release_claim(purchase_id, worker)
        except Exception as exc:  # noqa: BLE001 — one bad row must not strand the rest
            logger.error(
                "reap_agentic_poll: purchase=%s could not be released (error_type=%s); the "
                "requeue will free it after the lease expires",
                purchase_id,
                type(exc).__name__,
            )
        released += 1
    return released


# ── the run ──────────────────────────────────────────────────────────────────────────────────


async def run_reap_agentic_purchase_poll(
    *,
    worker_id: Optional[str] = None,
    now: Union[None, datetime, Callable[[], datetime]] = None,
) -> PollReport:
    """One poll tick. See the module header for the order, the gate and the invariant.

    `worker_id` MUST BE UNIQUE PER CONCURRENT RUN — it is the claim fence, not a label. The
    default mints one; a caller passing its own owns that property.

    Returns a `PollReport` and never raises for anything a purchase did. A driver exception out
    of `advance` is caught per row, counted, and the row's claim is released with a backoff; one
    bad row must not end the batch, for the same reason it must not in
    jobs/agent_card_revocation_sweep.py — the rows behind it are the ones holding PII.

    It DOES raise `asyncio.CancelledError`, on purpose: see the module header.
    """
    started = _monotonic()
    clock = _resolve_now(now)

    worker = worker_id or _worker_id()
    claim_batch = _env_int(DIALS["claim_batch"])
    lease_seconds = _env_int(DIALS["lease_seconds"])
    hosted_max_age = _env_int(DIALS["hosted_max_age_seconds"])
    max_attempts = _env_int(DIALS["max_attempts"])
    error_backoff = _env_int(DIALS["error_backoff_seconds"])
    budget_seconds = _env_int(DIALS["poll_budget_seconds"])

    counts = {name: 0 for name in _COUNTS}

    def _budget_spent() -> bool:
        return (_monotonic() - started) >= budget_seconds

    def _report() -> PollReport:
        return PollReport(duration_ms=int((_monotonic() - started) * 1000), **counts)

    # ── 1. stale leases — ALWAYS, ARMED OR NOT ───────────────────────────────────────────────
    # Bounded by ONE statement rather than a drain loop: a requeue that keeps finding full
    # batches means workers are dying faster than they poll, and freeing 4,000 leases in that
    # situation just hands them to the same failing workers. One batch per tick is the right
    # rate, and the rows are ordered oldest-claim-first, so nothing starves.
    counts["requeued"] = await ledger.requeue_stale_claims(
        lease_seconds=lease_seconds, limit=SWEEP_BATCH
    )

    # ── 2. the PII deadline — ALWAYS, ARMED OR NOT ───────────────────────────────────────────
    async def _expire(limit: int) -> List[str]:
        return await ledger.expire_overdue_purchases(
            max_age_seconds=hosted_max_age, limit=limit
        )

    counts["expired"] = await _sweep_until_drained(
        _expire, SWEEP_BATCH, "expire_overdue", _budget_spent
    )

    # ── 3. the attempt ceiling — ALWAYS, ARMED OR NOT ────────────────────────────────────────
    async def _fail_exhausted(limit: int) -> List[str]:
        # `include_processing=False`, ALWAYS, and it is not configurable from here. A purchase in
        # 'processing' has been APPROVED BY THE BUYER and its payment is in flight with Reap;
        # auto-failing it on a counter writes 'failed' over a charge whose outcome we do not
        # know, so our ledger says the purchase failed while the buyer's card says otherwise.
        # A PAYMENT STUCK IN FLIGHT IS A HUMAN DECISION: an operator who has reconciled the
        # checkout with Reap calls `fail_exhausted_purchases(..., include_processing=True)` by
        # hand, or transitions the row. There is deliberately no env var for it — a dial would
        # let somebody arm it once and forget, which is the same as not having decided.
        #
        # It is also why step 3b exists: a refusal nobody can see is indistinguishable from a
        # sweep that had nothing to do.
        return await ledger.fail_exhausted_purchases(
            max_attempts, limit=limit, include_processing=False
        )

    counts["failed_exhausted"] = await _sweep_until_drained(
        _fail_exhausted, SWEEP_BATCH, "fail_exhausted", _budget_spent
    )

    # ── 3b. make step 3's refusal visible — READ ONLY, ALWAYS ────────────────────────────────
    try:
        stuck = await database.fetch_one(
            _PROCESSING_OVER_ATTEMPTS_SQL, {"max_attempts": max_attempts}
        )
        # `stuck["stuck"]`, not `.get(...)`: a `databases` Record supports __getitem__ and has no
        # `.get`, and the first cut of this line swallowed its own AttributeError into the guard
        # below — a diagnostic that reported itself as broken on every tick.
        counts["processing_over_attempts"] = int(stuck["stuck"]) if stuck is not None else 0
        if counts["processing_over_attempts"]:
            logger.error(
                "reap_agentic_poll: %d purchase(s) are in 'processing' at or past "
                "max_attempts=%d. The counter will NEVER fail these — a payment is in flight "
                "and a human must reconcile them with Reap",
                counts["processing_over_attempts"],
                max_attempts,
            )
    except Exception as exc:  # noqa: BLE001 — a diagnostic must never fail the run that carries it
        counts["errors"] += 1
        logger.error(
            "reap_agentic_poll: could not count stuck 'processing' rows (error_type=%s)",
            type(exc).__name__,
        )

    # ── 4. claim, step, release — ONLY WHEN ARMED ────────────────────────────────────────────
    #
    # THE GATE IS INSIDE THE JOB, NOT AT REGISTRATION. The job is ALWAYS registered, so deploying
    # this is inert rather than absent: an operator arms the rail with an env var and the next
    # tick picks it up, with no redeploy and no scheduler restart. That is the shape
    # services/external_conversion_poller.py already uses (`_poller_enabled`), and it is why
    # there is no second gate in `start_scheduler` — two gates for one concept is two things to
    # get out of step.
    #
    # `is_enabled()` is REUSED from the state machine rather than re-read here. It is the
    # allowlist-of-truthy-spellings reader that decides whether `start_purchase` will open a
    # purchase at all, and a poller that armed on a spelling the opener refused (or vice versa)
    # would be a rail that half exists.
    #
    # IT GATES THIS STEP AND NOTHING ABOVE IT. See the module header: gating the whole run was a
    # measured PII retention bug, and `is_configured()` being half the condition made a
    # credential blip enough to cause it.
    if not purchase_svc.is_enabled() or not rc.is_configured():
        counts["skipped_disabled"] = 1
        logger.debug(
            "reap_agentic_poll: step 4 skipped (enabled=%s configured=%s); the sweeps ran",
            purchase_svc.is_enabled(),
            rc.is_configured(),
        )
        return _report()

    if _budget_spent():
        # The sweeps ate the budget. Claiming now would take leases this run cannot service and
        # then hand them straight back, having spent a claim (and, in three states, an ATTEMPT)
        # on each. `attempts` is incremented by the claim itself, so a run that claims and
        # abandons is a run that walks rows toward `fail_exhausted_purchases` for free.
        logger.warning(
            "reap_agentic_poll: the %ds budget was spent by the sweeps; claiming nothing "
            "this tick",
            budget_seconds,
        )
        return _report()

    rows = await ledger.claim_due_purchases(worker, limit=claim_batch)
    counts["claimed"] = len(rows)

    # EVERYTHING FROM HERE IS INSIDE ONE try/finally, AND THAT IS FIX F1/F2. The `finally` is the
    # only thing standing between a cancelled or raising run and a batch of leases held for a
    # full lease window — with, in two states, a buyer's address and email on them.
    try:
        for row in rows:
            purchase_id = str(row["id"])

            if _budget_spent():
                # Give the rest of the batch back UNTOUCHED rather than starting a partner call
                # chain we cannot finish inside the run deadline. No `next_poll_at`: the row
                # stays due, so the next tick takes it first.
                #
                # Counted as `abandoned_budget`, NOT as `released` (fix F8): "the partner was not
                # ready" and "we ran out of time" are different facts and an operator tuning the
                # budget needs to tell them apart.
                # NO `last_error_code` HERE. Nothing went wrong: the row is healthy and simply
                # due again. `last_error_code` is what a human reads to find out why a purchase
                # stalled, and putting an error-shaped value on a row that was merely deferred is
                # how that column stops meaning anything. Passing None COALESCEs, so whatever
                # real reason is already on the row survives.
                if await _release_guarded(purchase_id, worker, counts):
                    counts["abandoned_budget"] += 1
                continue

            try:
                result = await purchase_svc.advance(purchase_id, worker)
            except Exception as exc:  # noqa: BLE001 — one bad row must not end the batch
                # THE TYPE AND THE PURCHASE ID, AND NOTHING ELSE. `advance` raises out of the
                # driver (the known case is `uq_reap_agentic_purchases_checkout`, a partner
                # replaying a checkout id we already stored), and a driver exception's MESSAGE
                # carries row content — on this table that content includes the buyer's email
                # and address.
                counts["errors"] += 1
                logger.error(
                    "reap_agentic_poll: advance RAISED for purchase=%s error_type=%s; releasing "
                    "with a %ds backoff",
                    purchase_id,
                    type(exc).__name__,
                    error_backoff,
                )
                try:
                    released = await ledger.release_claim(
                        purchase_id,
                        worker,
                        next_poll_at=clock() + timedelta(seconds=error_backoff),
                        # AND RECORD WHY, which is new with #2204's adoption round. Before it,
                        # `release_claim` took a schedule and nothing else, so the reason a
                        # purchase stalled lived only in a log line: "why has this been stuck in
                        # 'quoting' for an hour?" had no answer in the row. THIS is the path that
                        # needed it most — a step that RAISED wrote nothing at all.
                        #
                        # Through the SERVICE'S fold, not a local copy. `release_claim` REFUSES a
                        # code outside `^[a-z0-9_:.-]{1,64}` rather than truncating or folding it,
                        # and `_error_code` is the one place this rail lowercases, trims to the
                        # column width and substitutes `error_code_unrepresentable` instead of
                        # losing the fact that something went wrong. A second implementation here
                        # would be a second vocabulary in one column.
                        last_error_code=purchase_svc._error_code(
                            f"poller_advance_raised:{type(exc).__name__}"
                        ),
                    )
                except Exception as release_exc:  # noqa: BLE001
                    # The `finally` below is the backstop for this row; say so rather than
                    # letting it look handled.
                    logger.error(
                        "reap_agentic_poll: purchase=%s could not be released after the raise "
                        "(error_type=%s); the leftover sweep will take it",
                        purchase_id,
                        type(release_exc).__name__,
                    )
                    continue
                if released is None:
                    # The claim already moved — a sweep terminated the row, or our lease aged out
                    # mid-step. Nothing to release and nothing to schedule; the row is somebody
                    # else's now.
                    counts["lost_claim"] += 1
                continue

            outcome = result.outcome
            if outcome in ("advanced", "released", "lost_claim", "terminal"):
                counts[outcome] += 1
                logger.debug(
                    "reap_agentic_poll: purchase=%s outcome=%s state=%s",
                    purchase_id,
                    outcome,
                    result.state,
                )
            else:
                # 'missing' — a row we claimed a moment ago and cannot read now — or an outcome
                # added to the service without updating this list. Either is a bug worth seeing,
                # and counting it under `errors` is what makes it visible; silently dropping it
                # would make a whole new outcome class invisible in the one report an operator
                # reads.
                counts["errors"] += 1
                logger.error(
                    "reap_agentic_poll: purchase=%s returned an unhandled outcome %r",
                    purchase_id,
                    outcome,
                )

            # UNCONDITIONAL, and a fenced no-op wherever the claim is already gone. See the
            # module header for which outcomes leave the lease held (every non-terminal
            # `advanced` does). No `next_poll_at` AND NO `last_error_code`: `release_claim`
            # COALESCEs both, so the schedule the state machine chose AND the reason it recorded
            # on an `outcome="released"` step both survive this call untouched. Passing a code
            # here would overwrite the service's own, more specific one on every ordinary tick.
            #
            # GUARDED, and INSIDE the per-row loop body (fix F1): in the first cut this call sat
            # bare, so a pool blip on row 2 of 10 propagated out of the whole function and
            # abandoned the eight leases behind it.
            await _release_guarded(purchase_id, worker, counts)
    finally:
        # THE INVARIANT, CHECKED RATHER THAN ASSERTED IN PROSE — and checked on the way out of a
        # cancellation too, which is the path the first cut had no answer for at all.
        counts["errors"] += await _release_leftovers(worker)

    report = _report()
    # THE PROOF LINE: `[ts] INFO - reap_agentic_poll: PollReport(...)` on the worker's stdout.
    operator_logger.info("reap_agentic_poll: %s", report)
    return report


async def _release_guarded(purchase_id: str, worker: str, counts: Dict[str, int]) -> bool:
    """`release_claim`, with one row's failure kept to that row. True when the call SUCCEEDED.

    The boolean is what lets the caller count `abandoned_budget` AFTER the await rather than
    before it (fix F8): a count incremented before the write it describes is a count that lies
    the first time the write fails.
    """
    try:
        await ledger.release_claim(purchase_id, worker)
        return True
    except Exception as exc:  # noqa: BLE001 — one bad row must not end the batch
        counts["errors"] += 1
        logger.error(
            "reap_agentic_poll: purchase=%s could not be released (error_type=%s); the leftover "
            "sweep will take it",
            purchase_id,
            type(exc).__name__,
        )
        return False
