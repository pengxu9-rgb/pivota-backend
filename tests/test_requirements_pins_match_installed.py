"""Every exact (`==`) pin in requirements.txt must match what is installed.

Issue #1759, follow-up 2. The local quality-gate venv was running
`databases 0.9.0` + `SQLAlchemy 2.0.49` while requirements.txt — and therefore
CI and production `web` — pinned `databases==0.7.0` + `SQLAlchemy==1.4.52`.

That is not a cosmetic drift. `databases` 0.7.0 parks its `Connection` in a
ContextVar that child tasks INHERIT; 0.8+ keys the connection map per task. The
inherited-Connection behavior is the entire root cause of #1754 (every
APScheduler job on prod wedged behind one shared asyncpg connection), and two of
the tests in tests/test_scheduler_job_isolation.py can only fail on 0.7.0:

  * test_wrapped_jobs_do_not_share_the_startup_databases_connection
  * test_one_wedged_job_does_not_starve_other_jobs

On 0.9.0 they pass no matter what the code under test does, because the library
already isolates per task. So a venv one minor version ahead of the pin silently
disarms the regression net for the defect class it was written to catch — while
still reporting green.

An exact `==` pin exists precisely because the version's behavior matters. This
test makes any disagreement loud instead of silent. It does not carry an
exemption list: if a pin should be allowed to float, loosen it in
requirements.txt rather than excusing it here.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"

# name==version, tolerating extras (`uvicorn[standard]==1.2`) and trailing
# comments. Anything that is not an exact pin (`<`, `>=`, unpinned) is skipped —
# those are deliberately allowed to float. The name must START alphanumeric so a
# pip option line (`--only-binary==:all:`) is not parsed as a package.
_EXACT_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")


def _normalize(name: str) -> str:
    """PEP 503 name normalization. `SQLAlchemy`, `sqlalchemy` and `python_pptx`
    are the same distribution; keying on the raw requirements.txt spelling would
    make a cosmetic rename (e.g. what `pip-compile` emits) look like a dropped
    pin."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _exact_pins() -> dict[str, str]:
    """{normalized name: pinned version}. `importlib.metadata.version()` accepts
    any spelling, so normalizing here loses nothing."""
    pins: dict[str, str] = {}
    for line in _REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        m = _EXACT_PIN.match(line)
        if m:
            pins[_normalize(m.group(1))] = m.group(2)
    return pins


def test_requirements_txt_declares_the_pins_this_guard_exists_for():
    """Guard the guard: if the two pins that carry the #1754 semantics ever stop
    being exact pins, this file's premise is gone and someone must revisit it —
    rather than the check quietly narrowing to nothing."""
    pins = _exact_pins()
    for name in ("databases", "sqlalchemy"):
        assert name in pins, (
            f"{name} is no longer an exact `==` pin in requirements.txt. "
            "tests/test_scheduler_job_isolation.py depends on the pinned "
            "connection semantics (issue #1754/#1759) — re-pin it, or delete "
            "this guard deliberately."
        )


def test_installed_versions_match_every_exact_pin():
    pins = _exact_pins()
    assert pins, f"parsed no exact pins from {_REQUIREMENTS} — the parser is broken"

    mismatched: list[str] = []
    missing: list[str] = []
    for name, pinned in sorted(pins.items()):
        try:
            installed = version(name)
        except PackageNotFoundError:
            missing.append(f"  {name}: pinned {pinned}, NOT INSTALLED")
            continue
        if installed != pinned:
            mismatched.append(f"  {name}: pinned {pinned}, installed {installed}")

    if mismatched or missing:
        raise AssertionError(
            "This environment disagrees with requirements.txt, so it is not "
            "testing what CI and production run:\n"
            + "\n".join(mismatched + missing)
            + "\n\nRebuild it:\n"
            "  python -m pip install -r requirements.txt -r requirements-dev.txt\n\n"
            "Why this is fatal rather than cosmetic: see the module docstring — "
            "a `databases` version ahead of the pin makes the #1754 "
            "shared-Connection defect class invisible locally while the suite "
            "still reports green."
        )
