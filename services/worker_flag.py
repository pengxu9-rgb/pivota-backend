"""AUDIT_WORKER_ENABLED, parsed ONCE for every reader.

Deliberately tiny and import-free apart from `os`: the boot-loop gates in
services/scheduler_job_allowlist.py read it from the webhook retry workers' start functions, and
those must not drag services.audit_scheduler (APScheduler, the job table, the drain parser) into
a process just to read one environment variable.
"""

from __future__ import annotations

import os
from typing import Optional

WORKER_FLAG_ENV = "AUDIT_WORKER_ENABLED"
WORKER_FLAG_TRUTHY = ("1", "true", "yes", "on")


def worker_flag_override() -> Optional[bool]:
    """None when AUDIT_WORKER_ENABLED is unset or blank (no explicit decision), else True for a
    truthy spelling (case- and whitespace-insensitive) and False for anything else.
    services.audit_scheduler._queue_worker_enabled and the SCHEDULER_JOB_ALLOWLIST fail-closed
    rule both read it here, so they cannot disagree about what "explicitly true" means."""
    raw = (os.getenv(WORKER_FLAG_ENV) or "").strip().lower()
    if not raw:
        return None
    return raw in WORKER_FLAG_TRUTHY
