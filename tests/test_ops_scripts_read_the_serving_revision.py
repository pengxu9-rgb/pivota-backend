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
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GCP = REPO / "infra" / "gcp"
SERVING = "a" * 40          # what the 100%-traffic revision runs
TEMPLATE = "b" * 40         # what the template asks for — a deploy that did not take


# The default traffic block: one revision serving, plus a 0%-traffic candidate left behind by a
# deploy that did not take. That split is the whole subject of this module.
_DEFAULT_TRAFFIC = ('{"revisionName":"rev-live","percent":100},'
                    '{"revisionName":"rev-cand","percent":0,"tag":"c-x"}')


def _stub_gcloud(tmp_path: Path, *, armed_serving="true", armed_template="false",
                 traffic: str = _DEFAULT_TRAFFIC, extra_status: str = "",
                 revision_image: str | None = None) -> Path:
    """A `gcloud` whose service TEMPLATE and SERVING revision deliberately disagree.

    `traffic` is DATA, not a string patched into the generated script afterwards. The stub is
    written with shell line-continuations, so a test doing surgery on the combined JSON matched
    nothing, silently exercised the default shape, and reported the opposite of what it
    claimed. Measured: that is exactly what happened to the lone-nameless case.
    """
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
      echo '{{"status":{{{extra_status}"traffic":[{traffic}]}},'\\
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
  # The image on a REVISION is a DIGEST reference (Cloud Run resolves the tag at revision
  # creation), and value() prints EMPTY with rc 0 for a field path that does not resolve — both
  # modelled so the helper's contract is tested against what gcloud does. The digest here is the
  # revision's sha padded to 64 hex, so a test can still tell WHICH revision answered.
  case "$4 $*" in
    "rev-live "*"value(spec.containers[0].image)"*) printf '%s\\n' "{revision_image if revision_image is not None else img + "@sha256:" + SERVING + "0" * 24}"; exit 0 ;;
    "rev-cand "*"value(spec.containers[0].image)"*) printf '%s\\n' "{img}@sha256:{TEMPLATE}{"0" * 24}"; exit 0 ;;
  esac
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
    binn = _stub_gcloud(
        tmp_path,
        traffic='{"revisionName":"rev-live","percent":100},{"revisionName":"rev-cand","percent":100}')
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
    binn = _stub_gcloud(
        tmp_path,
        traffic='{"revisionName":"rev-live","percent":50},{"revisionName":"rev-cand","percent":50}')
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
    binn = _stub_gcloud(
        tmp_path, traffic='{"revisionName":"rev-live","percent":100,"tag":"c-abc123"}')
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


def test_a_lone_nameless_entry_is_refused(tmp_path):
    """The input that pins the NAME clause specifically.

    The two-entry case below is already refused by `len(live) != 1`, so it never exercises the
    name check beside it — two guards closing one door. A single entry at 100 with no
    revisionName and no latestRevision reaches the name clause and nothing else.
    """
    # latestReadyRevisionName IS present, so the only thing standing between this entry and an
    # answer is the `latestRevision` conjunct: a fallback made unconditional would answer
    # rev-live here. Without this field the case could not tell the name clause from the
    # conjunct — two guards, one door.
    binn = _stub_gcloud(tmp_path, traffic='{"percent":100}',
                        extra_status='"latestReadyRevisionName":"rev-live",')
    script = f'''
set -euo pipefail
GCLOUD="{binn / 'gcloud'}"; PROJECT=pivota-prod; REGION=us-west1
. "{GCP / '_serving_revision.sh'}"
serving_revision worker && echo UNEXPECTED_SUCCESS || echo REFUSED
'''
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert "REFUSED" in done.stdout + done.stderr


def test_a_latest_revision_entry_without_a_name_falls_back(tmp_path):
    """The fallback the UCP guard carried, preserved when the helper absorbed it: an entry
    marked `latestRevision` resolves through `status.latestReadyRevisionName`. Dropping it
    would make that script exit 2 forever if the shape ever appeared — the same failure this
    whole change removes from its sibling."""
    binn = _stub_gcloud(tmp_path, traffic='{"percent":100,"latestRevision":true}',
                        extra_status='"latestReadyRevisionName":"rev-live",')
    script = f'''
set -euo pipefail
GCLOUD="{binn / 'gcloud'}"; PROJECT=pivota-prod; REGION=us-west1
. "{GCP / '_serving_revision.sh'}"
serving_revision worker
'''
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "rev-live" in done.stdout, (
        f"the latestRevision fallback was dropped: {done.stdout!r} {done.stderr!r}"
    )


