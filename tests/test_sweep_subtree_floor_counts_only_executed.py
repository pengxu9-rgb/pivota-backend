"""The sweep's subtree minimums must count tests that RAN, not tests that were collected.

The global floor already subtracts skips (`ran = total - skipped`). The per-subtree loop
did not, so an entire subtree could go inert — every case collected, none executed — and
still clear its minimum.

The global floor is not a backstop for that. `readiness.tests` is 133 cases against ~578 of
global headroom, so the whole tree can stop executing with BOTH gates green. (It was 79
against ~245 when this was written; un-quarantining readiness/tests/test_routes.py grew
both numbers and the conclusion holds a fortiori.)
(`tests.services` is incidentally covered only because 1,682 skips would exceed the global
headroom — arithmetic luck, not a guarantee, and it evaporates when the floor next rises.)

NOTE: pytest files an **xfail** under `<skipped type="pytest.xfail">`, so this filter
excludes xfails as well as skips. That is deliberate and consistent — the global floor's
`total - skipped` already counted them the same way, because pytest's own
`testsuite@skipped` attribute includes xfail. An ERROR (setup failure) is `<error>` and is
still counted, which is correct: an errored test makes pytest exit non-zero and the sweep
step runs under `pipefail`, so the job is already red — an error can never produce the
green-with-inert-subtree shape this gate exists to catch.

A MODULE-level skip was always caught: it collapses collection to one <testcase> per
module. The gap is a per-test `@pytest.mark.skipif` sweep, which keeps the case count
identical and the execution count zero — exactly what an environment-dependent guard
degrades into. `tests/test_vertex_gemini_credentials.py` was one ambient-ADC check away
from that shape until #1911.

These tests EXTRACT AND RUN the workflow's own assert script rather than restating it, so
they exercise the shipping code. A restated copy would keep passing while the workflow
regressed.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SWEEP = REPO / ".github" / "workflows" / "backend-test-sweep.yml"


def _assert_script() -> str:
    """Pull the inline `python - <<'PY' ... PY` assert script out of the workflow."""
    wf = yaml.safe_load(SWEEP.read_text(encoding="utf-8"))
    runs = [
        s["run"]
        for job in wf["jobs"].values()
        for s in job.get("steps", [])
        if isinstance(s, dict) and isinstance(s.get("run"), str) and "SUBTREES" in s["run"]
    ]
    assert len(runs) == 1, f"expected exactly one step defining SUBTREES, found {len(runs)}"
    m = re.search(r"<<'PY'\n(.*?)\n\s*PY\b", runs[0], re.S)
    assert m, "could not locate the heredoc'd assert script inside the sweep step"
    return textwrap.dedent(m.group(1))


def _run(tmp_path: Path, xml: str):
    (tmp_path / "sweep.xml").write_text(xml)
    return subprocess.run(
        ["python3", "-c", _assert_script()],
        cwd=tmp_path, capture_output=True, text=True,
    )


def _xml(*, services: int, readiness: int, skip_readiness: bool = False,
         skip_services: bool = False, other: int = 11000,
         readiness_skipped: int = 0) -> str:
    """readiness_skipped: mark exactly N of the readiness cases skipped (partial-skip case)."""
    cases = []
    for i in range(services):
        body = "<skipped/>" if skip_services else ""
        cases.append(f'<testcase classname="tests.services.test_x" name="t{i}">{body}</testcase>')
    for i in range(readiness):
        marked = skip_readiness or i < readiness_skipped
        body = "<skipped/>" if marked else ""
        cases.append(f'<testcase classname="readiness.tests.test_y" name="t{i}">{body}</testcase>')
    for i in range(other):
        cases.append(f'<testcase classname="tests.test_other" name="t{i}"/>')
    total = services + readiness + other
    skipped = (readiness if skip_readiness else readiness_skipped) + (services if skip_services else 0)
    # Faithful to what pytest --junitxml actually writes: a <testsuites> wrapper around a
    # single <testsuite>, carrying errors/failures/name. An earlier version of this helper
    # emitted a BARE <testsuite> root, which never exercised the workflow's
    # `root if root.tag == 'testsuite' else root.find('testsuite')` line — so replacing it
    # with `suite = root` survived every case here. Verified against real output:
    #   <testsuites name="pytest tests"><testsuite name="pytest" errors="0" ... >
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="0" failures="0" skipped="{skipped}" tests="{total}" time="1.0">'
        + "".join(cases)
        + "</testsuite></testsuites>"
    )


def test_a_fully_skipped_subtree_is_rejected(tmp_path):
    """THE hole. Every readiness case collected, none executed — previously exit 0."""
    proc = _run(tmp_path, _xml(services=1682, readiness=133, skip_readiness=True))
    assert proc.returncode != 0, (
        "a subtree whose every test is SKIPPED cleared its minimum:\n" + proc.stdout
    )
    assert "readiness.tests" in (proc.stdout + proc.stderr)


def test_a_healthy_run_still_passes(tmp_path):
    """Guard the guard: the fix must not red a genuinely green sweep."""
    proc = _run(tmp_path, _xml(services=1682, readiness=133))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "readiness.tests: 133" in proc.stdout


def test_partial_skips_are_subtracted(tmp_path):
    """120 is the minimum; 133 collected with 14 skipped is 119 executed — below it."""
    # Built directly rather than by string-patching a hardcoded total, which silently
    # no-ops if the `other=` default ever changes.
    proc = _run(tmp_path, _xml(services=1682, readiness=133, skip_readiness=False,
                               readiness_skipped=14))
    assert proc.returncode != 0, "119 executed should not clear a minimum of 120:\n" + proc.stdout


def test_the_global_floor_still_bites(tmp_path):
    """Unchanged behaviour — the fix must not disturb the global gate."""
    proc = _run(tmp_path, _xml(services=1682, readiness=133, other=100))
    assert proc.returncode != 0
    # Assert the GLOBAL line specifically. The bare word "floor" appears in
    # sys.exit's summary on EVERY failure, subtree ones included, so matching it
    # would let this case keep its name while quietly testing the subtree gate --
    # which is what would happen the first time a floor raise pushes the readiness
    # minimum past the 133 this fixture models.
    out = proc.stdout + proc.stderr
    assert "only 1915 tests executed" in out, out
