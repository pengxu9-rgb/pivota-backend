"""`deploy prod` must not ship a commit whose test sweep is red, or absent.

MEASURED 2026-09-03, which is why this file exists. `Backend Test Sweep` concluded
FAILURE on main for six consecutive commits — d2cbc8ae5, f624e3b0e, a11521e99,
eb4a298d3, 4a45c5d55, 61b97f8bd — and `deploy prod` ran on the same commits and
concluded SUCCESS on every one it was not concurrency-cancelled out of. Production
took three deploys in thirty-three minutes off a red suite, and no workflow, alarm
or check anywhere in the repo said so, because the deploy had no `needs:` at all.

WHY THE TESTS BELOW MOSTLY EXECUTE THE GATE RATHER THAN DESCRIBE IT. A workflow
ratchet that only greps for strings is a paraphrase of the file, and it passes for
any rewrite that keeps the vocabulary while inverting the logic — a deny-list of
"bad" conclusions reads identically to an allow-list of `success` and ships on the
one conclusion nobody enumerated. So this module lifts the gate's script OUT of
`deploy-prod.yml` and RUNS it, against a stubbed `gh`, once per conclusion GitHub
can produce. What is asserted is the exit code, which is the thing `needs:` reads.

The three structural facts a running script cannot show — that the deploy job
`needs:` the gate, that the gate resolves the same SHA the deploy ships, and that
the poll deadline sits above the timeouts of the workflows it waits for — are
asserted off the parsed YAML.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy-prod.yml"

GATE_JOB = "test-gate"
DEPLOY_JOB = "deploy"

# EVERY job that puts code into production, not just `web`'s. Added 2026-09-05 with the
# worker and proof-issuer jobs: a second deploying job that does not `needs:` the gate
# reopens the 2026-09-03 finding one service over, and a ratchet written around the single
# name `deploy` would not have noticed. Discovered rather than listed — see
# test_every_deploying_job_is_covered_by_this_module, which fails if a job deploys
# something and is not in here.
DEPLOY_JOBS = ("deploy", "deploy-worker", "deploy-proof-issuer")

# The workflow FILES whose `name:` the gate must require. The names themselves are
# never written here — they are read out of those files, so renaming a workflow
# without updating the gate's list fails this module instead of quietly turning the
# gate into a wait for a workflow that no longer publishes under that name.
REQUIRED_WORKFLOW_FILES = (
    WORKFLOWS / "backend-test-sweep.yml",
    WORKFLOWS / "postgres-dialect-gate.yml",
)

# Everything GitHub can conclude a run with. Only `success` may release the deploy;
# the rest are here so a regression that special-cases one of them is caught by name
# rather than by hoping the parametrisation happened to cover it.
BLOCKING_CONCLUSIONS = (
    "failure",
    "cancelled",
    "timed_out",
    "skipped",
    "action_required",
    "stale",
    "startup_failure",
    "neutral",
    # NOT a real conclusion. It is the whole point: a gate written as an allow-list
    # blocks on a string it has never seen, and a gate written as a deny-list ships.
    "a_conclusion_github_has_not_invented_yet",
)

# A run that exists but has not finished, and a run that does not exist at all.
# Both are SILENCE, and silence must block.
NON_REPORTING_STATES = ("queued", "in_progress", "absent")


def _doc() -> dict:
    return yaml.safe_load(DEPLOY.read_text())


def _jobs() -> dict:
    jobs = _doc().get("jobs") or {}
    assert isinstance(jobs, dict) and jobs, f"{DEPLOY.name} declares no jobs"
    return jobs


def _triggers() -> dict:
    doc = _doc()
    # PyYAML reads the bare key `on:` as the boolean True (YAML 1.1).
    trigger = doc.get(True) if True in doc else doc.get("on")
    if isinstance(trigger, list):
        return {t: {} for t in trigger}
    return trigger if isinstance(trigger, dict) else {}


def _gate_script() -> str:
    """The gate's Python, lifted out of the workflow so the tests can run it.

    Fails loudly when it cannot find it. An empty extraction would run nothing, exit
    0, and turn every "must block" case red for the wrong reason while the "releases
    on success" case stayed green — the vacuous-harness shape.
    """
    jobs = _jobs()
    assert GATE_JOB in jobs, (
        f"{DEPLOY.name} has no `{GATE_JOB}` job. The production deploy is not "
        "waiting for any test workflow — this is the 2026-09-03 finding."
    )
    bodies = [
        step["run"]
        for step in (jobs[GATE_JOB].get("steps") or [])
        if isinstance(step, dict) and isinstance(step.get("run"), str)
        and "<<'PY'" in step["run"]
    ]
    assert len(bodies) == 1, (
        f"expected exactly one heredoc step in `{GATE_JOB}`, found {len(bodies)}"
    )
    body = bodies[0]
    start = body.index("<<'PY'") + len("<<'PY'\n")
    end = body.rindex("\nPY")
    script = body[start:end]
    assert len(script.splitlines()) > 50, (
        "the extracted gate script is implausibly short — extraction broke, and "
        "every case below would be measuring an empty file"
    )
    return script


def _required_names() -> list[str]:
    """The workflow names the gate's own script says it requires."""
    match = re.search(r"^REQUIRED = \(([^)]*)\)", _gate_script(), re.M)
    assert match, "the gate script declares no REQUIRED tuple of workflow names"
    return re.findall(r'"([^"]+)"', match.group(1))