def test_a_nameless_hundred_percent_entry_is_refused(tmp_path):
    """An entry at 100 with no revisionName is an inconsistent traffic block. Filtering it out
    and answering with its neighbour would resolve a split by ignoring half of it — so the
    filter keeps such entries and the count check then refuses. This matches the pre-existing
    Store Audit guards, whose semantics the helper had to preserve when it absorbed them."""
    binn = _stub_gcloud(
        tmp_path, traffic='{"revisionName":"rev-live","percent":100},{"percent":100}')
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
    # THE SECOND gcloud CALL, not the first. `serving_image nosuchsvc` fails inside
    # `serving_revision` and returns before the `revisions describe` pipeline ever runs — so
    # this case passed with `| head -1` restored, which is the exact bug it names. A mutation
    # audit caught that. The service must RESOLVE and its revision must then be undescribable.
    binn = _stub_gcloud(tmp_path, traffic='{"revisionName":"rev-ghost","percent":100}')
    script = f'''
set -eu
GCLOUD="{binn / 'gcloud'}"; PROJECT=pivota-prod; REGION=us-west1
. "{GCP / '_serving_revision.sh'}"
serving_image worker && echo UNEXPECTED_SUCCESS || echo REFUSED
'''
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert "REFUSED" in done.stdout, (
        "without pipefail, a describable service whose REVISION cannot be described returned "
        f"success with empty output: {done.stdout!r}. deploy_worker.sh would read that as "
        "'service absent', take the create path, and deploy with no rollback anchor."
    )


# ── which script asks which question ───────────────────────────────────────────────────


# A template read, in every spelling a script might plausibly use. Matching only the
# `--format=value(spec.template...)` literal banned ONE syntactic form: a mutation audit read
# the same field via `--format=json | python3 -c '...["spec"]["template"]...'` and every case
# stayed green. A ratchet that matches one form permits the others.
# Every way a script might read something OTHER than the serving revision. Not just the
# `spec.template` spellings: `latestCreatedRevisionName` reaches the same wrong answer by a
# different field, and a mutation audit used it to defeat the earlier version of this list.
_TEMPLATE_READS = ("spec.template", '["template"]', "['template']", '"template"',
                   "latestCreatedRevisionName")


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
    # THE SOURCE LINE ITSELF CONTAINS "serving_revision", so searching the whole body for that
    # substring could never fail once the assert above passed — "sources it but never calls it"
    # was undetectable, and a mutant that swapped a call for a raw runtime read while keeping
    # the source line survived. Drop the sourcing line before looking for a CALL.
    calls = "\n".join(l for l in body.splitlines() if "_serving_revision.sh" not in l)
    assert any(f in calls for f in ("serving_revision", "serving_image", "serving_env")), (
        f"{script} sources the helper but never calls it. {why}"
    )
    hit = [t for t in _TEMPLATE_READS if t in body]
    assert not hit, (
        f"{script} still reads the template ({hit}) for a runtime question. {why}"
    )


