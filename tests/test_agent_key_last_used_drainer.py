"""The key last-used drainer survives what it should, stops when it should, and says so.

Round-2 review of be#2890 (reports/portal_fixes_review_2026_11_22.md): an internal CancelledError
ended the drainer permanently and silently, the drop counter was never logged, the shutdown await
was unbounded, and nothing tested that app_lifespan starts it (deleting the call survived).
"""

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime

import pytest

LOGGER = "db.agents.key_last_used"


@pytest.fixture
async def agents_db(monkeypatch):
    import db.agents as module

    monkeypatch.setattr(module, "IS_POSTGRES", False)  # plain execute path; the PG path is in *_postgres.py
    monkeypatch.setattr(module, "_KEY_LAST_USED_MEMO", OrderedDict())
    monkeypatch.setattr(module, "_key_last_used_queue", None)
    monkeypatch.setattr(module, "_key_last_used_task", None)
    monkeypatch.setattr(module, "_key_last_used_restarts", [])
    monkeypatch.setattr(module, "_key_last_used_dropped", 0)
    monkeypatch.setattr(module, "_key_last_used_drop_logged_at", None)
    monkeypatch.setattr(module, "is_synthetic_call", lambda: False)
    yield module
    # Inside the loop (an async fixture), so no drainer outlives the test's event loop.
    await module.shutdown_key_last_used_drainer(timeout=2.0)


def _item(name):
    return ("api_keys", name, datetime.utcnow())


