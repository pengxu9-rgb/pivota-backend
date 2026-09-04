"""The WIF provider's boundary must be the repo's IMMUTABLE id, not its name.

WHY THIS FILE EXISTS. infra/gcp/setup_github_wif.sh configures the only thing
standing between a GitHub OIDC token and a service account holding run.admin on
production. It used to pin `assertion.repository == '<owner>/<name>'`, with a
comment calling the rename hazard "STILL OPEN, deliberately". On 2026-09-04 the
account was renamed and both halves of that hazard landed within minutes:

  * every prod deploy failed at google-github-actions/auth with
    `unauthorized_client: The given credential is rejected by the attribute
    condition` -- the token now carried the new name;
  * GitHub does not reserve a released username, so `gh api users/<old-owner>`
    returned 404 while the provider still trusted that exact name. Registering
    it, creating a repo of the same name and pushing a workflow on main would
    have minted a token this provider accepted.

The script is not runnable in CI (it talks to GCP), but it takes `GCLOUD` and
`GH` from the environment, so it can be driven with fakes and asked what it
WOULD have sent. That is what these tests do: they assert on the exact argv
reaching `gcloud`, which is the thing that actually changes production IAM.

A test that only grepped the source for "repository_id" would pass on a script
that computed the right string and then sent the wrong one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "infra" / "gcp" / "setup_github_wif.sh"

REPO = "pengxu9-rgb/pivota-backend"
REPO_ID = "1075520615"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="needs bash to run the shell script"
)


# A gcloud that answers the two describes the script needs to get to the end,
# claims every service account exists (so the script does not exit early), and
# appends every invocation to $CALLS.
_FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
case "$*" in
  *"projects describe"*)            echo 371394967380 ;;
  *"remove-iam-policy-binding"*)    exit "${REMOVE_RC:-0}" ;;
  *describe*)                       exit 0 ;;
esac
exit 0
"""

_FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
if [ -n "${GH_FAIL:-}" ]; then exit 1; fi
echo "__REPO_ID__"
""".replace("__REPO_ID__", REPO_ID)


def _run(tmp_path: Path, args=(), env_extra=None):
    gcloud = tmp_path / "fake-gcloud"
    gcloud.write_text(_FAKE_GCLOUD)
    gcloud.chmod(0o755)
    gh = tmp_path / "fake-gh"
    gh.write_text(_FAKE_GH)
    gh.chmod(0o755)

    calls = tmp_path / "calls.txt"
    gh_calls = tmp_path / "gh_calls.txt"
    calls.touch()
    gh_calls.touch()

    env = {
        **os.environ,
        "GCLOUD": str(gcloud),
        "GH": str(gh),
        "CALLS": str(calls),
        "GH_CALLS": str(gh_calls),
    }
    env.update(env_extra or {})

    proc = subprocess.run(
        ["bash", str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO_ROOT),
        timeout=60,
    )
    return proc, calls.read_text(), gh_calls.read_text()


def _condition(calls: str) -> str:
    """The --attribute-condition the script sent to gcloud."""
    for line in calls.splitlines():
        if "--attribute-condition=" in line:
            return line.split("--attribute-condition=", 1)[1].split(" --")[0]
    raise AssertionError(f"no attribute-condition was ever sent:\n{calls}")


# ---------------------------------------------------------------------------
# The condition
# ---------------------------------------------------------------------------


def test_condition_pins_the_immutable_repository_id(tmp_path):
    proc, calls, _gh = _run(tmp_path, [REPO, REPO_ID])

    assert proc.returncode == 0, proc.stderr
    cond = _condition(calls)
    assert f"assertion.repository_id == '{REPO_ID}'" in cond, cond


def test_condition_does_not_pin_the_mutable_name(tmp_path):
    """The name must be GONE, not kept beside the id.

    Keeping `assertion.repository == '<name>'` as a second conjunct would leave
    the availability half of the 2026-09-04 incident fully intact: the next
    rename fails that conjunct and every deploy dies again, while adding no
    security the id does not already provide.
    """
    proc, calls, _gh = _run(tmp_path, [REPO, REPO_ID])

    cond = _condition(calls)
    assert "assertion.repository ==" not in cond, (
        f"the mutable name is still a conjunct -- a rename will break deploys: {cond}"
    )


def test_condition_still_pins_the_branch(tmp_path):
    """The pre-existing control must survive the hardening."""
    proc, calls, _gh = _run(tmp_path, [REPO, REPO_ID])

    assert "assertion.ref == 'refs/heads/main'" in _condition(calls)


def test_repository_id_is_mapped_so_the_binding_can_key_on_it(tmp_path):
    """A principalSet on attribute.repository_id only resolves if the provider
    maps that attribute; without the mapping the binding silently matches
    nothing and every deploy fails auth."""
    proc, calls, _gh = _run(tmp_path, [REPO, REPO_ID])

    mapping = [l for l in calls.splitlines() if "--attribute-mapping=" in l]
    assert mapping, calls
    assert "attribute.repository_id=assertion.repository_id" in mapping[0]


# ---------------------------------------------------------------------------
# The IAM binding
# ---------------------------------------------------------------------------


def _members(calls: str, verb: str) -> list[str]:
    out = []
    for line in calls.splitlines():
        if f"{verb}-iam-policy-binding" in line and "--member=" in line:
            out.append(line.split("--member=", 1)[1].split(" --")[0])
    return out


def test_impersonation_is_granted_to_the_id_keyed_principal(tmp_path):
    proc, calls, _gh = _run(tmp_path, [REPO, REPO_ID])

    added = _members(calls, "add")
    assert any(f"attribute.repository_id/{REPO_ID}" in m for m in added), added


def test_the_name_keyed_binding_is_actively_removed(tmp_path):
    """Adding the id-keyed member does NOT retire the name-keyed one --
    add-iam-policy-binding only adds. A surviving
    `attribute.repository/<owner>/<name>` member keeps granting impersonation to
    whoever ends up owning that name, which is exactly the 2026-09-04 exposure.
    """
    proc, calls, _gh = _run(tmp_path, [REPO, REPO_ID])

    removed = _members(calls, "remove")
    assert any(f"attribute.repository/{REPO}" in m for m in removed), (
        f"the stale name-keyed binding is never removed: {removed}"
    )


def test_a_missing_stale_binding_is_not_an_error(tmp_path):
    """Idempotence: on a fresh project there is no name-keyed binding to
    retire, and `remove-iam-policy-binding` exits non-zero for that. Under
    `set -e` an unguarded call would abort the script before it granted the
    deploy roles below it."""
    proc, calls, _gh = _run(tmp_path, [REPO, REPO_ID], {"REMOVE_RC": "1"})

    assert proc.returncode == 0, (
        f"a missing stale binding aborted the run:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "nothing to retire" in proc.stdout


# ---------------------------------------------------------------------------
# Resolving the id
# ---------------------------------------------------------------------------


def test_the_id_is_resolved_from_gh_when_omitted(tmp_path):
    proc, calls, gh_calls = _run(tmp_path, [REPO])

    assert proc.returncode == 0, proc.stderr
    assert f"repos/{REPO}" in gh_calls, gh_calls
    assert f"assertion.repository_id == '{REPO_ID}'" in _condition(calls)


def test_it_refuses_to_configure_a_provider_when_the_id_is_unknown(tmp_path):
    """The failure that matters: no `gh`, no argument. Configuring the provider
    anyway would mean falling back to a name-only boundary -- shipping the hole
    this change exists to close -- so it must exit non-zero having sent gcloud
    nothing.
    """
    proc, calls, _gh = _run(tmp_path, [REPO], {"GH_FAIL": "1"})

    assert proc.returncode != 0, "configured a provider with no id pin"
    assert "--attribute-condition=" not in calls, (
        "a provider was configured despite an unresolvable id"
    )
    assert "gh api repos/" in proc.stderr, proc.stderr


@pytest.mark.parametrize("bad", ["", "not-a-number", "12x", "1075620615'"])
def test_a_non_numeric_id_is_rejected(tmp_path, bad):
    """The id is interpolated into a CEL expression inside single quotes, the
    same injection surface the repo-name shape check already guards. A digits-
    only check is what keeps `1' || true || '` out of the condition."""
    proc, calls, _gh = _run(tmp_path, [REPO, bad], {"GH_FAIL": "1"})

    assert proc.returncode != 0, f"accepted id {bad!r}"
    assert "--attribute-condition=" not in calls


def test_the_repo_name_shape_guard_still_holds(tmp_path):
    """Pre-existing control, re-pinned because this change edits the lines
    around it: a repo argument that closes the CEL quote must be refused."""
    proc, calls, _gh = _run(tmp_path, ["a/b' || true || '", REPO_ID])

    assert proc.returncode != 0
    assert "--attribute-condition=" not in calls
