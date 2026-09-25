"""The per-IP anonymous invoke budget must not leak between tests.

`_INVOKE_ANON_IP_LIMIT_STORE` is a module-level dict keyed by IP and calendar
minute, and every anonymous test shares the key "testclient". Without the
autouse reset in tests/conftest.py, one file's spend starves another's — a
failure that depends on suite order AND wall-clock timing, so it reproduces
intermittently in CI and not locally.

These two tests are ORDER-DEPENDENT BY DESIGN: the first spends the budget, the
second asserts it starts clean. Together they fail if the reset is removed.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)


def _store():
    import routes.agent_shop_gateway as gateway

    return gateway._INVOKE_ANON_IP_LIMIT_STORE


def _spend(n: int) -> None:
    import routes.agent_shop_gateway as gateway

    for _ in range(n):
        gateway._check_invoke_anon_rate_limit("testclient")


def test_a_first_spends_the_entire_anonymous_budget() -> None:
    """Exhaust the budget, exactly as a burst of anonymous calls does."""
    import routes.agent_shop_gateway as gateway

    _spend(60)
    # The 61st is refused: the limiter is armed and the budget is gone.
    assert gateway._check_invoke_anon_rate_limit("testclient") is False
    assert _store()["testclient"][1] >= 60


def test_b_next_test_starts_with_a_clean_budget() -> None:
    """Runs immediately after the test above, which left the budget exhausted.

    Without the autouse reset this fails on the FIRST call — which is precisely
    how an unrelated file's spend used to make a burst test 429."""
    import routes.agent_shop_gateway as gateway

    assert _store().get("testclient") is None, (
        "the anonymous invoke budget leaked from the previous test; "
        "the autouse reset in tests/conftest.py is not running"
    )
    assert gateway._check_invoke_anon_rate_limit("testclient") is True


def test_the_limiter_is_reset_not_disabled() -> None:
    """The reset must not neuter enforcement — tests that assert a 429 depend on
    the limiter still refusing once the budget is spent WITHIN their own test."""
    import routes.agent_shop_gateway as gateway

    assert gateway._invoke_anon_rpm() == 60, "suite-wide reset must not zero the rpm"
    _spend(60)
    assert gateway._check_invoke_anon_rate_limit("testclient") is False
