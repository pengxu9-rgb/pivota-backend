import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from services.agent_task_manager import AgentTaskManager, TaskState


@pytest.mark.asyncio
async def test_single_flight_rejects_parallel_session() -> None:
    manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=1,
        task_timeout_seconds=5.0,
        max_calls_per_session=10,
        max_duplicate_payloads=10,
    )

    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "ok"

    # First task for the session should be accepted.
    task1_id, fut1 = await manager.enqueue(
        operation="op",
        session_id="sess-1",
        payload_hash=AgentTaskManager.compute_payload_hash("op", {"n": 1}),
        coro_factory=lambda: slow(),
    )

    # Second in-flight task for the same session must be rejected (single-flight).
    with pytest.raises(HTTPException) as exc_info:
        await manager.enqueue(
            operation="op",
            session_id="sess-1",
            payload_hash=AgentTaskManager.compute_payload_hash("op", {"n": 2}),
            coro_factory=lambda: slow(),
        )
    assert exc_info.value.status_code == 409

    # Let the first task finish cleanly.
    await fut1
    state = await manager.get_task_state(task1_id)
    assert state in {
        TaskState.SUCCEEDED,
        TaskState.CANCELLED,
        TaskState.TIMEOUT,
    }


@pytest.mark.asyncio
async def test_queue_rejects_when_full() -> None:
    # max_queue_size=0 means we never allow waiting tasks; only active workers.
    manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=0,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=100,
    )

    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "ok"

    # First task (no session id) occupies the only worker slot.
    _, fut1 = await manager.enqueue(
        operation="op",
        session_id=None,
        payload_hash=AgentTaskManager.compute_payload_hash("op", {"n": 1}),
        coro_factory=lambda: slow(),
    )

    # Any additional task (regardless of session) should see global backpressure.
    with pytest.raises(HTTPException) as exc_info:
        await manager.enqueue(
            operation="op",
            session_id=None,
            payload_hash=AgentTaskManager.compute_payload_hash("op", {"n": 2}),
            coro_factory=lambda: slow(),
        )
    assert exc_info.value.status_code == 429

    await fut1


@pytest.mark.asyncio
async def test_loop_detection_by_payload_hash() -> None:
    manager = AgentTaskManager(
        max_workers=2,
        max_queue_size=4,
        task_timeout_seconds=5.0,
        max_calls_per_session=10,
        max_duplicate_payloads=2,
    )

    async def immediate() -> str:
        return "ok"

    payload_key: Any = {"query": "same", "page": 1}
    payload_hash = AgentTaskManager.compute_payload_hash("op", payload_key)

    # First two calls with identical payload are allowed.
    _, fut1 = await manager.enqueue(
        operation="op",
        session_id="loop-session",
        payload_hash=payload_hash,
        coro_factory=lambda: immediate(),
    )
    await fut1

    _, fut2 = await manager.enqueue(
        operation="op",
        session_id="loop-session",
        payload_hash=payload_hash,
        coro_factory=lambda: immediate(),
    )
    await fut2

    # Third identical call should be rejected as a potential tool loop.
    with pytest.raises(HTTPException) as exc_info:
        await manager.enqueue(
            operation="op",
            session_id="loop-session",
            payload_hash=payload_hash,
            coro_factory=lambda: immediate(),
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "TOOL_LOOP_DETECTED"


@pytest.mark.asyncio
async def test_session_budget_limit() -> None:
    manager = AgentTaskManager(
        max_workers=2,
        max_queue_size=4,
        task_timeout_seconds=5.0,
        max_calls_per_session=2,
        max_duplicate_payloads=10,
    )

    async def immediate() -> str:
        return "ok"

    # Two calls within budget are fine.
    for idx in range(2):
        _, fut = await manager.enqueue(
            operation="op",
            session_id="budget-session",
            payload_hash=AgentTaskManager.compute_payload_hash("op", {"n": idx}),
            coro_factory=lambda: immediate(),
        )
        await fut

    # Third call should exceed per-session call budget.
    with pytest.raises(HTTPException) as exc_info:
        await manager.enqueue(
            operation="op",
            session_id="budget-session",
            payload_hash=AgentTaskManager.compute_payload_hash("op", {"n": 3}),
            coro_factory=lambda: immediate(),
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "SESSION_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_cancel_queued_task_marks_cancelled() -> None:
    manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=1,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=100,
    )

    async def slow() -> str:
        await asyncio.sleep(0.2)
        return "ok"

    # First task occupies worker.
    _, fut1 = await manager.enqueue(
        operation="op",
        session_id=None,
        payload_hash=AgentTaskManager.compute_payload_hash("op", {"n": 1}),
        coro_factory=lambda: slow(),
    )

    # Second task is queued.
    task2_id, fut2 = await manager.enqueue(
        operation="op",
        session_id=None,
        payload_hash=AgentTaskManager.compute_payload_hash("op", {"n": 2}),
        coro_factory=lambda: slow(),
    )

    # Cancel queued task before it starts.
    await manager.cancel(task2_id, reason="test_cancel")

    with pytest.raises(asyncio.CancelledError):
        await fut2

    state2 = await manager.get_task_state(task2_id)
    assert state2 == TaskState.CANCELLED

    await fut1


@pytest.mark.asyncio
async def test_cancel_running_task_marks_cancelled() -> None:
    manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=0,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=100,
    )

    async def slow() -> str:
        await asyncio.sleep(0.5)
        return "ok"

    task_id, fut = await manager.enqueue(
        operation="op",
        session_id="cancel-session",
        payload_hash=AgentTaskManager.compute_payload_hash("op", {"n": 1}),
        coro_factory=lambda: slow(),
    )

    # Give the task a moment to start.
    await asyncio.sleep(0.05)
    await manager.cancel(task_id, reason="test_running_cancel")

    with pytest.raises(asyncio.CancelledError):
        await fut

    state = await manager.get_task_state(task_id)
    assert state == TaskState.CANCELLED

