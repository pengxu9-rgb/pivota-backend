from __future__ import annotations

import asyncio
import os
import time
import uuid
import hashlib
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Deque, Dict, Optional, Tuple

from fastapi import HTTPException

from utils.logger import logger


TaskCoroutine = Callable[[], Awaitable[Any]]


class TaskState(str, Enum):
    """
    Internal lifecycle states for queued agent tasks.

    These are used for structured logging, metrics, and tests.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass
class SessionBudget:
    """
    Lightweight per-session budgeting data.

    - call_count: total tasks accepted for this session in this process
    - recent_hashes: rolling window of recent payload hashes for loop detection
    """

    call_count: int = 0
    recent_hashes: Deque[str] = field(
        default_factory=lambda: deque(maxlen=8)
    )


@dataclass
class TaskRecord:
    """
    In-memory record for a single queued task.
    """

    id: str
    operation: str
    session_id: Optional[str]
    request_id: Optional[str]
    payload_hash: str
    coro_factory: TaskCoroutine
    enqueued_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    state: TaskState = TaskState.QUEUED
    error: Optional[str] = None
    result: Any = None
    future: asyncio.Future = field(default_factory=asyncio.Future)
    runner: Optional[asyncio.Task] = None


class AgentTaskManager:
    """
    Bounded in-process queue + worker pool for heavy agent work.

    Design goals:
    - Limit concurrent heavy tasks per process (worker pool).
    - Bound the number of queued tasks (backpressure with 429/409).
    - Enforce per-session single-flight (at most 1 in-flight task).
    - Enforce simple per-session budgets and loop detection.
    - Provide basic structured logging and in-memory metrics.

    NOTE: The queue is per-process. In a multi-worker deployment
    (e.g. Gunicorn + uvicorn workers), each worker keeps its own
    queue and budgets. This is still a strict improvement over
    unbounded concurrency but should be called out in runbooks.
    """

    def __init__(
        self,
        max_workers: int = 8,
        max_queue_size: int = 64,
        task_timeout_seconds: float = 8.0,
        max_calls_per_session: int = 64,
        max_duplicate_payloads: int = 3,
        max_queue_wait_seconds: float = 5.0,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if max_queue_size < 0:
            raise ValueError("max_queue_size must be >= 0")

        self._max_workers = max_workers
        self._max_queue_size = max_queue_size
        self._task_timeout_seconds = task_timeout_seconds
        self._max_calls_per_session = max_calls_per_session
        self._max_duplicate_payloads = max_duplicate_payloads
        self._max_queue_wait_seconds = max_queue_wait_seconds

        self._lock = asyncio.Lock()
        self._queue: Deque[str] = deque()
        self._tasks: Dict[str, TaskRecord] = {}
        self._running: Dict[str, TaskRecord] = {}
        # session_id -> task_id currently in-flight (queued or running)
        self._session_inflight: Dict[str, str] = {}
        self._session_budgets: Dict[str, SessionBudget] = {}
        # request_id -> task_id for idempotency
        self._request_index: Dict[str, str] = {}

        # Minimal in-memory counters for observability
        self._metrics: Dict[str, int] = {
            "enqueued": 0,
            "started": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "timed_out": 0,
            "expired": 0,
            "rejected": 0,
        }

    @classmethod
    def from_env(cls) -> "AgentTaskManager":
        """
        Build a manager using environment-configured limits.

        All env vars are optional and have safe defaults.
        """

        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if not raw:
                return default
            try:
                value = int(raw)
                return value if value > 0 else default
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if not raw:
                return default
            try:
                value = float(raw)
                return value if value > 0 else default
            except ValueError:
                return default

        max_workers = _int("AGENT_SHOP_MAX_WORKERS", 8)
        max_queue_size = _int("AGENT_SHOP_MAX_QUEUE_SIZE", 64)
        task_timeout_seconds = _float("AGENT_SHOP_TASK_TIMEOUT_SECONDS", 8.0)
        max_calls_per_session = _int("AGENT_SHOP_MAX_CALLS_PER_SESSION", 64)
        max_duplicate_payloads = _int("AGENT_SHOP_MAX_DUPLICATE_CALLS_PER_SESSION", 3)
        max_queue_wait_seconds = _float("AGENT_SHOP_MAX_QUEUE_WAIT_SECONDS", 5.0)

        return cls(
            max_workers=max_workers,
            max_queue_size=max_queue_size,
            task_timeout_seconds=task_timeout_seconds,
            max_calls_per_session=max_calls_per_session,
            max_duplicate_payloads=max_duplicate_payloads,
            max_queue_wait_seconds=max_queue_wait_seconds,
        )

    @staticmethod
    def compute_payload_hash(operation: str, payload_key: Any) -> str:
        """
        Compute a stable hash for loop detection.

        `payload_key` should be a JSON-serializable object capturing the
        logical tool + arguments (e.g. operation + normalized payload).
        """
        try:
            serialized = repr(payload_key)
        except Exception:
            serialized = str(payload_key)
        raw = f"{operation}:{serialized}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def snapshot(self) -> Dict[str, Any]:
        """
        Return a shallow snapshot of queue/worker state for diagnostics.
        """
        async with self._lock:
            return {
                "max_workers": self._max_workers,
                "max_queue_size": self._max_queue_size,
                "task_timeout_seconds": self._task_timeout_seconds,
                "max_queue_wait_seconds": self._max_queue_wait_seconds,
                "running": len(self._running),
                "queued": len(self._queue),
                "metrics": dict(self._metrics),
            }

    async def enqueue(
        self,
        *,
        operation: str,
        session_id: Optional[str],
        payload_hash: str,
        coro_factory: TaskCoroutine,
        request_id: Optional[str] = None,
    ) -> Tuple[str, asyncio.Future]:
        """
        Enqueue a new task or start it immediately if capacity allows.

        Returns (task_id, future). The caller should await `future` to get
        the result or exception. If the queue is full, budgets exceeded,
        or single-flight is violated, this method raises HTTPException.
        """
        async with self._lock:
            # Idempotency: when a request_id is provided and we have an existing
            # task for it, return the same task_id and future.
            if request_id:
                existing_id = self._request_index.get(request_id)
                if existing_id:
                    existing = self._tasks.get(existing_id)
                    if existing is not None:
                        logger.info(
                            "agent_queue.idempotent_hit",
                            extra={
                                "operation": operation,
                                "session_id": session_id,
                                "request_id": request_id,
                                "task_id": existing.id,
                            },
                        )
                        return existing.id, existing.future
            # Global backpressure: bound total in-flight (running + queued).
            in_flight = len(self._running) + len(self._queue)
            if in_flight >= self._max_workers + self._max_queue_size:
                self._metrics["rejected"] += 1
                logger.warning(
                    "agent_queue.reject_full",
                    extra={
                        "operation": operation,
                        "session_id": session_id,
                        "running": len(self._running),
                        "queued": len(self._queue),
                        "max_workers": self._max_workers,
                        "max_queue_size": self._max_queue_size,
                    },
                )
                raise HTTPException(
                    status_code=429,
                    detail="SHOP_BACKEND_OVERLOADED",
                )

            if session_id:
                budget = self._session_budgets.get(session_id)
                if not budget:
                    budget = SessionBudget()
                    self._session_budgets[session_id] = budget

                budget.call_count += 1
                if (
                    self._max_calls_per_session > 0
                    and budget.call_count > self._max_calls_per_session
                ):
                    self._metrics["rejected"] += 1
                    logger.warning(
                        "agent_queue.reject_session_budget",
                        extra={
                            "operation": operation,
                            "session_id": session_id,
                            "call_count": budget.call_count,
                            "max_calls_per_session": self._max_calls_per_session,
                        },
                    )
                    raise HTTPException(
                        status_code=429,
                        detail="SESSION_BUDGET_EXCEEDED",
                    )

                # Loop detection: same tool + args repeatedly.
                if self._max_duplicate_payloads > 0:
                    budget.recent_hashes.append(payload_hash)
                    duplicate_count = sum(
                        1 for h in budget.recent_hashes if h == payload_hash
                    )
                    # Allow up to `max_duplicate_payloads` identical calls, and
                    # reject on the next one (i.e. "max + 1").
                    if duplicate_count > self._max_duplicate_payloads:
                        self._metrics["rejected"] += 1
                        logger.warning(
                            "agent_queue.reject_loop_detected",
                            extra={
                                "operation": operation,
                                "session_id": session_id,
                                "duplicates": duplicate_count,
                                "max_duplicate_payloads": self._max_duplicate_payloads,
                            },
                        )
                        raise HTTPException(
                            status_code=429,
                            detail="TOOL_LOOP_DETECTED",
                        )

                # Per-session single-flight: at most one in-flight task.
                if session_id in self._session_inflight:
                    self._metrics["rejected"] += 1
                    logger.info(
                        "agent_queue.reject_single_flight",
                        extra={
                            "operation": operation,
                            "session_id": session_id,
                        },
                    )
                    raise HTTPException(
                        status_code=409,
                        detail="SESSION_ALREADY_RUNNING",
                    )

            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            task_id = str(uuid.uuid4())
            record = TaskRecord(
                id=task_id,
                operation=operation,
                session_id=session_id,
                request_id=request_id,
                payload_hash=payload_hash,
                coro_factory=coro_factory,
                future=future,
            )
            self._tasks[task_id] = record
            self._metrics["enqueued"] += 1

            if request_id:
                self._request_index[request_id] = task_id

            if session_id:
                self._session_inflight[session_id] = task_id

            if len(self._running) < self._max_workers:
                self._start_task(record)
            else:
                self._queue.append(task_id)
                logger.info(
                    "agent_queue.enqueued",
                    extra={
                        "task_id": task_id,
                        "operation": operation,
                        "session_id": session_id,
                        "running": len(self._running),
                        "queued": len(self._queue),
                    },
                )

        return task_id, future

    def _start_task(self, record: TaskRecord) -> None:
        """
        Transition a task from QUEUED to RUNNING, enforcing max queue wait.

        NOTE: This method assumes the caller is holding self._lock.
        """
        now = time.time()
        record.started_at = now
        queue_wait = now - record.enqueued_at

        if self._max_queue_wait_seconds > 0 and queue_wait > self._max_queue_wait_seconds:
            record.state = TaskState.EXPIRED
            record.error = (
                f"Queue wait {queue_wait:.3f}s exceeded "
                f"{self._max_queue_wait_seconds:.3f}s"
            )
            record.finished_at = now
            self._metrics["expired"] += 1
            if record.session_id:
                current = self._session_inflight.get(record.session_id)
                if current == record.id:
                    self._session_inflight.pop(record.session_id, None)
            if not record.future.done():
                record.future.set_exception(
                    HTTPException(status_code=503, detail="QUEUE_TIMEOUT")
                )
            logger.warning(
                "agent_queue.queue_timeout",
                extra={
                    "task_id": record.id,
                    "operation": record.operation,
                    "session_id": record.session_id,
                    "queue_wait": queue_wait,
                    "max_queue_wait_seconds": self._max_queue_wait_seconds,
                },
            )
            # Attempt to start another queued task if capacity is available.
            self._promote_next_locked()
            return

        record.state = TaskState.RUNNING
        self._running[record.id] = record
        self._metrics["started"] += 1
        logger.info(
            "agent_queue.start",
            extra={
                "task_id": record.id,
                "operation": record.operation,
                "session_id": record.session_id,
                "running": len(self._running),
                "queued": len(self._queue),
            },
        )
        record.runner = asyncio.create_task(self._run_task(record))

    def _promote_next_locked(self) -> None:
        """
        Start the next queued task if there is worker capacity.

        NOTE: Caller must hold self._lock.
        """
        while self._queue and len(self._running) < self._max_workers:
            next_id = self._queue.popleft()
            next_record = self._tasks.get(next_id)
            if not next_record or next_record.state != TaskState.QUEUED:
                continue
            self._start_task(next_record)
            break

    async def _run_task(self, record: TaskRecord) -> None:
        try:
            result = await asyncio.wait_for(
                record.coro_factory(),
                timeout=self._task_timeout_seconds,
            )
            record.result = result
            record.state = TaskState.SUCCEEDED
        except HTTPException as exc:
            record.error = str(exc.detail)
            record.state = TaskState.FAILED
            if not record.future.done():
                record.future.set_exception(exc)
        except asyncio.TimeoutError:
            record.error = f"Task exceeded {self._task_timeout_seconds}s"
            record.state = TaskState.TIMEOUT
            self._metrics["timed_out"] += 1
            if not record.future.done():
                record.future.set_exception(
                    HTTPException(
                        status_code=504,
                        detail="UPSTREAM_TIMEOUT",
                    )
                )
        except asyncio.CancelledError:
            record.state = TaskState.CANCELLED
            self._metrics["cancelled"] += 1
            if not record.future.done():
                record.future.set_exception(asyncio.CancelledError())
            raise
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            record.state = TaskState.FAILED
            self._metrics["failed"] += 1
            logger.error(
                "agent_queue.task_error",
                extra={
                    "task_id": record.id,
                    "operation": record.operation,
                    "session_id": record.session_id,
                    "error": record.error,
                },
            )
            if not record.future.done():
                record.future.set_exception(
                    HTTPException(
                        status_code=500,
                        detail="SHOP_BACKEND_ERROR",
                    )
                )
        else:
            self._metrics["completed"] += 1
            if not record.future.done():
                record.future.set_result(record.result)
        finally:
            record.finished_at = time.time()
            async with self._lock:
                # Clear running and in-flight session markers.
                self._running.pop(record.id, None)
                if record.session_id:
                    current = self._session_inflight.get(record.session_id)
                    if current == record.id:
                        self._session_inflight.pop(record.session_id, None)

                # Start next queued task if capacity allows.
                self._promote_next_locked()

    async def cancel(self, task_id: str, reason: str = "client_disconnect") -> None:
        """
        Best-effort cancellation of a queued or running task.

        - For queued tasks we drop them from the queue and mark CANCELLED.
        - For running tasks we signal cancellation; handlers should be
          written to respond reasonably to asyncio.CancelledError.
        """
        async with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return

            if record.state == TaskState.QUEUED:
                try:
                    self._queue.remove(task_id)
                except ValueError:
                    # Already dequeued; treat as best-effort.
                    pass
                record.state = TaskState.CANCELLED
                record.error = reason
                record.finished_at = time.time()
                self._metrics["cancelled"] += 1
                if record.session_id:
                    current = self._session_inflight.get(record.session_id)
                    if current == record.id:
                        self._session_inflight.pop(record.session_id, None)
                if not record.future.done():
                    record.future.set_exception(asyncio.CancelledError())
                logger.info(
                    "agent_queue.cancel_queued",
                    extra={
                        "task_id": record.id,
                        "operation": record.operation,
                        "session_id": record.session_id,
                        "reason": reason,
                    },
                )
                return

            if record.state == TaskState.RUNNING and record.runner:
                record.error = reason
                record.runner.cancel()
                logger.info(
                    "agent_queue.cancel_running",
                    extra={
                        "task_id": record.id,
                        "operation": record.operation,
                        "session_id": record.session_id,
                        "reason": reason,
                    },
                )

    async def get_task_state(self, task_id: str) -> Optional[TaskState]:
        """
        Helper for tests and diagnostics to inspect a task state.
        """
        async with self._lock:
            record = self._tasks.get(task_id)
            return record.state if record else None