# ── structure ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_the_deploy_job_waits_for_the_gate(job):
    jobs = _jobs()
    assert job in jobs, f"{DEPLOY.name} no longer has a `{job}` job"
    needs = jobs[job].get("needs")
    needs = [needs] if isinstance(needs, str) else (needs or [])
    assert GATE_JOB in needs, (
        f"the `{job}` job does not `needs: [{GATE_JOB}]`, so it runs "
        "regardless of whether the tests passed. Measured 2026-09-03: three "
        "production deploys off a red Backend Test Sweep."
    )


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_nothing_lets_the_deploy_outvote_the_gate(job):
    """A `needs:` plus `if: always()` is a gate with the wires cut."""
    condition = str(_jobs()[job].get("if") or "")
    assert "always(" not in condition.replace(" ", ""), (
        f"the `{job}` job carries `{condition}` — `always()` makes it run "
        "even when the gate it needs failed, which is not a gate."
    )


def test_every_deploying_job_is_covered_by_this_module():
    """DEPLOY_JOBS must not fall behind the workflow.

    A new job that ships a service and is not in that tuple is untested by every case
    above — and it would look fine, because the parametrisation would simply not mention
    it. Detect deploying jobs by what they RUN rather than by name: the gap this closes
    was created by adding services, and the next one will be too."""
    marks = ("deploy_backend.sh", "deploy_worker.sh", "run deploy", "builds submit")
    for name, job in _jobs().items():
        body = yaml.dump(job)
        if any(m in body for m in marks):
            assert name in DEPLOY_JOBS, (
                f"job `{name}` deploys something but is not in DEPLOY_JOBS, so nothing "
                "here checks that it waits for the test gate."
            )


@pytest.mark.parametrize("job", [j for j in DEPLOY_JOBS if j != "deploy"])
def test_the_other_services_wait_for_web(job):
    """web first, deliberately. All three run the same image against the same database, so
    a commit is half-shipped between them either way; ordering them behind `web` means the
    lag always points the same direction — the direction the drift alarm names and a human
    reads first. The reverse (a worker running a commit `web` failed to take) is the same
    drift with nothing accustomed to reading it."""
    needs = _jobs()[job].get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    assert DEPLOY_JOB in needs, (
        f"`{job}` does not `needs: [{DEPLOY_JOB}]`. It would then race web's deploy and "
        "could put a commit on the worker that never reached the public API."
    )


@pytest.mark.parametrize("job", [j for j in DEPLOY_JOBS if j != "deploy"])
def test_a_candidate_dispatch_does_not_deploy_the_other_services(job):
    """A dispatch with promote unchecked means "build and health-check without putting
    anything in front of users". Neither service has such a state — the worker takes no
    traffic at all, so there is no 0% to hold it at, and it drains queues from its app
    lifespan the moment it boots. Deploying them there makes "candidate" mean "fully live".
    Nothing asserted this until a mutation audit removed the `if:` and the suite stayed green."""
    condition = str(_jobs()[job].get("if") or "")
    assert "inputs.promote" in condition and "push" in condition, (
        f"`{job}` carries `if: {condition}` — it no longer skips on a candidate dispatch."
    )


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_every_deploy_runs_in_the_protected_environment(job):
    """`environment: gcp-prod` is where required reviewers attach without touching this file.
    A job that quietly drops it deploys production outside whatever protection is configured
    there, and looks identical in a diff."""
    assert _jobs()[job].get("environment") == "gcp-prod", (
        f"`{job}` is not in the `gcp-prod` environment, so it deploys production outside it."
    )


