"""The worker has ONE deploy path, and it verifies the worker before claiming success.

WHY THIS FILE EXISTS. `worker` sat 94 commits behind `web` for a month (measured
2026-09-05) because rolling it meant running setup_scheduler.sh — which also reconciles
~20 Cloud Run Jobs and every Cloud Scheduler trigger and demands a <gateway-tag>. So
nobody ran it to ship an image; they hand-copied its eight-line worker block instead, on
2026-09-02 (worker-00016-rmv) and again on 2026-09-05 (worker-00018-rm6). A critical
section that is copied by hand at the moment of use is not a definition — the copy and
the original drift, and nobody finds out until the shapes disagree in production.

So the shape now lives in infra/gcp/deploy_worker.sh, and both setup_scheduler.sh and
deploy-prod.yml call it. These tests hold that property down, and they hold down the two
invariants that make the worker the worker (exactly one instance, no traffic) plus the
one that makes its deploy honest (it asks the running process whether the scheduler
actually came up).

The behavioural cases EXECUTE the script against a stubbed `gcloud` — the script already
takes GCLOUD from the environment — because a ratchet that greps for `--max-instances 1`
passes for a file that also, later, sets it to something else.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKER = REPO / "infra" / "gcp" / "deploy_worker.sh"
SCHEDULER = REPO / "infra" / "gcp" / "setup_scheduler.sh"

SHA = "a" * 40
PREV = "b" * 40


def _gcloud_stub(tmp_path: Path, *, probe_fails: bool = False, image_missing: bool = False,
                 prev_image: str = PREV, describe_error: str = "") -> Path:
    """A `gcloud` that records every invocation and can fail on demand.

    Recording matters more than answering: the assertions below read the RECORDED argv of
    the `run deploy` call, so they measure the flags the script actually sent rather than
    the flags its source appears to contain.
    """
    binn = tmp_path / "bin"
    binn.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    argvlog = tmp_path / "argv.log"
    prev = (f"us-west1-docker.pkg.dev/pivota-shared/pivota/backend:{prev_image}"
            if prev_image else "")
    stub = binn / "gcloud"
    # An empty `prev` models a service that does not exist. `describe_error` models the
    # DIFFERENT thing the old code could not distinguish: the API failing for any other reason.
    describe_error_block = (
        f'echo "{describe_error}" >&2; exit 1;; *) ' if describe_error else ""
    )
    stub.write_text(f"""#!/usr/bin/env bash
# Flatten newlines: the probe program is a multi-line `python -c` argument, and a
# line-based log would record only its first line — so an assertion about the probe's
# CONTENT would silently be reading an empty string. Measured: that is exactly what
# happened on the first run of this module.
printf '%s\\n' "${{*//$'\\n'/ }}" >> {log}
# ...and again, losslessly: args separated by \\037, calls by \\036, so a multi-line argument
# (the probe's `python -c` program) survives intact for tests that EXECUTE it.
printf '%s\\037' "$@" >> {argvlog}; printf '\\036' >> {argvlog}
case "$1 $2" in
  "artifacts docker")
      {"exit 1" if image_missing else "exit 0"} ;;
esac
if [ "$1" = run ] && [ "$2" = services ] && [ "$3" = describe ]; then
  case "$*" in
    *status.url*) echo "https://worker-xyz.a.run.app" ;;
    *) {describe_error_block}printf '%s\\n' "{prev}" ;;
  esac
  exit 0
fi
if [ "$1" = run ] && [ "$2" = jobs ] && [ "$3" = execute ]; then
  {"exit 1" if probe_fails else "exit 0"}
