"""The Cloud Build test runner (infra/gcp/cloudbuild.tests.yaml) runs CI's own steps, read from the
workflows. That only means "GREEN there = green on CI" while it knows EVERY run step of the jobs
it stands in for. scripts/ops/cloud_build_tests.py holds that list (RUNS / NOT_RUN) and refuses a
workflow that no longer matches it.

The first test runs the check against this checkout's real workflows, so a PR that adds, drops or
re-conditions a step in backend-test-sweep.yml or postgres-dialect-gate.yml fails HERE, in CI, and
is told to update the runner in the same PR. The rest pin that each kind of drift is refused.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("cloud_build_tests", ROOT / "scripts" / "ops" / "cloud_build_tests.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

SWEEP, GATE = runner.SWEEP_WF, runner.GATE_WF


def _step(wf: dict, job: str, selector: str) -> dict:
    [step] = [s for s in wf["jobs"][job]["steps"] if "run" in s and runner._matches(s, selector)]
    return step


@pytest.mark.parametrize("workflow, job", sorted(runner.RUNS))
def test_the_runner_knows_every_run_step_of_the_ci_jobs_it_stands_in_for(workflow, job):
    runner.check_job(runner._workflow(workflow), workflow, job)


def test_every_step_the_build_config_asks_for_is_one_the_runner_runs():
    """The selectors in cloudbuild.tests.yaml and in `summary` must be RUNS keys: `step` refuses
    any other, so a typo there is a red build, not a silently different step."""
    config = (ROOT / "infra" / "gcp" / "cloudbuild.tests.yaml").read_text()
    # What the build config and `summary` supply for each step's `${{ }}` env values.
    provides = {
        "sweep": ["SWEEP_SHARD", "SWEEP_SHARDS"], "Merge the shards": ["SHARD_RESULT", "SWEEP_SHARDS"],
        "Assert the sweep actually ran": ["SWEEP_EVENT"], "Preflight": [], "discover": ["SHARD"],
        "run_gate": ["SHARD", "GATE_FILES"], "Assert the gate": ["GATE_MODULES", "JUNIT"],
        "Recovery report": ["SHARD"], "Prove the shards are one complete run": ["SHARD_RESULT"],
    }
    for (workflow, job), selectors in runner.RUNS.items():
        for selector in selectors:
            script = runner.step_script(workflow, job, selector, provides[selector])
            assert script.startswith("#!/usr/bin/env bash") and "${{" not in script
    for selector in runner.RUNS[(GATE, "shard")]:
        assert f'"{selector}"' in config or f" {selector} " in config, selector
    assert "--step sweep" in config
    with pytest.raises(SystemExit):
        runner.step_script(GATE, "shard", "Install deps", [])


def _mutations():
    def added_step(wf):
        wf["jobs"]["shard"]["steps"].append({"name": "A new verdict", "run": "exit 1"})

    def dropped_step(wf):
        wf["jobs"]["shard"]["steps"].remove(_step(wf, "shard", "Preflight"))

    def working_directory(wf):
        _step(wf, "shard", "run_gate")["working-directory"] = "sub"

    def new_condition(wf):
        _step(wf, "shard", "Recovery report")["if"] = "${{ matrix.shard == 1 }}"

    def condition_added(wf):
        _step(wf, "shard", "Preflight")["if"] = "always()"

    def other_shell(wf):
        _step(wf, "shard", "discover")["shell"] = "sh"

    def install_changed(wf):
        _step(wf, "shard", "Install deps")["run"] += "pip install something-else\n"

    def workflow_defaults(wf):
        wf["defaults"] = {"run": {"working-directory": "sub"}}

    def job_container(wf):
        wf["jobs"]["shard"]["container"] = "python:3.12"

    def duplicated_step(wf):
        wf["jobs"]["shard"]["steps"].append(copy.deepcopy(_step(wf, "shard", "discover")))

    return [added_step, dropped_step, working_directory, new_condition, condition_added, other_shell,
            install_changed, workflow_defaults, job_container, duplicated_step]


@pytest.mark.parametrize("mutate", _mutations(), ids=lambda f: f.__name__)
def test_a_ci_job_that_changed_shape_is_refused(mutate):
    wf = copy.deepcopy(runner._workflow(GATE))
    runner.check_job(wf, GATE, "shard")
    mutate(wf)
    with pytest.raises(SystemExit):
        runner.check_job(wf, GATE, "shard")


def test_a_step_the_sweep_job_adds_is_refused():
    wf = copy.deepcopy(runner._workflow(SWEEP))
    wf["jobs"]["sweep"]["steps"].append({"name": "Assert something new", "run": "exit 1"})
    with pytest.raises(SystemExit):
        runner.check_job(wf, SWEEP, "sweep")


@pytest.mark.parametrize("value", [True, False, 1.1])
def test_an_env_value_yaml_1_1_reads_differently_from_github_is_refused(value):
    """PyYAML turns a bare `yes` into True and `1.10` into 1.1; GitHub passes the text."""
    with pytest.raises(SystemExit):
        runner._literal_env({"FLAG": value}, "job x", set())
    assert runner._literal_env({"N": 3, "S": "yes", "E": None}, "job x", set()) == {"N": "3", "S": "yes", "E": ""}