@pytest.mark.parametrize(
    "script, expected_read, why",
    [
        ("deploy_backend.sh", "value(spec.template.spec.containers[0].env)",
         "the pool guard predicts what THIS deploy will apply, and --update-env-vars merges "
         "into the template - the serving revision is the config being replaced"),
        ("deploy_backend.sh",
         'value(spec.template.metadata.annotations."autoscaling.knative.dev/maxScale")',
         "same guard, the other half of its arithmetic"),
        ("restore_to_cloudsql.sh",
         "value(spec.template.metadata.annotations['autoscaling.knative.dev/minScale'])",
         "it captures minScale to PUT BACK with `run services update`, which sets the template"),
    ],
)
def test_scripts_asking_what_the_next_deploy_inherits_keep_the_template(
        script, expected_read, why):
    """The counterpart, and the reason this module is not "spec.template is banned".

    THE EXACT READ, not "some template read somewhere". deploy_backend.sh has two, so an
    `any(...)` over the file was satisfied by whichever one had not been mutated — switching
    the pool guard alone to `value(status.traffic)` passed. Asserted against comment-stripped
    source, because a seven-line comment explaining the read satisfied the earlier version.
    """
    body = _code_only(GCP / script)
    assert expected_read in body, (
        f"{script} no longer performs `{expected_read}` in its code (only, perhaps, in a "
        f"comment). {why}"
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
    # NOT a search for one spelling of the predicate. `x["percent"] == 100`, single quotes,
    # reversed operands and extra whitespace all evade that, and a mutation audit used the
    # subscript form to smuggle a drifting copy back in. Flag any script that inspects a
    # traffic block AT ALL: outside the helper, none has business doing so.
    copies = []
    for f in sorted(GCP.glob("*.sh")):
        if f.name == "_serving_revision.sh":
            continue
        body = _code_only(f)
        # `percent` AND `revisionName` together = resolving WHICH REVISION is serving, which is
        # the helper's job. Matching one spelling of the predicate was not a ratchet (a mutation
        # audit smuggled a copy back in as `x["percent"] == 100`); matching merely "touches
        # traffic" is too wide the other way — deploy_backend.sh's sweep_stale_tags reads
        # tag/percent to find STALE TAGS, a different question, and flagging it would make this
        # cry wolf until someone deleted it.
        if "percent" in body and "revisionName" in body:
            copies.append(f.name)
    assert not copies, (
        f"these scripts carry their own 100%-traffic resolution instead of sourcing the "
        f"helper: {copies}. That is how the `not x.get(\"tag\")` conjunct survived in one "
        f"copy after being removed from another."
    )


def test_an_empty_image_answer_is_refused_by_the_helper(tmp_path):
    """gcloud's value() projection prints EMPTY with rc 0 when the field path does not
    resolve. The header promises "non-zero when it cannot answer", and deploy_worker.sh read
    an empty answer as "the service does not exist" — a deploy over a live worker with no
    rollback anchor. Empty is not an answer."""
    out = _source_and_call(
        tmp_path,
        'serving_image worker && echo "ANSWERED:[$(serving_image worker)]" || echo REFUSED',
        revision_image="",
    )
    assert "REFUSED" in out and "ANSWERED" not in out, out


def test_the_image_the_helper_returns_is_a_digest_reference(tmp_path):
    """Documented in the helper and relied on by deploy_worker.sh: a revision's image is
    `...@sha256:<64 hex>`. The stub answers that shape, so a caller that derives a commit
    from the reference is caught by the deploy_worker tests, not by production."""
    out = _source_and_call(tmp_path, "serving_image worker")
    digest = out.rsplit("@sha256:", 1)[1] if "@sha256:" in out else ""
    assert len(digest) == 64 and digest.startswith(SERVING), out


# ── the workflow's copy must agree with the helper ─────────────────────────────────────────
# `prod-deploy-drift.yml` cannot source a shell file, so it carries the one remaining inline
# copy of the 100%-traffic predicate, and the helper's header says "change both or neither".
# That was a comment, and the copy promptly drifted (the alarm filtered a nameless entry out and
# answered with its neighbour while the helper refused). This lifts BOTH programs out of their
# files and runs them over one fixture table.

_WORKFLOW = REPO / ".github" / "workflows" / "prod-deploy-drift.yml"


def _helper_predicate() -> str:
    text = (GCP / "_serving_revision.sh").read_text(encoding="utf-8")
    start = text.index("serving_revision(){")
    body = text[start:]
    a = body.index("python3 -c '") + len("python3 -c '")
    b = body.index("' 2>/dev/null", a)
    return body[a:b]


def _workflow_predicate() -> str:
    text = _WORKFLOW.read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if 'rev="$(printf' in l and "python3 -c '" in l)
    a = line.index("python3 -c '") + len("python3 -c '")
    b = line.index("' 2>/dev/null", a)
    return line[a:b]


def _answer(program: str, payload: str) -> str:
    done = subprocess.run([sys.executable, "-c", program], input=payload, capture_output=True,
                          text=True, timeout=30)
    # The helper signals "cannot answer" by exit 1 with nothing printed; the workflow prints
    # nothing and exits 0 (its caller tests emptiness). Both reduce to "the printed name".
    return done.stdout.strip()


@pytest.mark.parametrize(
    "traffic, extra, expect",
    [
        ('[{"revisionName":"A","percent":100}]', "", "A"),
        ('[{"revisionName":"A","percent":100},{"revisionName":"B","percent":0,"tag":"c-x"}]', "", "A"),
        ('[{"revisionName":"A","percent":100,"tag":"c-x"}]', "", "A"),
        ('[{"percent":100,"latestRevision":true}]', '"latestReadyRevisionName":"L",', "L"),
        ('[{"percent":100}]', '"latestReadyRevisionName":"L",', ""),
        ('[{"revisionName":"A","percent":100},{"percent":100}]', "", ""),
        ('[{"revisionName":"A","percent":50},{"revisionName":"B","percent":50}]', "", ""),
        ("[]", "", ""),
        ('[{"revisionName":"A","percent":"100"}]', "", ""),
    ],
)
def test_the_alarm_and_the_helper_resolve_every_traffic_shape_identically(traffic, extra, expect):
    payload = '{"status":{' + extra + '"traffic":' + traffic + "}}"
    helper = _answer(_helper_predicate(), payload)
    workflow = _answer(_workflow_predicate(), payload)
    assert helper == workflow == expect, (
        f"traffic={traffic} extra={extra!r}: helper={helper!r} workflow={workflow!r} "
        f"expected={expect!r}. The alarm and the scripts it watches disagree on what is serving."
    )