fi
exit 0
""")
    stub.chmod(0o755)
    return binn


def _run(tmp_path: Path, *args: str, env_extra: dict | None = None, **stub_kw):
    binn = _gcloud_stub(tmp_path, **stub_kw)
    env = dict(os.environ)
    env["GCLOUD"] = str(binn / "gcloud")
    env.update(env_extra or {})
    done = subprocess.run(["bash", str(WORKER), *args], capture_output=True, text=True,
                          env=env, timeout=120)
    calls = (tmp_path / "calls.log").read_text().splitlines() if (tmp_path / "calls.log").exists() else []
    return done.returncode, done.stdout + done.stderr, calls


def _deploys(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("run deploy worker")]


def _argv_calls(tmp_path: Path) -> list[list[str]]:
    raw = tmp_path / "argv.log"
    if not raw.exists():
        return []
    return [[a for a in call.split("\x1f") if a]
            for call in raw.read_bytes().decode().split("\x1e")]


def _deploy_argvs(tmp_path: Path) -> list[list[str]]:
    return [a for a in _argv_calls(tmp_path) if a[:3] == ["run", "deploy", "worker"]]


def _flag(argv: list[str], name: str) -> str:
    """The VALUE of a flag, by position.

    Not a substring search, and that distinction is why this helper exists: `assert
    "--max-instances 1" in sent` is satisfied by `--max-instances 10`, so the single-instance
    invariant — the one thing that makes this the single-instance drainer — passed for a
    ten-instance deploy. Found by a mutation audit, 2026-09-05.
    """
    assert name in argv, f"{name} was not passed to gcloud run deploy: {argv}"
    return argv[argv.index(name) + 1]


# ── the shape, measured off what the script actually sends ─────────────────────────────


def test_the_worker_is_deployed_as_exactly_one_instance(tmp_path):
    """min=max=1 with concurrency 1 is not tuning, it is the definition of "the
    single-instance drainer". Two instances means two APScheduler processes, and
    APScheduler's max_instances=1 is enforced PER PROCESS — so every tick double-fires,
    including the settlement and refund lanes. The queue itself survives that (every claim
    is `FOR UPDATE SKIP LOCKED`); the third-party side effects do not."""
    code, out, calls = _run(tmp_path, "prod", SHA)
    assert code == 0, f"the happy path must succeed:\n{out}"
    argvs = _deploy_argvs(tmp_path)
    assert len(argvs) == 1, f"expected exactly one deploy, got {len(argvs)}"
    argv = argvs[0]
    # EXACT equality: `"--max-instances 1" in sent` is also true of `--max-instances 10`.
    assert _flag(argv, "--min-instances") == "1", f"min-instances: {argv}"
    assert _flag(argv, "--max-instances") == "1", f"max-instances: {argv}"
    assert _flag(argv, "--concurrency") == "1", f"concurrency: {argv}"
    assert _flag(argv, "--cpu") == "1" and _flag(argv, "--memory") == "2Gi", (
        f"the worker's cpu/memory must not drift to web's: {argv}"
    )


def test_the_worker_never_takes_public_traffic(tmp_path):
    """`--ingress internal` and no --allow-unauthenticated. The worker has no callers; an
    exposed drainer is an unauthenticated surface with a database pool behind it."""
    _, _, calls = _run(tmp_path, "prod", SHA)
    sent = _deploys(calls)[0]
    assert "--ingress internal" in sent, f"expected --ingress internal, got: {sent}"
    assert "--allow-unauthenticated" not in sent, (
        f"the worker must not be publicly invokable: {sent}"
    )
    assert "sa-worker@pivota-prod" in sent, (
        f"the worker must run as sa-worker, not the backend account: {sent}"
    )


def test_no_candidate_revision_is_created(tmp_path):
    """deploy_backend.sh ships every revision `--tag c-<sha> --no-traffic`, probes it, then
    promotes. That is right for `web` and wrong here: a 0%-traffic revision with minScale 1
    KEEPS AN INSTANCE ALIVE (deploy_backend.sh's own sweep_stale_tags comment documents
    these immortal instances), and a worker instance does work from its app lifespan rather
    than from requests. The candidate window would therefore run a second drainer."""
    _, _, calls = _run(tmp_path, "prod", SHA)
    sent = _deploys(calls)[0]
    assert "--no-traffic" not in sent and "--tag" not in sent, (
        f"a tagged 0%-traffic worker revision is a second live drainer: {sent}"
    )


def test_the_commit_is_restamped_on_every_roll(tmp_path):
    """PIVOTA_COMMIT_SHA is the ONLY source of the sha this service reports about itself —
    Cloud Run injects nothing. A roll that changed the image and left it stale would ship
    new code under the old commit, and the drift alarm would go green over that lie. A
    silent alarm is worse than a missing one, because it invites trust."""
    _, _, calls = _run(tmp_path, "prod", SHA)
    sent = _deploys(calls)[0]
    assert f"PIVOTA_COMMIT_SHA={SHA}" in sent, (
        f"the deploy did not restamp PIVOTA_COMMIT_SHA to {SHA}: {sent}"
    )


def test_preserve_mode_does_not_rewrite_the_services_configuration(tmp_path):
    """The worker carries ~242 env vars and its secret mounts. `--update-env-vars` merges
    the handful this script owns; `--set-env-vars` / `--env-vars-file` REPLACE the set and
    would drop the rest — which is how five flags were lost on the gateway on 2026-08-30."""
    _, _, calls = _run(tmp_path, "prod", SHA)
    sent = _deploys(calls)[0]
    assert "--update-env-vars" in sent, f"preserve mode must merge, not replace: {sent}"
    assert "--set-env-vars" not in sent and "--env-vars-file" not in sent, (
        f"preserve mode must not rewrite the whole environment: {sent}"
    )
    assert "--set-secrets" not in sent, (
        f"preserve mode must leave secret mounts alone: {sent}"
    )


def test_arming_the_drainers_is_refused_rather_than_ignored(tmp_path):
    """AUDIT_WORKER_ENABLED only reaches the service through the env FILE, so WORKERS is
    inert under preserve. Accepting and ignoring it would report a clean, successful deploy
    that did not do the one thing it was run for — the same trap deploy_backend.sh already
    had to add this guard for, after the cutover runbook's headline command was exactly
    `WORKERS=true ... prod <tag>`."""
    code, out, calls = _run(tmp_path, "prod", SHA, env_extra={"WORKERS": "true"})
    assert code != 0, f"WORKERS under preserve must be refused, not ignored:\n{out}"
    assert not _deploys(calls), "it must refuse BEFORE deploying anything"
    assert "AUDIT_WORKER_ENABLED" in out, (
        "the refusal must say how to actually arm the drainers, or it is just a wall"
    )


def test_an_absent_image_is_refused_before_anything_is_touched(tmp_path):
    """Cloud Run accepts a deploy naming an image that does not exist and fails minutes
    later with a generic Ready=False. Answer it here, where the error can be specific."""
    code, out, calls = _run(tmp_path, "prod", SHA, image_missing=True)
    assert code != 0, f"an absent image must be refused:\n{out}"
    assert not _deploys(calls), "nothing may be deployed once the image is known absent"


# ── the verification, which is the part that was never there ───────────────────────────


def test_the_deploy_is_verified_against_the_running_scheduler(tmp_path):
    """`gcloud run deploy` returns success as soon as the container passes its startup
    probe, and this container passes that probe whether or not the scheduler booted: the
    drainers start from the app lifespan, so a scheduler that raised on boot leaves a
    perfectly healthy HTTP server answering /health with 200. The worker emits no logs
    either (no logging config, so Python's WARNING default drops every logger.info), which
    is why "it deployed" has never been evidence that it works."""
    _, out, calls = _run(tmp_path, "prod", SHA)
    probes = [c for c in calls if c.startswith("run jobs create")]
    assert probes, "no in-VPC probe job was created — the deploy is unverified"
    assert "__scheduler_health" in probes[0], (
        f"the probe does not ask /__scheduler_health: {probes[0]}"
    )
    assert "fireable_job_count" in probes[0], (
        "the probe does not check fireable_job_count. A worker whose scheduler is RUNNING "
        "with zero fireable jobs is up and will do nothing — the exact silent failure."
    )
    assert [c for c in calls if c.startswith("run jobs execute")], (
        "the probe job was created but never executed"
    )


def test_an_unhealthy_worker_fails_the_deploy_and_is_rolled_back(tmp_path):
    """There is no candidate to hold back here, so the bad revision is already live when
    the probe answers. Reporting success would be a lie; leaving it running would be worse.
    Roll the IMAGE back rather than shifting traffic: traffic is not the control for a
    service that works from its lifespan, so update-traffic would leave the unhealthy
    instance alive and draining alongside the old one."""
    code, out, calls = _run(tmp_path, "prod", SHA, probe_fails=True)
    assert code != 0, f"a worker whose scheduler did not come up must fail the deploy:\n{out}"
    argvs = _deploy_argvs(tmp_path)
    assert len(argvs) == 2, f"expected a deploy then a rollback deploy, got {len(argvs)}"
    # THE IMAGE IS THE ROLLBACK. Asserting `PREV in <the whole line>` was satisfied by the
    # PIVOTA_COMMIT_SHA restamp alone, so a rollback that redeployed the BROKEN image while
    # stamping the old sha passed — producing exactly the "reports the sha it failed to run"
    # state this test names. Found by a mutation audit, 2026-09-05.
    assert _flag(argvs[0], "--image").endswith(f":{SHA}"), "the forward deploy shipped the wrong image"
    assert _flag(argvs[1], "--image").endswith(f":{PREV}"), (
        f"the rollback did not redeploy the previous IMAGE: {_flag(argvs[1], '--image')}"
    )
    assert f"PIVOTA_COMMIT_SHA={PREV}" in " ".join(argvs[1]), (
        "the rollback restamped the wrong commit — the service would report the sha it "
        "failed to run"
    )


def test_a_failed_probe_with_no_previous_image_still_fails(tmp_path):
    """First deploy of the service: there is nothing to roll back to. That must not turn
    into a pass — an unverifiable deploy is not a verified one."""
    code, out, calls = _run(tmp_path, "prod", SHA, probe_fails=True, prev_image="")
    assert code != 0, f"an unhealthy first deploy must still fail:\n{out}"
    assert len(_deploys(calls)) == 1, "nothing to roll back to, so exactly one deploy"


# ── the probe program, EXECUTED ────────────────────────────────────────────────────────
# Everything above asserts the probe's TEXT — that the call contains "__scheduler_health",
# "fireable_job_count". That is a paraphrase, and a mutation audit proved it: replacing the
# whole verdict with `ok = True` left this module green, because those strings still appear
# in the diagnostic print(). So the program is lifted out of the recorded argv and RUN
# against a local HTTP server, once per payload the real worker can produce.


def _probe_program(tmp_path: Path) -> str:
    """The exact `python -c` program the script ships, byte for byte.

    From the LOSSLESS argv log, not the flattened line log: the latter joins the program's
    lines into one and glues the following `--quiet` onto the end, so the recovered text is a
    SyntaxError rather than the shipped program — a harness artifact that would be reported
    as a defect in the script.
    """
    _run(tmp_path, "prod", SHA)
    for args in _argv_calls(tmp_path):
        if args[:3] == ["run", "jobs", "create"]:
            for a in args:
                if a.startswith("--args=") and "^|^-c|" in a:
                    return a.split("^|^-c|", 1)[1]
    raise AssertionError("no `run jobs create` carrying a probe program was recorded")


@pytest.mark.parametrize(
    "health, build, expect_ok, why",
    [
        ({"state_name": "RUNNING", "boot_error": None, "fireable_job_count": 7,
          "worker_enabled": True}, {"full_sha": SHA}, True,
         "a healthy armed worker on the right commit is the only thing that may pass"),
        ({"state_name": "RUNNING", "boot_error": None, "fireable_job_count": 0,
          "worker_enabled": False}, {"full_sha": SHA}, False,
         "fireable_job_count 0 means _add_job registered nothing - the drainers are disarmed"),
        ({"state_name": "RUNNING", "boot_error": "boom", "fireable_job_count": 7,
          "worker_enabled": True}, {"full_sha": SHA}, False,
         "a scheduler that raised on boot still answers 200; boot_error is the only tell"),
        ({"state_name": "PAUSED", "boot_error": None, "fireable_job_count": 7,
          "worker_enabled": True}, {"full_sha": SHA}, False,
         "PAUSED fires nothing, and `running` cannot distinguish it from RUNNING"),
        ({"state_name": "RUNNING", "boot_error": None, "fireable_job_count": 7,
          "worker_enabled": True}, {"full_sha": "0" * 40}, False,
         "a stale commit means the restamp did not take"),
        ({"state_name": "RUNNING", "boot_error": None, "fireable_job_count": 7,
          "worker_enabled": True}, {}, False,
         "NO sha must fail: full_sha is None exactly when PIVOTA_COMMIT_SHA is missing, which "
         "is the 'new code under the old commit' state the restamp exists to prevent"),
        ({"state_name": "RUNNING", "boot_error": None, "fireable_job_count": 7,
          "worker_enabled": True}, {"full_sha": ""}, False,
         "an empty sha is the same absence, spelled differently"),
    ],
)
def test_the_probe_program_verdict(tmp_path, health, build, expect_ok, why):
    import http.server, json as _json, threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = _json.dumps(health if "scheduler" in self.path else build).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        raw = _probe_program(tmp_path)
        prog = raw.replace(
            "https://worker-xyz.a.run.app", f"http://127.0.0.1:{srv.server_address[1]}"
        )
        # If the stub's URL ever changes, this replace silently no-ops and the probe makes a
        # REAL outbound request to that hostname — a test that quietly stops testing, and
        # starts reaching the network from CI. Fail here instead.
        assert prog != raw, (
            "the probe's URL was not substituted; the stub's status.url no longer matches what "
            "this test replaces, and the program would have called the real hostname"
        )
        done = subprocess.run(["python3", "-c", prog], capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    ok = done.returncode == 0
    assert ok == expect_ok, (
        f"probe returned rc={done.returncode} (ok={ok}), expected ok={expect_ok}.\n"
        f"WHY THIS CASE EXISTS: {why}\nhealth={health}\nbuild={build}\n"
        f"output={done.stdout}{done.stderr}"
    )


def test_an_unreadable_current_image_refuses_before_deploying(tmp_path):
    """`|| true` on the describe could not tell "no such service" from "the API failed", and
    both produced an empty PREV_IMAGE. That reads as a first deploy, silently disabling the
    rollback — and a probe failure then strands production on the broken image with nothing to
    go back to. An unknown current state must refuse while refusing is still free."""
    code, out, calls = _run(tmp_path, "prod", SHA,
                            describe_error="ERROR: (gcloud.run.services.describe) INTERNAL: 500")
    assert code != 0, f"an unreadable current image must refuse:\n{out}"
    assert not _deploys(calls), (
        "it deployed anyway. The point is to refuse BEFORE the service is changed, while "
        "there is still something to roll back to."
    )


def test_a_genuinely_absent_service_is_still_created(tmp_path):
    """The counterpart, and why the test above is not merely 'refuse on any error': a real
    first deploy must still work, or the refusal has broken bootstrapping."""
    # GCLOUD'S REAL MESSAGE, measured against gcloud 581.0.0:
    #     ERROR: (gcloud.run.services.describe) Cannot find service [worker]
    # An earlier version of this test invented `... (NOT_FOUND)`, which the script's pattern
    # happened to match — so the test certified a branch that was DEAD against real gcloud.
    # An assertion about another program's output is worth exactly what it was measured against.
    code, out, calls = _run(
        tmp_path, "prod", SHA, prev_image="",
        describe_error="ERROR: (gcloud.run.services.describe) Cannot find service [worker]")
    assert code == 0, f"a genuinely absent service must still be created:\n{out}"
    assert len(_deploys(calls)) == 1


def test_a_project_level_not_found_does_not_read_as_an_absent_service(tmp_path):
    """`NOT_FOUND` also appears in `NOT_FOUND: Project ... not found`. Treating that as "the
    service does not exist" would deploy over a live worker with no rollback anchor — fail-open
    in the precise direction this guard exists to close. The pattern is scoped to a message
    naming THIS service for that reason."""
    code, out, calls = _run(
        tmp_path, "prod", SHA,
        describe_error="ERROR: (gcloud.run.services.describe) NOT_FOUND: Project pivota-typo not found")
    assert code != 0, f"a project-level not-found must refuse, not proceed:\n{out}"
    assert not _deploys(calls), "it deployed with no idea what was running"


def test_the_documented_arming_command_still_reconciles(tmp_path):
    """`WORKERS=true PAUSED=0 infra/gcp/setup_scheduler.sh prod <a> <b>` is the arming command in
    infra/gcp/README.md, and delegating the worker deploy broke it: WORKERS reaches the child
    through the ENVIRONMENT, so simply not naming it on the preserve branch changed nothing, the
    child's guard exited 2, and `set -e` killed the script before ONE Cloud Run Job or Scheduler
    trigger was reconciled.

    EXECUTED, not grepped. `assert "env -u WORKERS" in code_only` passes with that string sitting
    on the WRONG branch — verified: moving it to the apply branch restores the regression in full
    and leaves the whole module green."""
    binn = _gcloud_stub(tmp_path)
    env = dict(os.environ)
    env.update(GCLOUD=str(binn / "gcloud"), WORKERS="true", PAUSED="0",
               CLOUDSDK_CORE_PROJECT="pivota-prod")
    done = subprocess.run(
        ["bash", str(REPO / "infra" / "gcp" / "setup_scheduler.sh"), "prod", SHA, "gw" + "0" * 38],
        capture_output=True, text=True, env=env, timeout=180)
    out = done.stdout + done.stderr
    assert "has no effect under CONFIG=preserve" not in out, (
        "the documented arming command died at deploy_worker.sh's WORKERS guard — the child "
        f"inherited WORKERS from this script's environment:\n{out[-2500:]}"
    )
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert "run deploy worker" in calls, (
        f"the worker was never deployed by the arming command:\n{out[-2500:]}"
    )


# ── one definition ─────────────────────────────────────────────────────────────────────


def test_setup_scheduler_does_not_carry_its_own_copy_of_the_worker_deploy():
    """The whole point. Two definitions of the worker's shape drift, and the drift is
    invisible until production disagrees with the file someone was reading."""
    text = SCHEDULER.read_text()
    # STRIP COMMENTS FIRST. `"deploy_worker.sh" in text` was satisfied by the prose block
    # explaining the delegation, so a script that had stopped deploying the worker entirely
    # still passed — the "mentions it in a comment" hole this module's own docstring warns
    # about, in this module. Found by a mutation audit, 2026-09-05.
    code_only = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert re.search(r'deploy_worker\.sh"?\s', code_only), (
        "setup_scheduler.sh no longer CALLS deploy_worker.sh (a comment naming it does not "
        "count) — if it grew its own copy back, the shape has two definitions again."
    )
    assert '"$ENV" "$BACKEND_TAG"' in code_only, (
        "the delegation no longer passes (env, tag) in that order — deploy_worker.sh takes "
        "`staging|prod <tag>`, so swapping them deploys a tag named 'prod'."
    )
    assert "env -u WORKERS" in code_only, (
        "the preserve branch no longer unsets WORKERS for the child. WORKERS arrives through "
        "the ENVIRONMENT, so not naming it does nothing: deploy_worker.sh's guard then kills "
        "`WORKERS=true ... setup_scheduler.sh` — the documented arming command — before a "
        "single Cloud Run Job or Scheduler trigger is reconciled."
    )
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"run deploy worker\b", body), (
        "setup_scheduler.sh deploys the worker itself again, alongside deploy_worker.sh. "
        "That is the hand-copied critical section this file exists to prevent."
    )


def test_the_workflow_and_the_scheduler_script_call_the_same_file():
    """If CI rolled the worker one way and the reconcile script another, the two would
    produce different services and only one of them would be reviewed."""
    workflow = "\n".join(
        l for l in (REPO / ".github" / "workflows" / "deploy-prod.yml").read_text().splitlines()
        if not l.lstrip().startswith("#")
    )
    assert "deploy_worker.sh" in workflow, (
        "deploy-prod.yml does not call deploy_worker.sh, so CI is not using the single "
        "definition this module is holding down."
    )


@pytest.mark.parametrize("bad", [[], ["prod"], ["staging"], ["nonsense", SHA]])
def test_it_refuses_an_incomplete_or_unknown_invocation(tmp_path, bad):
    code, out, _ = _run(tmp_path, *bad)
    assert code != 0, f"`deploy_worker.sh {' '.join(bad)}` must be refused:\n{out}"
