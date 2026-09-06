"""Ops scripts must read the SERVING revision when they mean "what is running".

`spec.template` is the template of the LAST REVISION CREATED, which is not the one taking
traffic. They diverge exactly when something went wrong — a candidate that failed its health
check, a revision that never became Ready — and that is precisely when these scripts are
consulted. `prod-deploy-drift.yml` shipped with that confusion and reported a FAILED deploy as
shipped (#2091); the same mistake was then made by hand in a verification command minutes after
that fix merged. It is an easy read to reach for.

BOTH QUESTIONS ARE REAL, and this module pins which script is asking which:

  "what is RUNNING"            -> the 100%-traffic revision  (deploy_worker's rollback target,
                                  setup_scheduler's arming summary)
  "what will the NEXT deploy   -> `spec.template`, because `run deploy --update-env-vars`
   inherit"                       merges into the template  (deploy_backend's pool guard,
                                  restore_to_cloudsql's minScale capture)

The behavioural cases drive the real scripts against a stubbed `gcloud` (they honour
`GCLOUD=<path>`) in the state that separates the two: a template already moved on while an
older revision still serves.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GCP = REPO / "infra" / "gcp"
SERVING = "a" * 40          # what the 100%-traffic revision runs
TEMPLATE = "b" * 40         # what the template asks for — a deploy that did not take


def _stub_gcloud(tmp_path: Path, *, armed_serving="true", armed_template="false") -> Path:
    """A `gcloud` whose service TEMPLATE and SERVING revision deliberately disagree."""
    binn = tmp_path / "bin"
    binn.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    img = "us-west1-docker.pkg.dev/p/p/backend"
    stub = binn / "gcloud"
    stub.write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {log}
if [ "$1" = run ] && [ "$2" = services ] && [ "$3" = describe ]; then
  # REAL gcloud fails for a service that does not exist. A stub that answers for any name is
  # more forgiving than reality, and every case built on it is weaker than it looks — the
  # refusal case below caught exactly that here.
  case "$4" in
    worker|web|proof-issuer) ;;
    *) echo "ERROR: (gcloud.run.services.describe) Cannot find service [$4]" >&2; exit 1 ;;
  esac
  case "$*" in
    *status.url*) echo "https://svc-xyz.a.run.app"; exit 0 ;;
    *--format=json*)
      echo '{{"status":{{"traffic":[{{"revisionName":"rev-live","percent":100}},'\\
'{{"revisionName":"rev-cand","percent":0,"tag":"c-x"}}]}},'\\
'"spec":{{"template":{{"spec":{{"containers":[{{"image":"{img}:{TEMPLATE}",'\\
'"env":[{{"name":"AUDIT_WORKER_ENABLED","value":"{armed_template}"}}]}}]}}}}}}}}'
      exit 0 ;;
    *spec.template.spec.containers*image*) echo "{img}:{TEMPLATE}"; exit 0 ;;
    *spec.template.spec.containers*env*)
      echo "[{{'name': 'AUDIT_WORKER_ENABLED', 'value': '{armed_template}'}}]"; exit 0 ;;
  esac
  exit 0
fi
if [ "$1" = run ] && [ "$2" = revisions ] && [ "$3" = describe ]; then
  case "$4" in
    rev-live)
      echo '{{"spec":{{"containers":[{{"image":"{img}:{SERVING}",'\\
'"env":[{{"name":"AUDIT_WORKER_ENABLED","value":"{armed_serving}"}}]}}]}}}}'
      exit 0 ;;
    rev-cand)
      echo '{{"spec":{{"containers":[{{"image":"{img}:{TEMPLATE}",'\\
'"env":[{{"name":"AUDIT_WORKER_ENABLED","value":"{armed_template}"}}]}}]}}}}'
      exit 0 ;;
  esac
  exit 1
fi
exit 0
""")
    stub.chmod(0o755)
    return binn


def _source_and_call(tmp_path: Path, snippet: str, **stub_kw) -> str:
    binn = _stub_gcloud(tmp_path, **stub_kw)
    script = f'''
set -euo pipefail
GCLOUD="{binn / 'gcloud'}"; PROJECT=pivota-prod; REGION=us-west1
. "{GCP / '_serving_revision.sh'}"
{snippet}
'''
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    return (done.stdout + done.stderr).strip()


# ── the helper itself ──────────────────────────────────────────────────────────────────


def test_serving_image_ignores_the_template(tmp_path):
    """THE WHOLE POINT. The template names a revision that never took traffic; the answer must
    be the image the 100%-traffic revision actually runs."""
    out = _source_and_call(tmp_path, "serving_image worker")
    assert SERVING in out and TEMPLATE not in out, (
        f"serving_image returned the TEMPLATE's image, which is the deploy that did not take: {out}"
    )


