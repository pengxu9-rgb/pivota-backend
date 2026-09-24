"""The alerting worked on 2026-08-28. Nobody heard it.

The backend wedged on database pool exhaustion at 10:46:56Z and served 503/504 for 5h40m. The
uptime check detected it correctly — `check_passed` for api.pivota.cc went to 0.00 by 12:32 and
stayed there — and `prod: host is down` fired as designed. The single notification channel pointed
at an address nobody reads, chosen by a silent default: `ALERT_EMAIL` fell back to
`gcloud config get-value account`, i.e. whoever last ran the script.

Detection was never the gap. DELIVERY was. These tests pin the three lines that decide whether a
firing alert reaches a person, because all three are invisible on a dashboard that cheerfully
reports "5 policies, all enabled".

Same family as tests/test_setup_scheduler_is_safe_to_rerun.py: assertions against the shell source,
because there is nothing importable here.
"""

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "infra" / "gcp" / "setup_monitoring.sh"


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _uncommented(text: str) -> str:
    """Drop comment lines AND human-prose arguments.

    Load-bearing: this file's rationale mentions `gcloud config get-value account` and the old
    threshold in prose. A whole-file grep would match the explanation and pass while the code
    underneath had regressed.

    Stripping `#` alone was not enough — that leak simply moved into non-comment prose. The
    string "PoolCheckoutTimeout" also appears in the metric's `--description` and in the alert
    policy's `documentation` text, so an assertion for it passed against a GUTTED --log-filter.
    Operator-facing prose is documentation wherever it lives; only executable content may
    satisfy an assertion here.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # `"$GCLOUD" logging metrics create ...` also begins with a quote, so the
        # discriminator is `"` NOT followed by `$`: prose opens with a word, a
        # command opens with an expansion.
        if stripped.startswith("--description"):
            continue
        if stripped.startswith('"') and not stripped.startswith('"$'):
            continue
        kept.append(line)
    return "\n".join(kept)


def test_prod_refuses_to_infer_the_alert_destination(source: str) -> None:
    """The exact regression: prod must not guess where pages go."""
    body = _uncommented(source)
    assert 'if [ "$ENV" = prod ]; then' in body
    assert "ALERT_EMAIL is required for prod" in body
    # The convenience default must survive for staging, which pages no one — deleting it
    # outright would be a different (and worse) change than the one this guards.
    assert 'config get-value account' in body


def test_the_inferred_default_is_not_reachable_from_prod(source: str) -> None:
    """A fallback that prod can still reach is the bug wearing a guard.

    Asserts ordering, not mere presence: the `exit 2` for prod must come BEFORE the
    `gcloud config get-value account` assignment, or prod falls through to it anyway.
    """
    body = _uncommented(source)
    guard = body.index("ALERT_EMAIL is required for prod")
    fallback = body.index('ALERT_EMAIL="$("$GCLOUD" config get-value account')
    assert guard < fallback, "prod guard must precede the inferred fallback"
    # The assertion this file exists for. Ordering and message text are both
    # satisfied by four echoes that print a scary warning and then fall straight
    # through into the fallback — verified: deleting this one line reproduced the
    # original bug verbatim while the whole suite stayed green.
    assert "exit 2" in body[guard:fallback], (
        "the prod guard warns but does not EXIT — it falls through to the inferred fallback"
    )


def test_the_verification_check_covers_the_create_branch(source: str) -> None:
    """The CREATE arm is the one that manufactures an undeliverable channel.

    An API-created email channel is born UNVERIFIED. Putting the check only in the
    reuse arm instruments the branch that observes the damage and stays silent on
    the branch that causes it — which is exactly how the live 08-28 channel came to
    exist while the script printed a clean summary.

    Asserts by position: the check must sit after the `fi` that closes the
    create/reuse if-else, not inside either arm.
    """
    body = _uncommented(source)
    create = body.index("create channel")
    check = body.index("CHANNEL_VERIFIED=")
    closing_fi = body.index("\nfi\n", create)
    assert check > closing_fi, (
        "the verification check is inside an arm of the if/else; it must follow both"
    )


def test_an_unverified_channel_is_surfaced(source: str) -> None:
    """Cloud Monitoring delivers nothing to an unverified email channel.

    The live channel on 2026-08-28 reported no verificationStatus at all, so every policy was
    firing into the void with no signal anywhere that this was happening.
    """
    body = _uncommented(source)
    assert "verificationStatus" in body
    assert '[ "$CHANNEL_VERIFIED" != VERIFIED ]' in body
    # SHORTEST-prefix strip. `##` yields a bare channel id, the GET 404s, the
    # status reads empty and the warning fires on every run including a genuinely
    # verified channel — a permanent false alarm is worse than no alarm, because
    # it teaches the operator to skip the one line that matters.
    assert '${CHANNEL#projects/*/}' in body
    assert '${CHANNEL##projects/*/}' not in body
    # An undeliverable channel must fail the run, not just print to stderr.
    assert "CHANNEL_UNDELIVERABLE" in body and "exit 1" in body


def test_the_lb_5xx_threshold_is_reachable_at_real_traffic(source: str) -> None:
    """0.2 req/s asks for 12 5xx per second on an API serving ~0.03 req/s.

    A total outage returning 5xx to every caller still sat an order of magnitude under it, which
    is why this policy had never fired once. The assertion is on the *magnitude* rather than an
    exact number so the threshold stays tunable — what must not come back is a value that no
    achievable failure can reach.
    """
    body = _uncommented(source)
    assert "COMPARISON_GT 0.2 300s" not in body, "the unreachable threshold is back"
    marker = "prod: load balancer 5xx"
    idx = body.rindex(marker)
    tail = body[idx : idx + 800]
    threshold = float(tail.split("COMPARISON_GT")[1].split()[0])
    assert threshold <= 0.05, (
        f"threshold {threshold}/s is unreachable at ~0.03 req/s baseline traffic"
    )
    # The window is half the claim: 0.01/s over 300s needs 4 errors, over 600s
    # needs 7. The comment justifies the 600s figure, so a silent revert to 300s
    # makes the rationale describe a policy that no longer exists.
    assert "COMPARISON_GT %s 600s 600s" % tail.split("COMPARISON_GT")[1].split()[0] in tail


def test_pool_exhaustion_has_its_own_alert(source: str) -> None:
    """num_backends cannot catch a client-side leak.

    From the server a leaked connection is indistinguishable from an idle one, so the Cloud SQL
    policy read 9% throughout an outage caused entirely by connection exhaustion. This alert
    watches the application's own symptom instead.
    """
    body = _uncommented(source)
    assert "logging metrics create" in body
    assert "prod: database pool exhausted" in body
    assert "logging.googleapis.com/user/pool_checkout_timeout" in body
    # Anchored to the --log-filter, not to the bare class name: that name also
    # appears in --description and in the policy documentation, so the loose
    # assertion passed against a filter with its predicates deleted.
    log_filter = body[body.index("--log-filter=") : body.index("--log-filter=") + 300]
    assert 'textPayload:"PoolCheckoutTimeout"' in log_filter
    # The line the code DELIBERATELY emits carries no class name and is a WARNING,
    # while the designed path returns HTTPException(503) with no traceback at all.
    # Requiring severity>=ERROR plus the class name matches only when the exception
    # escapes every classifier — the outcome the type exists to prevent.
    assert 'textPayload:"database pool checkout timed out"' in log_filter
    assert "severity>=ERROR" not in log_filter


def test_the_log_metric_is_idempotent(source: str) -> None:
    """setup_monitoring.sh advertises itself as safe to re-run.

    `gcloud logging metrics create` fails on an existing metric, so without the describe guard a
    second run would abort under `set -e` before reaching the policies below it.
    """
    body = _uncommented(source)
    assert "logging metrics describe" in body
    create = body.index("logging metrics create")
    describe = body.index("logging metrics describe")
    assert describe < create, "existence check must precede creation"
    # Polarity, not just order. `if ! describe` inverts the guard: it skips
    # creation when the metric is absent and retries it when present, which under
    # `set -e` aborts the run before any policy below is reached.
    line = body[body.rindex("\n", 0, describe) + 1 : describe]
    assert "!" not in line, "the existence guard is inverted"


# ---- retailer-ingest-drain: the two outcomes that exit 0 ------------------------------------
#
# The drain records a held or failed stage and exits 0, so "prod: Cloud Run job failing" never sees
# them. Two log-based metrics key on the job's one summary line instead. These tests feed that line
# - produced by the job's REAL main(), not restated here - through the filters' own substring
# predicates, so a change to the job's output format or to the filter fails here rather than as a
# metric that silently never increments.
SCHEDULER = SCRIPT.parent / "setup_scheduler.sh"


def _filter(source: str, var: str) -> str:
    m = re.search(rf"^{var}='(.*)'$", source, re.M)
    assert m, f"{var} not found as a single-quoted assignment"
    return m.group(1)


_ATOM = re.compile(r'^textPayload(:|=~)"((?:[^"\\]|\\.)*)"$')


def _matches(log_filter: str, line: str) -> bool:
    """Evaluate the filter's textPayload clauses against one log line.

    Understands exactly the shapes the drain filters use: top-level AND of atoms or of one
    parenthesised OR group; `textPayload:"..."` is a case-insensitive substring (Logging's `:`),
    `textPayload=~"..."` an RE2 search. resource.* atoms are checked by their own test. Anything
    else fails loudly rather than being skipped, so a new clause cannot go unevaluated.
    """
    def atom(text: str) -> bool:
        m = _ATOM.match(text.strip())
        assert m, f"unrecognised clause {text!r}"
        value = m.group(2).replace('\\"', '"')
        return value.lower() in line.lower() if m.group(1) == ":" else bool(re.search(value, line))

    evaluated = 0
    for clause in log_filter.split(" AND "):
        clause = clause.strip()
        if clause.startswith("resource."):
            continue
        evaluated += 1
        if clause.startswith("(") and clause.endswith(")"):
            if not any(atom(a) for a in clause[1:-1].split(" OR ")):
                return False
        elif not atom(clause):
            return False
    assert evaluated >= 2, f"expected the marker and an outcome clause in {log_filter!r}"
    return True


def _summary_line(monkeypatch, capsys, stage, counts) -> str:
    """The line the REAL main() + drain_once() print. Only the ledger and the stage are faked;
    `stage=None` is an idle tick (nothing claimed), which still reports the lane's counts."""
    import jobs.retailer_ingest_drain as drain

    class _DB:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

    async def _claim(**kw):
        return None if stage is None else {"id": "rij_1", "status": "queued"}

    async def _run_stage(job, *, db):
        return dict(stage)

    async def _counts(**kw):
        return dict(counts)

    monkeypatch.setenv("RETAILER_INGEST_DRAIN_ENABLED", "1")
    monkeypatch.setattr("sys.argv", ["drain"])
    monkeypatch.setattr(drain, "database", _DB())
    monkeypatch.setattr(drain.ledger, "claim_due_job", _claim)
    monkeypatch.setattr(drain.ledger, "status_counts", _counts)
    monkeypatch.setattr(drain, "run_stage", _run_stage)
    assert drain.main() == 0
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(drain.SUMMARY_MARKER)]
    return line