async def _until(predicate, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_cancelled_error_from_inside_the_write_is_one_skipped_stamp(monkeypatch, agents_db, caplog):
    written = []

    async def execute(query, values=None):
        if values["key_hash"] == "boom":
            raise asyncio.CancelledError()  # e.g. a driver cancelling its own work
        written.append(values["key_hash"])

    monkeypatch.setattr(agents_db.database, "execute", execute)
    caplog.set_level(logging.WARNING, logger=LOGGER)
    task = agents_db.start_key_last_used_drainer()
    queue = agents_db._key_last_used_queue
    for name in ("boom", "after"):
        queue.put_nowait(_item(name))
    await asyncio.wait_for(queue.join(), timeout=3)

    assert written == ["after"], "the drainer went on after the internal cancel"
    assert not task.done() and agents_db._key_last_used_task is task, "same task, not ended"
    assert "cancelled inside the write" in caplog.text


@pytest.mark.asyncio
async def test_a_drainer_that_ends_without_a_shutdown_is_restarted_and_logged(monkeypatch, agents_db, caplog):
    calls = {"n": 0}
    real = agents_db._drain_key_last_used

    async def flaky(queue):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("drainer bug")
        await real(queue)

    written = []

    async def execute(query, values=None):
        written.append(values["key_hash"])

    monkeypatch.setattr(agents_db, "_drain_key_last_used", flaky)
    monkeypatch.setattr(agents_db.database, "execute", execute)
    caplog.set_level(logging.ERROR, logger=LOGGER)
    first = agents_db.start_key_last_used_drainer()
    await _until(lambda: calls["n"] == 2)

    assert first.done() and agents_db._key_last_used_task is not first
    assert "ended unexpectedly (RuntimeError('drainer bug')); restarting" in caplog.text
    agents_db._key_last_used_queue.put_nowait(_item("after-restart"))
    await asyncio.wait_for(agents_db._key_last_used_queue.join(), timeout=3)
    assert written == ["after-restart"], "the restarted drainer works"


@pytest.mark.asyncio
async def test_restarts_are_capped_then_stamping_stops_loudly(monkeypatch, agents_db, caplog):
    async def always_broken(queue):
        raise RuntimeError("drainer bug")

    monkeypatch.setattr(agents_db, "_drain_key_last_used", always_broken)
    caplog.set_level(logging.ERROR, logger=LOGGER)
    agents_db.start_key_last_used_drainer()
    await _until(lambda: agents_db._key_last_used_queue is None)

    assert len(agents_db._key_last_used_restarts) == agents_db._KEY_LAST_USED_MAX_RESTARTS
    assert "stamping stopped" in caplog.text
    # With no queue, the auth path queues nothing (and never raises).
    agents_db._enqueue_key_last_used(source="api_keys", api_key="k", key_hash_sha256="h", agent={"is_active": True})


@pytest.mark.asyncio
async def test_stop_then_cancel_is_a_shutdown_not_a_crash(agents_db, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    task = agents_db.start_key_last_used_drainer()
    agents_db.stop_key_last_used_drainer()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)

    assert agents_db._key_last_used_task is task, "not restarted"
    assert "restarting" not in caplog.text


@pytest.mark.asyncio
async def test_drops_are_logged_with_the_total_at_most_once_per_window(monkeypatch, agents_db, caplog):
    monkeypatch.setattr(agents_db, "_key_last_used_queue", asyncio.Queue(maxsize=1))
    agents_db._key_last_used_queue.put_nowait(_item("full"))
    caplog.set_level(logging.WARNING, logger=LOGGER)
    for i in range(3):
        agents_db._enqueue_key_last_used(source="api_keys", api_key="k", key_hash_sha256=f"h{i}", agent={"is_active": True})

    assert agents_db._key_last_used_dropped == 3
    drops = [r.getMessage() for r in caplog.records if "queue full" in r.getMessage()]
    assert drops == ["agent key last-used queue full: 1 stamps dropped since start (queue max 2000)"]
    monkeypatch.setattr(agents_db, "_key_last_used_drop_logged_at", agents_db._key_last_used_drop_logged_at - 61)
    agents_db._enqueue_key_last_used(source="api_keys", api_key="k", key_hash_sha256="h9", agent={"is_active": True})
    assert [r.getMessage() for r in caplog.records if "queue full" in r.getMessage()][-1].startswith(
        "agent key last-used queue full: 4 stamps dropped"
    )


@pytest.mark.asyncio
async def test_shutdown_waits_at_most_its_timeout(monkeypatch, agents_db):
    async def stubborn(queue):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(1.0)  # ignores the cancel for a while, as a stuck write could

    monkeypatch.setattr(agents_db, "_drain_key_last_used", stubborn)
    agents_db.start_key_last_used_drainer()
    await asyncio.sleep(0)
    started = asyncio.get_running_loop().time()

    await agents_db.shutdown_key_last_used_drainer(timeout=0.2)

    assert asyncio.get_running_loop().time() - started < 1.0
    assert agents_db._key_last_used_queue is None


@pytest.mark.asyncio
async def test_the_app_lifespan_starts_the_drainer_and_shuts_it_down(monkeypatch, agents_db):
    import main
    import routes.agent_shop_gateway as gateway

    async def noop():
        return None

    async def idle():
        await asyncio.sleep(3600)

    for name in ("startup_event", "startup", "shutdown_event", "shutdown"):
        monkeypatch.setattr(main, name, noop)
    monkeypatch.setattr(main, "run_database_reconnect_supervisor", idle)
    monkeypatch.setattr(main, "require_platform_env", lambda: "development")
    monkeypatch.setattr(gateway, "start_group_shadow_arming", lambda: None)

    async with main.app_lifespan(main.app):
        task = agents_db._key_last_used_task
        assert agents_db._key_last_used_queue is not None, "the lifespan started the drainer"
        assert task is not None and not task.done()

    assert agents_db._key_last_used_queue is None
    await asyncio.sleep(0)
    assert task.done(), "the lifespan cancelled it on the way out"


@pytest.mark.asyncio
async def test_a_shutdown_in_the_middle_of_a_write_ends_the_drainer_without_a_restart(monkeypatch, agents_db, caplog):
    # The reviewer's probe, as a test: the cancel lands while a write is awaiting the DB. That is a
    # real cancel of the task (cancelling() is set), so it must end the drainer, release the item,
    # and not be "restarted" as if it had crashed.
    entered = asyncio.Event()
    finished = []

    async def slow_execute(query, values=None):
        entered.set()
        await asyncio.sleep(3600)  # a write in flight
        finished.append(values["key_hash"])

    monkeypatch.setattr(agents_db.database, "execute", slow_execute)
    caplog.set_level(logging.WARNING, logger=LOGGER)
    task = agents_db.start_key_last_used_drainer()
    queue = agents_db._key_last_used_queue
    queue.put_nowait(_item("mid-write"))
    await asyncio.wait_for(entered.wait(), timeout=3)
    started = asyncio.get_running_loop().time()

    await agents_db.shutdown_key_last_used_drainer(timeout=2.0)

    assert asyncio.get_running_loop().time() - started < 1.0, "the cancel ended the write promptly"
    assert task.done() and task.cancelled(), "a real cancel ends the task as cancelled"
    assert agents_db._key_last_used_task is task, "not restarted"
    assert finished == [], "the write did not complete after the shutdown"
    await asyncio.wait_for(queue.join(), timeout=1)  # task_done ran for the in-flight item
    assert "restarting" not in caplog.text and "skipped" not in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_a_superseded_drainer_ending_does_not_start_another(monkeypatch, agents_db, caplog):
    # Only the CURRENT drainer is restarted. An older task that ends (e.g. one already replaced by a
    # restart) must not spawn a second drainer next to the live one.
    caplog.set_level(logging.ERROR, logger=LOGGER)
    old = agents_db.start_key_last_used_drainer()
    current = agents_db._spawn_key_last_used_drainer(agents_db._key_last_used_queue)  # what a restart does
    assert agents_db._key_last_used_task is current and current is not old

    old.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old
    await asyncio.sleep(0.05)

    assert agents_db._key_last_used_task is current, "the live drainer is unchanged"
    assert agents_db._key_last_used_restarts == [], "no restart was counted"
    assert not current.done()
    live = [t for t in asyncio.all_tasks() if t.get_name() == "agent-key-last-used" and not t.done()]
    assert live == [current], "exactly one drainer is running"
    assert "restarting" not in caplog.text
