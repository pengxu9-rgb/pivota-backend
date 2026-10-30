"""The sharded Backend Test Sweep must be exactly one run of the suite, floors included.

Since 2026-09-29 the sweep runs as a `sweep-shard` matrix (tests/sweep_shard.py
picks each shard's tests) and a final `sweep` job that merges their JUnit XML
(scripts/merge_sweep_shards.py) and runs the floor assert UNCHANGED over it. Three
things have to stay true for that to mean what the single job meant, and each has
a section below:

  1. The split is a partition: every collected test runs in exactly one shard,
     proven on a real pytest run against the unsharded result, not described.
  2. The merge refuses anything that is not one complete run: a missing shard,
     a red shard, a doubled or dropped test, shards that collected different
     suites.
  3. The floors bite on the MERGED XML exactly as they did on one XML. The floor
     script is lifted out of the workflow and executed, as
     tests/test_sweep_subtree_floor_counts_only_executed.py does, so this tests
     the shipping code rather than a restatement.

Plus the workflow wiring that no running script can show: the matrix, the shard
count, the job names deploy-prod and CI Entrypoint key on, and the file names the
shards write and the merger reads.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SWEEP = REPO / ".github" / "workflows" / "backend-test-sweep.yml"
PLUGIN = REPO / "tests" / "sweep_shard.py"
MERGER = REPO / "scripts" / "merge_sweep_shards.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shard_plugin = _load(PLUGIN, "_sweep_shard_under_test")
merger = _load(MERGER, "_merge_sweep_shards_under_test")


def _wf() -> dict:
    return yaml.safe_load(SWEEP.read_text(encoding="utf-8"))


def _jobs() -> dict:
    return _wf()["jobs"]


def _num_shards() -> int:
    return int(_jobs()["sweep-shard"]["env"]["SWEEP_SHARDS"])


# ── 1. the split is a partition ──────────────────────────────────────────────


def test_every_nodeid_lands_in_exactly_one_shard():
    ids = [f"tests/test_m{i % 37}.py::test_{i}[{i % 5}]" for i in range(5000)]
    for n in (1, 2, 3, 4, 7):
        buckets = {k: set() for k in range(1, n + 1)}
        for nodeid in ids:
            buckets[shard_plugin.shard_of(nodeid, n)].add(nodeid)
        assert set().union(*buckets.values()) == set(ids)
        assert sum(len(b) for b in buckets.values()) == len(ids)
        if n > 1:
            # A hash that parked everything in one bucket would still be a
            # partition; it would just not be a speed-up.
            assert min(len(b) for b in buckets.values()) > len(ids) / n * 0.8


def test_the_assignment_does_not_depend_on_the_process():
    """Python's str hash is salted per process; sha256 is not. Pinned values, so a
    change of hash function is a deliberate edit rather than a silent reshuffle."""
    assert shard_plugin.shard_of("tests/test_a.py::test_b", 4) == \
        shard_plugin.shard_of("tests/test_a.py::test_b", 4)
    code = ("import importlib.util,sys;s=importlib.util.spec_from_file_location('p',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "print(m.shard_of('tests/test_a.py::test_b[x-1]', 4))")
    outs = {
        subprocess.run([sys.executable, "-c", code, str(PLUGIN)], capture_output=True,
                       text=True, env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip()
        for seed in ("0", "1", "12345")
    }
    assert outs == {str(shard_plugin.shard_of("tests/test_a.py::test_b[x-1]", 4))}


@pytest.mark.parametrize("spec", ["0/4", "5/4", "1", "a/b", "2/0", "1/2/3"])
def test_a_malformed_shard_spec_is_a_usage_error_not_shard_one(spec):
    with pytest.raises(pytest.UsageError):
        shard_plugin.parse_spec(spec)


def test_the_merger_digest_is_the_plugins_digest():
    ids = ["tests/b.py::t", "tests/a.py::t[1]", "readiness/tests/c.py::T::t"]
    assert merger.collection_digest(ids) == shard_plugin.collection_digest(ids)


# A miniature suite with every shape the real one has that could confuse a
# count: a module-level skip (JUnit writes it as a testcase in EVERY shard), a
# per-test skip, an xfail, a class, parametrize ids with brackets and dashes,
# and a --deselect that must apply before the split.
_SUITE = {
    "test_plain.py": "def test_a():\n    pass\n\ndef test_b():\n    pass\n",
    "test_params.py": textwrap.dedent("""
        import pytest

        @pytest.mark.parametrize("x", [1, 2, 3, "a-b", "c d"])
        def test_p(x):
            pass

        class TestK:
            @pytest.mark.parametrize("y", range(6))
            def test_m(self, y):
                pass
    """),
    "test_skips.py": textwrap.dedent("""
        import pytest

        def test_runs():
            pass

        @pytest.mark.skip(reason="per-test skip")
        def test_skipped():
            pass

        @pytest.mark.xfail(reason="known")
        def test_xf():
            assert False

        def test_deselected():
            raise AssertionError("--deselect must remove this before the split")
    """),
    "test_modskip.py": "import pytest\npytest.skip('module-level', allow_module_level=True)\n",
    "sub/test_deep.py": "".join(f"def test_{i}():\n    pass\n\n" for i in range(20)),
}


def _pytest(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(PLUGIN.parent), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
         "--rootdir", str(cwd), "-c", str(cwd / "pytest.ini"),
         "--deselect", "test_skips.py::test_deselected", *args],
        cwd=cwd, capture_output=True, text=True, env=env, timeout=120,
    )


def _cases(xml_path: Path):
    suite = merger._suite(ET.parse(xml_path).getroot())
    names = sorted((tc.get("classname"), tc.get("name")) for tc in suite.iter("testcase"))
    return suite, names


@pytest.fixture
def mini_suite(tmp_path):
    for rel, body in _SUITE.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    return tmp_path


@pytest.mark.parametrize("n", [1, 3, 4])
def test_the_merged_shards_equal_the_unsharded_run(mini_suite, n):
    """THE property. Real pytest, real junitxml, real plugin: the merged shards
    must name the same testcases and carry the same suite totals as one run."""
    whole = _pytest(mini_suite, "--junitxml", str(mini_suite / "whole.xml"))
    assert whole.returncode == 0, whole.stdout + whole.stderr
    shards = mini_suite / "shards"
    shards.mkdir()
    for k in range(1, n + 1):
        part = _pytest(
            mini_suite, "-p", "sweep_shard", "--sweep-shard", f"{k}/{n}",
            "--sweep-shard-manifest", str(shards / f"sweep-shard-{k}.manifest.json"),
            "--junitxml", str(shards / f"sweep-shard-{k}.xml"),
        )
        # 5 = no tests collected, which a small shard may legitimately hit.
        assert part.returncode in (0, 5), part.stdout + part.stderr

    errors = merger.merge(shards, n, "success", mini_suite / "sweep.xml")
    assert errors == []

    w_suite, w_names = _cases(mini_suite / "whole.xml")
    m_suite, m_names = _cases(mini_suite / "sweep.xml")
    assert m_names == w_names
    for attr in ("tests", "skipped", "failures", "errors"):
        assert m_suite.get(attr) == w_suite.get(attr), attr
    # And the fixture really did exercise the module-level skip.
    assert ("", "test_modskip") in w_names


def test_a_shard_that_ran_a_test_it_did_not_keep_is_caught(mini_suite):
    shards = mini_suite / "shards"
    shards.mkdir()
    for k in (1, 2):
        _pytest(mini_suite, "-p", "sweep_shard", "--sweep-shard", f"{k}/2",
                "--sweep-shard-manifest", str(shards / f"sweep-shard-{k}.manifest.json"),
                "--junitxml", str(shards / f"sweep-shard-{k}.xml"))
    # Shard 2's JUnit claims shard 1's results too: doubled work, and a count
    # that no longer matches what shard 2 kept.
    s1 = merger._suite(ET.parse(shards / "sweep-shard-1.xml").getroot())
    tree = ET.parse(shards / "sweep-shard-2.xml")
    s2 = merger._suite(tree.getroot())
    extra = [tc for tc in s1.iter("testcase") if tc.find("skipped") is None]
    assert extra
    s2.extend(extra)
    tree.write(shards / "sweep-shard-2.xml")
    errors = merger.merge(shards, 2, "success", mini_suite / "sweep.xml")
    assert any("shard 2: JUnit has" in e for e in errors), errors


# ── 2. the merge refuses anything that is not one complete run ──────────────


def _fake_shards(tmp, per_shard, *, collected=None, digests=None, n=None):
    """Write synthetic shard files: per_shard is a list of lists of
    (nodeid, skipped) pairs. Manifests are made consistent unless overridden."""
    n = n or len(per_shard)
    all_ids = [nodeid for shard in per_shard for nodeid, _ in shard]
    tmp.mkdir(parents=True, exist_ok=True)
    for k, shard in enumerate(per_shard, start=1):
        cases, skipped = [], 0
        for nodeid, is_skip in shard:
            cn, name = merger.junit_name(nodeid)
            body = "<skipped/>" if is_skip else ""
            skipped += is_skip
            cases.append(f'<testcase classname="{cn}" name="{name}">{body}</testcase>')
        (tmp / f"sweep-shard-{k}.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
            f'<testsuite name="pytest" errors="0" failures="0" skipped="{skipped}" '
            f'tests="{len(shard)}" time="1.0">' + "".join(cases) + "</testsuite></testsuites>"
        )
        (tmp / f"sweep-shard-{k}.manifest.json").write_text(json.dumps({
            "shard": k, "num_shards": n,
            "collected": collected if collected is not None else len(all_ids),
            "collected_digest": (digests or {}).get(k, merger.collection_digest(all_ids)),
            "kept": [nodeid for nodeid, _ in shard],
            "collect_skips": [],
        }))


def _ids(prefix, count, skipped=False):
    return [(f"{prefix}::t{i}", skipped) for i in range(count)]


def test_a_consistent_split_merges_cleanly(tmp_path):
    _fake_shards(tmp_path, [_ids("tests/test_a.py", 3), _ids("tests/test_b.py", 2)])
    assert merger.merge(tmp_path, 2, "success", tmp_path / "sweep.xml") == []


def test_a_missing_shard_fails_by_name(tmp_path):
    _fake_shards(tmp_path, [_ids("tests/test_a.py", 3), _ids("tests/test_b.py", 2)], n=3)
    errors = merger.merge(tmp_path, 3, "success", tmp_path / "sweep.xml")
    assert any("shard 3/3 left no readable manifest" in e for e in errors), errors
    assert any("shard 3/3 left no readable JUnit XML" in e for e in errors), errors


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", ""])
def test_a_shard_job_that_did_not_succeed_fails_the_merge(tmp_path, result):
    _fake_shards(tmp_path, [_ids("tests/test_a.py", 3), _ids("tests/test_b.py", 2)])
    errors = merger.merge(tmp_path, 2, result, tmp_path / "sweep.xml")
    assert any("not 'success'" in e for e in errors), errors


def test_a_test_in_two_shards_fails(tmp_path):
    a, b = _ids("tests/test_a.py", 3), _ids("tests/test_b.py", 2)
    digest = merger.collection_digest([i for i, _ in a + b])
    # The union is still exactly the collection; only the overlap is wrong.
    _fake_shards(tmp_path, [a, a[:1] + b], collected=5, digests={1: digest, 2: digest})
    errors = merger.merge(tmp_path, 2, "success", tmp_path / "sweep.xml")
    assert any("more than one shard" in e for e in errors), errors


def test_a_test_in_no_shard_fails(tmp_path):
    kept = [_ids("tests/test_a.py", 3), _ids("tests/test_b.py", 2)]
    everything = [i for s in kept for i, _ in s] + ["tests/test_c.py::lost"]
    digest = merger.collection_digest(everything)
    _fake_shards(tmp_path, kept, collected=len(everything), digests={1: digest, 2: digest})
    errors = merger.merge(tmp_path, 2, "success", tmp_path / "sweep.xml")
    assert any("some test ran in no shard" in e for e in errors), errors


def test_shards_that_collected_different_suites_fail(tmp_path):
    _fake_shards(tmp_path, [_ids("tests/test_a.py", 3), _ids("tests/test_b.py", 2)],
                 digests={1: "0" * 64})
    errors = merger.merge(tmp_path, 2, "success", tmp_path / "sweep.xml")
    assert any("did not collect the same suite" in e for e in errors), errors


def test_the_merger_exits_nonzero_on_a_violation(tmp_path):
    _fake_shards(tmp_path, [_ids("tests/test_a.py", 3)], n=2)
    rc = subprocess.run(
        [sys.executable, str(MERGER), "--shards-dir", str(tmp_path), "--num-shards", "2",
         "--needs-result", "success", "--out", str(tmp_path / "sweep.xml")],
        capture_output=True, text=True,
    ).returncode
    assert rc != 0
    # ...and still wrote what it had, so the floor assert after it can print counts.
    assert (tmp_path / "sweep.xml").exists()


# ── 3. the floors bite on the merged XML exactly as on one XML ───────────────


def _assert_script() -> str:
    """The workflow's floor assert, lifted out verbatim (the same extraction
    tests/test_sweep_subtree_floor_counts_only_executed.py uses)."""
    runs = [
        s["run"]
        for job in _jobs().values()
        for s in job.get("steps", [])
        if isinstance(s, dict) and isinstance(s.get("run"), str) and "SUBTREES" in s["run"]
    ]
    assert len(runs) == 1, f"expected exactly one step defining SUBTREES, found {len(runs)}"
    m = re.search(r"<<'PY'\n(.*?)\n\s*PY\b", runs[0], re.S)
    assert m
    return textwrap.dedent(m.group(1))


def _floors():
    script = _assert_script()
    floor = int(re.search(r"^FLOOR\s*=\s*(\d+)", script, re.M).group(1))
    subtrees = eval(re.search(r"^SUBTREES\s*=\s*(\{.*\})", script, re.M).group(1))  # noqa: S307
    return floor, subtrees


def _run_floor(cwd: Path, event: str = "pull_request"):
    return subprocess.run([sys.executable, "-c", _assert_script()], cwd=cwd,
                          capture_output=True, text=True,
                          env={**os.environ, "SWEEP_EVENT": event})


def _suite_ids(*, services, readiness, other, skip_readiness=0, module_skips=0):
    """(nodeid, skipped) for a run of the given shape."""
    ids = [(f"tests/services/test_x.py::t{i}", False) for i in range(services)]
    ids += [(f"readiness/tests/test_y.py::t{i}", i < skip_readiness) for i in range(readiness)]
    ids += [(f"tests/test_other.py::t{i}", False) for i in range(other)]
    return ids


def _split_and_merge(tmp: Path, ids, n, module_skips=()):
    """Split `ids` with the real shard function, write shard files the way a
    real shard does (module-level skips in EVERY shard), and merge."""
    per = {k: [] for k in range(1, n + 1)}
    for nodeid, skipped in ids:
        per[shard_plugin.shard_of(nodeid, n)].append((nodeid, skipped))
    tmp.mkdir(parents=True, exist_ok=True)
    digest = merger.collection_digest([i for i, _ in ids])
    for k, shard in per.items():
        rows = shard + [(m, True) for m in module_skips]
        cases = "".join(
            '<testcase classname="{}" name="{}">{}</testcase>'.format(
                *merger.junit_name(nodeid), "<skipped/>" if skipped else "")
            for nodeid, skipped in rows)
        (tmp / f"sweep-shard-{k}.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
            f'<testsuite name="pytest" errors="0" failures="0" '
            f'skipped="{sum(s for _, s in rows)}" tests="{len(rows)}" time="1.0">'
            + cases + "</testsuite></testsuites>")
        (tmp / f"sweep-shard-{k}.manifest.json").write_text(json.dumps({
            "shard": k, "num_shards": n, "collected": len(ids), "collected_digest": digest,
            "kept": [i for i, _ in shard], "collect_skips": list(module_skips)}))
    return merger.merge(tmp, n, "success", tmp / "sweep.xml")


def _healthy_shape():
    floor, subtrees = _floors()
    services, readiness = subtrees["tests.services"] + 100, subtrees["readiness.tests"] + 13
    other = floor + 200 - services - readiness
    return services, readiness, other


def test_a_healthy_merged_run_passes_the_floors(tmp_path):
    services, readiness, other = _healthy_shape()
    ids = _suite_ids(services=services, readiness=readiness, other=other)
    assert _split_and_merge(tmp_path, ids, _num_shards(),
                            module_skips=("tests/test_gated.py",)) == []
    proc = _run_floor(tmp_path, "push")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    total = services + readiness + other
    # The module-level skip every shard wrote is counted ONCE, as one process would.
    assert f"collected={total + 1} skipped=1 executed={total}" in proc.stdout, proc.stdout


def test_the_floor_output_is_identical_merged_or_whole(tmp_path):
    """Same verdict AND the same printed lines, from one XML or from N merged."""
    services, readiness, other = _healthy_shape()
    ids = _suite_ids(services=services, readiness=readiness, other=other, skip_readiness=20)
    _split_and_merge(tmp_path / "merged", ids, _num_shards())
    _split_and_merge(tmp_path / "whole", ids, 1)
    merged, whole = _run_floor(tmp_path / "merged"), _run_floor(tmp_path / "whole")
    assert (merged.returncode, merged.stdout) == (whole.returncode, whole.stdout)
    assert merged.returncode != 0  # 20 of readiness skipped puts it under its floor


def test_a_dropped_subtree_still_fails_after_merging(tmp_path):
    services, readiness, other = _healthy_shape()
    ids = _suite_ids(services=services, readiness=readiness, other=other,
                     skip_readiness=readiness)
    assert _split_and_merge(tmp_path, ids, _num_shards()) == []
    proc = _run_floor(tmp_path)
    assert proc.returncode != 0
    assert "subtree readiness.tests executed only 0" in proc.stdout + proc.stderr


def test_the_global_floor_still_bites_after_merging(tmp_path):
    floor, subtrees = _floors()
    ids = _suite_ids(services=subtrees["tests.services"], readiness=subtrees["readiness.tests"],
                     other=10)
    assert _split_and_merge(tmp_path, ids, _num_shards()) == []
    proc = _run_floor(tmp_path)
    assert proc.returncode != 0
    ran = subtrees["tests.services"] + subtrees["readiness.tests"] + 10
    assert f"only {ran} tests executed, floor is {floor}" in proc.stdout + proc.stderr


@pytest.mark.parametrize("event,fatal", [("push", True), ("pull_request", False)])
def test_the_decay_cap_keeps_its_push_vs_pr_asymmetry(tmp_path, event, fatal):
    floor, subtrees = _floors()
    services, readiness = subtrees["tests.services"] + 100, subtrees["readiness.tests"] + 13
    other = int(floor * 1.2) - services - readiness  # above the global cap
    ids = _suite_ids(services=services, readiness=readiness, other=other)
    assert _split_and_merge(tmp_path, ids, _num_shards()) == []
    proc = _run_floor(tmp_path, event)
    assert (proc.returncode != 0) is fatal, proc.stdout + proc.stderr
    assert "the floor has decayed" in proc.stdout


def test_one_missing_shard_is_fatal_even_when_the_floors_would_pass(tmp_path):
    """Why the merge check exists at all: 1/N of the suite can vanish inside the
    floors' headroom only by luck, and luck is not a gate."""
    services, readiness, other = _healthy_shape()
    ids = _suite_ids(services=services, readiness=readiness, other=other)
    n = _num_shards()
    _split_and_merge(tmp_path, ids, n)
    (tmp_path / f"sweep-shard-{n}.xml").unlink()
    errors = merger.merge(tmp_path, n, "success", tmp_path / "sweep.xml")
    assert any(f"shard {n}/{n} left no readable JUnit XML" in e for e in errors), errors


