"""The Reap agentic purchase rail's alerts: three log-based metrics and three policies.

infra/gcp/setup_monitoring.sh cannot be run from a test (it provisions), and Cloud Monitoring
cannot read the ledger, so everything the alerts know arrives as LOG LINES from one scheduler job
on the `worker` service. That makes two things worth pinning, and this file pins both:

  THE LINES ARE THE JOB'S, NOT RESTATED HERE. Each scenario below runs the real
  `run_reap_agentic_purchase_poll` (real ledger, real state machine, faked transport) under
  production's logging state, and collects what the worker would hand Cloud Run on BOTH streams:
  the "pivota" stdout handler's output, and stderr through logging's last-resort handler — which
  is where a module logger's WARNING/ERROR goes on a service whose root logger nobody configures
  (read on staging 2026-10-01: bare message, stderr, no severity). The scheduler runner's lines
  come from the real runner the same way.

  THE FILTERS ARE THE SCRIPT'S, NOT RESTATED HERE. They are parsed out of the shell source and
  evaluated — the whole boolean expression, resource clauses included — against those lines.

So a change to the report's format, to a message, to a logger, or to a filter fails here rather
than as a metric that silently never increments, or one that pages a dark environment.

    .venv/bin/python -m pytest tests/test_reap_rail_alerts.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES  # noqa: E402
import db.reap_agentic_ledger as ledger  # noqa: E402
import jobs.reap_agentic_purchase_poll as job  # noqa: E402
import services.scheduler_job_runner as runner  # noqa: E402
from pivota_log_capture import capture_pivota_stdout, pivota_lines, root_as_in_prod  # noqa: E402

# The poller's own SQLite fixtures and helpers. The FIXTURES are imported by name on purpose:
# pytest discovers a fixture wherever the name is bound, and a second copy of the faked Reap
# surface would be a second thing to keep in step with the state machine.
from test_reap_agentic_purchase import _resolved  # noqa: E402
from test_reap_agentic_purchase_poll import (  # noqa: E402,F401
    _Clock,
    _all_claims,
    _db,
    _env,
    _get,
    _no_network,
    _park,
    _raw,
    _run,
    _start,
    attribution,
    reap,
)

pytestmark = pytest.mark.skipif(
    IS_POSTGRES, reason="drives the poller on its SQLite fixtures; the filters are dialect-free"
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "gcp" / "setup_monitoring.sh"
RUNBOOK = ROOT / "docs" / "runbooks" / "reap_agentic_purchase.md"

STUCK_POLICY = "prod: Reap purchase stuck over 30 minutes"
FAILING_POLICY = "prod: Reap purchase poller failing"
SILENT_POLICY = "prod: Reap purchase poller went silent"

#: metric name -> the shell variable holding its filter.
METRICS = {
    "reap_agentic_poll_report": "REAP_POLL_REPORT_FILTER",
    "reap_agentic_poll_stuck": "REAP_POLL_STUCK_FILTER",
    "reap_agentic_poll_failing": "REAP_POLL_FAILING_FILTER",
}


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


CHANNEL = "projects/p/notificationChannels/1"


def reap_policies() -> dict:
    """The three policies' JSON, from the script's OWN generators — the python inside `policy`
    and `promql_policy` is extracted and run; the provisioning script itself never is. Same idea
    as tests/test_monitoring_policy_snapshot.py, keyed on the function these policies are
    written through."""
    source = SCRIPT.read_text(encoding="utf-8")
    generators = {
        name: source.split(name + "() {", 1)[1].split("python3 -c '\n", 1)[1].split("' \"$@\"", 1)[0]
        for name in ("policy", "promql_policy")
    }
    out = {}
    for name, generator, body in re.findall(
        r'^upsert_on_new_metric "([^"]+)" "\$\((policy|promql_policy) (.*?)\)"$', source, re.S | re.M
    ):
        args = shlex.split(body.replace("\\\n", "").replace("\\`", "`"))
        proc = subprocess.run(
            [sys.executable, "-c", generators[generator], *args, CHANNEL],
            text=True, capture_output=True, check=True,
        )
        out[name] = json.loads(proc.stdout)
    return out


def _filter(source: str, var: str) -> str:
    m = re.search(rf"^{var}='(.*)'$", source, re.M)
    assert m, f"{var} not found as a single-quoted assignment"
    return m.group(1)


@pytest.fixture(scope="module")
def filters(source):
    return {name: _filter(source, var) for name, var in METRICS.items()}


# ── a small evaluator for the Logging query language, as far as these filters use it ─────────

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<lparen>\() | (?P<rparen>\)) |
        (?P<word>AND|OR|NOT)\b |
        (?P<field>[A-Za-z_][A-Za-z0-9_.]*)(?P<op>=~|:|=)"(?P<value>(?:[^"\\]|\\.)*)"
    )""",
    re.X,
)


def _tokens(text: str):
    at, out = 0, []
    while at < len(text):
        if text[at:].strip() == "":
            break
        m = _TOKEN.match(text, at)
        assert m, f"unrecognised filter syntax at {text[at:at + 40]!r}"
        at = m.end()
        if m.group("lparen"):
            out.append(("(",))
        elif m.group("rparen"):
            out.append((")",))
        elif m.group("word"):
            out.append((m.group("word"),))
        else:
            out.append(("atom", m.group("field"), m.group("op"), m.group("value")))
    return out


def _atom(entry: dict, field: str, op: str, raw: str) -> bool:
    assert field in entry, f"the filter reads {field!r}, which this model of a log entry lacks"
    have = entry[field]
    want = raw.replace('\\"', '"')
    if op == "=":
        return have == want
    if op == ":":  # Logging's `:` is a case-insensitive substring
        return want.lower() in have.lower()
    return bool(re.search(want, have))  # `=~` is an RE2 search; these patterns are plain


def matches(log_filter: str, entry: dict) -> bool:
    """Evaluate the WHOLE filter against one entry.

    The Logging query language binds NOT, then OR, then AND — OR is TIGHTER than AND, the
    opposite of most languages — so that is the grammar here. Anything outside the shapes these
    filters use fails loudly rather than being skipped.
    """
    toks = _tokens(log_filter)
    pos = 0

    def peek():
        return toks[pos][0] if pos < len(toks) else None

    def factor() -> bool:
        nonlocal pos
        kind = peek()
        if kind == "NOT":
            pos += 1
            return not factor()
        if kind == "(":
            pos += 1
            value = conjunction()
            assert peek() == ")", "unbalanced parentheses"
            pos += 1
            return value
        assert kind == "atom", f"expected a comparison, found {toks[pos:pos + 1]!r}"
        _, field, op, raw = toks[pos]
        pos += 1
        return _atom(entry, field, op, raw)

    def disjunction() -> bool:
        nonlocal pos
        value = factor()
        while peek() == "OR":
            pos += 1
            value = factor() or value
        return value

    def conjunction() -> bool:
        nonlocal pos
        value = disjunction()
        while peek() == "AND":
            pos += 1
            value = disjunction() and value
        return value

    result = conjunction()
    assert pos == len(toks), f"trailing filter text: {toks[pos:]!r}"
    return result