def test_the_worker_is_not_deployed_through_deploy_backend_sh():
    """deploy_backend.sh would be wrong here in three independent ways, and two of them
    fail silently — see the header of infra/gcp/deploy_worker.sh. The one that matters
    most: it ships every revision `--tag c-<sha> --no-traffic`, and a 0%-traffic revision
    with minScale 1 keeps an instance ALIVE. A worker instance drains queues from its app
    lifespan, not from requests, so the candidate window would run two drainers and
    double-fire every APScheduler tick, including the settlement lanes."""
    body = yaml.dump(_jobs()["deploy-worker"])
    assert "deploy_worker.sh" in body, "the worker job must use infra/gcp/deploy_worker.sh"
    assert "deploy_backend.sh" not in body, (
        "the worker job invokes deploy_backend.sh, which applies web's shape "
        "(min-instances 2 on a service whose whole design is exactly one process) and "
        "runs a tagged candidate revision alongside the live one."
    )


def test_the_proof_issuer_deploy_pins_the_shape_it_is_not_allowed_to_change():
    """deploy_backend.sh's constants are WEB's connection budget, recomputed 2026-08-29
    (concurrency 80 -> 20, max-instances 20 -> 10) after two pool-exhaustion outages.
    proof-issuer has run concurrency 80 / maxScale 20 since 08-27 because it has not been
    deployed since. An unpinned automatic roll would cut it from 1600 request slots to
    200 as a side effect of shipping a commit — an unreviewed capacity change nobody asked
    this workflow to make."""
    body = yaml.dump(_jobs()["deploy-proof-issuer"])
    # THE VALUES, not merely the presence of the flags. `"CONCURRENCY_LIMIT=" in body` passed
    # for `CONCURRENCY_LIMIT=20 MAX_INSTANCES=10` — which IS the 1600-to-200 request-slot cut
    # this test exists to prevent, applied, with the suite green. Found by a mutation audit,
    # 2026-09-05. These are proof-issuer's live values as measured that day; changing the
    # service's capacity means editing them here, where review can see it.
    for pin, value in (("CPU_LIMIT", "2"), ("MEMORY_LIMIT", "4Gi"), ("CONCURRENCY_LIMIT", "80"),
                       ("MIN_INSTANCES", "2"), ("MAX_INSTANCES", "20")):
        assert f"{pin}={value}" in body, (
            f"the proof-issuer job does not pin {pin}={value}. Without it the service adopts "
            "web's shape constants and is silently reshaped on every push to main."
        )
    # The entrypoint override is load-bearing for a different reason: deploy_backend.sh
    # passes RUN_COMMAND/RUN_ARGS unconditionally once either is set, and proof_issuer_main
    # also serves /health — so omitting them boots the WRONG application behind a health
    # check that answers 200 either way, and the candidate gate promotes it.
    assert "proof_issuer_main:app" in body, (
        "the proof-issuer job does not override the entrypoint, so it would deploy the "
        "main backend app onto this service behind a /health that cannot tell them apart."
    )


def test_the_gate_requires_the_sweep_and_the_dialect_gate_by_their_exact_names():
    declared = _required_names()
    for path in REQUIRED_WORKFLOW_FILES:
        name = yaml.safe_load(path.read_text())["name"]
        assert name in declared, (
            f"the gate does not require {path.name} by its `name:` ({name!r}). "
            f"It requires {declared!r}. The GitHub API keys workflow runs on that "
            "exact string, so a mismatch is a gate that waits forever or, worse, "
            "one whose required set is silently empty."
        )


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_the_gate_resolves_the_same_sha_the_deploy_ships(job):
    """A gate that checks one commit while the handler ships another is decoration — and
    with three handlers, one of them resolving a different expression would put a
    DIFFERENT commit on that service than on the other two, which is drift manufactured by
    the pipeline itself."""
    jobs = _jobs()
    gate_sha = (jobs[GATE_JOB].get("env") or {}).get("GATE_SHA")
    deploy_sha = (jobs[job].get("env") or {}).get("SHA")
    assert gate_sha and deploy_sha, "both jobs must resolve a SHA through `env:`"
    assert gate_sha == deploy_sha, (
        "the gate and the deploy resolve DIFFERENT expressions:\n"
        f"  {GATE_JOB}.env.GATE_SHA = {gate_sha}\n"
        f"  {job}.env.SHA    = {deploy_sha}\n"
        "so the commit whose tests were checked is not the commit that ships."
    )


