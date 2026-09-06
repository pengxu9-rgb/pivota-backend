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


# ── which script asks which question ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "script, must_use_serving, why",
    [
        ("deploy_worker.sh", True,
         "its rollback target must be a KNOWN-GOOD image; the template may name one that "
         "never became Ready, so rolling 'back' to it rolls forward into the breakage"),
        ("setup_scheduler.sh", True,
         "its summary line says 'live', and an operator reads it that way"),
    ],
)
def test_scripts_asking_what_is_running_use_the_helper(script, must_use_serving, why):
    body = "\n".join(l for l in (GCP / script).read_text().splitlines()
                     if not l.lstrip().startswith("#"))
    assert "_serving_revision.sh" in body, f"{script} does not source the helper. {why}"
    assert not [l for l in body.splitlines()
                if "spec.template.spec.containers" in l], (
        f"{script} still reads spec.template for a runtime question. {why}"
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
    """The counterpart, and the reason this module is not "spec.template is banned". Blanket-
    replacing these would be a regression, so they are pinned deliberately."""
    text = (GCP / script).read_text()
    assert "spec.template" in text, f"{script} no longer reads the template. {why}"
    assert "_serving_revision.sh" in text, (
        f"{script} reads the template but does not explain why, so the next reader "
        f"'fixes' it. It should reference the helper and say it is asking the other question."
    )
