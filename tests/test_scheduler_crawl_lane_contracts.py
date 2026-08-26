"""Two contracts the crawl lane depends on that nothing else checks.

`infra/gcp/setup_scheduler.sh` now creates two jobs that fetch third-party merchant
storefronts: `external-seed-destination-sweep` (is this link still there) and
`external-seed-content-refresh` (is the price we quote still true). Both are ordinary-looking
shell blocks, and both have a failure mode that reports SUCCESS while having seen nothing.

1. EGRESS. A crawl job must be created with `mkcrawljob`, which pins `--subnet pivota-crawl`,
   not `mkjob`, which uses the default subnet. The script already says why for the sweep:
   from any other IP "most brand hosts" answer with a Cloudflare bot challenge. Nothing
   crashes -- the fetch returns a challenge page, the extractor finds no price, and the run
   reports `price_unavailable` for the whole corpus. Swapping one helper for the other is a
   one-word edit that produces a green, entirely blind nightly job.

2. NON-OVERLAP. `services/crawl_politeness` keeps its per-host schedule in PROCESS MEMORY.
   Two crawl jobs running at once therefore do not share a token bucket -- they pace
   independently, and every brand host sees double the agreed request rate. The politeness
   guarantee is only true while at most one crawl job runs at a time, which is a property of
   the CRON SCHEDULE and the TASK TIMEOUT together, expressed nowhere near the pacing code.
   A later "move the sweep an hour earlier" is exactly the sort of edit that breaks it
   silently, so the windows are computed and compared here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCHEDULER = REPO / "infra" / "gcp" / "setup_scheduler.sh"

# Jobs that leave our network to fetch a third-party storefront.
CRAWL_JOBS = ("external-seed-destination-sweep", "external-seed-content-refresh")


@pytest.fixture(scope="module")
def script() -> str:
    return SCHEDULER.read_text(encoding="utf-8")


def _job_block(script: str, job: str) -> str:
    """The `mk*job <name> ...` invocation, up to the end of its backslash continuations."""
    match = re.search(rf"^\s*(mk\w*job)\s+{re.escape(job)}\b(.*?)(?=\n\s*(?:else|fi)\b)",
                      script, re.S | re.M)
    assert match, f"no job block for {job}"
    return match.group(1) + match.group(2)


def _cron_of(script: str, job: str) -> str:
    match = re.search(rf'^\s*sched\s+{re.escape(job)}-cron\s+"([^"]+)"', script, re.M)
    assert match, f"no sched line for {job}"
    return match.group(1)


def _timeout_seconds(block: str) -> int:
    match = re.search(r"--task-timeout\s+(\d+)s", block)
    assert match, "crawl jobs must set an explicit --task-timeout"
    return int(match.group(1))


def _window(script: str, job: str) -> tuple[int, int]:
    """(start_minute, end_minute) within a day, from the cron and the task timeout."""
    minute, hour = _cron_of(script, job).split()[:2]
    assert minute.isdigit() and hour.isdigit(), f"{job}: expected a fixed daily time"
    start = int(hour) * 60 + int(minute)
    return start, start + _timeout_seconds(_job_block(script, job)) // 60


@pytest.mark.parametrize("job", CRAWL_JOBS)
def test_a_storefront_fetching_job_leaves_from_the_crawl_subnet(script: str, job: str) -> None:
    """`mkjob` would be syntactically fine and functionally blind."""
    assert _job_block(script, job).startswith("mkcrawljob"), (
        f"{job} fetches merchant storefronts and must use mkcrawljob (--subnet pivota-crawl); "
        "from any other egress IP brand hosts answer with a bot challenge and the run reports "
        "success having read nothing"
    )


DAY_MINUTES = 24 * 60


def test_a_crawl_window_cannot_outlast_its_own_daily_cycle(script: str) -> None:
    """A job whose timeout exceeds a day overlaps ITSELF, before any sibling is involved."""
    for job in CRAWL_JOBS:
        start, end = _window(script, job)
        assert end - start < DAY_MINUTES, (
            f"{job} may run for {end - start} minutes on a daily schedule -- the next "
            "execution starts while the previous one is still crawling"
        )


def test_the_crawl_jobs_never_run_at_the_same_time(script: str) -> None:
    """crawl_politeness paces per PROCESS, so overlap doubles the rate each host sees.

    Compared CYCLICALLY, not just in clock order: these are daily jobs, so the last window
    of the day runs into the FIRST window of the next one. Checking only `earlier.end <=
    later.start` misses a long-running late job colliding with tomorrow morning's sweep --
    a mutant that stretched this job's --task-timeout to 24h survived that weaker form.
    """
    ordered = sorted(
        ((job, _window(script, job)) for job in CRAWL_JOBS), key=lambda kv: kv[1][0]
    )
    for index, (job, (_start, end)) in enumerate(ordered):
        next_job, (next_start, _next_end) = ordered[(index + 1) % len(ordered)]
        # The successor of the last job is tomorrow's first job.
        boundary = next_start if index + 1 < len(ordered) else next_start + DAY_MINUTES
        assert end <= boundary, (
            f"{job} can still be crawling at minute {end} when {next_job} starts at "
            f"{boundary} (minutes past midnight UTC, wrapping to the next day) -- two crawl "
            "processes do not share a token bucket, so each brand host would see double the "
            "paced rate"
        )


@pytest.mark.parametrize("flag", ["EXTERNAL_SEED_DESTINATION_SWEEP", "EXTERNAL_SEED_CONTENT_REFRESH"])
def test_a_crawl_flag_defaults_off_and_is_strictly_validated(script: str, flag: str) -> None:
    """`PAUSED=true` once armed a job on a one-minute schedule; the script's own comment
    records that outage. Anything but an exact `true` must be treated as off."""
    assert f': "${{{flag}:=false}}"' in script, f"{flag} must default to false"
    assert re.search(rf'case "\${flag}" in true\|false\)', script), (
        f"{flag} must be validated as exactly true|false, not truthiness"
    )


def test_turning_a_crawl_flag_off_disarms_an_existing_trigger(script: str) -> None:
    """A creation skip is not a disarm: the trigger from a previous run keeps crawling."""
    for job in CRAWL_JOBS:
        assert re.search(
            rf'scheduler jobs pause {re.escape(job)}-cron --location "\$REGION"', script
        ), f"{job}-cron has no idempotent disarm branch"
