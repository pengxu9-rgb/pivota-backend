"""The sweep's subtree minimums must count tests that RAN, not tests that were collected.

The global floor already subtracts skips (`ran = total - skipped`). The per-subtree loop
did not, so an entire subtree could go inert — every case collected, none executed — and
still clear its minimum.

The global floor is not a backstop for that. `readiness.tests` is 79 cases against 251 of
global headroom, so the whole tree can stop executing with BOTH gates green.
(`tests.services` is incidentally covered only because 1,682 skips would exceed the global
headroom — arithmetic luck, not a guarantee, and it evaporates when the floor next rises.)

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
         skip_services: bool = False, other: int = 11000) -> str:
    cases = []
    for i in range(services):
        body = "<skipped/>" if skip_services else ""
        cases.append(f'<testcase classname="tests.services.test_x" name="t{i}">{body}</testcase>')
    for i in range(readiness):
        body = "<skipped/>" if skip_readiness else ""
        cases.append(f'<testcase classname="readiness.tests.test_y" name="t{i}">{body}</testcase>')
    for i in range(other):
        cases.append(f'<testcase classname="tests.test_other" name="t{i}"/>')
    total = services + readiness + other
    skipped = (readiness if skip_readiness else 0) + (services if skip_services else 0)
    return (f'<testsuite tests="{total}" skipped="{skipped}">' + "".join(cases) + "</testsuite>")


def test_a_fully_skipped_subtree_is_rejected(tmp_path):
    """THE hole. Every readiness case collected, none executed — previously exit 0."""
    proc = _run(tmp_path, _xml(services=1682, readiness=79, skip_readiness=True))
    assert proc.returncode != 0, (
        "a subtree whose every test is SKIPPED cleared its minimum:\n" + proc.stdout
    )
    assert "readiness.tests" in (proc.stdout + proc.stderr)


def test_a_healthy_run_still_passes(tmp_path):
    """Guard the guard: the fix must not red a genuinely green sweep."""
    proc = _run(tmp_path, _xml(services=1682, readiness=79))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "readiness.tests: 79" in proc.stdout


def test_partial_skips_are_subtracted(tmp_path):
    """70 is the minimum; 79 collected with 10 skipped is 69 executed — below it."""
    xml = _xml(services=1682, readiness=69) .replace(
        "</testsuite>",
        "".join(f'<testcase classname="readiness.tests.test_y" name="s{i}"><skipped/></testcase>'
                for i in range(10)) + "</testsuite>",
    ).replace('skipped="0"', 'skipped="10"').replace('tests="12751"', 'tests="12761"')
    proc = _run(tmp_path, xml)
    assert proc.returncode != 0, "69 executed should not clear a minimum of 70:\n" + proc.stdout


def test_the_global_floor_still_bites(tmp_path):
    """Unchanged behaviour — the fix must not disturb the global gate."""
    proc = _run(tmp_path, _xml(services=1682, readiness=79, other=100))
    assert proc.returncode != 0
    assert "floor" in (proc.stdout + proc.stderr).lower()