# (stage summary, lane counts by status). status_counts() GROUPs BY status, so a status with no
# jobs is ABSENT rather than 0; both shapes are covered.
HELD_STAGE = {"job_id": "rij_1", "stage": "dry_run", "outcome": "held", "status": "held"}
HELD = [
    (HELD_STAGE, {"held": 1}),
    (HELD_STAGE, {"queued": 2}),                     # counts read before the hold landed
    # A STANDING hold: nothing claimed, or another store's stage ran - still alerting.
    (None, {"held": 1, "queued": 4}),
    (None, {"held": 12}),
    ({"job_id": "rij_2", "stage": "apply", "outcome": "applied", "status": "done"}, {"done": 3, "held": 2}),
]
FAILED = [
    ({"job_id": "rij_1", "stage": "apply", "outcome": "apply_refused", "status": "failed"}, {"failed": 1}),
    ({"job_id": "rij_1", "stage": "dry_run", "outcome": "crawl_failed", "status": "failed",
      "reason": 'crawl failed: upstream said {"outcome": "held"} and {"held": 3}'}, {"failed": 1}),
    ({"job_id": "rij_1", "stage": "apply", "outcome": "readback_failed", "status": "failed"}, {"failed": 2}),
]
QUIET = [
    (None, {}),
    (None, {"queued": 3, "done": 10, "failed": 4}),     # a terminal failure is not re-alerted per tick
    (None, {"held": 0}),
    ({"job_id": "rij_1", "stage": "dry_run", "outcome": "clean", "status": "apply_due"}, {"apply_due": 1}),
    ({"job_id": "rij_1", "stage": "apply", "outcome": "applied", "status": "done"}, {"done": 1}),
    ({"job_id": "rij_1", "stage": "dry_run", "outcome": "crawl_throttled", "status": "queued",
      "reason": 'throttled, retry later: {"status": "failed"}'}, {"queued": 1}),
    ({"job_id": "rij_1", "stage": "dry_run", "outcome": "nothing_to_ingest", "status": "nothing"}, {"nothing": 1}),
]


