"""deploy_backend.sh under CONFIG=preserve keeps a service's existing allUsers invoker binding.

Staging `web` was made allUsers-invokable on 2026-09-29 (Peng) because the staging gateway calls
/agent/internal/auth/introspect without an ID token. The script defaulted PUBLIC=0 for staging, so
every preserve deploy ran --no-allow-unauthenticated and silently REVOKED that binding, breaking the
gateway. These tests run the script's own PUBLIC-resolution block (extracted verbatim) against a
stubbed gcloud and assert the flag it would pass to `gcloud run deploy`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "infra" / "gcp" / "deploy_backend.sh"

ALL_USERS = {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers", "serviceAccount:x@p.iam"]}]}
PRIVATE = {"bindings": [{"role": "roles/run.invoker", "members": ["serviceAccount:x@p.iam"]}]}
OTHER_ROLE = {"bindings": [{"role": "roles/run.viewer", "members": ["allUsers"]}]}


def _block() -> str:
    """The script's lines from the PUBLIC default through the PUBLIC_FLAG decision."""
    text = SCRIPT.read_text()
    start = text.index('_PUBLIC_EXPLICIT="${PUBLIC+1}"')
    end_match = re.search(r"^\[ \"\$PUBLIC\" = 1 \] && PUBLIC_FLAG=.*$", text[start:], re.M)
    assert end_match, "PUBLIC_FLAG decision line moved"
    return text[start : start + end_match.end()]


def _flag(tmp_path: Path, *, env: str, config: str, policy, public: str | None = None) -> tuple[str, str]:
    stub = tmp_path / "gcloud"
    if policy is None:
        stub.write_text("#!/usr/bin/env bash\necho 'ERROR: not found' >&2\nexit 1\n")
    else:
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in *"run services get-iam-policy"*) ;; *) echo "stub: unexpected: $*" >&2; exit 9 ;; esac\n'
            f"cat <<'JSON'\n{json.dumps(policy)}\nJSON\n"
        )
    stub.chmod(0o755)
    prelude = (
        "set -euo pipefail\n"
        f'ENV="{env}"; CONFIG="{config}"\n'
        f'PROJECT="pivota-{env}"\n'
        f'GCLOUD="{stub}"\n'
        + (f'PUBLIC="{public}"\n' if public is not None else "unset PUBLIC\n")
    )
    program = prelude + _block() + '\necho "FLAG=$PUBLIC_FLAG"\n'
    proc = subprocess.run(["bash", "-c", program], capture_output=True, text=True, env={**os.environ})
    assert proc.returncode == 0, proc.stderr
    flag = re.search(r"^FLAG=(\S+)$", proc.stdout, re.M)
    assert flag, proc.stdout
    return flag.group(1), proc.stderr


def test_preserve_keeps_an_existing_all_users_binding_on_staging(tmp_path):
    flag, err = _flag(tmp_path, env="staging", config="preserve", policy=ALL_USERS)
    assert flag == "--allow-unauthenticated"
    assert "keeps it (PUBLIC=1)" in err


def test_preserve_leaves_a_private_staging_service_private(tmp_path):
    flag, _ = _flag(tmp_path, env="staging", config="preserve", policy=PRIVATE)
    assert flag == "--no-allow-unauthenticated"


def test_all_users_on_another_role_does_not_count(tmp_path):
    flag, _ = _flag(tmp_path, env="staging", config="preserve", policy=OTHER_ROLE)
    assert flag == "--no-allow-unauthenticated"


def test_an_explicit_public_0_still_makes_it_private(tmp_path):
    flag, err = _flag(tmp_path, env="staging", config="preserve", policy=ALL_USERS, public="0")
    assert flag == "--no-allow-unauthenticated"
    assert "keeps it" not in err


def test_config_apply_is_unchanged_and_follows_the_env_default(tmp_path):
    flag, _ = _flag(tmp_path, env="staging", config="apply", policy=ALL_USERS)
    assert flag == "--no-allow-unauthenticated"


@pytest.mark.parametrize("policy", [None, {"bindings": None}, {}])
def test_an_unreadable_or_empty_policy_falls_back_to_the_env_default(tmp_path, policy):
    flag, _ = _flag(tmp_path, env="staging", config="preserve", policy=policy)
    assert flag == "--no-allow-unauthenticated"


def test_prod_is_unchanged(tmp_path):
    flag, _ = _flag(tmp_path, env="prod", config="preserve", policy=PRIVATE)
    assert flag == "--allow-unauthenticated"


# A failed IAM read falls back to the env default (private on staging), which REVOKES an allUsers
# binding the script could not see. It must say so rather than revoke silently.
UNREADABLE_WARNING = "could not read"


@pytest.mark.parametrize("policy", [None, "not a policy object"])
def test_an_unreadable_policy_warns_that_the_deploy_revokes_public_access(tmp_path, policy):
    flag, err = _flag(tmp_path, env="staging", config="preserve", policy=policy)
    assert flag == "--no-allow-unauthenticated"
    assert UNREADABLE_WARNING in err
    assert "REVOKES" in err and "PUBLIC=1" in err


@pytest.mark.parametrize("policy", [ALL_USERS, PRIVATE, OTHER_ROLE, {}, {"bindings": None}])
def test_a_readable_policy_never_warns(tmp_path, policy):
    _, err = _flag(tmp_path, env="staging", config="preserve", policy=policy)
    assert UNREADABLE_WARNING not in err


@pytest.mark.parametrize(
    "env,config,public",
    [("staging", "preserve", "0"), ("staging", "apply", None), ("prod", "preserve", None)],
)
def test_no_iam_read_means_no_warning(tmp_path, env, config, public):
    _, err = _flag(tmp_path, env=env, config=config, policy=None, public=public)
    assert UNREADABLE_WARNING not in err
