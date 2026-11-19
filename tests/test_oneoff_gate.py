"""The shared production one-off gate opens only on a clean read, and closes on anything else."""

from datetime import datetime, timedelta, timezone
import json
import subprocess
import urllib.parse

import pytest

from scripts.ops import oneoff_gate as gate

QUIET = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)
CRON = "example-db-cron"


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def pages(*values, span_minutes=4):
    count = len(values)
    points = [{"value": {"doubleValue": v},
               "interval": {"endTime": stamp(QUIET - timedelta(minutes=span_minutes * i / max(count - 1, 1)))}}
              for i, v in enumerate(values)]
    return [{"timeSeries": [{"points": points}]}]


def job(name, *, latest=None):
    return {"metadata": {"name": name}, "status": {"latestCreatedExecution": latest} if latest else {}}


def execution(job_name, *, done=True, started=None):
    started = started if started is not None else stamp(QUIET - timedelta(seconds=30))
    return {"metadata": {"labels": {"run.googleapis.com/job": job_name}},
            "status": {"completionTime": stamp(QUIET)} if done else {"startTime": started}}


def required_jobs():
    return {name: job(name) for name in gate.REQUIRED_JOBS}


def evaluate(now=QUIET, *, cpu=None, executions=None, jobs=None, per_job=None):
    return gate.evaluate(now, pages=cpu if cpu is not None else pages(0.05, 0.06, 0.07),
                         executions=executions if executions is not None else [execution("example-done")],
                         jobs=jobs if jobs is not None else required_jobs(), per_job=per_job or {})


# ------------------------------------------------------------------ windows (through evaluate, check_once and main)

@pytest.mark.parametrize(("hour", "minute", "closed"), [
    (5, 14, False), (5, 15, True), (6, 14, True), (6, 15, False),
    (10, 29, False), (10, 30, True), (11, 19, True), (11, 20, False),
])
def test_windows_are_utc_half_open_through_evaluate(hour, minute, closed):
    now = datetime(2026, 1, 2, hour, minute, tzinfo=timezone.utc)
    if closed:
        with pytest.raises(gate.GateClosed, match="inside"):
            evaluate(now)
    else:
        evaluate(now)


def test_window_in_another_timezone_is_read_in_utc():
    pacific = timezone(timedelta(hours=-8))
    with pytest.raises(gate.GateClosed, match="inside 05:15-06:15Z"):
        evaluate(datetime(2026, 1, 1, 21, 30, tzinfo=pacific))


def test_check_once_refuses_a_closed_window_before_any_read(monkeypatch):
    monkeypatch.setattr(gate, "gcloud_json", lambda *a, **k: pytest.fail("read inside a closed window"))
    with pytest.raises(gate.GateClosed, match="inside 10:30-11:20Z"):
        gate.check_once(lambda: datetime(2026, 1, 2, 10, 45, tzinfo=timezone.utc))


def test_main_rechecks_the_window_as_its_last_step(monkeypatch, capsys):
    times = iter([datetime(2026, 1, 2, 5, 15, 1, tzinfo=timezone.utc)])
    monkeypatch.setattr(gate, "check_once", lambda clock: "summary")
    assert gate.main(clock=lambda: next(times)) == 3
    assert capsys.readouterr().out == "GATE_CLOSED: inside 05:15-06:15Z\n"


# ------------------------------------------------------------------ CPU

def test_open_on_quiet_database():
    assert evaluate(cpu=pages(0.05, 0.19, 0.1)).startswith("cpu_peak_5m=0.19 ")


@pytest.mark.parametrize("values", [(0.05, 0.20, 0.1), (0.9, 0.1, 0.1)])
def test_closed_at_or_above_cpu_limit(values):
    with pytest.raises(gate.GateClosed, match="CPU peak"):
        evaluate(cpu=pages(*values))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "NaN"])
def test_a_non_finite_point_after_a_valid_one_closes(bad):
    with pytest.raises(gate.GateClosed, match="non-finite"):
        evaluate(cpu=pages(0.05, bad, 0.05))


@pytest.mark.parametrize(("cpu", "message"), [
    ([], "too few CPU points"), (pages(0.05), "too few CPU points"), (pages(0.05, 0.05), "too few CPU points"),
    (pages(0.05, 0.05, 0.05, span_minutes=2), "too few CPU points"),
    ([{"timeSeries": [{"points": [{"value": {}}]}]}], "unreadable CPU point"),
])
def test_too_few_or_unreadable_points_close(cpu, message):
    with pytest.raises(gate.GateClosed, match=message):
        evaluate(cpu=cpu)


