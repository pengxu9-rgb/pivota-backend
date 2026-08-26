"""Unit tests for :func:`config.platform.pytest_bypass_allowed`.

This is the shared helper extracted while closing two more instances of the
bug PR #1893 fixed in ``utils/auth.py``: a `PYTEST_CURRENT_TEST`-gated
shortcut with no `is_production()` check, in `routes/agent_auth.py` and
`routes/agent_briefs.py`. Centralizing the check here means a new call site
can't forget the production conjunct the way these two did.
"""
from __future__ import annotations

import pytest

from config import platform as P
from tests.test_platform_shim import _ALL_KEYS


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    P.reset_platform_state()
    yield
    P.reset_platform_state()


def test_false_when_pytest_current_test_is_unset():
    # PYTEST_CURRENT_TEST is genuinely set in os.environ for the duration of
    # this test run (pytest sets it itself), so this passes an explicit empty
    # env mapping rather than trying to unset the real one.
    assert P.pytest_bypass_allowed(env={}) is False


def test_true_when_pytest_current_test_is_set_outside_production(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y (call)")
    assert P.pytest_bypass_allowed() is True


@pytest.mark.parametrize("prod_var", ["PIVOTA_ENV", "RAILWAY_ENVIRONMENT", "K_SERVICE"])
def test_refuses_in_production_even_with_pytest_current_test_set(monkeypatch, prod_var):
    """Mutant check: PYTEST_CURRENT_TEST alone must not be enough."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y (call)")
    monkeypatch.setenv(
        prod_var, "pivota-backend-prod" if prod_var == "K_SERVICE" else "production"
    )
    assert P.pytest_bypass_allowed() is False


def test_logs_a_warning_when_refused_in_production(monkeypatch, caplog):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y (call)")
    monkeypatch.setenv("PIVOTA_ENV", "production")
    with caplog.at_level("WARNING"):
        assert P.pytest_bypass_allowed(bypass_name="the widget bypass") is False
    assert any("the widget bypass" in record.message for record in caplog.records)
