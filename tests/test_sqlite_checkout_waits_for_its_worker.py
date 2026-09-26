"""A sqlite checkout cancelled mid-connect must let aiosqlite's worker finish.

WHAT THIS PREVENTS (2026-09-23). The sweep job hung after printing its summary
until the 25-minute timeout, with 10-17 aiosqlite worker threads per run
logged dying on "Event loop is closed". A bare TestClient request runs on its
own loop, and `asyncio.run` cancels whatever the request left behind (the
usage logger's `create_task` insert, the decision-event flush worker) before
closing it. aiosqlite answers a cancelled connect by queueing its worker's stop
WITHOUT awaiting it, so the loop closed first and the worker's result post
raised. `db.database` now makes the checkout wait for the worker.

Deterministic: the sqlite3 connect is held open until after `asyncio.run`
would have closed the loop without that wait.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

from databases import Database

import db.database  # noqa: F401  installs the patch under test


def _workers() -> set:
    return {t for t in threading.enumerate() if "_connection_worker_thread" in t.name}


def test_a_checkout_cancelled_mid_connect_lets_its_worker_finish(monkeypatch, tmp_path):
    gate = threading.Event()
    real_connect = sqlite3.connect

    def held_connect(*args, **kwargs):
        gate.wait(5)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", held_connect)
    died: list = []
    monkeypatch.setattr(threading, "excepthook", died.append)

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'checkout.db'}")
    before = _workers()
    started: set = set()

    async def request() -> None:
        await database.connect()
        # Left pending on purpose, like a fire-and-forget insert.
        asyncio.get_running_loop().create_task(database.fetch_val("SELECT 1"))
        while not (_workers() - before):
            await asyncio.sleep(0)
        started.update(_workers() - before)
        # The connect answers only after asyncio.run has cancelled the task.
        threading.Timer(0.2, gate.set).start()

    asyncio.run(request())

    assert len(started) == 1
    worker = started.pop()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not died, f"aiosqlite worker died: {died[0].exc_value!r}"