def test_cpu_points_on_two_pages_are_all_read():
    first, second = pages(0.05, 0.05, 0.05)[0], {"timeSeries": [{"points": [
        {"value": {"doubleValue": 0.5}, "interval": {"endTime": stamp(QUIET)}}]}]}
    with pytest.raises(gate.GateClosed, match="CPU peak 0.5"):
        evaluate(cpu=[first, second])


class Reply:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.body


def test_cpu_query_names_the_database_metric_and_5_minute_window(monkeypatch):
    seen = []
    monkeypatch.setattr(gate.urllib.request, "urlopen", lambda request, timeout: seen.append(request) or Reply({}))
    gate.fetch_cpu_series(QUIET, "token")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0].full_url).query)
    assert seen[0].full_url.startswith("https://monitoring.googleapis.com/v3/projects/pivota-prod/timeSeries?")
    assert query["filter"] == ['metric.type="cloudsql.googleapis.com/database/cpu/utilization" AND '
                               'resource.labels.database_id="pivota-prod:pivota-pg"']
    assert query["interval.startTime"] == ["2026-01-02T06:55:00Z"] and query["interval.endTime"] == ["2026-01-02T07:00:00Z"]
    assert seen[0].headers["Authorization"] == "Bearer token"


def test_cpu_pages_are_followed_and_errors_close(monkeypatch):
    replies = iter([Reply({"timeSeries": [], "nextPageToken": "next"}), Reply({"timeSeries": []})])
    seen = []
    monkeypatch.setattr(gate.urllib.request, "urlopen", lambda request, timeout: seen.append(request) or next(replies))
    assert len(gate.fetch_cpu_series(QUIET, "t")) == 2
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(seen[1].full_url).query)["pageToken"] == ["next"]
    monkeypatch.setattr(gate.urllib.request, "urlopen", lambda request, timeout: Reply({"executionErrors": [{"code": 3}]}))
    with pytest.raises(gate.GateClosed, match="executionErrors"):
        gate.fetch_cpu_series(QUIET, "t")
    monkeypatch.setattr(gate.urllib.request, "urlopen", lambda request, timeout: Reply({"nextPageToken": "again"}))
    with pytest.raises(gate.GateClosed, match="too many pages"):
        gate.fetch_cpu_series(QUIET, "t")


def test_monitoring_failure_closes(monkeypatch):
    def urlopen(*_args, **_kwargs):
        raise OSError("network")

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    with pytest.raises(gate.GateClosed, match="monitoring read failed"):
        gate.fetch_cpu_series(QUIET, "token")


# ------------------------------------------------------------------ jobs and executions

@pytest.mark.parametrize("name", [CRON, "example-oneoff-1", gate.REQUIRED_JOBS[0]])
def test_any_running_job_closes_by_default(name):
    with pytest.raises(gate.ShortRunClosed, match=f"running: {name}"):
        evaluate(executions=[execution(name, done=False)])
    jobs = {**required_jobs(), name: job(name, latest={"name": name + "-x",
                                                       "creationTimestamp": stamp(QUIET - timedelta(seconds=10))})}
    with pytest.raises(gate.ShortRunClosed, match=name):
        evaluate(jobs=jobs)


@pytest.mark.parametrize("required", gate.REQUIRED_JOBS)
def test_a_running_required_job_seen_only_by_its_own_listing_closes(required):
    # An old long run that is neither its job's latest execution nor in the global list.
    old = execution(required, done=False, started=stamp(QUIET - timedelta(hours=3)))
    with pytest.raises(gate.GateClosed, match=f"long-running database job execution\\(s\\): {required}"):
        evaluate(per_job={required: [old]})


@pytest.mark.parametrize("started", [stamp(QUIET - timedelta(seconds=61)), "", "not a time"])
def test_long_or_undated_run_is_long_running_and_not_retryable(started):
    row = execution(CRON, done=False)
    row["status"]["startTime"] = started
    with pytest.raises(gate.GateClosed, match="long-running") as caught:
        evaluate(executions=[row])
    assert not isinstance(caught.value, gate.ShortRunClosed)


def failed_import(job_name, **status):
    """An execution whose image never imported: Completed=False, no completionTime (seen in prod since August)."""
    row = execution(job_name, done=False, started=stamp(QUIET - timedelta(days=80)))
    row["status"].update(conditions=[{"type": "Completed", "status": "False", "reason": "ContainerImageImportFailed"}],
                         **status)
    return row