def test_serving_env_ignores_the_template(tmp_path):
    out = _source_and_call(tmp_path, "serving_env worker AUDIT_WORKER_ENABLED",
                           armed_serving="true", armed_template="false")
    assert out == "true", f"serving_env read the template ('false') instead of the live 'true': {out!r}"


def test_an_unreadable_service_fails_rather_than_answering(tmp_path):
    """An unknown state must never look like a clean one — the rule the whole session turned on."""
    out = _source_and_call(tmp_path, 'serving_image nosuchsvc && echo UNEXPECTED_SUCCESS || echo REFUSED')
    assert "REFUSED" in out, f"an undescribable service produced an answer: {out}"


def test_two_revisions_both_claiming_all_traffic_are_refused(tmp_path):
    """The input only the `len(live) == 1` clause can catch.

    A 50/50 split is rejected by the `percent == 100` FILTER alone — neither entry qualifies —
    so it cannot exercise the clause beside it, and relaxing that to `if not live` survived a
    mutation audit. Two entries both claiming 100 is an inconsistent traffic block, and it is
    the shape that separates the two guards. Two guards that both close a door pin neither.
    """
    binn = _stub_gcloud(tmp_path)
    both = (binn / "gcloud").read_text().replace('"percent":0,"tag":"c-x"', '"percent":100')
    (binn / "gcloud").write_text(both)
    script = f'''
set -euo pipefail
GCLOUD="{binn / 'gcloud'}"; PROJECT=pivota-prod; REGION=us-west1
. "{GCP / '_serving_revision.sh'}"
serving_image worker && echo UNEXPECTED_SUCCESS || echo REFUSED
'''
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert "REFUSED" in done.stdout + done.stderr, (
        f"two revisions both at 100% produced a single answer: {done.stdout}{done.stderr}"
    )


def test_split_traffic_is_refused(tmp_path):
    """Two revisions sharing traffic have no single answer, and a lingering 0%-traffic candidate
    is exactly the half-finished state these scripts must not paper over."""
    binn = _stub_gcloud(tmp_path)
    split = (binn / "gcloud").read_text().replace('"percent":100', '"percent":50').replace(
        '"percent":0,"tag":"c-x"', '"percent":50')
    (binn / "gcloud").write_text(split)
    script = f'''
set -euo pipefail
GCLOUD="{binn / 'gcloud'}"; PROJECT=pivota-prod; REGION=us-west1
. "{GCP / '_serving_revision.sh'}"
serving_image worker && echo UNEXPECTED_SUCCESS || echo REFUSED
'''
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert "REFUSED" in done.stdout + done.stderr


def test_a_tagged_serving_revision_is_still_found(tmp_path):
    """THE BUG THIS CONSOLIDATION ACTUALLY FIXED, and it was live.

    `deploy_backend.sh` ships every candidate as `--tag c-<sha> --no-traffic`, health-checks
    it, then promotes — and the promoted revision KEEPS that tag until a later sweep. So the
    100%-traffic entry normally carries a tag. `setup_store_audit_commerce_jobs.sh` filtered
    with `and not x.get("tag")`, matched nothing, and exited 2 ("web needs exactly one untagged
    100%-traffic revision") on every run against real production. Measured 2026-09-06:
    web-00560-caw served 100% carrying tag c-d222cb8c4a51.

    setup_scheduler.sh had already removed that conjunct and written down why. The sibling copy
    kept it — which is precisely the drift one definition exists to prevent.
    """
    binn = _stub_gcloud(tmp_path)
    tagged = (binn / "gcloud").read_text().replace(
        '{"revisionName":"rev-live","percent":100}',
        '{"revisionName":"rev-live","percent":100,"tag":"c-abc123"}')
    (binn / "gcloud").write_text(tagged)
    script = f'''
set -euo pipefail
GCLOUD="{binn / 'gcloud'}"; PROJECT=pivota-prod; REGION=us-west1
. "{GCP / '_serving_revision.sh'}"
serving_revision worker
'''
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "rev-live" in done.stdout, (
        "a tagged 100%-traffic revision was not found. That is the NORMAL post-deploy state "
        f"here, not an edge case: {done.stdout!r} {done.stderr!r}"
    )


def test_a_nameless_hundred_percent_entry_is_refused(tmp_path):
    """An entry at 100 with no revisionName is an inconsistent traffic block. Filtering it out
    and answering with its neighbour would resolve a split by ignoring half of it — so the
    filter keeps such entries and the count check then refuses. This matches the pre-existing
    Store Audit guards, whose semantics the helper had to preserve when it absorbed them."""
    binn = _stub_gcloud(tmp_path)
    broken = (binn / "gcloud").read_text().replace(
        '{"revisionName":"rev-cand","percent":0,"tag":"c-x"}', '{"percent":100}')
    (binn / "gcloud").write_text(broken)
    script = f'''
set -euo pipefail
GCLOUD="{binn / 'gcloud'}"; PROJECT=pivota-prod; REGION=us-west1
. "{GCP / '_serving_revision.sh'}"
serving_revision worker && echo UNEXPECTED_SUCCESS || echo REFUSED
'''
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert "REFUSED" in done.stdout + done.stderr


