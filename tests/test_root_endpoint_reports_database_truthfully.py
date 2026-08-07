"""`/` must not report health it hasn't measured — the second door onto the
guarantee `/health` now makes.

THE DEFECT. `/` returned `"status": "healthy"` and `"health": "OK"` as string
literals while `db_status`, in the same payload, was computed and could say
`"disconnected"`. Three fields that were supposed to agree, with nothing
forcing them to. The HTTP status was 200 unconditionally, so every reader that
looks at the status code — the normal way to poll an indicator — got a green
answer from a process that could not serve one database-backed request.

WHY IT IS WORTH ITS OWN TEST. `/health` was made observe-only and 503-on-failure
so that a dead backend cannot hide behind a green check. Railway's healthcheck
points at `/health` — a committed API dump of the live `web` service records
`"healthcheckPath": "/health"` beside `"domain": "api.pivota.cc"` — but that is
a Railway SERVICE setting, not a fact this repo pins, and re-pointing it at `/`
is one dashboard edit. An honesty guarantee that holds on one path and not its
neighbour is a guarantee with a silent expiry date. These tests remove the
difference between the two doors.

WHAT IS PROVEN HERE, AND WHAT IS NOT. Proven: with the shared `database` in the
disconnected shape, the REAL `/` route answers 503, every reported field reads
non-green, and the handler performs no connect and no pool reset. NOT proven,
and not claimed: that this endpoint was implicated in the 12.5-hour outage of
2026-08-05/06. That incident is the motivation for looking here; its mechanism
was never reproduced from this code path.

The rule these tests enforce: `/` OBSERVES the database (never repairs it), and
its status code and every status-ish field in its body derive from that one
observation.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from utils import database_readiness as readiness

# The spy is the one `/health`'s tests are held to, imported rather than
# re-declared: a private copy would be free to drift into a friendlier fake
# than its neighbour's, and the point of this file is that both doors meet the
# same standard. (Runtime caveat, so the comment is not read as more than it
# is: `tests/` has no `__init__.py` and pytest's prepend import mode already
# loads that module under a top-level name, so this import produces a SECOND
# module object and a second, identical `SpyDatabase` class. One source file,
# two classes — the anti-drift property is of the source, not of the process.
# Harmless because the spy holds no module-level state.)
from tests.test_health_check_does_not_repair import SpyDatabase


def _health_body(response) -> dict:
    """Unwrap the global error envelope.

    `ErrorHandlerMiddleware` normalizes every JSON response >=400 into
    {"status": "error", "error": {"code", "message", "details"}}, so a 503
    payload arrives nested under error.details. Unwrap rather than assert the
    envelope's shape here, so the semantic assertions keep pinning `/` even if
    the envelope changes — the envelope itself is pinned once, deliberately, in
    `test_the_503_wire_format_is_the_error_envelope`. 2xx bodies pass through.
    """
    payload = response.json()
    if isinstance(payload, dict) and payload.get("status") == "error" and "error" in payload:
        return payload["error"].get("details", payload)
    return payload


@contextmanager
def _spy_installed(spy: SpyDatabase):
    """Bind BOTH names the handler could reach the database by, and unbind them
    before the caller's TestClient context exits.

    Two names: `probe_database_health` resolves `database` in
    `utils.database_readiness`, but `main` also holds its own module-level
    `from db.database import database`, and the pre-fix handler used exactly
    that one. Patching only the readiness name would leave a repair written as
    `await database.connect()` in main.py invisible to the counters below.

    Unbinding before the TestClient context exits is NOT tidiness — it is
    required for correctness, and getting it wrong broke the suite. The
    lifespan shutdown calls `database.disconnect()` through `main`'s namespace.
    With the spy still installed at that moment, teardown disconnects the SPY,
    and the real `databases.Database` singleton is left `is_connected=True`
    holding an asyncpg pool bound to a dead event loop; `connect()` then
    early-returns on that flag, so every later TestClient in the process runs
    its startup DDL against the corpse. On Postgres that failed 4 of the 5
    tests in this file with `InterfaceError: another operation is in progress`.
    It did NOT reproduce on sqlite, which is what CI's main sweep runs — so
    scoping the patch to the request is the whole defence.
    """
    import main

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(readiness, "database", spy)
        mp.setattr(main, "database", spy)
        yield spy


@pytest.fixture
def silent_supervisor(monkeypatch: pytest.MonkeyPatch):
    """Hold the reconnect supervisor off for the duration of the test.

    The lifespan starts `run_database_reconnect_supervisor`, which calls
    `database.connect()` on a disconnected backend — exactly the counter these
    tests read. Its interval is 30s so it would not realistically fire inside a
    sub-second test, but "would not realistically" is not a test guarantee: a
    slow CI box turns the assertion `connect_calls == 0` into a coin flip, and
    worse, a flake here would look like the handler repairing the database. The
    kill switch is re-read every cycle, so setting it before the lifespan
    starts keeps every counter below attributable to `/` alone.
    """
    monkeypatch.setenv("DB_RECONNECT_SUPERVISOR_ENABLED", "false")


def test_root_reports_503_and_no_green_field_when_the_backend_is_disconnected(
    silent_supervisor: None,
) -> None:
    """Drives the REAL `/` route. This is the assertion that stops `/` from
    answering "healthy" for a process that cannot reach its database."""
    from main import app

    with TestClient(app) as client:
        # Baseline through the same real route: a live backend must read green,
        # so the 503 below is attributable to connectivity and nothing else
        # (an app that 503s at `/` for an unrelated reason would pass a
        # disconnected-only assertion while proving nothing).
        baseline_resp = client.get("/")
        baseline = _health_body(baseline_resp)
        assert baseline_resp.status_code == 200, baseline
        assert baseline["db_status"] == "connected", "baseline: a live DB must read connected"

        with _spy_installed(SpyDatabase(connected=False)) as spy:
            resp = client.get("/")
            # Snapshot while the spy is installed and before any teardown.
            calls = (spy.connect_calls, spy.disconnect_calls, spy.execute_calls)

    assert resp.status_code == 503, (
        "`/` answered a success code for a process with no database — anything "
        "polling it (a monitor, a load balancer, or Railway itself if "
        "healthcheckPath is ever pointed here) would never see the outage")

    body = _health_body(resp)
    # Every status-ish field, not just the one that was already honest. The
    # defect was precisely that `db_status` told the truth while its neighbours
    # did not, so asserting `db_status` alone would have passed before the fix.
    assert body["status"] == "unhealthy"
    assert body["health"] != "OK"
    assert body["db_status"] == "disconnected"
    assert body["error"] == "DatabaseBackendNotRunning"

    # Observe, never repair: an indicator that fixes what it measures reports
    # the health of its own repair attempt.
    connect_calls, disconnect_calls, execute_calls = calls
    assert connect_calls == 0, "`/` tried to reconnect the database"
    assert disconnect_calls == 0, "`/` reset pool state"
    assert execute_calls == 0, "`/` queried a backend it knew was disconnected"


def test_root_reports_200_and_all_green_fields_when_the_database_answers(
    silent_supervisor: None,
) -> None:
    """The other half of the pair. Making `/` honest is worthless if it now
    reports trouble for a healthy process — that is how a truthful indicator
    gets switched off."""
    from main import app

    with TestClient(app) as client:
        with _spy_installed(SpyDatabase(connected=True)) as spy:
            resp = client.get("/")
            calls = (spy.connect_calls, spy.disconnect_calls, spy.execute_calls)

    assert resp.status_code == 200, _health_body(resp)
    body = _health_body(resp)
    assert body["status"] == "healthy"
    assert body["health"] == "OK"
    assert body["db_status"] == "connected"
    assert body["error"] is None

    # The banner fields are the only part of this payload with no health
    # meaning. Pin their PRESENCE — an unknown consumer may read them — without
    # blessing their values: `version` is a hardcoded "0.2.1-fixed" that this
    # change deliberately left alone rather than quietly widen its scope.
    assert "message" in body and "version" in body, "the banner fields were dropped"

    # It reached the database to find out, and still repaired nothing.
    connect_calls, disconnect_calls, execute_calls = calls
    assert execute_calls == 1, "`/` claimed health without probing"
    assert connect_calls == 0 and disconnect_calls == 0


def test_the_503_wire_format_is_the_error_envelope(
    silent_supervisor: None,
) -> None:
    """Pin what a CLIENT actually receives, which is not what the handler
    returns.

    `ErrorHandlerMiddleware` re-nests every >=400 JSON body, so on 503 the
    handler's payload moves under `error.details` and the TOP-LEVEL `status`
    reads "error", never "unhealthy". Two consequences worth having in writing
    rather than discovering during an incident: a client reading
    `body["status"]` from `/` never sees the handler's status string, and
    `body["db_status"]` — the one field that was always honest, and so the only
    one a pre-existing consumer could plausibly have read — is absent at the
    top level on 503 where it was always present before. The status CODE is the
    signal that survives the envelope intact, which is why the fix leads with
    it.

    Every other test here unwraps via `_health_body`; this one does not, so the
    envelope cannot change silently underneath them.
    """
    from main import app

    with TestClient(app) as client:
        with _spy_installed(SpyDatabase(connected=False)):
            resp = client.get("/")

    assert resp.status_code == 503
    payload = resp.json()
    assert payload["status"] == "error", "the envelope stopped wrapping /"
    assert "db_status" not in payload, (
        "db_status reappeared at the top level on 503 — the envelope changed, "
        "and a consumer's expectations along with it")
    assert payload["error"]["details"]["db_status"] == "disconnected"


@pytest.mark.parametrize("connected", [True, False])
def test_root_never_reports_a_status_its_db_field_contradicts(
    silent_supervisor: None,
    connected: bool,
) -> None:
    """THE INVARIANT, stated once and checked in both states.

    The original bug was not a wrong constant, it was three fields with no
    mechanism binding them together — so a future edit could fix `status` and
    leave `health` lying, and the suite above would still be green. This binds
    the HTTP status code, `status`, `health` and `db_status` to a single
    observation, in both directions.
    """
    from main import app

    with TestClient(app) as client:
        with _spy_installed(SpyDatabase(connected=connected)):
            resp = client.get("/")

    body = _health_body(resp)
    db_connected = body["db_status"] == "connected"

    assert db_connected is connected, "the probe did not observe the state it was given"
    assert (resp.status_code == 200) is db_connected, (
        f"HTTP {resp.status_code} contradicts db_status={body['db_status']!r}")
    assert (body["status"] == "healthy") is db_connected, (
        f"status={body['status']!r} contradicts db_status={body['db_status']!r}")
    assert (body["health"] == "OK") is db_connected, (
        f"health={body['health']!r} contradicts db_status={body['db_status']!r}")


@pytest.mark.parametrize("configured_timeout", [0.5, 1.5])
def test_root_bounds_a_stalled_database_by_its_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
    silent_supervisor: None,
    configured_timeout: float,
) -> None:
    """`/` used to await `database.execute("SELECT 1")` with NO timeout.

    A backend that accepts the query and never answers — the shape a health
    check most needs to survive — left this handler hanging instead of
    reporting on it, which reads to a poller as a timeout rather than as a
    service saying it is down. Restoring the pre-fix line verbatim makes this
    file take 33s instead of 3s: the handler hangs for the whole stall.

    TWO timeouts, with a window tied to each, because a single loose bound
    ("elapsed < 5s") pinned almost nothing — dropping the argument entirely
    (falling back to the primitive's own 3.0s default), reading the WRONG env
    var, and hardcoding 4.9s all survived it. What needs pinning is that the
    CONFIGURED value reaches the handler, so the wait must track the setting
    rather than merely be shorter than the stall.
    """

    class StallingDatabase(SpyDatabase):
        async def execute(self, _query):
            self.execute_calls += 1
            await asyncio.sleep(30)

    from main import app

    monkeypatch.setenv("DB_HEALTH_PROBE_TIMEOUT_SECONDS", str(configured_timeout))

    with TestClient(app) as client:
        with _spy_installed(StallingDatabase(connected=True)) as spy:
            started = time.monotonic()
            resp = client.get("/")
            elapsed = time.monotonic() - started
            execute_calls = spy.execute_calls

    assert resp.status_code == 503
    body = _health_body(resp)
    assert body["status"] == "unhealthy"
    # Exactly TimeoutError: `except Exception` cannot see CancelledError on
    # 3.11 (it derives from BaseException), so accepting it would be a value
    # that can never occur.
    assert body["error"] == "TimeoutError"
    assert execute_calls == 1

    # Lower bound: it really waited the configured time, rather than failing
    # early for an unrelated reason. Upper bound: 1.0s of slack for request
    # overhead on a loaded box — still tight enough that the primitive's 3.0s
    # default and a hardcoded 4.9s fall outside BOTH windows.
    assert configured_timeout * 0.8 <= elapsed < configured_timeout + 1.0, (
        f"`/` waited {elapsed:.2f}s for a stalled backend, but was configured "
        f"for {configured_timeout}s — the setting is not reaching the handler")
