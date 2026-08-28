"""The four-way verdict is the whole point of /__pool_health.

Three prod wedges (#1754, 2026-08-18, 2026-08-28) all presented as "the database
is broken" and all three times the database was fine — Cloud SQL under 10% of
max_connections, ~23 connections all plain `idle`, the app's own SQL returning in
0.03s over a direct connection. What was missing every time was a measurement
taken WHILE wedged that separates "our pool is starved" from "Postgres is down",
because the only remediation is a restart that destroys the evidence.

These tests pin that partition. Each verdict is a different operator action —
restart this instance, page the DBA, check egress, do nothing — so collapsing any
two of them silently is the failure mode worth guarding.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from routes.pool_health import _tasks_by_frame, _verdict


async def _drain(task: "asyncio.Task") -> None:
    """Cancel AND await.

    A bare `task.cancel()` only requests cancellation; the task is still pending
    when pytest-asyncio closes the loop, which surfaces as a flood of
    `RuntimeError: Event loop is closed` at GC and leaves a coroutine reachable
    from a dead loop's task set. Tests that leak tasks contaminate whatever runs
    next in the same process.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# Two frames deliberately in DIFFERENT functions: `_framework_entry` is the
# task's outermost coroutine (uvicorn's run_asgi stands here in production) and
# `_app_code` is where the task is really parked. An implementation that reports
# the outermost frame names the wrong one.
async def _app_code() -> None:
    await asyncio.sleep(30)


_APP_CODE_LINE = _app_code.__code__.co_firstlineno + 1


async def _framework_entry() -> None:
    await _app_code()


_FRAMEWORK_LINE = _framework_entry.__code__.co_firstlineno + 1


class TestVerdict:
    """pool_ok x direct_ok -> the operator's next move."""

    def test_pool_starved_is_distinguished_from_a_dead_database(self) -> None:
        """THE distinction. Both have a failing pool; the responses are opposite.

        pool_starved  -> restart this instance, service returns
        database_unreachable -> restarting the fleet makes it WORSE
        """
        assert _verdict(pool_ok=False, direct_ok=True) == "pool_starved"
        assert _verdict(pool_ok=False, direct_ok=False) == "database_unreachable"

    def test_healthy_requires_the_pool_to_answer(self) -> None:
        assert _verdict(pool_ok=True, direct_ok=True) == "healthy"

    def test_a_blocked_control_probe_is_not_reported_as_healthy(self) -> None:
        """If the control cannot run, say so rather than implying it passed.

        A working pool with an unreachable direct probe means the probe's own
        path is broken (egress, IAM) — the endpoint's control is untrustworthy
        and an operator must know that before relying on the other verdicts.
        """
        assert _verdict(pool_ok=True, direct_ok=False) == "direct_probe_blocked"

    def test_an_unknown_control_never_reports_a_wedge(self) -> None:
        """`direct_ok=None` means DATABASE_URL was unset — no control ran.

        Claiming `pool_starved` on no evidence would send an operator to restart
        instances during what might be a real database outage. Unknown must stay
        unknown.
        """
        assert _verdict(pool_ok=False, direct_ok=None) == "unknown"