def test_the_deploy_does_not_read_github_sha_under_a_workflow_run_trigger():
    """Under `workflow_run`, `github.sha` is the head of the DEFAULT BRANCH, not the
    commit the triggering run tested. Swapping the trigger in without also swapping
    every `github.sha` for `github.event.workflow_run.head_sha` silently converts
    this file from "deploy what was tested" to "deploy whatever main is now"."""
    if "workflow_run" not in _triggers():
        return
    text = DEPLOY.read_text()
    assert "github.event.workflow_run.head_sha" in text and not re.search(
        r"github\.sha", text
    ), (
        "deploy-prod.yml now has a `workflow_run` trigger but still reads "
        "`github.sha`, which under that event is the default branch's head rather "
        "than the tested commit."
    )


def test_the_poll_deadline_sits_above_every_required_workflows_own_timeout():
    """Otherwise the gate reports `never reported` on a suite that was merely slow,
    and hard-blocks a deploy whose tests then pass — the ci-entrypoint.yml lesson."""
    match = re.search(r"^DEADLINE_MINUTES = ([0-9.]+)$", _gate_script(), re.M)
    assert match, "the gate script declares no DEADLINE_MINUTES"
    deadline = float(match.group(1))

    slowest = 0
    for path in REQUIRED_WORKFLOW_FILES:
        for job in (yaml.safe_load(path.read_text()).get("jobs") or {}).values():
            # No `timeout-minutes:` means GitHub's default of 360, which no sane
            # deadline can clear. Say so rather than silently reading it as 0.
            declared = (job or {}).get("timeout-minutes")
            assert declared is not None, (
                f"a job in {path.name} declares no `timeout-minutes`, so it may run "
                "for 360 minutes and this gate cannot wait it out. Give it one."
            )
            slowest = max(slowest, int(declared))
    assert deadline > slowest, (
        f"the gate waits {deadline}m but a required workflow may run {slowest}m"
    )

    job_timeout = _jobs()[GATE_JOB].get("timeout-minutes")
    assert job_timeout is not None and job_timeout > deadline, (
        f"the gate job's timeout-minutes ({job_timeout}) must exceed its own "
        f"deadline ({deadline}), or GitHub kills the job before it can print WHICH "
        "workflow never reported."
    )


# ── the override ───────────────────────────────────────────────────────────────


def test_the_manual_override_demands_a_reason_and_is_not_the_default():
    inputs = (_triggers().get("workflow_dispatch") or {}).get("inputs") or {}
    assert "reason" in inputs, (
        "workflow_dispatch has no `reason` input, so a hand-run production deploy "
        "records nothing about why it happened."
    )
    assert inputs["reason"].get("required") is True, "`reason` must be required"

    assert "bypass_test_gate" in inputs, (
        "there is no explicit switch for skipping the gate, which means either the "
        "gate cannot be bypassed in an emergency or it is bypassed implicitly."
    )
    bypass = inputs["bypass_test_gate"]
    assert bypass.get("type") == "boolean", "the bypass must be a boolean, not free text"
    assert bypass.get("default") is False, (
        "the bypass defaults to ON — the override must never be the default path."
    )


# ── behaviour: the gate is EXECUTED, once per conclusion ────────────────────────


def _stub_gh(tmp_path: Path, sweep_state: str, gate_state: str = "completed:success",
             fail: bool = False) -> Path:
    """A `gh` that answers the runs query with a state we choose."""
    binary = tmp_path / "gh"
    if fail:
        binary.write_text('#!/bin/sh\necho "API rate limit exceeded" >&2\nexit 1\n')
    else:
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "def run(name, spec, number):\n"
            "    if spec == 'absent':\n"
            "        return None\n"
            "    status, _, conclusion = spec.partition(':')\n"
            "    return {'name': name, 'status': status,\n"
            "            'conclusion': conclusion or None, 'run_number': number,\n"
            "            'html_url': 'https://example.invalid/' + str(number)}\n"
            f"runs = [r for r in (run({_required_names()[0]!r}, {sweep_state!r}, 1),\n"
            f"                    run({_required_names()[1]!r}, {gate_state!r}, 2)) if r]\n"
            "json.dump({'total_count': len(runs), 'workflow_runs': runs}, sys.stdout)\n"
        )
    binary.chmod(0o755)
    return binary


