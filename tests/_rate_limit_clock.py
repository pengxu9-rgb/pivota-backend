"""Pin the rate limiter's clock to the middle of one window, for tests that count requests.

`middleware/rate_limiter.py` counts in FIXED windows keyed on the wall clock
(`bucket = int(time.time() // window_seconds)`). A test that sends a burst and
asserts an exact 200/429 sequence therefore fails whenever the burst happens to
straddle a window boundary: the counter starts again at zero and the last
requests in the burst come back 200. On main 66fe3eb89 (2026-10-09) that turned
`test_rejected_requests_do_not_refill_the_window[memory]` red as
`[429, 429, ... 200, 200, ...]`, failed the push sweep, and stopped the deploy.

Only the limiter's own `time` binding is replaced, never the global `time`
module, so TestClient, anyio and everything else keep the real clock.
"""
from __future__ import annotations

import time as _real_time
import types

import middleware.rate_limiter as _rate_limiter


def freeze_rate_limiter_clock(monkeypatch) -> float:
    """Freeze `middleware.rate_limiter`'s clock half a window into the current window; return it."""
    window = float(_rate_limiter.settings.rate_limit_window_seconds)
    now = _real_time.time()
    frozen = (now // window) * window + window / 2
    monkeypatch.setattr(_rate_limiter, "time", types.SimpleNamespace(time=lambda: frozen))
    return frozen