class TestTasksByFrame:
    """The measurement absent from all three post-mortems: what is parked."""

    @pytest.mark.asyncio
    async def test_it_names_the_frame_a_parked_task_is_sitting_in(self) -> None:
        async def parked() -> None:
            await asyncio.sleep(30)

        task = asyncio.create_task(parked())
        await asyncio.sleep(0)  # let it reach the await
        try:
            frames = _tasks_by_frame()
            assert any(
                "test_pool_health_partitions_pool_from_database.py" in key
                for key in frames
            ), f"the parked task's own frame is missing from {frames}"
            assert all(isinstance(v, int) for v in frames.values())
        finally:
            await _drain(task)

    @pytest.mark.asyncio
    async def test_it_leaks_no_arguments_or_sql(self) -> None:
        """/__pool_health is unauthenticated, like /__build and /__scheduler_health.

        A code position answers "which code is parked" completely, so there is no
        reason to emit locals — and every reason not to, since the values parked
        in a DB-heavy frame are query text and arguments.
        """
        secret = "s3cret-token-value"  # noqa: S105 — bait, must not appear

        async def parked_holding_a_secret(token: str = secret) -> None:
            await asyncio.sleep(30)

        task = asyncio.create_task(parked_holding_a_secret())
        await asyncio.sleep(0)
        try:
            rendered = repr(_tasks_by_frame())
            assert secret not in rendered
            assert "token" not in rendered
        finally:
            await _drain(task)

    @pytest.mark.asyncio
    async def test_it_reports_the_deepest_app_frame_not_the_outermost(self) -> None:
        """The defect that made the original implementation useless.

        `Task.get_stack()` on a SUSPENDED task returns only the outermost
        coroutine frame — coroutine frames do not link `f_back` while suspended,
        so the walk stops after one and `limit` never binds. Under uvicorn every
        request task's outermost coroutine is `RequestResponseCycle.run_asgi`, so
        every parked request collapsed into a single anonymous group.

        The original test could not see this: its task's coroutine was defined in
        the test file, so outermost and parked frame were the SAME frame. Here
        they are deliberately different — `_framework_entry` is the outermost and
        `_app_code` is where the task is actually parked.
        """
        task = asyncio.create_task(_framework_entry())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            frames = _tasks_by_frame()
            reported = [k for k in frames if "test_pool_health" in k]
            assert reported, f"no app frame reported at all: {frames}"
            lines = {int(k.rsplit(":", 1)[1]) for k in reported}
            assert _APP_CODE_LINE in lines, (
                f"reported {sorted(lines)}, expected the parked frame at line "
                f"{_APP_CODE_LINE} (_app_code), not the outermost entry at "
                f"{_FRAMEWORK_LINE} (_framework_entry)"
            )
        finally:
            await _drain(task)

    @pytest.mark.asyncio
    async def test_plumbing_frames_are_never_the_answer(self) -> None:
        """Walking all the way down lands in asyncio.sleep on every parked task.

        A dump that says `tasks.py:711` for everything is as useless as one that
        says `h11_impl.py:259` for everything.
        """
        from routes.pool_health import _is_app_frame

        assert not _is_app_frame("/usr/lib/python3.11/asyncio/tasks.py")
        assert not _is_app_frame("/app/.venv/lib/python3.11/site-packages/uvicorn/x.py")
        assert _is_app_frame("/app/routes/agent_shop_gateway.py")

        task = asyncio.create_task(_framework_entry())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            assert not any("tasks.py" in k for k in _tasks_by_frame())
        finally:
            await _drain(task)


class TestPoolCounters:
    """Untested in the first cut, and 100% wrong as a result."""

    def test_it_reads_the_pool_off_the_backend(self, monkeypatch) -> None:
        """`_pool` is an attribute of the BACKEND, not the Database facade.

        Reading `database._pool` returns None on a perfectly healthy pool, and
        getattr never raises, so the endpoint reported `no_pool` unconditionally —
        a state that elsewhere in this repo means "the pool object is gone", i.e.
        a different incident with a different fix. The single number this endpoint
        exists to produce could never appear.
        """
        import db.database as dbmod
        from routes.pool_health import _pool_counters

        class FakePool:
            def get_size(self) -> int:
                return 6

            def get_idle_size(self) -> int:
                return 0

            def get_max_size(self) -> int:
                return 6

        class FakeBackend:
            _pool = FakePool()

        class FakeDatabase:
            _backend = FakeBackend()

        monkeypatch.setattr(dbmod, "database", FakeDatabase())
        counters = _pool_counters()
        assert counters["state"] == "present", (
            f"a live pool reported as {counters!r} — the endpoint is blind"
        )
        assert counters["max_size"] == 6
        # Fully checked out and nothing returned: the wedge signature.
        assert counters["in_use"] == 6

    def test_in_use_is_size_minus_idle(self, monkeypatch) -> None:
        import db.database as dbmod
        from routes.pool_health import _pool_counters

        class FakePool:
            def get_size(self) -> int:
                return 6

            def get_idle_size(self) -> int:
                return 4

            def get_max_size(self) -> int:
                return 6

        class FakeBackend:
            _pool = FakePool()

        class FakeDatabase:
            _backend = FakeBackend()

        monkeypatch.setattr(dbmod, "database", FakeDatabase())
        assert _pool_counters()["in_use"] == 2


class TestDirectProbeIsBounded:
    """This route is unauthenticated and has NO rate limit.

    Both global middlewares early-return for non-/agent paths, so anyone on the
    internet can drive it. Each uncached call opens a real connection OUTSIDE the
    pool budget — unbounded, that saturates the 300-connection ceiling and the
    diagnostic becomes the incident.
    """

    @pytest.mark.asyncio
    async def test_concurrent_callers_open_one_connection(self, monkeypatch) -> None:
        import routes.pool_health as ph

        calls = {"n": 0}

        async def fake_uncached():
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return {"ok": True, "elapsed_ms": 1}

        monkeypatch.setattr(ph, "_probe_direct_uncached", fake_uncached)
        ph._cached.clear()
        results = await asyncio.gather(*[ph._probe_direct() for _ in range(12)])
        assert calls["n"] == 1, f"opened {calls['n']} connections for 12 callers"
        assert all(r["ok"] for r in results)


def test_the_endpoint_is_actually_mounted() -> None:
    """A route module that is never included is a file, not an endpoint.

    The whole value of this work is being able to curl it during an incident, so
    "the functions are correct" is not the claim that matters — "the path answers"
    is. Guards the include_router line in main.py, which is trivially droppable in
    a merge and would fail nothing else here.
    """
    from main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/__pool_health" in paths