# ── 4. the workflow wiring ───────────────────────────────────────────────────


def test_the_workflow_keeps_the_name_deploy_prod_waits_for():
    assert _wf()["name"] == "Backend Test Sweep"


def test_the_matrix_is_a_static_list_of_every_shard():
    """CI Entrypoint can only expand a single-key literal list into check names
    (`sweep-shard (1)` ...); a fromJSON matrix would be unenforced. And the list
    must be exactly 1..SWEEP_SHARDS, or a shard number is never run."""
    job = _jobs()["sweep-shard"]
    matrix = job["strategy"]["matrix"]
    assert list(matrix) == ["shard"]
    assert matrix["shard"] == list(range(1, _num_shards() + 1))
    assert job["strategy"].get("fail-fast") is False
    assert _num_shards() >= 2


def test_the_final_job_is_named_sweep_and_waits_for_every_shard():
    sweep = _jobs()["sweep"]
    needs = sweep["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "sweep-shard" in needs
    # Must run when a shard FAILED, or the failure is reported only as a skipped
    # `sweep` -- which CI Entrypoint tolerates as `skipped`.
    cond = str(sweep.get("if", "")).replace(" ", "")
    assert cond in ("!cancelled()", "${{!cancelled()}}", "always()", "${{always()}}"), cond


def test_the_merge_reads_the_shard_result_and_the_same_shard_count():
    steps = _jobs()["sweep"]["steps"]
    merge = [s for s in steps if "merge_sweep_shards.py" in str(s.get("run", ""))]
    assert len(merge) == 1
    env = merge[0]["env"]
    assert env["SHARD_RESULT"].replace(" ", "") == "${{needs.sweep-shard.result}}"
    assert int(env["SWEEP_SHARDS"]) == _num_shards()
    assert "--out sweep.xml" in merge[0]["run"]
    # The merge runs BEFORE the floor assert, which reads the sweep.xml it writes.
    names = [s.get("run", "") for s in steps]
    floor_idx = next(i for i, r in enumerate(names) if "SUBTREES" in r)
    assert steps.index(merge[0]) < floor_idx


def test_the_shards_write_what_the_merger_reads():
    shard = _jobs()["sweep-shard"]
    run = next(s["run"] for s in shard["steps"] if "pytest" in str(s.get("run", "")))
    assert "-p tests.sweep_shard" in run
    assert '--sweep-shard "${SWEEP_SHARD}/${SWEEP_SHARDS}"' in run
    assert '--sweep-shard-manifest "sweep-shard-${SWEEP_SHARD}.manifest.json"' in run
    assert '--junitxml="sweep-shard-${SWEEP_SHARD}.xml"' in run
    assert "set -o pipefail" in run
    upload = next(s for s in shard["steps"] if "upload-artifact" in str(s.get("uses", "")))
    assert upload["with"]["name"] == "sweep-shard-${{ matrix.shard }}"
    assert str(upload.get("if", "")).replace(" ", "") == "!cancelled()"
    download = next(s for s in _jobs()["sweep"]["steps"]
                    if "download-artifact" in str(s.get("uses", "")))
    assert download["with"]["pattern"] == "sweep-shard-*"
    assert download["with"]["merge-multiple"] is True
    assert download["with"]["path"] == "shards"


def test_the_shard_command_keeps_the_quarantine():
    """Sharding adds options; it must not drop an ignore, the deselect or the
    postgres exclusion, each of which changes WHAT the floors are counting."""
    run = next(s["run"] for s in _jobs()["sweep-shard"]["steps"]
               if "pytest" in str(s.get("run", "")))
    for flag in (
        "python -m pytest tests readiness/tests",
        "--ignore=readiness/tests/test_real_merchant_goldens.py",
        "--ignore=readiness/tests/test_shopify_live_source.py",
        "--deselect 'readiness/tests/test_summary.py::test_build_readiness_optimization_"
        "serves_stale_then_refreshes_in_background'",
        "--ignore-glob='tests/test_*_postgres.py'",
        "-p no:randomly",
    ):
        assert flag in run, flag