def _entry(line: str, *, service: str = "worker", resource_type: str = "cloud_run_revision"):
    return {
        "resource.type": resource_type,
        "resource.labels.service_name": service,
        "textPayload": line,
    }


def _count(filters, metric: str, lines) -> int:
    return sum(1 for line in lines if matches(filters[metric], _entry(line)))


def _counts(filters, lines) -> dict:
    return {name: _count(filters, name, lines) for name in METRICS}


@contextlib.contextmanager
def worker_log():
    """Every line the worker would write, on both streams, for the block.

    stdout: the "pivota" handler's own stream, formatted by it. stderr: `logging.lastResort`,
    which is what a module logger's WARNING/ERROR reaches when root has no handler — production's
    state on this service. Each line is one Cloud Run log entry's textPayload.
    """
    lines = []
    err = io.StringIO()
    with root_as_in_prod(), capture_pivota_stdout() as out, contextlib.redirect_stderr(err):
        yield lines
    lines.extend(pivota_lines(out))
    lines.extend(line for line in err.getvalue().splitlines() if line)


# ── the evaluator itself ─────────────────────────────────────────────────────────────────────


def test_the_evaluator_gives_or_precedence_over_and_like_cloud_logging():
    e = _entry("b c")
    # `a AND b OR c` is `a AND (b OR c)` in the Logging query language.
    assert not matches('textPayload:"a" AND textPayload:"b" OR textPayload:"c"', e)
    assert matches('(textPayload:"a" AND textPayload:"b") OR textPayload:"c"', e)
    assert matches('NOT textPayload:"a" AND textPayload:"b"', e)
    with pytest.raises(AssertionError):
        matches('textPayload:"a" AND severity>=ERROR', e)


def test_no_filter_leans_on_operator_precedence(filters):
    """Every AND/OR mix in the filters is parenthesised, so a reader who assumes the usual
    precedence and one who knows Logging's read the same expression."""
    for name, text in filters.items():
        depth_ops = {}
        depth = 0
        for tok in _tokens(text):
            if tok[0] == "(":
                depth += 1
                depth_ops.pop(depth, None)
            elif tok[0] == ")":
                depth_ops.pop(depth, None)
                depth -= 1
            elif tok[0] in ("AND", "OR"):
                seen = depth_ops.setdefault(depth, tok[0])
                assert seen == tok[0], f"{name} mixes AND and OR at one nesting level"


# ── the lines a real run prints, through the real filters ────────────────────────────────────


async def test_a_healthy_armed_tick_is_one_heartbeat_and_nothing_else(filters, reap):
    await _start()
    with worker_log() as lines:
        report = await _run(worker_id="w1")
    assert report.advanced == 1 and report.stuck_over_age == 0
    assert _counts(filters, lines) == {
        "reap_agentic_poll_report": 1,
        "reap_agentic_poll_stuck": 0,
        "reap_agentic_poll_failing": 0,
    }, lines


async def test_a_stuck_purchase_counts_on_every_armed_tick_until_it_moves(filters, reap):
    """A STANDING condition: the policy's window is sized on the next tick repeating it."""
    paid = await _start(buyer_ref="bref_paid")
    await _park(paid, "processing", job.STUCK_AFTER_SECONDS + 5, reap_checkout_id="chk_stuck")
    with worker_log() as lines:
        await _run(worker_id="w1")
        await _run(worker_id="w2")
    assert _counts(filters, lines) == {
        "reap_agentic_poll_report": 2,
        "reap_agentic_poll_stuck": 2,
        "reap_agentic_poll_failing": 0,
    }, lines

    await _raw("UPDATE reap_agentic_purchases SET state = 'completed' WHERE id = :i", {"i": paid})
    with worker_log() as lines:
        await _run(worker_id="w3")
    assert _count(filters, "reap_agentic_poll_stuck", lines) == 0, lines


async def test_ten_or_more_stuck_purchases_still_match(filters, reap):
    """`[1-9]` is the FIRST digit: 10, 25 and 100 all begin with one."""
    for n in range(10):
        pid = await _start(buyer_ref=f"bref_{n}")
        await _park(pid, "processing", 4000, reap_checkout_id=f"chk_{n}")
    with worker_log() as lines:
        report = await _run(worker_id="w1")
    assert report.stuck_over_age == 10
    assert _count(filters, "reap_agentic_poll_stuck", lines) == 1


async def test_a_buyer_on_a_live_hosted_page_never_reaches_the_stuck_metric(filters, reap):
    waiting = await _start()
    await _park(waiting, "awaiting_approval", 3000, reap_checkout_id="chk_wait")
    await _raw(
        "UPDATE reap_agentic_purchases SET hosted_url_expires_at = datetime('now', '+600 seconds') "
        "WHERE id = :i",
        {"i": waiting},
    )
    with worker_log() as lines:
        await _run(worker_id="w1")
    assert _counts(filters, lines) == {
        "reap_agentic_poll_report": 1,
        "reap_agentic_poll_stuck": 0,
        "reap_agentic_poll_failing": 0,
    }, lines


async def test_a_raising_step_reaches_the_failing_metric(filters, monkeypatch, reap):
    await _start()

    async def _raises(pid, worker):
        raise RuntimeError("driver text that must stay out of the logs")

    monkeypatch.setattr(job.purchase_svc, "advance", _raises)
    with worker_log() as lines:
        report = await _run(worker_id="w1")
    assert report.errors == 1
    hit = [line for line in lines if matches(filters["reap_agentic_poll_failing"], _entry(line))]
    # Two entries for one failure: the job's own ERROR on stderr, and the report with errors=1.
    assert len(hit) == 2, lines
    assert any("advance RAISED" in line for line in hit)
    assert any("PollReport(" in line and "errors=1" in line for line in hit)
    assert _count(filters, "reap_agentic_poll_stuck", lines) == 0


