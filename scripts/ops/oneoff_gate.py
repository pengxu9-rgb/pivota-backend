"""Controller gate for production one-off jobs. ONE source for the laptop and the Cloud Build launcher.

Prints a summary line and then the literal line `GATE_OPEN` (exit 0) only when ALL hold; otherwise prints
`GATE_CLOSED: <reason>` and exits 3. Any API error, timeout, empty or unreadable answer closes the gate.

  - now (UTC) is outside 05:15-06:15Z and 10:30-11:20Z, checked first AND again as the very last step;
  - pivota-pg CPU utilization is below 0.20 at every point of the last 5 minutes (Cloud Monitoring), with at
    least 3 finite points spanning at least 3 minutes, no executionErrors, and every page read;
  - no Cloud Run job execution is running, except jobs on the reviewed NON_DB_JOBS list (empty). Read three
    ways: every job's latest execution, ALL executions (no --limit), and per job for relgraph-sync and
    retailer-ingest-drain. The jobs list must contain those two, and the execution list must not be empty;
  - a running execution older than 60 s (or with no readable start) closes at once; a shorter one (a minute
    cron) is re-read up to 3 times, 25 s apart; the whole gate gives up (CLOSED) after 5 minutes.

THE GATE IS NOT A LOCK: two callers can both read OPEN. A launcher that must not overlap another takes its own
lock (the seed batch launcher creates a GCS object with ifGenerationMatch=0).

    python3 scripts/ops/oneoff_gate.py            # gcloud credentials of the caller (laptop or build SA)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PROJECT = "pivota-prod"
REGION = "us-west1"
DATABASE_ID = "pivota-prod:pivota-pg"
CPU_LIMIT = 0.20
LOOKBACK = timedelta(minutes=5)
MIN_CPU_POINTS = 3
MIN_CPU_SPAN = timedelta(minutes=3)
MAX_CPU_PAGES = 5
CLOSED_WINDOWS_UTC = ((5 * 60 + 15, 6 * 60 + 15), (10 * 60 + 30, 11 * 60 + 20))
TIMEOUT_SECONDS = 30
DEADLINE_SECONDS = 300
# Jobs that always exist in pivota-prod and are checked execution by execution, as the original gate did.
REQUIRED_JOBS = ("relgraph-sync", "retailer-ingest-drain")
# EVERY Cloud Run job counts as a database job unless it is listed here, by exact name, with a reviewed reason.
# A new or unknown job therefore closes the gate. Entries need evidence that the job cannot reach pivota-pg.
NON_DB_JOBS: dict[str, str] = {}
SHORT_RUN = timedelta(seconds=60)
RUNNING_RETRIES = 3
RUNNING_RETRY_SECONDS = 25


class GateClosed(Exception):
    pass


class ShortRunClosed(GateClosed):
    """Only short database runs are running: the one reason that is re-read instead of closing."""


_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})\Z")


def parse_time(value) -> datetime | None:
    """RFC 3339 from gcloud / Monitoring, any fraction length, on Python 3.9+. None when unreadable."""
    match = _TIME.fullmatch(str(value or ""))
    if not match:
        return None
    whole, fraction, zone = match.groups()
    text = whole + (("." + (fraction + "000000")[:6]) if fraction else "") + ("+00:00" if zone == "Z" else zone)
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def window_reason(now: datetime) -> str | None:
    utc = now.astimezone(timezone.utc)
    minute = utc.hour * 60 + utc.minute
    for start, end in CLOSED_WINDOWS_UTC:
        if start <= minute < end:
            return f"inside {start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}Z"
    return None


def require_outside_window(now: datetime) -> None:
    reason = window_reason(now)
    if reason:
        raise GateClosed(reason)


def gcloud_json(args: list[str], timeout: int = 180):
    try:
        done = subprocess.run(["gcloud", *args, "--project", PROJECT, "--format=json"], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateClosed(f"gcloud {' '.join(args[0:3])} failed: {type(error).__name__}") from None
    if done.returncode != 0:
        raise GateClosed(f"gcloud {' '.join(args[0:3])} exited {done.returncode}")
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise GateClosed(f"gcloud {' '.join(args[0:3])} returned unreadable JSON") from None


def access_token() -> str:
    try:
        done = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                              timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise GateClosed("no access token") from None
    token = done.stdout.strip()
    if done.returncode != 0 or not token:
        raise GateClosed("no access token")
    return token


def cpu_query(now: datetime, page_token: str | None = None) -> str:
    params = {
        "filter": ('metric.type="cloudsql.googleapis.com/database/cpu/utilization" AND '
                   f'resource.labels.database_id="{DATABASE_ID}"'),
        "interval.startTime": (now - LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if page_token:
        params["pageToken"] = page_token
    return f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/timeSeries?{urllib.parse.urlencode(params)}"


def fetch_cpu_series(now: datetime, token: str) -> list[dict]:
    """Every page of the CPU series; executionErrors or more than MAX_CPU_PAGES pages close the gate."""
    pages, page_token = [], None
    for _ in range(MAX_CPU_PAGES):
        request = urllib.request.Request(cpu_query(now, page_token), headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:
                page = json.loads(reply.read(2_000_000))
        except Exception as error:  # noqa: BLE001 - any failure closes the gate
            raise GateClosed(f"monitoring read failed: {type(error).__name__}") from None
        if not isinstance(page, dict):
            raise GateClosed("monitoring returned an unreadable page")
        if page.get("executionErrors"):
            raise GateClosed("monitoring reported executionErrors")
        pages.append(page)
        page_token = page.get("nextPageToken")
        if not page_token:
            return pages
    raise GateClosed("monitoring answer has too many pages")


def cpu_peak(pages: list[dict]) -> float:
    values, times = [], []
    for page in pages:
        for series in page.get("timeSeries", []):
            for point in series.get("points", []):
                try:
                    value = float(point["value"]["doubleValue"])
                except (KeyError, TypeError, ValueError):
                    raise GateClosed("unreadable CPU point") from None
                if not math.isfinite(value):
                    raise GateClosed("non-finite CPU point")
                values.append(value)
                times.append(parse_time(point.get("interval", {}).get("endTime")))
    if len(values) < MIN_CPU_POINTS or None in times or max(times) - min(times) < MIN_CPU_SPAN:
        raise GateClosed(f"too few CPU points in the last 5 min ({len(values)})")
    return max(values)


# An ExecutionReference (a job's latestCreatedExecution) in one of these states has ended.
ENDED_REFERENCE_STATUSES = frozenset({"EXECUTION_SUCCEEDED", "EXECUTION_FAILED", "EXECUTION_CANCELLED"})


def execution_finished(execution: dict) -> bool:
    """Has this execution ended? A completionTime says so; so does a Completed condition of "False" (failed or
    cancelled, e.g. an image that never imported, which never gets a completionTime), or every task failed or
    cancelled with none running. "Unknown" (in progress) and anything unreadable are NOT finished."""
    status = execution.get("status") or {}
    if status.get("completionTime"):
        return True
    if any(isinstance(c, dict) and c.get("type") == "Completed" and c.get("status") == "False"
           for c in status.get("conditions") or []):
        return True
    tasks = (execution.get("spec") or {}).get("taskCount")
    ended = [status.get("failedCount") or 0, status.get("cancelledCount") or 0]
    return (type(tasks) is int and tasks > 0 and not status.get("runningCount")
            and all(type(n) is int for n in ended) and sum(ended) >= tasks)


def running_executions(executions: list, jobs: dict, per_job: dict) -> list[tuple[str, datetime | None]]:
    """(job name, start time) of every running execution, from the three reads."""
    running = []
    for name, job in jobs.items():
        latest = job.get("status", {}).get("latestCreatedExecution")
        if latest and not latest.get("completionTimestamp") and latest.get("completionStatus") not in ENDED_REFERENCE_STATUSES:
            running.append((name, parse_time(latest.get("creationTimestamp"))))
    for execution in [*executions, *(e for rows in per_job.values() for e in rows)]:
        status = execution.get("status", {})
        if execution_finished(execution):
            continue
        metadata = execution.get("metadata", {})
        name = metadata.get("labels", {}).get("run.googleapis.com/job", "")
        if not name:
            raise GateClosed("running execution without a job label")
        running.append((name, parse_time(status.get("startTime") or metadata.get("creationTimestamp"))))
    return [(name, started) for name, started in running if name not in NON_DB_JOBS]


def evaluate(now: datetime, *, pages: list[dict], executions: list, jobs: dict, per_job: dict) -> str:
    require_outside_window(now)
    missing = [name for name in REQUIRED_JOBS if name not in jobs]
    if missing:
        raise GateClosed(f"jobs list lacks always-present job(s): {', '.join(missing)}")
    if not executions:
        raise GateClosed("execution list is empty")
    peak = cpu_peak(pages)
    if not peak < CPU_LIMIT:
        raise GateClosed(f"pivota-pg CPU peak {peak} >= {CPU_LIMIT}")
    running = running_executions(executions, jobs, per_job)
    # No start time counts as long-running: fail closed.
    long = sorted({name for name, started in running if started is None or now - started > SHORT_RUN})
    if long:
        raise GateClosed(f"long-running database job execution(s): {', '.join(long)}")
    names = sorted({name for name, _ in running})
    if names:
        raise ShortRunClosed(f"{len(names)} database job execution(s) running: {', '.join(names)}")
    return f"cpu_peak_5m={peak} db_job_executions_running=0 utc={now.strftime('%H:%MZ')}"


def check_once(clock=None) -> str:
    clock = clock or (lambda: datetime.now(timezone.utc))
    require_outside_window(clock())
    jobs = {job["metadata"]["name"]: job for job in gcloud_json(["run", "jobs", "list", "--region", REGION])}
    executions = gcloud_json(["run", "jobs", "executions", "list", "--region", REGION])
    per_job = {name: gcloud_json(["run", "jobs", "executions", "list", "--job", name, "--region", REGION, "--limit", "20"])
               for name in REQUIRED_JOBS}
    # CPU is read last so its 5-minute window ends as close to the verdict as possible.
    later = clock()
    return evaluate(later, pages=fetch_cpu_series(later, access_token()), executions=executions, jobs=jobs,
                    per_job=per_job)


def main(sleep=None, clock=None, monotonic=None) -> int:
    sleep = sleep or time.sleep
    clock = clock or (lambda: datetime.now(timezone.utc))
    monotonic = monotonic or time.monotonic
    started = monotonic()
    try:
        for attempt in range(RUNNING_RETRIES):
            if monotonic() - started > DEADLINE_SECONDS:
                raise GateClosed("gate deadline passed")
            try:
                summary = check_once(clock)
                break
            except ShortRunClosed:
                if attempt == RUNNING_RETRIES - 1:
                    raise
                sleep(RUNNING_RETRY_SECONDS)
        if monotonic() - started > DEADLINE_SECONDS:
            raise GateClosed("gate deadline passed")
        # The very last step: a slow read must not print OPEN inside a closed window.
        require_outside_window(clock())
    except GateClosed as closed:
        print(f"GATE_CLOSED: {closed}", flush=True)
        return 3
    except Exception as error:  # noqa: BLE001 - fail closed on anything unexpected
        print(f"GATE_CLOSED: unexpected {type(error).__name__}", flush=True)
        return 3
    print(summary, flush=True)
    print("GATE_OPEN", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
