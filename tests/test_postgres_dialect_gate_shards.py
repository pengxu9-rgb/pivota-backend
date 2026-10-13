"""The Postgres Dialect Gate's shards are disjoint, complete and stable, and the final
job refuses any set of shard results that is not one whole gate run.

scripts/postgres_dialect_gate_shards.py carries the why. These run in the Backend Test
Sweep (no database): the name does not match `test_*_postgres.py`, deliberately.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "postgres-dialect-gate.yml"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shards = _load(ROOT / "scripts" / "postgres_dialect_gate_shards.py",
               "_postgres_dialect_gate_shards_under_test")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _env() -> dict:
    return _workflow()["env"]


def _num_shards() -> int:
    return int(_env()["DIALECT_GATE_SHARDS"])


def _ride_along_shard() -> int:
    return int(_env()["RIDE_ALONG_SHARD"])


def _ride_alongs() -> list:
    return _env()["RIDE_ALONGS"].split()


# ── the workflow's shape ──────────────────────────────────────────────────────


def test_the_workflow_keeps_the_names_deploy_prod_and_ci_entrypoint_wait_for():
    doc = _workflow()
    assert doc["name"] == "Postgres Dialect Gate"
    final = doc["jobs"]["postgres-dialect-gate"]
    assert final["name"] == "execute real routes on real Postgres"
    assert final["needs"] == "shard"
    # Without it a red shard leaves the final job SKIPPED, which CI Entrypoint tolerates.
    assert final["if"] == "!cancelled()"


def test_the_matrix_is_a_static_list_of_every_shard():
    """CI Entrypoint can only enforce a single-key, literal-list matrix with a static
    name; anything else is printed as unenforceable and silently not required."""
    shard = _workflow()["jobs"]["shard"]
    assert "${{" not in shard["name"]
    matrix = shard["strategy"]["matrix"]
    assert list(matrix) == ["shard"]
    assert matrix["shard"] == list(range(1, _num_shards() + 1))
    assert shard["strategy"]["fail-fast"] is False
    assert _ride_along_shard() in matrix["shard"]


def test_the_critical_path_stays_inside_the_deadlines_that_wait_for_it():
    jobs = _workflow()["jobs"]
    path = jobs["shard"]["timeout-minutes"] + jobs["postgres-dialect-gate"]["timeout-minutes"]
    # deploy-prod's and CI Entrypoint's poll deadlines are both 50 minutes.
    assert path < 50


def test_the_partial_update_ride_along_runs_last():
    assert _ride_alongs()[-1] == "tests/test_quality_backfill_partial_update.py"


# ── the partition ─────────────────────────────────────────────────────────────


def test_every_gate_file_and_ride_along_runs_in_exactly_one_shard():
    gate = shards.discover(ROOT)
    ride = shards.present(ROOT, _ride_alongs())
    assert gate and len(ride) == len(_ride_alongs())
    seen = []
    for k in range(1, _num_shards() + 1):
        planned = shards.plan(gate, ride, k, _num_shards(), _ride_along_shard())
        assert any(f in gate for f in planned), f"shard {k} has no gate file"
        seen += planned
    assert sorted(seen) == sorted(gate + ride)
    assert len(seen) == len(set(seen))


def test_a_shard_keeps_the_unsharded_order_and_ends_with_the_ride_alongs():
    gate = shards.discover(ROOT)
    ride = shards.present(ROOT, _ride_alongs())
    planned = shards.plan(gate, ride, _ride_along_shard(), _num_shards(), _ride_along_shard())
    head, tail = planned[: -len(ride)], planned[-len(ride):]
    assert tail == ride
    assert head == [f for f in gate if f in head]


def test_adding_a_file_moves_no_other_file():
    gate = [f"tests/test_f{i}_postgres.py" for i in range(60)]
    before = {f: shards.shard_of(f, 3) for f in gate}
    grown = sorted(gate + ["tests/test_aaa_new_postgres.py"])
    for f in gate:
        assert shards.shard_of(f, 3) == before[f]
    assert sum(len(shards.plan(grown, [], k, 3, 1)) for k in (1, 2, 3)) == 61


@pytest.mark.parametrize("bad", [0, 4])
def test_a_shard_out_of_range_is_an_error(bad):
    with pytest.raises(ValueError):
        shards.plan(["tests/test_a_postgres.py"], [], bad, 3, 1)


# ── verify ────────────────────────────────────────────────────────────────────

GATE = sorted(f"tests/test_m{i}_postgres.py" for i in range(12))
RIDE = ["tests/test_ride_a.py", "tests/test_ride_b.py"]
N, RIDE_SHARD = 3, 2


@pytest.fixture
def repo(tmp_path):
    for f in GATE + RIDE:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("")
    return tmp_path


def _junit(cases, skipped=0):
    rows = "".join(f'<testcase classname="tests.{m}" name="test_x" time="0.1"/>' for m in cases)
    return (f'<testsuites><testsuite name="pytest" tests="{len(cases)}" skipped="{skipped}" '
            f'errors="0" failures="0" time="1.0">{rows}</testsuite></testsuites>')


def _write_shards(repo, out, *, drop=None, skip_in=None, mute=None, recovery_in=RIDE_SHARD,
                  manifest_files=None):
    for k in range(1, N + 1):
        if k == drop:
            continue
        d = out / f"postgres-dialect-gate-1-shard-{k}"
        d.mkdir(parents=True)
        files = shards.plan(GATE, RIDE, k, N, RIDE_SHARD)
        if manifest_files and k in manifest_files:
            files = manifest_files[k]
        modules = [shards.module_of(f) for f in files if f in GATE]
        (d / f"shard-{k}.manifest.json").write_text(
            json.dumps({"shard": k, "num_shards": N, "files": files, "gate_modules": modules}))
        ran = [shards.module_of(f) for f in files if shards.module_of(f) != mute]
        (d / f"pytest-postgres-shard-{k}.xml").write_text(
            _junit(ran, skipped=1 if k == skip_in else 0))
        if k == recovery_in:
            (d / f"pytest-recovery-postgres-shard-{k}.xml").write_text(_junit(["recovery"]))


def _verify(repo, out, needs="success"):
    return shards.verify(out, repo, N, RIDE, RIDE_SHARD, RIDE_SHARD, needs)


def test_a_complete_run_verifies(repo, tmp_path):
    _write_shards(repo, tmp_path / "s")
    assert _verify(repo, tmp_path / "s") == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_a_shard_job_that_did_not_succeed_fails(repo, tmp_path, result):
    _write_shards(repo, tmp_path / "s")
    assert any(result in e for e in _verify(repo, tmp_path / "s", needs=result))


def test_a_missing_shard_fails_by_number(repo, tmp_path):
    _write_shards(repo, tmp_path / "s", drop=3)
    errors = _verify(repo, tmp_path / "s")
    assert any("shard 3" in e and "never reported" in e for e in errors)
    assert any(e.startswith("in no shard:") for e in errors)


def test_any_skip_fails(repo, tmp_path):
    _write_shards(repo, tmp_path / "s", skip_in=1)
    assert any("SKIPPED" in e for e in _verify(repo, tmp_path / "s"))


def test_a_gate_module_that_ran_nothing_fails(repo, tmp_path):
    victim = shards.module_of(shards.plan(GATE, RIDE, 1, N, RIDE_SHARD)[0])
    _write_shards(repo, tmp_path / "s", mute=victim)
    assert any(victim in e and "executed no tests" in e for e in _verify(repo, tmp_path / "s"))


def test_a_shard_that_ran_a_different_list_fails(repo, tmp_path):
    files = shards.plan(GATE, RIDE, 1, N, RIDE_SHARD)
    _write_shards(repo, tmp_path / "s", manifest_files={1: files[1:]})
    errors = _verify(repo, tmp_path / "s")
    assert any("shard 1 ran" in e for e in errors)
    assert any(e.startswith("in no shard:") and files[0] in e for e in errors)


def test_the_recovery_report_must_run_in_its_shard(repo, tmp_path):
    _write_shards(repo, tmp_path / "a", recovery_in=None)
    assert any("recovery report never ran" in e for e in _verify(repo, tmp_path / "a"))
    _write_shards(repo, tmp_path / "b", recovery_in=1)
    errors = _verify(repo, tmp_path / "b")
    assert any("recovery report never ran" in e for e in errors)
    assert any("belongs to shard 2" in e for e in errors)


def test_plan_writes_the_step_outputs_and_manifest(repo, tmp_path, monkeypatch):
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    manifest = tmp_path / "m.json"
    code = shards.main(["plan", "--root", str(repo), "--num-shards", str(N), "--shard",
                        str(RIDE_SHARD), "--ride-along-shard", str(RIDE_SHARD),
                        "--ride-alongs", " ".join(RIDE), "--manifest", str(manifest)])
    assert code == 0
    files = shards.plan(GATE, RIDE, RIDE_SHARD, N, RIDE_SHARD)
    lines = out.read_text().splitlines()
    assert lines[0] == "files=" + " ".join(files)
    assert lines[1] == "gate_modules=" + " ".join(
        shards.module_of(f) for f in files if f in GATE)
    assert json.loads(manifest.read_text())["files"] == files


def test_verify_exits_nonzero_on_a_violation(repo, tmp_path):
    _write_shards(repo, tmp_path / "s", skip_in=2)
    code = shards.main(["verify", "--root", str(repo), "--shards-dir", str(tmp_path / "s"),
                        "--num-shards", str(N), "--ride-along-shard", str(RIDE_SHARD),
                        "--ride-alongs", " ".join(RIDE), "--recovery-shard", str(RIDE_SHARD),
                        "--needs-result", "success"])
    assert code == 1