@pytest.mark.parametrize("where", ["global", "per_job"])
def test_a_failed_or_cancelled_execution_without_a_completion_time_is_finished(where):
    row = failed_import(CRON)
    if where == "global":
        assert evaluate(executions=[execution("example-done"), row]).startswith("cpu_peak_5m=")
    else:
        assert evaluate(per_job={gate.REQUIRED_JOBS[0]: [row]}).startswith("cpu_peak_5m=")


def test_every_task_failed_or_cancelled_and_none_running_is_finished():
    row = execution(CRON, done=False, started=stamp(QUIET - timedelta(days=3)))
    row["spec"] = {"taskCount": 2}
    row["status"].update(failedCount=1, cancelledCount=1)
    assert evaluate(executions=[execution("example-done"), row]).startswith("cpu_peak_5m=")
    row["status"]["runningCount"] = 1                       # one task still running: not finished
    with pytest.raises(gate.GateClosed, match="long-running"):
        evaluate(executions=[row])
    row["status"].update(runningCount=0, cancelledCount=0)  # one of two tasks ended: not finished
    with pytest.raises(gate.GateClosed, match="long-running"):
        evaluate(executions=[row])


@pytest.mark.parametrize("conditions", [
    [{"type": "Completed", "status": "Unknown"}],
    [{"type": "Ready", "status": "False"}],                 # not the Completed condition
    [{"type": "Completed", "status": "false"}],             # not the exact API value
    "unreadable",
])
def test_an_in_progress_or_unreadable_execution_older_than_60_s_still_closes(conditions):
    row = execution(CRON, done=False, started=stamp(QUIET - timedelta(seconds=61)))
    row["status"]["conditions"] = conditions
    with pytest.raises(gate.GateClosed, match="long-running"):
        evaluate(executions=[row])


@pytest.mark.parametrize(("completion_status", "closes"), [
    ("EXECUTION_FAILED", False), ("EXECUTION_CANCELLED", False), ("EXECUTION_SUCCEEDED", False),
    ("EXECUTION_RUNNING", True), ("EXECUTION_PENDING", True), (None, True)])
def test_a_jobs_latest_execution_that_ended_without_a_timestamp_is_finished(completion_status, closes):
    latest = {"name": CRON + "-x", "creationTimestamp": stamp(QUIET - timedelta(days=80))}
    if completion_status:
        latest["completionStatus"] = completion_status
    jobs = {**required_jobs(), CRON: job(CRON, latest=latest)}
    if closes:
        with pytest.raises(gate.GateClosed, match=f"long-running database job execution\\(s\\): {CRON}"):
            evaluate(jobs=jobs)
    else:
        assert evaluate(jobs=jobs).startswith("cpu_peak_5m=")


def test_listed_non_db_job_does_not_close(monkeypatch):
    monkeypatch.setattr(gate, "NON_DB_JOBS", {"example-probe": "reviewed: no database access"})
    evaluate(executions=[execution("example-done"), execution("example-probe", done=False, started="")])


def test_reviewed_non_db_list_is_empty():
    assert gate.NON_DB_JOBS == {}


@pytest.mark.parametrize(("jobs", "executions", "message"), [
    ({}, [execution("example-done")], "lacks always-present"),
    ({gate.REQUIRED_JOBS[0]: job(gate.REQUIRED_JOBS[0])}, [execution("example-done")], "lacks always-present"),
    (None, [], "execution list is empty"),
])
def test_empty_answers_close(jobs, executions, message):
    with pytest.raises(gate.GateClosed, match=message):
        evaluate(jobs=jobs, executions=executions)


def test_unlabelled_running_execution_closes():
    with pytest.raises(gate.GateClosed, match="without a job label"):
        evaluate(executions=[{"metadata": {}, "status": {}}])


def test_check_once_reads_all_executions_and_each_required_job(monkeypatch):
    calls = []

    def gcloud(args, timeout=180):
        calls.append(args)
        if args[:3] == ["run", "jobs", "list"]:
            return list(required_jobs().values())
        return [execution("example-done")]

    monkeypatch.setattr(gate, "gcloud_json", gcloud)
    monkeypatch.setattr(gate, "access_token", lambda: "t")
    monkeypatch.setattr(gate, "fetch_cpu_series", lambda now, token: pages(0.01, 0.01, 0.01))
    assert gate.check_once(lambda: QUIET).startswith("cpu_peak_5m=0.01")
    every = [a for a in calls if a[:4] == ["run", "jobs", "executions", "list"] and "--job" not in a]
    assert every == [["run", "jobs", "executions", "list", "--region", gate.REGION]]
    per_job = sorted(a[a.index("--job") + 1] for a in calls if "--job" in a)
    assert per_job == sorted(gate.REQUIRED_JOBS)


