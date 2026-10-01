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
import os
import re
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
from test_reap_agentic_purchase_poll import (  # noqa: E402,F401
    _db,
    _env,
    _no_network,
    _park,
    _raw,
    _run,
    _start,
    attribution,
    reap,
)
from test_monitoring_policy_snapshot import generated_policies  # noqa: E402

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


async def test_a_deploy_landing_mid_run_does_not_page(filters, monkeypatch):
    """Shutting the service down cancels the runner's WRAPPER while a run is in flight, and the
    runner says so at ERROR ("ABANDONED as a zombie (wrapper cancelled ...)"). That is every
    deploy that lands mid-step, not a failing poller."""
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

    policy = generated_policies()[STUCK_POLICY]
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
    policy = generated_policies()[FAILING_POLICY]
    threshold = _threshold(policy)
    assert threshold["filter"] == (
        'metric.type="logging.googleapis.com/user/reap_agentic_poll_failing" '
        'AND resource.type="cloud_run_revision"'
    )
    assert threshold["comparison"] == "COMPARISON_GT" and threshold["thresholdValue"] == 0
    assert threshold["duration"] == "0s"
    assert "docs/runbooks/reap_agentic_purchase.md" in policy["documentation"]["content"]


def test_the_silent_policy_needs_a_heartbeat_to_have_existed():
    """DARK-SAFE BY CONSTRUCTION. The query is `(reports in the 6h before the window) unless
    (reports in the window)`: with no report series at all — an environment never armed — the
    left side is empty and so is the result. A bare absence condition, or `... < 1`, would page
    production the moment it was installed."""
    from services.audit_scheduler import _JOB_RUN_DEADLINES

    policy = generated_policies()[SILENT_POLICY]
    (condition,) = policy["conditions"]
    promql = condition["conditionPrometheusQueryLanguage"]
    series = 'logging_googleapis_com:user_reap_agentic_poll_report{monitored_resource="cloud_run_revision"}'
    m = re.fullmatch(
        r"\(sum\(sum_over_time\((?P<a>.+?)\[(?P<lookback>\d+)h\] offset (?P<gap>\d+)m\)\) > 0\) "
        r"unless \(sum\(sum_over_time\((?P<b>.+?)\[(?P<window>\d+)m\]\)\) > 0\)",
        promql["query"],
    )
    assert m, promql["query"]
    assert m["a"] == m["b"] == series
    assert "reap_agentic_poll_report" in METRICS
    # The "was alive" side must end where the "is silent" side begins, or a poller that is
    # reporting right now would satisfy both and the two could not disagree.
    assert m["gap"] == m["window"]
    # One run may take its whole deadline between two reports.
    assert int(m["window"]) * 60 >= _JOB_RUN_DEADLINES["reap_agentic_purchase_poll"]
    # Long enough that the incident is still open when somebody reads the email; short enough
    # that a rail disarmed on purpose stops being "recently alive" the same day.
    assert int(m["lookback"]) == 6
    assert promql["duration"] == "300s" and promql["evaluationInterval"] == "60s"
    # sum_over_time over the per-minute delta samples; increase()/rate() misread them.
    assert "increase(" not in promql["query"] and "rate(" not in promql["query"]
    assert "disarmed on purpose" in policy["documentation"]["content"]


def test_every_policy_goes_to_the_channel_and_is_in_the_runbook():
    policies = generated_policies()
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for name in (STUCK_POLICY, FAILING_POLICY, SILENT_POLICY):
        policy = policies[name]
        assert policy["displayName"] == name
        assert policy["notificationChannels"], name
        assert policy["alertStrategy"] == {"autoClose": "3600s"}
        assert name in runbook, f"the runbook's Alerts section does not name {name!r}"