def test_the_held_metric_counts_every_tick_while_a_store_is_held(source, monkeypatch, capsys) -> None:
    held = _filter(source, "RID_HELD_FILTER")
    for stage, counts in HELD:
        assert _matches(held, _summary_line(monkeypatch, capsys, stage, counts)), (stage, counts)
    for stage, counts in FAILED + QUIET:
        assert not _matches(held, _summary_line(monkeypatch, capsys, stage, counts)), (stage, counts)


def test_the_failed_metric_counts_exactly_the_failed_stages(source, monkeypatch, capsys) -> None:
    failed = _filter(source, "RID_FAILED_FILTER")
    for stage, counts in FAILED:
        assert _matches(failed, _summary_line(monkeypatch, capsys, stage, counts)), (stage, counts)
    for stage, counts in HELD + QUIET:
        assert not _matches(failed, _summary_line(monkeypatch, capsys, stage, counts)), (stage, counts)


def test_the_drain_filters_name_the_job_the_scheduler_creates(source) -> None:
    scheduler = SCHEDULER.read_text(encoding="utf-8")
    for var in ("RID_HELD_FILTER", "RID_FAILED_FILTER"):
        f = _filter(source, var)
        assert 'resource.type="cloud_run_job"' in f
        (job,) = re.findall(r'resource\.labels\.job_name="([^"]+)"', f)
        assert re.search(rf"^mkcrawljob {re.escape(job)} ", scheduler, re.M), (
            f"{var} watches job {job!r}, which setup_scheduler.sh does not create"
        )