@pytest.mark.parametrize(("value", "expected"), [
    ("2026-01-02T07:00:00Z", datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)),
    ("2026-01-02T07:00:00.123456789Z", datetime(2026, 1, 2, 7, 0, 0, 123456, tzinfo=timezone.utc)),
    ("2026-01-02T07:00:00.5+01:00", datetime(2026, 1, 2, 6, 0, 0, 500000, tzinfo=timezone.utc)),
    ("2026-01-02 07:00:00", None), ("", None), (None, None), ("2026-13-02T07:00:00Z", None),
])
def test_timestamps_parse_any_fraction_or_fail_closed(value, expected):
    assert gate.parse_time(value) == expected


# ------------------------------------------------------------------ main: retries, deadline, output

def test_main_prints_gate_open_last_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(gate, "check_once", lambda clock: "summary line")
    assert gate.main(clock=lambda: QUIET) == 0
    assert capsys.readouterr().out.splitlines() == ["summary line", "GATE_OPEN"]


def test_main_retries_only_a_short_run_then_opens(monkeypatch, capsys):
    answers = iter([gate.ShortRunClosed("1 database job execution(s) running: cron"), "summary"])

    def once(clock):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(gate, "check_once", once)
    sleeps = []
    assert gate.main(sleep=sleeps.append, clock=lambda: QUIET) == 0
    assert sleeps == [gate.RUNNING_RETRY_SECONDS]
    assert capsys.readouterr().out.splitlines() == ["summary", "GATE_OPEN"]


def test_main_closes_after_the_last_short_run_retry(monkeypatch, capsys):
    calls, sleeps = [], []

    def once(clock):
        calls.append(1)
        raise gate.ShortRunClosed("1 database job execution(s) running: cron")

    monkeypatch.setattr(gate, "check_once", once)
    assert gate.main(sleep=sleeps.append, clock=lambda: QUIET) == 3
    assert len(calls) == gate.RUNNING_RETRIES and len(sleeps) == gate.RUNNING_RETRIES - 1
    assert capsys.readouterr().out.startswith("GATE_CLOSED: 1 database job")


@pytest.mark.parametrize("reason", [
    "pivota-pg CPU peak 0.5 >= 0.2",
    # The old message text, raised as a plain GateClosed: retry is chosen by type, never by text.
    "1 database job execution(s) running: cron",
])
def test_main_does_not_retry_other_closures(monkeypatch, capsys, reason):
    calls = []

    def once(clock):
        calls.append(1)
        raise gate.GateClosed(reason)

    monkeypatch.setattr(gate, "check_once", once)
    assert gate.main(sleep=lambda _: None, clock=lambda: QUIET) == 3
    out = capsys.readouterr().out
    assert calls == [1] and "GATE_OPEN" not in out.splitlines() and out.startswith("GATE_CLOSED: ")


def test_main_closes_after_its_deadline(monkeypatch, capsys):
    assert gate.DEADLINE_SECONDS == 300
    ticks = iter([0, 0, 301])
    monkeypatch.setattr(gate, "check_once", lambda clock: "summary")
    assert gate.main(clock=lambda: QUIET, monotonic=lambda: next(ticks)) == 3
    assert capsys.readouterr().out == "GATE_CLOSED: gate deadline passed\n"


def test_main_closes_on_unexpected_error(monkeypatch, capsys):
    monkeypatch.setattr(gate, "check_once", lambda clock: {}["boom"])
    assert gate.main(clock=lambda: QUIET) == 3
    assert capsys.readouterr().out == "GATE_CLOSED: unexpected KeyError\n"


@pytest.mark.parametrize(("returncode", "stdout", "error"), [
    (1, "[]", None), (0, "not json", None), (0, "", subprocess.TimeoutExpired("gcloud", 1)),
])
def test_gcloud_failures_close(monkeypatch, returncode, stdout, error):
    def run(*_args, **_kwargs):
        if error:
            raise error
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(gate.subprocess, "run", run)
    with pytest.raises(gate.GateClosed):
        gate.gcloud_json(["run", "jobs", "list"])


@pytest.mark.parametrize(("returncode", "stdout"), [(1, "ya29.token\n"), (0, "  \n")])
def test_token_failures_close_even_with_output(monkeypatch, returncode, stdout):
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=""))
    with pytest.raises(gate.GateClosed, match="no access token"):
        gate.access_token()