def test_the_return_contract_holds_without_pipefail(tmp_path):
    """`serving_image` ended in `| head -1`, so the pipeline's status was head's and a failing
    describe returned 0 with empty output — an unreadable service looking clean, which is the
    one thing the header promises cannot happen. Latent while every caller sets pipefail, but
    this is a shared file and a GitHub Actions `run:` step is `bash -e` WITHOUT it."""
    binn = _stub_gcloud(tmp_path)
    script = f'''
set -eu
GCLOUD="{binn / 'gcloud'}"; PROJECT=pivota-prod; REGION=us-west1
. "{GCP / '_serving_revision.sh'}"
serving_image nosuchsvc && echo UNEXPECTED_SUCCESS || echo REFUSED
'''
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert "REFUSED" in done.stdout, (
        f"without pipefail an unreadable service returned success: {done.stdout!r}"
    )


# ── which script asks which question ───────────────────────────────────────────────────


# A template read, in every spelling a script might plausibly use. Matching only the
# `--format=value(spec.template...)` literal banned ONE syntactic form: a mutation audit read
# the same field via `--format=json | python3 -c '...["spec"]["template"]...'` and every case
# stayed green. A ratchet that matches one form permits the others.
_TEMPLATE_READS = ("spec.template", '["template"]', "['template']", '"template"')


def _code_only(path: Path) -> str:
    """Source with comment lines removed.

    Load-bearing, not tidiness: the previous version of these tests searched raw text, so a
    seven-line comment block *explaining* the template read satisfied every assertion. Two
    mutants that switched the real reads to runtime reads, leaving the comments untouched,
    survived — the classification these tests claim to pin was not pinned at all.
    """
    return "\n".join(l for l in path.read_text().splitlines()
                      if not l.lstrip().startswith("#"))


@pytest.mark.parametrize(
    "script, why",
    [
        ("deploy_worker.sh",
         "its rollback target must be a KNOWN-GOOD image; the template can name one that "
         "never became Ready, so rolling 'back' to it rolls forward into the breakage"),
        ("setup_scheduler.sh",
         "its summary line says 'live', and an operator reads it that way"),
        ("setup_store_audit_commerce_jobs.sh",
         "it gates job creation on the revision actually serving web"),
        ("setup_store_audit_ucp_jobs.sh",
         "same gate, same question"),
    ],
)
def test_scripts_asking_what_is_running_use_the_helper(script, why):
    body = _code_only(GCP / script)
    assert "_serving_revision.sh" in body, f"{script} does not source the helper. {why}"
    assert "serving_revision" in body or "serving_image" in body or "serving_env" in body, (
        f"{script} sources the helper but never calls it. {why}"
    )
    hit = [t for t in _TEMPLATE_READS if t in body]
    assert not hit, (
        f"{script} still reads the template ({hit}) for a runtime question. {why}"
    )


@pytest.mark.parametrize(
    "script, why",
    [
        ("deploy_backend.sh",
         "its pool guard predicts what THIS deploy will apply, and --update-env-vars merges "
         "into the template - the serving revision is the config being replaced"),
        ("restore_to_cloudsql.sh",
         "it captures minScale to PUT BACK with `run services update`, which sets the template"),
    ],
)
def test_scripts_asking_what_the_next_deploy_inherits_keep_the_template(script, why):
    """The counterpart, and the reason this module is not "spec.template is banned".

    Asserted against COMMENT-STRIPPED source. Reading raw text made this vacuous: switching
    both real reads to runtime reads while leaving the explanatory comments passed.
    """
    body = _code_only(GCP / script)
    assert any(t in body for t in _TEMPLATE_READS), (
        f"{script} no longer reads the template in its code (only, perhaps, in a comment). {why}"
    )
    assert "serving_revision" not in body and "serving_image" not in body, (
        f"{script} switched to the serving revision. {why}"
    )
    assert "_serving_revision.sh" in (GCP / script).read_text(), (
        f"{script} reads the template but does not explain why, so the next reader 'fixes' it."
    )


def test_the_serving_read_is_defined_once():
    """The whole point of the helper. Every inline re-implementation is a place to drift, and
    one of them HAD drifted — see test_a_tagged_serving_revision_is_still_found."""
    copies = []
    for f in sorted(GCP.glob("*.sh")):
        if f.name == "_serving_revision.sh":
            continue
        if 'percent") == 100' in _code_only(f) or 'percent")==100' in _code_only(f):
            copies.append(f.name)
    assert not copies, (
        f"these scripts carry their own 100%-traffic resolution instead of sourcing the "
        f"helper: {copies}. That is how the `not x.get(\"tag\")` conjunct survived in one "
        f"copy after being removed from another."
    )