def test_the_drain_alerts_exist_and_fire_on_one_event(source) -> None:
    body = _uncommented(source)
    # DURATION 0s on both: a log counter writes no points between matching lines. HELD aligns over
    # 3600s - more than the 10-minute cadence - so a standing hold is one incident, not one per tick.
    for metric, window in (("retailer_ingest_drain_held", "3600s"), ("retailer_ingest_drain_failed", "300s")):
        assert f"upsert_log_metric {metric} " in body
        at = body.index(f'metric.type="logging.googleapis.com/user/{metric}"')
        tail = body[at: at + 250]
        assert f"COMPARISON_GT 0 {window} 0s " in tail, tail
    scheduler = SCHEDULER.read_text(encoding="utf-8")
    assert 'sched retailer-ingest-drain-cron "*/10 * * * *"' in scheduler, (
        "the held window is sized against a 10-minute cadence; re-derive it if the cadence moves"
    )
    assert '"prod: retailer ingest held for review"' in body
    assert '"prod: retailer ingest job failed"' in body


def test_a_drain_crash_is_still_the_job_failing_policy(source) -> None:
    """An unexpected exception exits 1: the generic policy must not be scoped to other jobs."""
    body = _uncommented(source)
    at = body.index('metric.type="run.googleapis.com/job/completed_task_attempt_count"')
    line = body[at: body.index("\n", at)]
    assert 'metric.label.result="failed"' in line
    assert "job_name" not in line, "the job-failing policy is scoped; the drain would page nowhere"


def test_the_drain_metrics_are_idempotent(source) -> None:
    body = _uncommented(source)
    at = body.index("upsert_log_metric() {")
    fn = body[at: body.index("\n}", at)]
    assert fn.index("logging metrics describe") < fn.index("logging metrics update") < fn.index("logging metrics create")
    guard = fn[fn.rindex("\n", 0, fn.index("logging metrics describe")) + 1: fn.index("logging metrics describe")]
    assert "!" not in guard, "the existence guard is inverted"
