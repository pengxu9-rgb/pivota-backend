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

import pytest

from routes.pool_health import _tasks_by_frame, _verdict


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
            task.cancel()

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
            task.cancel()

    @pytest.mark.asyncio
    async def test_output_is_bounded(self) -> None:
        """A wedge is exactly when task counts are abnormal.

        An unbounded dict here would make the diagnostic unreadable in the one
        situation it exists for.
        """
        from routes.pool_health import _MAX_FRAME_GROUPS

        assert len(_tasks_by_frame()) <= _MAX_FRAME_GROUPS


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
