"""SCHEDULER_JOB_ALLOWLIST: let a worker run ONLY the jobs it names.

`AUDIT_WORKER_ENABLED=true` starts every scheduled job in services/audit_scheduler.py, and several
of them act on the outside world: merchant crawls and audits, store disconnects, catalog import
and sync drains, merchant order-sync writes, PSP auto-finalize, Stripe Connect transfers, alerts.
A worker armed for ONE purpose (the staging Reap demo runs only `reap_agentic_purchase_poll`)
must not start the rest. This module is that filter; the runbook is
docs/runbooks/scheduler_job_allowlist.md.

THE CONTRACT

  * Unset, empty, or whitespace-only  -> NO FILTER. Every caller gets exactly today's behaviour.
    `active_allowlist()` returns None and nothing else here records or logs anything.
  * Set                                 -> a comma-separated list of ids. Entries are trimmed;
    empty entries are dropped; matching is exact and case-sensitive (no prefixes, no globs).
    Only listed ids start. A value that is set but names NO ids (",", " , ") is a filter that
    allows NOTHING, not "no filter": an operator who typed a list meant to restrict, and the
    fail-open reading would start every job on a worker that was meant to run one.

WHAT AN ID IS. The id a job registers under in `audit_scheduler._add_job` — the same id
/__scheduler_health reports — or one of the process-level loops in `PROCESS_LOOP_IDS`, which are
started at boot outside APScheduler (main.startup_event) and are gated here too, so "only the
listed ids run" holds for everything a worker process starts on its own.

Skipped ids are recorded (not silently missing) and reported on /__scheduler_health as
`skipped_by_allowlist`; ids in the list that match nothing are logged once per scheduler boot
at WARNING and reported as `job_allowlist_unknown_ids`.
"""

from __future__ import annotations

import os
from typing import Dict, FrozenSet, Iterable, List, Optional

ENV_VAR = "SCHEDULER_JOB_ALLOWLIST"

# Background loops a process starts at boot OUTSIDE APScheduler (see main.startup_event). The
# ids are the names these loops are spawned under. They are NOT gated by AUDIT_WORKER_ENABLED —
# they run on every process, web included — so without this gate an allowlisted worker would
# still deliver outbound webhook retries and (if its own flag is on) delete expired photos.
AGENT_WEBHOOK_RETRY_WORKER = "agent_webhook_retry_worker"
MERCHANT_WEBHOOK_RETRY_WORKER = "merchant_webhook_retry_worker"
PHOTO_CLEANUP_LOOP = "photo_cleanup_loop"
PROCESS_LOOP_IDS: FrozenSet[str] = frozenset(
    {AGENT_WEBHOOK_RETRY_WORKER, MERCHANT_WEBHOOK_RETRY_WORKER, PHOTO_CLEANUP_LOOP}
)

KIND_SCHEDULER_JOB = "scheduler_job"
KIND_PROCESS_LOOP = "process_loop"

# id -> kind, for every id this process declined to start because of the allowlist.
_SKIPPED: Dict[str, str] = {}
# Listed ids that matched no known job at the last scheduler boot.
_UNKNOWN: List[str] = []


def parse_allowlist(raw: Optional[str]) -> Optional[FrozenSet[str]]:
    """None means NO FILTER. A frozenset (possibly empty) is the set of ids allowed to run."""
    if raw is None or not raw.strip():
        return None
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def active_allowlist() -> Optional[FrozenSet[str]]:
    return parse_allowlist(os.getenv(ENV_VAR))


def is_allowed(job_id: str, allowlist: Optional[FrozenSet[str]]) -> bool:
    return allowlist is None or job_id in allowlist


def note_skipped(job_id: str, kind: str) -> None:
    _SKIPPED[job_id] = kind


def process_loop_allowed(loop_id: str) -> bool:
    """Gate for a boot-time loop. True (start it) whenever the allowlist is unset."""
    allowlist = active_allowlist()
    if is_allowed(loop_id, allowlist):
        return True
    note_skipped(loop_id, KIND_PROCESS_LOOP)
    return False


def begin_scheduler_boot() -> None:
    """Forget the previous boot's scheduler-job skips (restart_scheduler re-registers). Skips of
    process loops are kept: those loops were decided once, at process start."""
    for job_id in [j for j, kind in _SKIPPED.items() if kind == KIND_SCHEDULER_JOB]:
        del _SKIPPED[job_id]
    _UNKNOWN.clear()


def unknown_ids(allowlist: Optional[FrozenSet[str]], known: Iterable[str]) -> List[str]:
    if allowlist is None:
        return []
    return sorted(allowlist - set(known) - PROCESS_LOOP_IDS)


def record_unknown(ids: Iterable[str]) -> None:
    _UNKNOWN[:] = sorted(ids)


def skipped_ids() -> List[str]:
    return sorted(_SKIPPED)


def diagnostics() -> Dict[str, object]:
    """Fields for /__scheduler_health. EMPTY when the allowlist is unset, so the endpoint's
    response is unchanged for every process that does not set it."""
    allowlist = active_allowlist()
    if allowlist is None:
        return {}
    return {
        "job_allowlist": sorted(allowlist),
        "skipped_by_allowlist": skipped_ids(),
        "job_allowlist_unknown_ids": list(_UNKNOWN),
    }


def _reset_for_tests() -> None:
    _SKIPPED.clear()
    _UNKNOWN.clear()
