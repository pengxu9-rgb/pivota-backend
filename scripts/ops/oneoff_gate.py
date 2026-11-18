"""Controller gate for production one-off jobs. ONE source for the laptop and the Cloud Build launcher.

Prints a summary line and then the literal line `GATE_OPEN` (exit 0) only when ALL hold; otherwise prints
`GATE_CLOSED: <reason>` and exits 3. Any API error, timeout or unreadable answer closes the gate.

  - now (UTC) is outside 05:15-06:15Z and 10:30-11:20Z;
  - pivota-pg CPU utilization is below 0.20 at every point of the last 5 minutes (Cloud Monitoring);
  - no Cloud Run job execution is running, except jobs on the reviewed NON_DB_JOBS list. A short (<= 60 s) run
    is re-read up to 3 times, 25 s apart (minute crons); a longer run closes the gate at once.

    python3 scripts/ops/oneoff_gate.py            # gcloud credentials of the caller (laptop or build SA)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import urllib.parse
import urllib.request

PROJECT = "pivota-prod"
REGION = "us-west1"
DATABASE_ID = "pivota-prod:pivota-pg"
CPU_LIMIT = 0.20
LOOKBACK = timedelta(minutes=5)
CLOSED_WINDOWS_UTC = ((5 * 60 + 15, 6 * 60 + 15), (10 * 60 + 30, 11 * 60 + 20))
TIMEOUT_SECONDS = 30


class GateClosed(Exception):
    pass


def window_reason(now: datetime) -> str | None:
    minute = now.astimezone(timezone.utc).hour * 60 + now.astimezone(timezone.utc).minute
    for start, end in CLOSED_WINDOWS_UTC:
        if start <= minute < end:
            return f"inside {start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}Z"
    return None


def gcloud_json(args: list[str], timeout: int = 120):
    try:
        done = subprocess.run(["gcloud", *args, "--project", PROJECT, "--format=json"], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateClosed(f"gcloud {args[0:3]} failed: {type(error).__name__}") from None
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


def fetch_cpu_series(now: datetime, token: str) -> dict:
    query = urllib.parse.urlencode({
        "filter": ('metric.type="cloudsql.googleapis.com/database/cpu/utilization" AND '
                   f'resource.labels.database_id="{DATABASE_ID}"'),
        "interval.startTime": (now - LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    request = urllib.request.Request(f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/timeSeries?{query}",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:
            return json.loads(reply.read(2_000_000))
    except Exception as error:  # noqa: BLE001 - any failure closes the gate
        raise GateClosed(f"monitoring read failed: {type(error).__name__}") from None


def cpu_peak(series: dict) -> float:
    points = [point["value"]["doubleValue"] for item in series.get("timeSeries", []) for point in item.get("points", [])]
    if not points:
        raise GateClosed("no CPU points in the last 5 min")
    return max(float(value) for value in points)


# EVERY Cloud Run job counts as a database job unless it is listed here, by exact name, with a reviewed reason.
# A new or unknown job therefore closes the gate. Entries need evidence that the job cannot reach pivota-pg.
NON_DB_JOBS: dict[str, str] = {}
SHORT_RUN = timedelta(seconds=60)    # a running DB execution older than this closes the gate with no retry


def _parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def running_executions(executions: list, jobs: dict) -> list[tuple[str, datetime | None]]:
    """(job name, start time) of every running execution. Two independent reads: each job's latest execution
    (covers an old long run) and the newest executions across all jobs (a second concurrent run of one job)."""
    running = []
    for name, job in jobs.items():
        latest = job.get("status", {}).get("latestCreatedExecution")
        if latest and not latest.get("completionTimestamp"):
            running.append((name, _parse_time(latest.get("creationTimestamp"))))
    for execution in executions:
        status = execution.get("status", {})
        if status.get("completionTime"):
            continue
        metadata = execution.get("metadata", {})
        name = metadata.get("labels", {}).get("run.googleapis.com/job", "")
        if not name:
            raise GateClosed("running execution without a job label")
        running.append((name, _parse_time(status.get("startTime") or metadata.get("creationTimestamp"))))
    return [(name, started) for name, started in running if name not in NON_DB_JOBS]


def evaluate(now: datetime, *, series: dict, executions: list, jobs: dict) -> str:
    reason = window_reason(now)
    if reason:
        raise GateClosed(reason)
    peak = cpu_peak(series)
    if not peak < CPU_LIMIT:
        raise GateClosed(f"pivota-pg CPU peak {peak} >= {CPU_LIMIT}")
    running = running_executions(executions, jobs)
    # No start time counts as long-running: fail closed.
    long = sorted({name for name, started in running if started is None or now - started > SHORT_RUN})
    if long:
        raise GateClosed(f"long-running database job execution(s): {', '.join(long)}")
    names = sorted({name for name, _ in running})
    if names:
        raise GateClosed(f"{len(names)} database job execution(s) running: {', '.join(names)}")
    return f"cpu_peak_5m={peak} db_job_executions_running=0 utc={now.strftime('%H:%MZ')}"


RUNNING_RETRIES = 3          # minute-cron DB jobs run ~20 s: re-read instead of closing on a passing one
RUNNING_RETRY_SECONDS = 25


def check_once() -> str:
    now = datetime.now(timezone.utc)
    if window_reason(now):
        raise GateClosed(window_reason(now))
    jobs = {job["metadata"]["name"]: job for job in gcloud_json(["run", "jobs", "list", "--region", REGION])}
    executions = gcloud_json(["run", "jobs", "executions", "list", "--region", REGION, "--limit", "200"])
    # CPU is read last so its 5-minute window ends as close to the verdict as possible.
    later = datetime.now(timezone.utc)
    return evaluate(later, series=fetch_cpu_series(later, access_token()), executions=executions, jobs=jobs)


def main(sleep=None) -> int:
    import time

    sleep = sleep or time.sleep
    try:
        for attempt in range(RUNNING_RETRIES):
            try:
                summary = check_once()
                break
            except GateClosed as closed:
                if "job execution(s) running" not in str(closed) or attempt == RUNNING_RETRIES - 1:
                    raise
                sleep(RUNNING_RETRY_SECONDS)
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
