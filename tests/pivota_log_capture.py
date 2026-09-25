"""Capture what the "pivota" logger's stdout handler WRITES, under production's logging state.

`caplog` cannot prove a line reaches prod. It hangs a handler on the ROOT logger, so a module
logger's INFO shows up in `caplog.records` in a test and is dropped at the logger in prod, where
nothing configures root and it sits at Python's default WARNING (measured 2026-09-23: the sweep
and the Reap poller each had ok runs on /__scheduler_health and zero report lines in Cloud
Logging). The one channel that does land is `utils.logger`'s "pivota" logger: own INFO level, own
stdout handler, propagate=False. `test_market_telemetry.py` models the same state for its emitter.

So a test that wants to say "this line lands in prod" must (a) put root where prod has it and
(b) read the pivota handler's OWN stream — not root's records. This module does both, and it
swaps the stream on the real handler rather than adding a second one, so what the test reads is
byte-for-byte what the handler would have written to stdout, format included.
"""

from __future__ import annotations

import io
import logging
from contextlib import contextmanager
from typing import Iterator, List

import utils.logger as pivota



def pivota_stdout_handlers() -> List[logging.StreamHandler]:
    return [h for h in pivota.logger.handlers if getattr(h, "_pivota_stdout_handler", False)]


@contextmanager
def root_as_in_prod() -> Iterator[logging.Logger]:
    """Root at WARNING with NO handlers — production's state — restored on exit.

    Under pytest root carries the capture plugin's handlers and, after a `caplog.set_level`,
    whatever level a previous test left; both would let a module-logger INFO through and make a
    prod-dropped line look emitted. The level is also asserted AFTER the body runs, so a job that
    reconfigured root to get its line out fails here rather than flooding prod with INFO.
    """
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    root.setLevel(logging.WARNING)
    for handler in saved_handlers:
        root.removeHandler(handler)
    try:
        yield root
        assert root.level == logging.WARNING, "the job must not reconfigure the root logger"
        assert not root.handlers, "the job must not add a handler to the root logger"
    finally:
        root.setLevel(saved_level)
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)


@contextmanager
def capture_pivota_stdout() -> Iterator[io.StringIO]:
    """The pivota stdout handler's output for the block, as the handler formats it.

    The handler's stream is swapped, not replaced with a second handler: a second handler with
    its own formatter would pass on a line the real one dropped (a level set on the handler, a
    filter) and would not test the format the runbook quotes.
    """
    handlers = pivota_stdout_handlers()
    assert len(handlers) == 1, f"expected exactly one pivota stdout handler, found {len(handlers)}"
    handler = handlers[0]
    buffer = io.StringIO()
    previous = handler.setStream(buffer)
    try:
        yield buffer
    finally:
        handler.setStream(previous)


def pivota_lines(buffer: io.StringIO) -> List[str]:
    return [line for line in buffer.getvalue().splitlines() if line]
