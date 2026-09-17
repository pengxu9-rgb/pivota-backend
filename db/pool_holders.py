"""Who checked out the pool connections that never came back.

WHAT WAS MISSING (2026-09-16). `web` lost its whole pool again: four instances each held exactly
13 Postgres connections, every one plain `idle` for up to 12 hours, while Cloud SQL sat at 0.1-0.35
CPU. That is the fourth occurrence of the wedge `routes/pool_health.py` was written for, and
`/__pool_health` could not have named the cause either: `tasks_by_frame` lists tasks that are
PARKED, and a connection whose owning task has already finished — the leak case — has no task to
park. The connection is simply checked out with nobody holding it. The only code position that can
still be recovered is where it was ACQUIRED, and only if it was recorded at the time.

So every successful checkout records the acquiring application frames, and every release removes
the record. What remains is, by construction, the set of connections that are out right now, each
with its age and its acquisition site. A connection held longer than
`DB_POOL_HOLDER_WARN_SECONDS` is logged ONCE with that site, so the evidence reaches Cloud Logging
before the restart that destroys it.

FILE:LINE only, never locals, arguments or SQL — the snapshot is served by the unauthenticated
`/__pool_health`, like its sibling fields.

Cost: one bounded `f_back` walk per checkout (no source lookup, no `traceback` formatting) and a
scan of the live records, which is at most pool size plus whatever has leaked.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Frames that are plumbing, never the answer: the event loop, installed libraries, the stdlib,
# and this pool layer itself (every checkout passes through db/database.py).
_PLUMBING_MARKERS = (
    os.sep + "asyncio" + os.sep,
    os.sep + "site-packages" + os.sep,
    os.sep + "lib" + os.sep + "python",
)
# Full `db/<name>` suffixes, not bare basenames: `tests/test_pool_holders.py` also ends in
# `pool_holders.py`, and a basename match silently erased the test's own frames.
_SELF_FILES = (os.sep + "db" + os.sep + "pool_holders.py", os.sep + "db" + os.sep + "database.py")

#: How many application frames to keep per checkout. The innermost names the query helper; the
#: outer ones name the route or service that called it, which is usually the useful one.
MAX_SITE_FRAMES = 4
#: A frame walk longer than this is not a request stack; stop rather than walk a pathological one.
_MAX_WALK = 80


def _is_app_frame(filename: str) -> bool:
    if filename.startswith("<"):  # <frozen runpy>, <string>: interpreter internals, never a site
        return False
    if any(marker in filename for marker in _PLUMBING_MARKERS):
        return False
    return not filename.endswith(_SELF_FILES)


def acquisition_site(start_depth: int = 1) -> Tuple[str, ...]:
    """The application frames on the CURRENT stack, innermost first, as `file:line`.

    While a coroutine is executing, its frame's `f_back` chain runs through every coroutine
    awaiting it, so this walk reaches the route that asked for the connection. (A SUSPENDED
    coroutine does not link `f_back` — that is why routes/pool_health.py follows `cr_await`
    instead — but acquire records the site while it is running.)
    """
    try:
        frame = sys._getframe(start_depth)
    except ValueError:
        return ()
    site: List[str] = []
    for _ in range(_MAX_WALK):
        if frame is None or len(site) >= MAX_SITE_FRAMES:
            break
        filename = frame.f_code.co_filename
        if _is_app_frame(filename):
            site.append(f"{os.path.basename(filename)}:{frame.f_lineno}")
        frame = frame.f_back
    return tuple(site)


@dataclass
class Checkout:
    acquired_at: float
    site: Tuple[str, ...]
    warned: bool = False


class HolderRegistry:
    """Connections checked out and not yet released, keyed by the backend connection object.

    Keyed by `id()` and holding a strong reference to the object: a leaked `PostgresConnection`
    that got garbage-collected would otherwise free its id for reuse and silently merge two
    records — and a leaked connection is exactly the one worth keeping.
    """

    def __init__(self) -> None:
        self._live: Dict[int, Tuple[Any, Checkout]] = {}

    def record(self, conn: Any, site: Tuple[str, ...], *, now: Optional[float] = None) -> None:
        self._live[id(conn)] = (conn, Checkout(acquired_at=time.monotonic() if now is None else now,
                                               site=site))

    def forget(self, conn: Any) -> None:
        self._live.pop(id(conn), None)

    def __len__(self) -> int:
        return len(self._live)

    def warn_overdue(self, warn_after_seconds: float, *, now: Optional[float] = None) -> int:
        """Log each checkout older than the threshold exactly once. Returns how many were logged."""
        now = time.monotonic() if now is None else now
        logged = 0
        for _, checkout in list(self._live.values()):
            age = now - checkout.acquired_at
            if checkout.warned or age < warn_after_seconds:
                continue
            checkout.warned = True
            logged += 1
            logger.warning(
                "database pool connection held %.0fs without release — acquired at %s "
                "(live checkouts=%d). A connection held this long is leaked or wedged; "
                "this site is where it was taken.",
                age,
                " <- ".join(checkout.site) or "<no application frame>",
                len(self._live),
            )
        return logged

    def snapshot(self, *, now: Optional[float] = None, limit: int = 15) -> Dict[str, Any]:
        """Live checkouts grouped by acquisition site, oldest group first."""
        now = time.monotonic() if now is None else now
        groups: Dict[str, List[float]] = defaultdict(list)
        for _, checkout in self._live.values():
            key = " <- ".join(checkout.site) or "<no application frame>"
            groups[key].append(now - checkout.acquired_at)
        ordered = sorted(groups.items(), key=lambda kv: max(kv[1]), reverse=True)
        return {
            "checked_out": len(self._live),
            "by_site": [
                {"site": site, "count": len(ages), "oldest_seconds": round(max(ages), 1)}
                for site, ages in ordered[:limit]
            ],
        }


#: The process-wide registry the patched acquire/release write to.
REGISTRY = HolderRegistry()
