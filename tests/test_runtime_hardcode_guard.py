from scripts.audit_runtime_hardcodes import collect_violations
from utils.runtime_safety import require_runtime_gate


def test_runtime_hardcode_audit_passes() -> None:
    assert collect_violations() == []


def test_runtime_gate_fails_closed_in_production(monkeypatch) -> None:
    from fastapi import HTTPException

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENABLE_DIRECT_DB_CHECK", raising=False)

    try:
        require_runtime_gate("ENABLE_DIRECT_DB_CHECK")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("expected production runtime gate to fail closed")


def test_runtime_gate_allows_explicit_flag(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENABLE_DIRECT_DB_CHECK", "true")

    require_runtime_gate("ENABLE_DIRECT_DB_CHECK")