async def test_a_stuck_count_that_could_not_be_taken_is_failing_not_stuck(
    filters, monkeypatch, reap
):
    """The sentinel must not read as a count: `stuck_over_age=-1` is not `[1-9]`."""
    await _start()

    async def _boom(**kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(ledger, "count_stuck_purchases", _boom)
    with worker_log() as lines:
        await _run(worker_id="w1")
    assert any("stuck_over_age=-1" in line for line in lines), lines
    counts = _counts(filters, lines)
    assert counts["reap_agentic_poll_stuck"] == 0
    assert counts["reap_agentic_poll_failing"] == 2  # the ERROR line and the report's errors=1
    assert counts["reap_agentic_poll_report"] == 1


async def test_over_budget_ticks_still_page_for_a_stuck_payment(filters, monkeypatch, reap):
    """REVIEW OF #2488, ROUND 2, AS MEASURED: three consecutive over-budget ticks with a payment
    90 minutes in `processing`. With step 5 skipped on a spent budget every report read
    `stuck_over_age=-1, errors=0` and neither STUCK nor FAILING saw anything. Every one of these
    ticks must now reach the stuck metric."""
    paid = await _start(buyer_ref="bref_paid")
    await _park(paid, "processing", 90 * 60, reap_checkout_id="chk_stuck")
    clock = _Clock(monkeypatch)
    resolved = _resolved()

    def _slow_row(**kwargs):
        clock.spend_budget()
        return resolved

    reap.resolve_our_row = _slow_row
    with worker_log() as lines:
        for tick in range(3):
            await _start(buyer_ref=f"bref_slow_{tick}")
            report = await _run(worker_id=f"w{tick}")
            assert report.stuck_over_age == 1 and report.duration_ms >= 9_000_000
    assert _counts(filters, lines) == {
        "reap_agentic_poll_report": 3,
        "reap_agentic_poll_stuck": 3,
        "reap_agentic_poll_failing": 0,
    }, lines


async def test_a_count_that_timed_out_pages_as_failing(filters, monkeypatch, reap):
    """The other half: when the bounded count cannot be taken the tick is NOT silent. `-1`
    arrives with `errors=1` and the job's own ERROR line, and both reach the failing metric."""
    await _start()

    async def _hangs(**kwargs):
        await asyncio.sleep(8)

    monkeypatch.setattr(ledger, "count_stuck_purchases", _hangs)
    monkeypatch.setattr(job, "STUCK_COUNT_TIMEOUT_SECONDS", 0.05)
    with worker_log() as lines:
        report = await _run(worker_id="w1")
    assert report.stuck_over_age == job.NOT_COUNTED and report.errors == 1
    hit = [line for line in lines if matches(filters["reap_agentic_poll_failing"], _entry(line))]
    assert len(hit) == 2, lines
    assert any("error_type=TimeoutError" in line for line in hit)
    assert any("stuck_over_age=-1," in line and ", errors=1," in line for line in hit)
    assert _count(filters, "reap_agentic_poll_stuck", lines) == 0


async def test_a_payment_past_the_attempt_ceiling_reaches_the_failing_metric(
    filters, monkeypatch, reap
):
    """The report's `errors` is 0 here — the job logs an ERROR and counts
    `processing_over_attempts` — so this is caught by the "any other poller line" arm."""
    pid = await _start()
    monkeypatch.setenv("REAP_AGENTIC_MAX_ATTEMPTS", "5")
    await _park(pid, "processing", 10, attempts=100, reap_checkout_id="chk_ceiling")
    with worker_log() as lines:
        report = await _run(worker_id="w1")
    assert report.errors == 0 and report.processing_over_attempts == 1
    hit = [line for line in lines if matches(filters["reap_agentic_poll_failing"], _entry(line))]
    assert len(hit) == 1 and "max_attempts" in hit[0], lines


async def test_a_bad_dial_reaches_the_failing_metric_once(filters, monkeypatch, reap):
    monkeypatch.setenv("REAP_AGENTIC_CLAIM_BATCH", "ten")
    with worker_log() as lines:
        await _run(worker_id="w1")
        await _run(worker_id="w2")
    hit = [line for line in lines if matches(filters["reap_agentic_poll_failing"], _entry(line))]
    assert len(hit) == 1 and "REAP_AGENTIC_CLAIM_BATCH" in hit[0], lines


# ── a DARK environment prints nothing these metrics can count ────────────────────────────────


@pytest.mark.parametrize("how", ["unset", "off", "no_key"])
async def test_a_dark_rail_feeds_none_of_the_three_metrics(filters, monkeypatch, reap, how):
    """PRODUCTION TODAY: `REAP_AGENTIC_ENABLED` unset. Even with a purchase in the ledger that an
    armed run would report as stuck, a disarmed tick prints NO line — so there is no heartbeat
    series for the silent policy to miss, and nothing for the other two to count."""
    await _start(buyer_ref="bref_live")
    old = await _start(buyer_ref="bref_old")
    await _park(old, "processing", 99999, reap_checkout_id="chk_dark")
    if how == "unset":
        monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    elif how == "off":
        monkeypatch.setenv("REAP_AGENTIC_ENABLED", "0")
    else:
        monkeypatch.delenv("REAP_API_KEY", raising=False)

    with worker_log() as lines:
        for _ in range(3):
            report = await _run()
            assert report.skipped_disabled == 1

    assert lines == [], "a disarmed poller wrote a line"
    assert _counts(filters, lines) == dict.fromkeys(METRICS, 0)


async def test_an_armed_staging_pointed_at_a_real_host_is_failing_not_silent(
    filters, monkeypatch, reap
):
    """The one disarmed path that DOES speak: armed outside production against a non-sandbox
    host. Step 4 is refused with one ERROR per process — a misconfiguration somebody must fix,
    so it is the failing metric's — and still no report line."""
    await _start()
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://prod.api.reap.global")
    with worker_log() as lines:
        await _run()
        await _run()
    assert _counts(filters, lines) == {
        "reap_agentic_poll_report": 0,
        "reap_agentic_poll_stuck": 0,
        "reap_agentic_poll_failing": 1,
    }, lines


# ── the scheduler runner's lines for this job ────────────────────────────────────────────────


async def test_a_run_past_its_deadline_reaches_the_failing_metric(filters, monkeypatch):
    monkeypatch.setattr(runner, "_REGISTRY", {})

    async def _wedged():
        await asyncio.sleep(30)

    with worker_log() as lines:
        with pytest.raises(runner.JobDeadlineExceeded):
            await runner.run_isolated(
                "reap_agentic_purchase_poll", _wedged, deadline_seconds=0.05,
                cancel_grace_seconds=1.0,
            )
    hit = [line for line in lines if matches(filters["reap_agentic_poll_failing"], _entry(line))]
    assert len(hit) == 1 and "run deadline" in hit[0], lines


async def test_another_jobs_deadline_does_not(filters, monkeypatch):
    monkeypatch.setattr(runner, "_REGISTRY", {})

    async def _wedged():
        await asyncio.sleep(30)

    with worker_log() as lines:
        with pytest.raises(runner.JobDeadlineExceeded):
            await runner.run_isolated(
                "external_conversion_poll", _wedged, deadline_seconds=0.05,
                cancel_grace_seconds=1.0,
            )
    assert lines and _counts(filters, lines) == dict.fromkeys(METRICS, 0), lines


async def test_a_deploy_landing_while_the_runner_holds_a_stub_run_does_not_page(
    filters, monkeypatch
):
    """The runner's half on its own: shutting the service down cancels the runner's WRAPPER
    while a run is in flight, and the runner says so at ERROR ("ABANDONED as a zombie (wrapper
    cancelled ...)"). The poller's half — the claims it was holding — is the next test, and is
    the one the first cut of the filter got wrong."""
    monkeypatch.setattr(runner, "_REGISTRY", {})
    started = asyncio.Event()

    async def _in_flight():
        started.set()
        await asyncio.sleep(30)

    with worker_log() as lines:
        wrapper = asyncio.ensure_future(
            runner.run_isolated(
                "reap_agentic_purchase_poll", _in_flight, deadline_seconds=60,
                cancel_grace_seconds=0.05,
            )
        )
        await started.wait()
        wrapper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapper
        await asyncio.sleep(0.2)  # let the grace timer run inside the capture
    assert any("wrapper cancelled" in line for line in lines), lines
    assert _counts(filters, lines) == dict.fromkeys(METRICS, 0), lines


def _hang_mid_step(reap) -> asyncio.Event:
    """Make the partner never answer, so the REAL poller is parked inside the REAL `advance` of
    its first row with the whole batch claimed. The event is set once it is there."""
    in_step = asyncio.Event()

    async def _never_answers(**kwargs):
        in_step.set()
        await asyncio.sleep(60)

    reap.resolve_our_row = _never_answers
    return in_step


async def _until_no_claims() -> None:
    for _ in range(200):
        if not await _all_claims():
            return
        await asyncio.sleep(0.02)


async def test_a_deploy_landing_mid_step_with_claims_held_does_not_page(filters, monkeypatch, reap):
    """P1 FROM REVIEW OF #2488, reproduced as it was measured: the REAL poller, through the REAL
    `runner.run_isolated`, cancelled by its wrapper mid-step with three rows claimed. The job's
    `finally` releases all three and logs each — and the first cut of the failing filter matched
    those three lines, so every deploy that landed during a step would have paged
    (`{'report': 0, 'stuck': 0, 'failing': 3}`).

    Every line the worker wrote is fed through the filters; none may count."""
    monkeypatch.setattr(runner, "_REGISTRY", {})
    ids = [await _start(buyer_ref=f"bref_{n}") for n in range(3)]
    in_step = _hang_mid_step(reap)

    with worker_log() as lines:
        wrapper = asyncio.ensure_future(
            runner.run_isolated(
                "reap_agentic_purchase_poll", job.run_reap_agentic_purchase_poll,
                deadline_seconds=600, cancel_grace_seconds=5.0,
            )
        )
        await asyncio.wait_for(in_step.wait(), 10)
        assert sorted(await _all_claims()) == sorted(ids), "the run is not holding the batch"
        wrapper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapper
        await _until_no_claims()

    assert await _all_claims() == {}, "the cancelled run left claims behind"
    released = [line for line in lines if "released on cancellation" in line]
    assert len(released) == 3, lines
    assert not any("STILL CLAIMED" in line for line in lines), (
        "a cancelled run described its claims as a bug in the loop"
    )
    assert any("wrapper cancelled" in line for line in lines), lines
    assert len(lines) >= 4
    assert _counts(filters, lines) == dict.fromkeys(METRICS, 0), lines


async def test_a_real_run_cancelled_at_its_deadline_pages_once_for_the_deadline(
    filters, monkeypatch, reap
):
    """The other cancellation. The per-claim lines are the same expected ones and do not count;
    the RUNNER's deadline line does, exactly once — a wedged run is a failing poller."""
    monkeypatch.setattr(runner, "_REGISTRY", {})
    for n in range(3):
        await _start(buyer_ref=f"bref_{n}")
    _hang_mid_step(reap)

    with worker_log() as lines:
        with pytest.raises(runner.JobDeadlineExceeded):
            await runner.run_isolated(
                "reap_agentic_purchase_poll", job.run_reap_agentic_purchase_poll,
                deadline_seconds=0.5, cancel_grace_seconds=5.0,
            )
        await _until_no_claims()

    assert await _all_claims() == {}
    assert sum("released on cancellation" in line for line in lines) == 3, lines
    hit = [line for line in lines if matches(filters["reap_agentic_poll_failing"], _entry(line))]
    assert len(hit) == 1 and "run deadline" in hit[0], lines


async def test_a_claim_the_loop_forgot_still_pages_twice_over(filters, monkeypatch, reap):
    """THE PATH THE EXCLUSION MUST NOT HIDE. A run that FINISHED its loop and still holds a claim
    has a bug: it keeps its own ERROR text, which the filter matches, and the leftover is counted
    under `errors`, so the report line matches as well. Two independent arms, both live."""
    purchase_id = await _start()
    real_release = job.ledger.release_claim
    swallowed = []

    async def _swallow_once(pid, worker, **kwargs):
        if pid == purchase_id and not swallowed:
            swallowed.append(pid)
            return await _get(pid)  # looks like a successful release; releases nothing
        return await real_release(pid, worker, **kwargs)

    monkeypatch.setattr(job.ledger, "release_claim", _swallow_once)

    with worker_log() as lines:
        report = await _run(worker_id="w1")

    assert report.errors == 1 and await _all_claims() == {}
    hit = [line for line in lines if matches(filters["reap_agentic_poll_failing"], _entry(line))]
    assert len(hit) == 2, lines
    assert any("STILL CLAIMED after the loop" in line for line in hit), "arm (b), the job's line"
    assert any("PollReport(" in line and ", errors=1," in line for line in hit), "arm (a), errors"
    assert not any("released on cancellation" in line for line in lines)


def test_only_the_errors_field_itself_counts_as_errors(filters):
    """`, errors=` is anchored to the field. A later field whose NAME ends in `errors` and is
    non-zero must not read as the poller failing — and the real field still must."""
    def line(tail):
        return "[2026-10-01 09:39:48,028] INFO - reap_agentic_poll: PollReport(requeued=0, " + tail

    quiet = line("terminal=0, errors=0, skipped_disabled=0, transport_errors=7, duration_ms=19)")
    loud = line("terminal=0, errors=3, skipped_disabled=0, duration_ms=19)")
    failing = filters["reap_agentic_poll_failing"]
    assert not matches(failing, _entry(quiet))
    assert matches(failing, _entry(loud))
    assert 'textPayload=~", errors=[1-9]"' in failing


# ── lines that look close and must not count ─────────────────────────────────────────────────

#: Real textPayloads from the worker service (staging and prod, 2026-09/10), other than ours.
_NEIGHBOURS = [
    # The state machine's own hold, WARNING through the last-resort handler, once a minute while
    # a buyer has an enrollment page open. Its prefix is `reap_agentic: `, not `reap_agentic_poll: `.
    "reap_agentic: purchase=rp_4dc504fbc85f4ff9b922fbe2 state=needs_enrollment held at "
    "enrollment_pending, retry in 30s",
    # APScheduler, for ANY exception out of a job — including the cancel at shutdown.
    'Job "run_reap_agentic_purchase_poll (trigger: interval[0:00:30], next run at: 2026-10-01 '
    '09:40:18 UTC)" raised an exception',
    'Execution of job "run_reap_agentic_purchase_poll (trigger: interval[0:00:30], next run at: '
    '2026-10-01 09:40:18 UTC)" skipped: maximum number of running instances reached (1)',
    "[2026-09-30 21:24:18,363] WARNING - audit_scheduler: SCHEDULER_JOB_ALLOWLIST ACTIVE "
    "allowlist=['reap_agentic_purchase_poll'] worker_enabled=True "
    "registered=['reap_agentic_purchase_poll'] skipped_by_allowlist=47",
    "scheduler job 'nightly_index_health': run ABANDONED as a zombie (wrapper cancelled while "
    "the run was in flight) — it stays visible on /__scheduler_health and reachable via "
    "cancel-running. (issue #1754 wedge class)",
]


@pytest.mark.parametrize(
    "line",
    _NEIGHBOURS,
    # Short, fixed ids: the sweep shards by node id, and a whole log line is a poor one.
    ids=["state-machine-hold", "apscheduler-raised", "apscheduler-skipped", "allowlist-startup",
         "another-jobs-zombie"],
)
def test_a_neighbouring_worker_line_feeds_no_metric(filters, line):
    assert _counts(filters, [line]) == dict.fromkeys(METRICS, 0)


def test_the_metrics_only_count_the_worker_service(filters):
    """The same text from another service — the web tier imports the same modules — or from a
    Cloud Run JOB is not this rail's poller."""
    line = (
        "[2026-10-01 09:39:48,028] INFO - reap_agentic_poll: PollReport(requeued=0, expired=0, "
        "failed_exhausted=0, processing_over_attempts=0, stuck_over_age=3, claimed=0, advanced=0, "
        "released=0, abandoned_budget=0, lost_claim=0, terminal=0, errors=2, skipped_disabled=0, "
        "duration_ms=19)"
    )
    for name, text in filters.items():
        assert matches(text, _entry(line)), name
        assert not matches(text, _entry(line, service="web")), name
        assert not matches(text, _entry(line, resource_type="cloud_run_job")), name


def test_the_filters_name_things_that_exist(filters):
    scheduler = (ROOT / "services" / "audit_scheduler.py").read_text(encoding="utf-8")
    assert 'id="reap_agentic_purchase_poll"' in scheduler
    assert "reap_agentic_purchase_poll" in filters["reap_agentic_poll_failing"]
    # The service the scheduler runs on is deployed under this name.
    deploy = (ROOT / "infra" / "gcp" / "deploy_backend.sh").read_text(encoding="utf-8")
    assert '[ "$SERVICE" = worker ]' in deploy
    for text in filters.values():
        assert 'resource.labels.service_name="worker"' in text
        assert "severity" not in text, (
            "these lines carry NO severity on Cloud Run (plain text on stdout/stderr); a "
            "severity clause is a metric that can never count"
        )


# ── the provisioning script wires them up ────────────────────────────────────────────────────


def _uncommented(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_each_metric_is_created_from_its_own_filter(source):
    body = _uncommented(source)
    for metric, var in METRICS.items():
        # NAME, one line of description, then the filter variable — `upsert_log_metric`'s shape.
        assert re.search(
            rf'^upsert_log_metric {metric} \\\n  "[^"\n]+" \\\n  "\${var}"$', body, re.M
        ), f"{metric} is not created from ${var}"


def _threshold(policy: dict) -> dict:
    (condition,) = policy["conditions"]
    return condition["conditionThreshold"]


def test_the_stuck_policy_fires_on_one_report_and_stays_one_incident():
    from services.audit_scheduler import _JOB_RUN_DEADLINES

    policy = reap_policies()[STUCK_POLICY]
    threshold = _threshold(policy)
    assert threshold["filter"] == (
        'metric.type="logging.googleapis.com/user/reap_agentic_poll_stuck" '
        'AND resource.type="cloud_run_revision"'
    )
    (agg,) = threshold["aggregations"]
    assert agg["perSeriesAligner"] == "ALIGN_SUM" and agg["crossSeriesReducer"] == "REDUCE_SUM"
    assert threshold["comparison"] == "COMPARISON_GT" and threshold["thresholdValue"] == 0
    # A log counter writes no points between matching lines: a duration would miss a lone one.
    assert threshold["duration"] == "0s"
    # The window must outlast the longest gap between two reports of a healthy armed poller —
    # one run using its whole deadline — or a standing condition re-opens per slow tick.
    window = int(agg["alignmentPeriod"].rstrip("s"))
    assert window > _JOB_RUN_DEADLINES["reap_agentic_purchase_poll"], (
        "re-derive the stuck policy's window: the job's run deadline has reached it"
    )
    assert "30 minutes" in STUCK_POLICY and job.STUCK_AFTER_SECONDS == 30 * 60
    assert "docs/runbooks/reap_agentic_purchase.md" in policy["documentation"]["content"]


def test_the_failing_policy_fires_on_one_line():
    policy = reap_policies()[FAILING_POLICY]
    threshold = _threshold(policy)
    assert threshold["filter"] == (
        'metric.type="logging.googleapis.com/user/reap_agentic_poll_failing" '
        'AND resource.type="cloud_run_revision"'
    )
    assert threshold["comparison"] == "COMPARISON_GT" and threshold["thresholdValue"] == 0
    assert threshold["duration"] == "0s"
    assert "docs/runbooks/reap_agentic_purchase.md" in policy["documentation"]["content"]


def _silent_condition() -> dict:
    (condition,) = reap_policies()[SILENT_POLICY]["conditions"]
    return condition["conditionPrometheusQueryLanguage"]


def _silent_query():
    series = 'logging_googleapis_com:user_reap_agentic_poll_report{monitored_resource="cloud_run_revision"}'
    query = _silent_condition()["query"]
    m = re.fullmatch(
        r"\(sum\(sum_over_time\((?P<a>.+?)\[(?P<lookback>\d+)h\] offset (?P<gap>\d+)m\)\) > 0\) "
        r"unless \(sum\(sum_over_time\((?P<b>.+?)\[(?P<window>\d+)m\]\)\) > 0\)",
        query,
    )
    assert m, query
    assert m["a"] == m["b"] == series
    return m


def test_the_silent_policy_needs_a_heartbeat_to_have_existed():
    """DARK-SAFE BY CONSTRUCTION. The query is `(reports in the look-back before the window)
    unless (reports in the window)`: with no report series at all — an environment never armed —
    the left side is empty and so is the result. A bare absence condition, or `... < 1`, would
    page production the moment it was installed."""
    m = _silent_query()
    assert "reap_agentic_poll_report" in METRICS
    # The "was alive" side must end where the "is silent" side begins, or a poller that is
    # reporting right now would satisfy both and the two could not disagree.
    assert m["gap"] == m["window"]
    query = _silent_condition()["query"]
    # sum_over_time over the per-minute delta samples; increase()/rate() misread them.
    assert "increase(" not in query and "rate(" not in query


def test_the_silent_window_outlasts_a_slow_run_and_the_metrics_own_lag():
    """Two things can put a gap between two report lines AS MONITORING SEES THEM without the
    poller being dead: one run using its whole deadline, and a log-based metric arriving late —
    documented as up to 10 minutes. The window has to be longer than either."""
    from services.audit_scheduler import _JOB_RUN_DEADLINES

    window_s = int(_silent_query()["window"]) * 60
    assert window_s > _JOB_RUN_DEADLINES["reap_agentic_purchase_poll"]
    assert window_s > 10 * 60
    promql = _silent_condition()
    assert promql["duration"] == "300s" and promql["evaluationInterval"] == "60s"


def test_the_silent_look_back_is_the_longest_the_platform_documents():
    """A dead poller stops being "recently alive" when the look-back runs out, and after that no
    policy says anything — so the look-back is as long as it may be. For a user-defined log-based
    metric a PromQL condition may read "the most recent 25 hours" and "the sum of your retest
    window, alignment period, and any time shift caused by using the offset modifier must be at
    most 25 hours"."""
    m = _silent_query()
    duration_s = int(_silent_condition()["duration"].rstrip("s"))
    reach_s = int(m["lookback"]) * 3600 + int(m["gap"]) * 60 + duration_s
    assert int(m["lookback"]) == 24
    assert reach_s <= 25 * 3600, f"the query reaches back {reach_s}s, past the documented 25 hours"


def test_the_silent_policy_keeps_emailing_while_it_is_open():
    """One email for a poller that then stays dead for a day is one email somebody can miss."""
    policy = reap_policies()[SILENT_POLICY]
    (strategy,) = policy["alertStrategy"]["notificationChannelStrategy"]
    assert strategy["notificationChannelNames"] == policy["notificationChannels"] == [CHANNEL]
    seconds = int(strategy["renotifyInterval"].rstrip("s"))
    assert 30 * 60 <= seconds <= 24 * 3600, "the API bounds renotifyInterval to 30 minutes..24 hours"
    assert seconds == 3600
    assert policy["alertStrategy"]["autoClose"] == "3600s"
    doc = policy["documentation"]["content"]
    assert "snooze" in doc and "24 hours after the last report" in doc


def test_the_silent_policy_does_not_need_its_metric_to_exist_yet():
    """The heartbeat metric is created by the same run, seconds earlier, and a PromQL policy is
    refused when a metric it names has no descriptor — unless the condition says not to check.
    With the check off the API can no longer catch a typo, so the name is pinned here to the
    metric the script creates."""
    assert _silent_condition()["disableMetricValidation"] is True
    assert "logging_googleapis_com:user_reap_agentic_poll_report{" in _silent_condition()["query"]
    source = SCRIPT.read_text(encoding="utf-8")
    assert "upsert_log_metric reap_agentic_poll_report " in source


def test_the_optional_promql_arguments_leave_every_other_policy_as_it_was():
    """`upsert` deletes and re-creates, so a field the generator started adding to every PromQL
    policy would land on the live load-balancer policy on the next run. Without the optional
    arguments the generator's output has no renotify and no validation switch."""
    from test_monitoring_policy_snapshot import generated_policies

    lb = generated_policies()["prod: load balancer 5xx"]
    assert set(lb["alertStrategy"]) == {"autoClose"}
    (condition,) = lb["conditions"]
    assert set(condition["conditionPrometheusQueryLanguage"]) == {
        "query", "duration", "evaluationInterval"
    }


def test_every_policy_goes_to_the_channel_and_is_in_the_runbook():
    policies = reap_policies()
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for name in (STUCK_POLICY, FAILING_POLICY, SILENT_POLICY):
        policy = policies[name]
        assert policy["displayName"] == name
        assert policy["notificationChannels"] == [CHANNEL], name
        assert policy["alertStrategy"]["autoClose"] == "3600s"
        assert name in runbook, f"the runbook's Alerts section does not name {name!r}"


# ── the first run: policies over metrics created seconds earlier ─────────────────────────────
#
# Monitoring rejects a policy over a log metric it cannot see yet ("Cannot find metric(s) that
# match type ... it could take up to 10 minutes to become available"). Through `upsert` that
# rejection aborts the run. `upsert_on_new_metric` is the answer, and it is tested by running
# ITS OWN TEXT, extracted from the script, in a shell where `api` and `sleep` are stubs and
# `curl`/`gcloud` are tripwires. The provisioning script itself is never executed.

_REJECTED = json.dumps({"error": {"code": 404, "message": (
    'Cannot find metric(s) that match type = "logging.googleapis.com/user/reap_agentic_poll_stuck". '
    "If a metric was created recently, it could take up to 10 minutes to become available. "
    "Please try again soon.")}})

_API_STUB = r'''
import json, sys
state_path, method, path = sys.argv[1:4]
state = json.load(open(state_path))
state["calls"].append([method, path])
if method == "GET":
    out = state["get"] if state.get("get") is not None else {"alertPolicies": state["existing"]}
elif method == "POST":
    answers = state["post"]
    out = answers.pop(0) if len(answers) > 1 else answers[0]
else:
    out = {}
json.dump(state, open(state_path, "w"))
if out != "EMPTY":  # an empty body with exit 0: what a proxy or a truncated reply looks like
    print(json.dumps(out))
'''


def _shell_function(source: str, name: str) -> str:
    start = source.index(f"\n{name}() {{") + 1
    return source[start: source.index("\n}\n", start) + 3]


def _run_upserts(
    tmp_path, source, *, post, existing=(), tries=20, names=("prod: A",),
    function="upsert_on_new_metric", get=None,
):
    """Run `function` for each name against a scripted Monitoring API."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps(
        {"calls": [], "existing": list(existing), "post": list(post), "get": get}
    ))
    stub = tmp_path / "api_stub.py"
    stub.write_text(_API_STUB)
    calls = "".join(
        f"{function} {shlex.quote(n)} {shlex.quote(json.dumps({'displayName': n}))}\n"
        for n in names
    )
    script = f"""
set -euo pipefail
curl() {{ echo "TRIPWIRE: curl was called" >&2; exit 97; }}
gcloud() {{ echo "TRIPWIRE: gcloud was called" >&2; exit 97; }}
sleep() {{ echo "sleep $1" >> {shlex.quote(str(tmp_path / 'sleeps'))}; }}
api() {{ {shlex.quote(sys.executable)} {shlex.quote(str(stub))} {shlex.quote(str(state))} "$1" "$2"; }}
TOKEN=not-a-token
ENV=prod
{_shell_function(source, "environment_policy_name")}
{_shell_function(source, "environment_policy_body")}
{_shell_function(source, "check")}
{_shell_function(source, "require_policy_name")}
{_shell_function(source, "upsert")}
NEW_METRIC_TRIES={tries}
NEW_METRIC_RETRY_SECONDS=30
NEW_METRIC_DEFERRED=""
{_shell_function(source, "upsert_on_new_metric")}
{calls}
echo "DEFERRED=[$NEW_METRIC_DEFERRED] TRIES=$NEW_METRIC_TRIES"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    sleeps = (tmp_path / "sleeps").read_text().splitlines() if (tmp_path / "sleeps").exists() else []
    return proc, json.loads(state.read_text())["calls"], sleeps


_CREATED = {"name": "projects/p/alertPolicies/new", "displayName": "prod: A"}
_STALE = [
    {"name": "projects/p/alertPolicies/old1", "displayName": "prod: A"},
    {"name": "projects/p/alertPolicies/other", "displayName": "prod: host is down"},
    {"name": "projects/p/alertPolicies/old2", "displayName": "prod: A"},
]


def test_a_visible_metric_is_created_first_and_the_old_policy_deleted_after(tmp_path, source):
    """CREATE, THEN DELETE — the reverse of `upsert`. Every policy carrying the name goes (a run
    that died between the two steps leaves a pair), and nobody else's does."""
    proc, calls, sleeps = _run_upserts(tmp_path, source, post=[_CREATED], existing=_STALE)
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        ["GET", "alertPolicies"],
        ["POST", "alertPolicies"],
        ["DELETE", "alertPolicies/old1"],
        ["DELETE", "alertPolicies/old2"],
    ]
    assert sleeps == []
    assert "DEFERRED=[] TRIES=20" in proc.stdout


def test_a_metric_monitoring_cannot_see_yet_is_waited_for(tmp_path, source):
    proc, calls, sleeps = _run_upserts(
        tmp_path, source, post=[json.loads(_REJECTED)] * 3 + [_CREATED], existing=_STALE[:1]
    )
    assert proc.returncode == 0, proc.stderr
    assert [c[0] for c in calls] == ["GET", "POST", "POST", "POST", "POST", "DELETE"]
    assert sleeps == ["sleep 30"] * 3
    assert "DEFERRED=[] TRIES=17" in proc.stdout


def test_out_of_budget_the_policy_is_deferred_and_what_was_live_is_left_alone(tmp_path, source):
    """The run does NOT abort and does NOT delete: the policy that was live stays live, the name
    is kept for the final message, and the budget is SHARED — the second policy gets one attempt,
    not another ten minutes."""
    proc, calls, sleeps = _run_upserts(
        tmp_path, source, post=[json.loads(_REJECTED)], existing=_STALE, tries=2,
        names=("prod: A", "prod: B"),
    )
    assert proc.returncode == 0, proc.stderr
    assert [c[0] for c in calls] == ["GET", "POST", "POST", "POST", "GET", "POST"]
    assert not [c for c in calls if c[0] == "DELETE"], "a deferred policy's predecessor was deleted"
    assert sleeps == ["sleep 30"] * 2
    assert "DEFERRED=[prod: A, prod: B] TRIES=0" in proc.stdout
    assert "DEFERRED prod: A" in proc.stderr and "DEFERRED prod: B" in proc.stderr


def test_any_other_api_error_still_aborts_the_run(tmp_path, source):
    """Only the one known rejection is waited out. Everything else goes through `check`, as it
    does for every other policy in the script — and nothing is deleted on the way out."""
    denied = {"error": {"code": 403, "message": "Permission monitoring.alertPolicies.create denied"}}
    proc, calls, sleeps = _run_upserts(tmp_path, source, post=[denied], existing=_STALE)
    assert proc.returncode != 0 and proc.returncode != 97
    assert "policy prod: A FAILED: Permission monitoring.alertPolicies.create denied" in proc.stderr
    assert [c[0] for c in calls] == ["GET", "POST"]
    assert sleeps == [] and "DEFERRED=" not in proc.stdout


def test_the_script_wires_the_first_run_safely(source):
    body = _uncommented(source)
    # Every log metric exists before any policy is touched.
    first_policy = body.index('\nupsert "')
    for metric in METRICS:
        assert body.index(f"upsert_log_metric {metric} ") < first_policy
    # The three Reap policies, and only they, take the waiting path; they come after every
    # `upsert`, so a deferral cannot leave one of the nine existing policies unwritten.
    waited = re.findall(r'^upsert_on_new_metric "([^"]+)"', body, re.M)
    assert waited == [STUCK_POLICY, FAILING_POLICY, SILENT_POLICY]
    assert body.rindex('\nupsert "') < body.index('\nupsert_on_new_metric "')
    assert not re.search(r'^upsert "prod: Reap', body, re.M)
    # Ten minutes by default, shared: 20 tries of 30 s.
    assert 'NEW_METRIC_TRIES="${NEW_METRIC_TRIES:-20}"' in body
    assert 'NEW_METRIC_RETRY_SECONDS="${NEW_METRIC_RETRY_SECONDS:-30}"' in body
    # A deferral is said out loud and fails the run — after the summary, and without stepping in
    # front of the channel check, which must still be able to exit first.
    said = body.index('echo "NOT CREATED: $NEW_METRIC_DEFERRED"')
    channel = body.index('if [ "${CHANNEL_UNDELIVERABLE:-0}" = 1 ]; then')
    assert body.index('print("policies:", len(ps))') < said < channel
    tail = body[channel:]
    assert re.search(r'if \[ -n "\$NEW_METRIC_DEFERRED" \]; then\n  exit 1\nfi\s*$', tail)


# ── replies that used to be read as good news ────────────────────────────────────────────────
#
# Review of #2488, round 2, stub-ran the function: an EMPTY create reply was treated as
# "created" (the predecessor was deleted and nothing replaced it: 11 policies, exit 0), and an
# ERROR DOCUMENT from the list was treated as "no predecessors" (a duplicate: 13 policies,
# exit 0). Both abort now, in `upsert_on_new_metric` and in `upsert`, which had the same holes.

_LIST_ERROR = {"error": {"code": 503, "message": "The service is currently unavailable."}}


@pytest.mark.parametrize("reply", ["EMPTY", {}, {"displayName": "prod: A"}, {"name": ""}],
                         ids=["empty-body", "empty-object", "no-name", "blank-name"])
def test_a_create_reply_that_names_no_policy_deletes_nothing(tmp_path, source, reply):
    proc, calls, sleeps = _run_upserts(tmp_path, source, post=[reply], existing=_STALE)
    assert proc.returncode not in (0, 97), proc.stdout + proc.stderr
    assert "policy prod: A FAILED: the reply carries no policy name" in proc.stderr
    assert [c[0] for c in calls] == ["GET", "POST"], "the predecessor was deleted"
    assert "DEFERRED=" not in proc.stdout, "the run carried on past a create that created nothing"


@pytest.mark.parametrize("listing", [_LIST_ERROR, [], "EMPTY"], ids=["error-doc", "not-an-object", "empty-body"])
def test_a_list_that_is_not_a_policy_list_creates_nothing(tmp_path, source, listing):
    """An error document is not "no predecessors". Creating after one leaves two policies under
    one displayName, and the next run deletes only what it can see."""
    proc, calls, sleeps = _run_upserts(tmp_path, source, post=[_CREATED], get=listing)
    assert proc.returncode not in (0, 97), proc.stdout + proc.stderr
    assert calls == [["GET", "alertPolicies"]], "a policy was created after an unreadable list"
    if listing == _LIST_ERROR:
        assert "list policies FAILED" in proc.stderr and "currently unavailable" in proc.stderr


@pytest.mark.parametrize("function", ["upsert", "upsert_on_new_metric"])
def test_a_project_with_no_policies_at_all_is_not_an_error(tmp_path, source, function):
    """THE FIRST-EVER RUN. A project with zero alert policies answers the list with a bare `{}`
    — no `alertPolicies` key at all; `pivota-staging` does today. That is a legitimate empty
    list, not a malformed reply: the guard that refuses an error document must let it through,
    and the run goes on to create. A guard written as "the key must be present" would make the
    script unable to bootstrap a project."""
    proc, calls, sleeps = _run_upserts(tmp_path, source, post=[_CREATED], get={}, function=function)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls == [["GET", "alertPolicies"], ["POST", "alertPolicies"]]
    assert sleeps == [] and "   prod: A" in proc.stdout


def test_plain_upsert_also_aborts_on_an_error_list_and_on_a_nameless_create(tmp_path, source):
    """The nine existing policies go through `upsert`, which had both holes. The change there is
    strictly "abort where it used to continue": the healthy path issues the same calls."""
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir(); (tmp_path / "c").mkdir()
    proc, calls, _ = _run_upserts(tmp_path / "a", source, post=[_CREATED], get=_LIST_ERROR,
                                  function="upsert")
    assert proc.returncode not in (0, 97) and calls == [["GET", "alertPolicies"]]
    assert "list policies FAILED" in proc.stderr

    proc, calls, _ = _run_upserts(tmp_path / "b", source, post=["EMPTY"], function="upsert")
    assert proc.returncode not in (0, 97)
    assert "policy prod: A FAILED: the reply carries no policy name" in proc.stderr

    proc, calls, _ = _run_upserts(tmp_path / "c", source, post=[_CREATED], function="upsert")
    assert proc.returncode == 0, proc.stderr
    assert calls == [["GET", "alertPolicies"], ["POST", "alertPolicies"]]
    assert "   prod: A" in proc.stdout


def test_the_nine_existing_policies_render_exactly_as_before_this_change():
    """`upsert` was edited, and it deletes and re-creates: what it SENDS for the nine policies
    that exist in prod must not have moved by a byte. The digest is of the generators' raw output
    for every `upsert "..."` call, taken from `origin/main` (0524b5911) before this branch
    touched the script and identical on it. A deliberate change to one of those policies changes
    this digest; update it in the same commit, on purpose."""
    import hashlib

    source = SCRIPT.read_text(encoding="utf-8")
    generators = {
        name: source.split(name + "() {", 1)[1].split("python3 -c '\n", 1)[1].split("' \"$@\"", 1)[0]
        for name in ("policy", "promql_policy")
    }
    rendered = {}
    for name, generator, body in re.findall(
        r'^upsert "([^"]+)" "\$\((policy|promql_policy) (.*?)\)"$', source, re.S | re.M
    ):
        args = shlex.split(body.replace("\\\n", "").replace("\\`", "`"))
        rendered[name] = subprocess.run(
            [sys.executable, "-c", generators[generator], *args, CHANNEL],
            text=True, capture_output=True, check=True,
        ).stdout
    assert len(rendered) == 9, sorted(rendered)
    digest = hashlib.sha256(json.dumps(rendered, sort_keys=True).encode()).hexdigest()
    assert digest == "9ff583df92b8d4a70c771356c6ec7bc4b6336f17c50a907ab4f323beca2b3186"
