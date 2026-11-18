"""The shared production one-off gate opens only on a clean read, and closes on anything else."""

from datetime import datetime, timezone
import subprocess

import pytest

from scripts.ops import oneoff_gate as gate

QUIET = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)


def series(*values):
    return {"timeSeries": [{"points": [{"value": {"doubleValue": value}} for value in values]}]}


def job(name, *, latest=None):
    return {"metadata": {"name": name}, "status": {"latestCreatedExecution": latest} if latest else {}}


def execution(job_name, *, done=True, started="2026-01-02T06:59:30Z"):
    return {"metadata": {"labels": {"run.googleapis.com/job": job_name}},
            "status": {"completionTime": "2026-01-02T06:59:50Z"} if done else {"startTime": started}}


@pytest.mark.parametrize(("hour", "minute", "closed"), [
    (5, 14, False), (5, 15, True), (6, 14, True), (6, 15, False),
    (10, 29, False), (10, 30, True), (11, 19, True), (11, 20, False),
])
def test_windows_are_utc_half_open(hour, minute, closed):
    assert (gate.window_reason(datetime(2026, 1, 2, hour, minute, tzinfo=timezone.utc)) is not None) is closed


def test_open_on_quiet_database():
    summary = gate.evaluate(QUIET, series=series(0.05, 0.19), executions=[execution("oneoff-1")],
                            jobs={"oneoff-1": job("oneoff-1")})
    assert summary.startswith("cpu_peak_5m=0.19 ")


@pytest.mark.parametrize("values", [(0.05, 0.20), (0.9,)])
def test_closed_at_or_above_cpu_limit(values):
    with pytest.raises(gate.GateClosed, match="CPU peak"):
        gate.evaluate(QUIET, series=series(*values), executions=[], jobs={})


def test_closed_without_cpu_points():
    with pytest.raises(gate.GateClosed, match="no CPU points"):
        gate.evaluate(QUIET, series={}, executions=[], jobs={})


@pytest.mark.parametrize("name", ["oneoff-12-34", "reviewed-seed-batch-a", "some-cron", "eval-sql-runner"])
def test_any_running_job_closes_by_default(name):
    with pytest.raises(gate.GateClosed, match=f"execution\\(s\\) running: {name}"):
        gate.evaluate(QUIET, series=series(0.01), executions=[execution(name, done=False)], jobs={})
    latest_only = {name: job(name, latest={"name": name + "-x", "creationTimestamp": "2026-01-02T06:59:40Z"})}
    with pytest.raises(gate.GateClosed, match=name):
        gate.evaluate(QUIET, series=series(0.01), executions=[], jobs=latest_only)


@pytest.mark.parametrize("started", ["2026-01-02T06:58:59Z", None, "not a time"])
def test_long_or_undated_run_is_long_running(started):
    with pytest.raises(gate.GateClosed, match="long-running database job execution\\(s\\): cron"):
        gate.evaluate(QUIET, series=series(0.01), executions=[execution("cron", done=False, started=started)], jobs={})


def test_listed_non_db_job_does_not_close(monkeypatch):
    monkeypatch.setattr(gate, "NON_DB_JOBS", {"store-probe": "reviewed: no database access"})
    jobs = {"store-probe": job("store-probe", latest={"name": "store-probe-x"})}
    gate.evaluate(QUIET, series=series(0.01), executions=[execution("store-probe", done=False, started=None)], jobs=jobs)


def test_unlabelled_running_execution_closes():
    with pytest.raises(gate.GateClosed, match="without a job label"):
        gate.evaluate(QUIET, series=series(0.01), executions=[{"metadata": {}, "status": {}}], jobs={})


def test_reviewed_non_db_list_has_a_reason_per_entry():
    assert all(isinstance(reason, str) and reason.strip() for reason in gate.NON_DB_JOBS.values())


def test_main_prints_gate_open_last_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(gate, "check_once", lambda: "cpu_peak_5m=0.01 db_job_executions_running=0 utc=07:00Z")
    assert gate.main() == 0
    assert capsys.readouterr().out.splitlines()[-1] == "GATE_OPEN"


def test_main_retries_only_a_running_job_then_opens(monkeypatch, capsys):
    answers = iter([gate.GateClosed("1 database job execution(s) running: cron"), "summary"])

    def once():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(gate, "check_once", once)
    sleeps = []
    assert gate.main(sleep=sleeps.append) == 0
    assert sleeps == [gate.RUNNING_RETRY_SECONDS]
    assert capsys.readouterr().out.splitlines() == ["summary", "GATE_OPEN"]


@pytest.mark.parametrize("reason", ["pivota-pg CPU peak 0.5 >= 0.2", "long-running database job execution(s): cron"])
def test_main_does_not_retry_cpu_or_long_runs_and_never_prints_open(monkeypatch, capsys, reason):
    calls = []

    def once():
        calls.append(1)
        raise gate.GateClosed(reason)

    monkeypatch.setattr(gate, "check_once", once)
    assert gate.main(sleep=lambda _: None) == 3
    out = capsys.readouterr().out
    assert calls == [1] and "GATE_OPEN" not in out.splitlines() and out.startswith("GATE_CLOSED: ")


def test_main_closes_on_unexpected_error(monkeypatch, capsys):
    monkeypatch.setattr(gate, "check_once", lambda: {}["boom"])
    assert gate.main() == 3
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


def test_monitoring_failure_closes(monkeypatch):
    def urlopen(*_args, **_kwargs):
        raise OSError("network")

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    with pytest.raises(gate.GateClosed, match="monitoring read failed"):
        gate.fetch_cpu_series(QUIET, "token")