def _run_gate(tmp_path: Path, sweep_state: str, *, gate_state="completed:success",
              bypass="0", reason="", fail_api=False, shorten=False):
    """Execute the SHIPPED gate script and return (exit code, output)."""
    script = _gate_script()
    if shorten:
        # Only for the states that make the gate WAIT. The substitution is asserted
        # so that a renamed constant fails here instead of silently running the real
        # thirty-minute deadline inside a unit test.
        script, count = re.subn(
            r"^DEADLINE_MINUTES = [0-9.]+$", "DEADLINE_MINUTES = 0.01", script, flags=re.M
        )
        assert count == 1, "could not shorten the gate's deadline — the constant moved"
        script, count = re.subn(
            r"^POLL_SECONDS = [0-9.]+$", "POLL_SECONDS = 0.01", script, flags=re.M
        )
        assert count == 1, "could not shorten the gate's poll interval — the constant moved"

    path = tmp_path / "gate.py"
    path.write_text(script)
    _stub_gh(tmp_path, sweep_state, gate_state, fail=fail_api)

    env = dict(os.environ)
    env.update(
        PATH=f"{tmp_path}{os.pathsep}{env['PATH']}",
        REPO="owner/repo",
        GATE_SHA="1" * 40,
        EVENT="push",
        ACTOR="ratchet",
        REASON=reason,
        BYPASS=bypass,
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"),
    )
    done = subprocess.run(
        [sys.executable, str(path)], capture_output=True, text=True, env=env, timeout=180
    )
    return done.returncode, done.stdout + done.stderr


def test_a_green_sweep_releases_the_deploy(tmp_path):
    code, out = _run_gate(tmp_path, "completed:success")
    assert code == 0, f"a green sweep must release the deploy, got {code}:\n{out}"


@pytest.mark.parametrize("conclusion", BLOCKING_CONCLUSIONS)
def test_a_sweep_that_is_not_success_blocks_the_deploy(tmp_path, conclusion):
    code, out = _run_gate(tmp_path, f"completed:{conclusion}")
    assert code != 0, (
        f"the gate RELEASED the deploy on a sweep that concluded {conclusion!r}.\n"
        "Only `success` may release it — everything else, including a conclusion "
        f"this repo has never seen, must block.\n{out}"
    )
    assert conclusion in out, "the gate must say WHICH conclusion blocked it"


@pytest.mark.parametrize("state", NON_REPORTING_STATES)
def test_a_sweep_that_never_reports_blocks_the_deploy(tmp_path, state):
    """Silence is not success. A run still queued, a run still going, and a run that
    was never triggered at all are all absences of evidence."""
    spec = "absent" if state == "absent" else f"{state}:"
    code, out = _run_gate(tmp_path, spec, shorten=True)
    assert code != 0, (
        f"the gate RELEASED the deploy with the sweep in state {state!r} — it read "
        f"an absent verdict as a pass.\n{out}"
    )


def test_the_dialect_gate_blocks_on_its_own(tmp_path):
    """Both required workflows gate. On 2026-09-03's red commits the dialect gate
    happened to be green, so a test that only ever moves the sweep would pass for a
    gate that had quietly dropped the second workflow."""
    code, out = _run_gate(
        tmp_path, "completed:success", gate_state="completed:failure"
    )
    assert code != 0, f"a red Postgres Dialect Gate must block the deploy:\n{out}"


def test_an_unreadable_api_blocks_rather_than_releases(tmp_path):
    """An unknown test state is not a green one, and a swallowed step failure is the
    documented shape of every deploy-path trap in this repo."""
    code, out = _run_gate(tmp_path, "completed:success", fail_api=True)
    assert code != 0, f"a failing API call must block the deploy:\n{out}"


def test_the_bypass_refuses_a_token_reason(tmp_path):
    code, out = _run_gate(
        tmp_path, "completed:failure", bypass="1", reason="fix"
    )
    assert code != 0, f"the bypass accepted a three-character reason:\n{out}"


def test_the_bypass_ships_but_records_what_it_bypassed(tmp_path):
    reason = "prod is 500ing and the rollback target predates the gate"
    code, out = _run_gate(
        tmp_path, "completed:failure", bypass="1", reason=reason
    )
    assert code == 0, f"a reasoned emergency bypass must be able to ship:\n{out}"
    summary = (tmp_path / "summary.md").read_text()
    assert reason in summary and "ratchet" in summary, (
        "the bypass must record the reason and the actor in the run summary — "
        f"bypassing has to be visible, not merely possible:\n{summary}"
    )
    assert "failure" in summary, (
        "the bypass must record the STATE it overrode, not just that it happened"
    )
    assert "::warning::" in out, "the bypass must annotate the run, not pass quietly"
