"""`deploy_backend.sh` preserve mode must not propagate a drifted pool size.

preserve mode treats the LIVE service as the source of truth. That is deliberate
and correct — and it means this script's own `POOL_MAX` is never applied, so a
hand edit to the running service becomes permanent and every subsequent deploy
carries it forward.

Measured 2026-08-29 on prod `web`: `DB_POOL_MAX_SIZE` went 6 -> 20 in revision
web-00067-jzz at 17:01:15Z, from something outside this repo (the same edit added
WOOCOMMERCE_WEBHOOK_BASE_URL, which no file here defines). Three later preserve
deploys propagated it, each behaving exactly as designed. Nobody noticed, because
at idle the two configurations are indistinguishable — the pool is PER PROCESS,
so it only bites on scale-out, and what it produces then is pool exhaustion, the
same outage this codebase had that morning.

20 x 20 instances = 400 against a max_connections of 300 shared with worker,
gateway and ~20 Cloud Run Jobs.

Drives the REAL script through the `GCLOUD` injection point against a fake, and
asserts on OUTCOMES (exit status, refusal message) rather than source text — a
source-text assertion would let a rewrite pass while re-breaking the behaviour.
Same approach as tests/test_setup_scheduler_is_safe_to_rerun.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "gcp" / "deploy_backend.sh"


def _gcloud_shim(tmp_path: Path, pool_max: str, max_scale: str) -> Path:
    """A gcloud that answers `services describe` and records nothing else.

    Emits the same `[{'name': ..., 'value': ...}]` shape gcloud's
    `--format='value(...env)'` produces, so the parser under test is exercised
    for real rather than against a convenient fake shape.
    """
    shim = tmp_path / "gcloud"
    env_repr = (
        "{'name': 'PIVOTA_ENV', 'value': 'production'};"
        f"{{'name': 'DB_POOL_MAX_SIZE', 'value': '{pool_max}'}};"
        "{'name': 'DB_POOL_MIN_SIZE', 'value': '2'}"
    )
    shim.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [ "$1" = run ] && [ "$2" = services ] && [ "$3" = describe ]; then
              for a in "$@"; do
                case "$a" in
                  *maxScale*) echo "{max_scale}"; exit 0 ;;
                  *containers*env*) echo "{env_repr}"; exit 0 ;;
                esac
              done
              exit 0
            fi
            echo "SHIM_CALLED $*" >&2
            exit 0
            """
        )
    )
    shim.chmod(0o755)
    return shim


def _run(tmp_path: Path, pool_max: str, max_scale: str, **extra_env):
    env = dict(os.environ)
    env.update(
        {
            "GCLOUD": str(_gcloud_shim(tmp_path, pool_max, max_scale)),
            "CONFIG": "preserve",
            "PROMOTE": "0",
        }
    )
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), "prod", "deadbeefcafe"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_it_refuses_when_the_fleet_ceiling_exceeds_the_budget(tmp_path: Path) -> None:
    """The exact drift that shipped: 20 x 20 = 400 against max_connections 300."""
    result = _run(tmp_path, pool_max="20", max_scale="20")
    combined = result.stdout + result.stderr
    assert result.returncode == 2, (
        f"expected refusal, got {result.returncode}\n{combined[-2000:]}"
    )
    assert "400" in combined, "the refusal must state the computed ceiling"
    # Assert the SHAPE of the remediation, not a literal. Pinning "DB_POOL_MAX_SIZE=6" made this
    # test fail the moment the prod pool was legitimately resized (6 -> 12 after the 2026-08-29
    # wedge), which teaches the next person to edit the assertion rather than think about it. What
    # actually matters is that the remediation it prints would BRING THE FLEET BACK INSIDE BUDGET.
    # Anchored on `--update-env-vars`: the message ALSO prints the drifted live value in its
    # diagnostic ("DB_POOL_MAX_SIZE=20 x maxScale=20"), and a bare search finds that one first —
    # which would assert the drift against itself and pass for the wrong reason.
    match = re.search(r"--update-env-vars\s+DB_POOL_MAX_SIZE=(\d+)", combined)
    assert match, f"the refusal must name a runnable remediation\n{combined[-2000:]}"
    # The remediation must actually REDUCE the drifted pool (live is 20 here). Asserting the
    # script's own constant instead would just re-pin a literal, and multiplying by the injected
    # live maxScale is wrong too: the suggestion assumes the deploy also reasserts the script's
    # maxScale, which is the whole point of preserve mode reasserting shape.
    assert int(match.group(1)) < 20, (
        f"the remediation DB_POOL_MAX_SIZE={match.group(1)} does not reduce the drifted pool"
    )


@pytest.mark.parametrize(
    ("pool_max", "max_scale", "expected"),
    [
        ("6", "20", "pool drift check: 6 x 20 = 120"),   # the shape before 2026-08-29
        ("12", "10", "pool drift check: 12 x 10 = 120"),  # the shape after: same ceiling, 1.7x
    ],
)
def test_a_correct_configuration_passes(tmp_path: Path, pool_max: str, max_scale: str, expected: str) -> None:
    """Both in-budget shapes must deploy.

    A guard that fails closed on the CORRECT configuration is worse than none — it would block
    every deploy and get bypassed permanently within a day. The second row is the shape adopted
    after the 2026-08-29 pool wedge: the ceiling is unchanged at 120 connections, but concurrency
    per instance drops from 80 to 20 so the pool is 1.7x oversubscribed instead of 13x.
    """
    result = _run(tmp_path, pool_max=pool_max, max_scale=max_scale)
    combined = result.stdout + result.stderr
    assert "REFUSING to deploy" not in combined, combined[-2000:]
    assert expected in combined


def test_an_unreadable_value_is_not_a_pass(tmp_path: Path) -> None:
    """Unreadable must fail closed.

    A describe that errors, or a value this parser cannot find, must not wave the
    deploy through — a guard that treats "I could not check" as "fine" is theatre,
    and this repo has shipped that shape before.
    """
    result = _run(tmp_path, pool_max="", max_scale="20")
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined[-2000:]
    # Whitespace-normalised: the script wraps its message across lines for
    # readability, and an assertion that depends on where it happens to wrap
    # would break on a purely cosmetic edit.
    flat = " ".join(combined.lower().split())
    assert "refusing to deploy blind in preserve mode" in flat


def test_the_bypass_is_explicit(tmp_path: Path) -> None:
    """POOL_FLEET_BUDGET=0 disables the guard deliberately.

    An incident sometimes needs a deploy the guard would block. The escape hatch
    has to exist and has to be explicit, so reaching for it is a decision rather
    than an accident.
    """
    result = _run(tmp_path, pool_max="20", max_scale="20", POOL_FLEET_BUDGET="0")
    assert "REFUSING to deploy" not in (result.stdout + result.stderr)
