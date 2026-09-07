import pytest

from scripts.audit_runtime_hardcodes import (
    DENYLIST_FILES,
    DENYLIST_MARKER,
    collect_violations,
    line_is_violation,
)
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


_RIG = "merch_" + "efbc46b4619cfbdf"  # a rig merchant id the guard forbids (by content, not index)
_DENYLIST_FILE = "services/test_merchant_policy.py"


@pytest.mark.parametrize(
    "line, violates, why",
    [
        (f'DEFAULT_MERCHANT = "{_RIG}"', True, "a bare default is the leak this guard exists for"),
        (f'x = "{_RIG}"  # explained', True, "an inline comment does not launder a default"),
        (f"# the 763 rows under {_RIG} are held out by the indexable bit", False,
         "prose in a full-line comment cannot reach runtime"),
        (f'    "{_RIG}",  # {DENYLIST_MARKER}', False, "a denylist entry says why it names the id"),
        (f'    "{_RIG}",  # runtime-hardcode-guard: allowed', True,
         "the marker must name the REASON (denylist), not merely opt out"),
        (f'    "{_RIG}"  {DENYLIST_MARKER}', True, "the marker must sit in a trailing comment"),
        (f'{DENYLIST_MARKER} = "{_RIG}"', True, "the marker in code, not in a comment, is a default"),
        ("MERCHANT = 'merch_1234567890abcdef'", False, "an id the guard does not forbid"),
    ],
)
def test_the_guard_classifies_a_line_by_how_the_id_can_reach_runtime(line, violates, why):
    assert line_is_violation(line, _DENYLIST_FILE) is violates, why


def test_the_denylist_marker_is_honoured_only_in_the_denylist_file():
    """An unscoped marker is a self-service opt-out: a real default in services/ followed by
    the marker passed the audit in review. The marker exempts a line only in DENYLIST_FILES."""
    marked = f'FALLBACK_MERCHANT = "{_RIG}"  # {DENYLIST_MARKER}'
    assert _DENYLIST_FILE in DENYLIST_FILES
    assert line_is_violation(marked, _DENYLIST_FILE) is False
    for rel in ("services/pdp_renderability.py", "routes/employee_products.py", "", "scripts/x.py"):
        assert line_is_violation(marked, rel) is True, rel


def test_the_three_july_findings_are_no_longer_violations_for_the_right_reasons():
    """The guard was red from 2026-07-25 (#1579/#1584) until now over exactly these three
    lines, and the sweep quarantined it. Two are full-line comments explaining a rig's blast
    radius; one is the denylist that EXCLUDES the rig from serving. None is a runtime default."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    policy = (root / "services" / "test_merchant_policy.py").read_text(encoding="utf-8")
    assert f'"{_RIG}",  # {DENYLIST_MARKER}' in policy
    for rel in ("services/pdp_renderability.py", "scripts/step5_working_set.py"):
        lines = [l for l in (root / rel).read_text(encoding="utf-8").splitlines() if _RIG in l]
        assert lines, f"{rel} no longer mentions the rig; update this test"
        assert all(l.strip().startswith("#") for l in lines), (rel, lines)
