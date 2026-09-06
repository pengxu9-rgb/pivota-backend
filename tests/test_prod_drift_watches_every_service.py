"""`prod deploy drift` must see every service that ships from this repo's image.

MEASURED 2026-09-05, which is why this file exists. The alarm asked exactly one
question — what does `api.pivota.cc/health` say — and api.pivota.cc is `web`. On that
day `worker` was **94 commits behind** (a full month, covering every scheduler-lane
merge including #2074/#2077) and `proof-issuer` was **184 commits and 9 days behind**,
and this workflow had reported green throughout. Not because it was wrong about `web`,
but because the other two services were outside the only question it asked.

WHY THIS MODULE RUNS THE SCRIPT INSTEAD OF GREPPING IT. A ratchet that asserts the
string "worker" appears in the workflow passes for a file that mentions the worker in a
comment and never probes it — which is approximately the bug being fixed. So the step's
shell is lifted out of the YAML and EXECUTED against a real temporary git repository
with `curl` and `gcloud` stubbed, once per drift shape. What is asserted is the exit
code and which services are named, because the exit code is what turns into an alarm.

The one thing these cases cannot show — that a service is in the list at all — is
asserted separately, off the parsed YAML, in test_every_backend_service_is_watched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
DRIFT = WORKFLOWS / "prod-deploy-drift.yml"
DEPLOY = WORKFLOWS / "deploy-prod.yml"

# Every service deploy-prod.yml ships. The drift alarm must watch each one: a service
# that is deployed but unwatched is the exact 2026-09-05 finding, and a service that is
# watched but never deployed would alarm forever with no pipeline able to answer it.
# Derived from the deploy workflow rather than written twice — see the test that uses it.
SERVICE_OF_JOB = {
    "deploy": "web",
    "deploy-worker": "worker",
    "deploy-proof-issuer": "proof-issuer",
}


def _drift_script() -> str:
    doc = yaml.safe_load(DRIFT.read_text())
    steps = doc["jobs"]["drift"]["steps"]
    bodies = [s["run"] for s in steps if isinstance(s, dict) and isinstance(s.get("run"), str)]
    assert len(bodies) == 1, f"expected one `run:` step in the drift job, found {len(bodies)}"
    body = bodies[0]
    assert len(body.splitlines()) > 50, (
        "the extracted drift script is implausibly short — extraction broke, and every "
        "case below would be measuring an empty file"
    )
    return body


# ── the harness ────────────────────────────────────────────────────────────────────────


def _repo(tmp_path: Path) -> Path:
    """A real git repository whose history the script can actually walk.

    Not a mock: every question the script asks (`merge-base --is-ancestor`,
    `rev-list --first-parent`, `diff --name-only`, `show -s --format=%ct`) is answered by
    git itself, so a rewrite that keeps the vocabulary and inverts the logic fails here.
    """
    root = tmp_path / "repo"
    root.mkdir()
    run = lambda *a: subprocess.run(a, cwd=root, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (root / "services").mkdir()
    (root / "docs").mkdir()
    (root / "services" / "app.py").write_text("v0\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    return root


def _commit(root: Path, path: str, body: str, *, age_minutes: int = 0) -> str:
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    # The script measures the age of the MERGE that put the change on main, via the
    # committer date. Set it explicitly so the grace-window cases are deterministic
    # rather than dependent on how long the test suite took to get here.
    when = f"{int(__import__('time').time()) - age_minutes * 60} +0000"
    env = {**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    subprocess.run(["git", "commit", "-qm", f"change {path}"], cwd=root, check=True,
                   capture_output=True, env=env)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def _stubs(tmp_path: Path, *, web: str, images: dict, gcloud_fails: str = "",
           split: str = "") -> Path:
    """`curl` and `gcloud` that answer for a chosen production state.

    The gcloud stub models the TWO-CALL shape the job actually uses: `run services describe`
    to find the revision at 100% traffic, then `run revisions describe` to read THAT
    revision's image. An earlier harness modelled only the first call, answering
    `spec.template` — which made every case below silently agree with a job reading the
    wrong field, the very bug this shape exists to catch.

    `images` maps a service to what it SERVES. A value may be a plain sha, or a 2-tuple
    `(serving_sha, template_sha)` for the state that matters most: a deploy whose candidate
    failed its health check, leaving the new template at 0% and the old revision serving.
    A stub that could not express that could not test it.
    """
    binn = tmp_path / "bin"
    binn.mkdir(exist_ok=True)
    curl = binn / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'{{"version":{{"full_sha":"{web}"}}}}\'\n' if web else
        "#!/usr/bin/env bash\nexit 7\n"
    )
    curl.chmod(0o755)

    svc_cases, rev_cases = [], []
    for svc, val in images.items():
        serving, template = val if isinstance(val, tuple) else (val, val)
        live_rev, cand_rev = f"{svc}-live", f"{svc}-cand"
        if svc == split:
            traffic = (f'{{"revisionName":"{live_rev}","percent":50}},'
                       f'{{"revisionName":"{cand_rev}","percent":50}}')
        elif serving != template:
            # THE CANDIDATE-FAILED STATE: newest revision carries the new image at 0%, the
            # previous revision still takes every request.
            traffic = (f'{{"revisionName":"{live_rev}","percent":100}},'
                       f'{{"revisionName":"{cand_rev}","percent":0,"tag":"c-x"}}')
        else:
            traffic = f'{{"revisionName":"{live_rev}","percent":100}}'
        svc_cases.append(f'    {svc}) echo \'{{"status":{{"traffic":[{traffic}]}}}}\' ;;')
        rev_cases.append(f'    {live_rev}) echo "us-west1-docker.pkg.dev/p/p/backend:{serving}" ;;')
        rev_cases.append(f'    {cand_rev}) echo "us-west1-docker.pkg.dev/p/p/backend:{template}" ;;')

    gcloud = binn / "gcloud"
    gcloud.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = run ] && [ "$2" = services ] && [ "$3" = describe ]; then\n'
        f'  [ "$4" = "{gcloud_fails}" ] && exit 1\n'
        '  case "$4" in\n' + "\n".join(svc_cases) + "\n    *) exit 1 ;;\n  esac\n  exit 0\nfi\n"
        'if [ "$1" = run ] && [ "$2" = revisions ] && [ "$3" = describe ]; then\n'
        '  case "$4" in\n' + "\n".join(rev_cases) + "\n    *) exit 1 ;;\n  esac\n  exit 0\nfi\n"
        "exit 0\n"
    )
    gcloud.chmod(0o755)
    return binn


def _run_drift(tmp_path: Path, root: Path, *, web: str, images: dict,
               grace: str = "240", gcloud_fails: str = "", split: str = "") -> tuple[int, str]:
    script = tmp_path / "drift.sh"
    script.write_text(_drift_script())
    binn = _stubs(tmp_path, web=web, images=images, gcloud_fails=gcloud_fails, split=split)
    env = dict(os.environ)
    env["PATH"] = f"{binn}{os.pathsep}{env['PATH']}"
    env["GRACE_MINUTES"] = grace
    done = subprocess.run(["bash", str(script)], cwd=root, capture_output=True,
                          text=True, env=env, timeout=180)
    return done.returncode, done.stdout + done.stderr


# ── the cases ──────────────────────────────────────────────────────────────────────────


def test_all_three_on_main_is_quiet(tmp_path):
    root = _repo(tmp_path)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": head, "proof-issuer": head})
    assert code == 0, f"three services all on main must not alarm:\n{out}"


def test_a_stale_worker_alarms_even_though_web_is_current(tmp_path):
    """THE 2026-09-05 FINDING, as an executable case. `web` is exactly on main — the only
    thing the old alarm looked at — while the worker is a month of runtime commits behind.
    The old script exited 0 here."""
    root = _repo(tmp_path)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    head = _commit(root, "services/app.py", "v1\n", age_minutes=600)
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": old, "proof-issuer": head})
    assert code != 0, f"a worker a month behind main must alarm:\n{out}"
    assert "worker" in out, "the alarm must name WHICH service drifted"


def test_a_stale_proof_issuer_alarms(tmp_path):
    """proof-issuer drifts on its own. A test that only ever moves the worker would pass
    for an alarm that had quietly dropped the third service."""
    root = _repo(tmp_path)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    head = _commit(root, "services/app.py", "v1\n", age_minutes=600)
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": head, "proof-issuer": old})
    assert code != 0, f"a stale proof-issuer must alarm:\n{out}"
    assert "proof-issuer" in out


def test_every_drifted_service_is_named_not_just_the_first(tmp_path):
    """A loop that exited on the first drift would report `worker` and never mention
    `proof-issuer` — so the operator fixes one, re-runs, and discovers the next. A
    two-service gap must be reported as a two-service gap, once."""
    root = _repo(tmp_path)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    head = _commit(root, "services/app.py", "v1\n", age_minutes=600)
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": old, "proof-issuer": old})
    assert code != 0
    # PARSE ONLY THE VERDICT LINE. Slicing from the first "drifted:" to the end of the output
    # also swallowed the remediation footer, which literally lists `worker  infra/gcp/...` and
    # `proof-issuer  SERVICE=proof-issuer ...` — so the assertion held whenever the alarm fired
    # at all, and a loop that `break`s on the first drift passed it. That is the "assert x in
    # output where output always contains x" failure this very test was written to prevent.
    # Found by a mutation audit, 2026-09-05.
    verdict = [l for l in out.splitlines() if "::error::drifted:" in l]
    assert len(verdict) == 1, f"expected exactly one drifted verdict line, got {verdict}"
    named = sorted(verdict[0].split("drifted:", 1)[1].split())
    assert named == ["proof-issuer", "worker"], (
        f"the verdict named {named}; both drifted services must be reported in ONE run, or an "
        "operator fixes one, re-runs, and discovers the next tomorrow."
    )


def test_a_docs_only_gap_is_forgiven_for_every_service(tmp_path):
    """Docs never enter the container image. Alarming on them is the crying-wolf that
    launders a real alarm — and the exemption has to apply to the new services too, not
    only to the one it was written for."""
    root = _repo(tmp_path)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    head = _commit(root, "docs/runbook.md", "words\n", age_minutes=600)
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": old, "proof-issuer": old})
    assert code == 0, f"a docs-only gap must not alarm on any service:\n{out}"


def test_a_fresh_merge_is_a_pending_deploy_not_drift(tmp_path):
    """Every merge of runtime code makes the worker briefly stale by construction — the
    deploy job runs after `web`'s. Alarming instantly would make this a notification on
    every merge, which is how an alarm gets muted."""
    root = _repo(tmp_path)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    head = _commit(root, "services/app.py", "v1\n", age_minutes=5)
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": old, "proof-issuer": old})
    assert code == 0, f"a five-minute-old merge is a pending deploy, not drift:\n{out}"


def test_the_grace_window_expires(tmp_path):
    """The counterpart to the case above: forgiving a fresh merge must not be the same
    as forgiving it forever. Without this, the previous test passes for an alarm that
    never fires at all."""
    root = _repo(tmp_path)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    head = _commit(root, "services/app.py", "v1\n", age_minutes=5)
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": old, "proof-issuer": head}, grace="1")
    assert code != 0, f"past the grace window the same gap must alarm:\n{out}"


@pytest.mark.parametrize("svc", ["worker", "proof-issuer"])
def test_an_unreadable_service_blocks_rather_than_passes(tmp_path, svc):
    """An unknown state is not a good one. This is the same rule the deploy gate applies
    to an unreadable API: silence must never be able to read as fine, which is precisely
    how these two services stayed invisible for a month."""
    root = _repo(tmp_path)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": head, "proof-issuer": head},
                           gcloud_fails=svc)
    assert code != 0, f"an undescribable {svc} must alarm, not pass:\n{out}"
    assert svc in out


def test_an_unreachable_web_still_blocks(tmp_path):
    """The pre-existing behaviour, kept: the refactor into a per-service loop must not
    have turned a hard failure into a skipped iteration."""
    root = _repo(tmp_path)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()
    code, out = _run_drift(tmp_path, root, web="",
                           images={"worker": head, "proof-issuer": head})
    assert code != 0, f"an unreachable /health must alarm:\n{out}"


def test_a_candidate_that_failed_its_health_check_is_not_reported_as_shipped(tmp_path):
    """THE FALSE GREEN, and the reason this job reads traffic rather than `spec.template`.

    `deploy_backend.sh` — how proof-issuer ships — creates every revision `--tag c-<sha>
    --no-traffic`, health-checks it, and promotes only on 200. When that check fails it exits
    1 and leaves the candidate at 0% with the PREVIOUS revision serving. At that moment
    `spec.template` names the NEW image while users are still served the OLD one, so a job
    reading the template reports the new sha and goes green — permanently certifying the
    success of a deploy that failed, the one event this alarm exists to catch. Found in
    review, 2026-09-05."""
    root = _repo(tmp_path)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    head = _commit(root, "services/app.py", "v1\n", age_minutes=600)
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": head, "proof-issuer": (old, head)})
    assert code != 0, (
        "the alarm read a 0%-traffic candidate as shipped. Production is serving the OLD "
        f"commit and this reported green:\n{out}"
    )
    assert "proof-issuer" in out


def test_split_traffic_is_refused_rather_than_guessed(tmp_path):
    """A service divided between two revisions has no single 'what is live'. Picking one would
    be a coin flip reported as a fact — and a lingering 0%-traffic candidate is exactly the
    half-finished state this alarm most needs to not paper over."""
    root = _repo(tmp_path)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()
    code, out = _run_drift(tmp_path, root, web=head,
                           images={"worker": head, "proof-issuer": head}, split="proof-issuer")
    assert code != 0, f"split traffic must be refused, not resolved:\n{out}"
    assert "proof-issuer" in out


@pytest.mark.parametrize("tag", ["latest", "HEAD", "main", "deadbeef"])
def test_an_image_tag_that_is_not_a_commit_sha_is_refused(tmp_path, tag):
    """The tag is handed to `git cat-file` / `git merge-base` as a REVISION EXPRESSION, so
    anything git can resolve is accepted as 'the live commit'. A moving tag is the realistic
    way that happens: measured in review, with `backend:HEAD` the job compared production to
    itself and exited 0 reporting 'none touching code the container runs'."""
    root = _repo(tmp_path)
    head = _commit(root, "services/app.py", "v1\n", age_minutes=600)
    code, out = _run_drift(tmp_path, root, web=head, images={"worker": tag, "proof-issuer": head})
    assert code != 0, f"image tag {tag!r} was read as a commit and the job went green:\n{out}"
    assert "worker" in out


# ── structure ──────────────────────────────────────────────────────────────────────────


def test_every_backend_service_is_watched():
    """A service the pipeline DEPLOYS but the alarm does not WATCH is the whole finding.

    Read off both files rather than restated: adding a fourth deploy job without adding
    it to the alarm's list fails here, which is the only moment anyone is thinking about
    the question."""
    deployed = set()
    jobs = yaml.safe_load(DEPLOY.read_text())["jobs"]
    for job, service in SERVICE_OF_JOB.items():
        assert job in jobs, (
            f"deploy-prod.yml no longer has a `{job}` job. If the service was renamed or "
            f"retired, update SERVICE_OF_JOB — do not delete the coverage."
        )
        deployed.add(service)
    # Any OTHER job that runs a deploy script is a service nobody has mapped.
    for name, job in jobs.items():
        if name in SERVICE_OF_JOB or name == "test-gate":
            continue
        text = yaml.dump(job)
        # The SAME marks the sibling ratchet uses. A narrower set here let a fourth job that
        # shipped a service via a bare `gcloud run deploy` pass unnoticed — deployed on every
        # push and watched by nothing, the 2026-09-05 finding reopened. Found by a mutation
        # audit; keep this tuple and the gate module's identical.
        hit = [m for m in ("deploy_backend.sh", "deploy_worker.sh", "run deploy",
                           "builds submit") if m in text]
        assert not hit, (
            f"job `{name}` deploys something ({hit}) but is not in SERVICE_OF_JOB, so this "
            "module cannot check that the drift alarm watches it."
        )

    script = _drift_script()
    listed = script.split('SERVICES="', 1)[1].split('"', 1)[0].split()
    missing = deployed - set(listed)
    assert not missing, (
        f"deploy-prod.yml ships {sorted(missing)} but prod-deploy-drift.yml does not watch "
        f"them (it watches {listed}). A deployed-but-unwatched service is the 2026-09-05 "
        "finding: `worker` sat 94 commits behind for a month and the alarm was green."
    )


def test_the_alarm_does_not_deploy_anything():
    """This job authenticates to GCP now, which it never used to. That is a `describe`
    credential and it must stay one: an alarm that can also deploy is a pipeline checking
    its own work, which is exactly the independence this file exists to provide.

    The remediation footer is excluded, and that exclusion is the point of the test rather
    than a hole in it: `cat <<'EOF'` PRINTS those commands for an operator to read. A quoted
    heredoc delimiter means no expansion and no execution, so the same words are advice
    inside it and an action outside it. Checking the raw file text would confuse the two and
    force the footer to be deleted — removing the one thing that tells whoever is woken up
    at 02:17 what to actually run."""
    script = _drift_script()
    executed, inside = [], False
    for line in script.splitlines():
        stripped = line.strip()
        if not inside and stripped.startswith("cat <<'EOF'"):
            inside = True
            continue
        if inside:
            if stripped == "EOF":
                inside = False
            continue
        # Comment lines are excluded for the same reason as the heredoc: this file's whole
        # convention is that a script explains the neighbouring machinery, so `deploy_worker.sh`
        # appears in prose describing where the deploy-time scheduler probe lives. Naming a
        # script is not running one.
        if stripped.startswith("#"):
            continue
        executed.append(line)
    assert not inside, "unterminated heredoc — the exclusion above swallowed the rest of the script"
    body = "\n".join(executed)
    assert "gcloud run services describe" in body, (
        "the executed body no longer describes anything — the heredoc stripping above ate "
        "the real script, and every assertion below would be vacuous"
    )
    for forbidden in ("run deploy", "run services update", "update-traffic",
                      "builds submit", "deploy_backend.sh", "deploy_worker.sh"):
        assert forbidden not in body, (
            f"the drift alarm EXECUTES {forbidden!r}. It reports; it must not act."
        )
