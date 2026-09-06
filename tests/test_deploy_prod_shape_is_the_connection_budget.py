"""The prod service SHAPE the deploy applies is a connection budget, and must not regress.

prod `web` wedged its database pool twice in one week — 2026-08-28 (~6h) and 2026-08-29 (~1h, 310
timeouts). Every error in both was `PoolCheckoutTimeout: timed out waiting 120.0s for a database
connection`, while Cloud SQL sat RUNNABLE at 28/300 backends with 23 IDLE and traffic ran at
~0.03 req/s. Shape, not load: 80 concurrent requests against a pool of 6.

`deploy_backend.sh` is the source of truth for shape — its own comment warns that a value widened
by hand "will have it pulled back silently by the next CI deploy", which is exactly what happened
to the first fix. So the fix lives in the script, and this file holds it there: before it, reverting
POOL_MAX to 6 or CONCURRENCY to 80 broke no test in the repo.

HOW THIS ASSERTS, and where it does not:
  - The two `deploy_argv` tests assert OUTCOMES — the arguments the script really passes to
    `gcloud run deploy`, captured through the same GCLOUD injection point the pool-drift test uses.
  - The budget/floor tests necessarily read the script's CONSTANTS, because the constants ARE the
    contract. That is source parsing, and it is brittle to reformatting (quoting, line
    continuations, renaming the case arm); `_prod_constants` raises with an explicit message rather
    than a bare AttributeError so the next person knows what broke and why.
  - POOL_FLEET_BUDGET is READ from the script, never duplicated. A copy here could drift, and then
    this guard would be asserting against a stale version of the very budget it guards.
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

# Post-incident floors. These are REGRESSION PINS, not derived constants — say so rather than
# dress them up. 2026-08-29: pool 6 with concurrency 80 wedged production twice, so the shape
# adopted after it is 12/20. Moving either past these needs a deliberate edit here and a reason.
POOL_MAX_FLOOR = 12
CONCURRENCY_CEILING = 20


def _script_source() -> str:
    return SCRIPT.read_text()


def _prod_constants() -> dict[str, int]:
    """Parse the prod case arm. Raises with context — a bare regex None here is unreadable."""
    source = _script_source()
    line = next(
        (ln for ln in source.splitlines() if ln.strip().startswith("prod)")), None
    )
    assert line, "no `prod)` case arm in deploy_backend.sh — the shape contract moved; update this test"
    out: dict[str, int] = {}
    for name in ("MAX", "POOL_MAX", "CONCURRENCY"):
        match = re.search(rf"\b{name}=\"?(\d+)\"?", line)
        assert match, f"could not read {name} from the prod case arm:\n  {line.strip()}"
        out[name] = int(match.group(1))
    return out


def _fleet_budget() -> int:
    """The script's OWN budget, not a copy of it."""
    match = re.search(r'POOL_FLEET_BUDGET="\$\{POOL_FLEET_BUDGET:-(\d+)\}"', _script_source())
    assert match, "could not read POOL_FLEET_BUDGET's default from deploy_backend.sh"
    return int(match.group(1))


def _shim(tmp_path: Path) -> Path:
    shim = tmp_path / "gcloud"
    shim.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [ "$1" = run ] && [ "$2" = services ] && [ "$3" = describe ]; then
              for a in "$@"; do
                case "$a" in
                  *maxScale*) echo "10"; exit 0 ;;
                  *containers*env*) echo "{'name': 'PIVOTA_ENV', 'value': 'production'};{'name': 'DB_POOL_MAX_SIZE', 'value': '12'};{'name': 'DB_POOL_MIN_SIZE', 'value': '2'}"; exit 0 ;;
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


def _deploy_argv(tmp_path: Path, env_name: str, extra_env: dict | None = None) -> list[str]:
    """Capture the real `gcloud run deploy` argv for an environment.

    A MINIMAL env, deliberately: the script honours MAX_INSTANCES/MIN_INSTANCES/POOL_FLEET_BUDGET
    from the environment, so inheriting os.environ wholesale makes these tests fail on a machine
    that happens to export one of them.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "GCLOUD": str(_shim(tmp_path)),
        "CONFIG": "preserve",
        "PROMOTE": "0",
    }
    env.update(extra_env or {})
    result = subprocess.run(
        ["bash", str(SCRIPT), env_name, "deadbeefcafe"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    combined = result.stdout + result.stderr
    line = next(
        (ln for ln in combined.splitlines() if ln.startswith("SHIM_CALLED run deploy")), None
    )
    assert line, f"`{env_name}` never reached `gcloud run deploy`\n{combined[-3000:]}"
    return line.split()


def _flag(argv: list[str], name: str) -> str:
    assert name in argv, f"{name} is not passed to gcloud run deploy"
    return argv[argv.index(name) + 1]


def test_prod_deploys_the_concurrency_the_script_declares(tmp_path: Path) -> None:
    """Wiring: the per-env constant must actually reach gcloud, not a hardcoded literal."""
    argv = _deploy_argv(tmp_path, "prod")
    assert _flag(argv, "--concurrency") == str(_prod_constants()["CONCURRENCY"])
    assert _flag(argv, "--max-instances") == str(_prod_constants()["MAX"])


def test_staging_still_deploys(tmp_path: Path) -> None:
    """The staging arm needs its own CONCURRENCY or `set -u` aborts EVERY staging deploy.

    Deleting `CONCURRENCY=80` from the staging line left the whole suite green while
    `deploy_backend.sh staging <tag>` died with `CONCURRENCY: unbound variable`.
    """
    argv = _deploy_argv(tmp_path, "staging")
    concurrency = _flag(argv, "--concurrency")
    assert concurrency.isdigit() and int(concurrency) > 0, (
        f"staging deploys with a non-numeric --concurrency {concurrency!r}"
    )


def test_the_fleet_ceiling_stays_inside_the_scripts_own_budget() -> None:
    """max-instances x pool must fit the budget THIS SCRIPT enforces (read, never duplicated)."""
    prod = _prod_constants()
    budget = _fleet_budget()
    ceiling = prod["MAX"] * prod["POOL_MAX"]
    assert ceiling <= budget, (
        f"prod fleet ceiling {prod['MAX']} x {prod['POOL_MAX']} = {ceiling} "
        f"exceeds the script's own {budget}-connection budget"
    )


def test_the_pool_has_not_regressed_below_the_post_incident_floor() -> None:
    """A floor on POOL_MAX is the property the outage actually implies.

    An instance wedges once concurrent stalled connections reach POOL_MAX — the pool size is the
    thing that decides how many stalls it survives, independent of concurrency. Pinning a ratio
    instead (an earlier draft used concurrency/pool <= 2) had no derivation: it was chosen because
    it happened to kill a POOL_MAX=6 mutant, staging violates it at 10x, and it would block
    legitimate shapes like a large concurrency over cheap cached routes.
    """
    pool_max = _prod_constants()["POOL_MAX"]
    assert pool_max >= POOL_MAX_FLOOR, (
        f"prod POOL_MAX={pool_max} is below the post-incident floor of {POOL_MAX_FLOOR}; "
        "6 wedged production twice on 2026-08-28 and 2026-08-29"
    )


def test_concurrency_has_not_regressed_above_the_post_incident_ceiling() -> None:
    concurrency = _prod_constants()["CONCURRENCY"]
    assert concurrency <= CONCURRENCY_CEILING, (
        f"prod CONCURRENCY={concurrency} exceeds the post-incident ceiling of "
        f"{CONCURRENCY_CEILING}; 80 against a pool of 6 wedged production twice"
    )


# ── the shape overrides, added 2026-09-05 with proof-issuer's auto-deploy ───────────────────────
# deploy_backend.sh now honours CPU_LIMIT / MEMORY_LIMIT / CONCURRENCY_LIMIT, because the
# constants above are WEB's connection budget and every OTHER service deployed through this script
# was silently adopting them. proof-issuer had been running concurrency 80 / maxScale 20 since
# 2026-08-27, so an unpinned automatic roll would have cut it to 20 / 10 — an eightfold capacity
# change as a side effect of shipping a commit.
#
# That mechanism is also, unavoidably, a way to set `web`'s concurrency to 80 without touching the
# constants these tests guard. So it gets a fence: the override exists for the services the budget
# above is NOT about, and `web` must keep answering to the constants.

WORKFLOW = REPO / ".github" / "workflows" / "deploy-prod.yml"
SHAPE_OVERRIDES = ("CPU_LIMIT", "MEMORY_LIMIT", "CONCURRENCY_LIMIT",
                   "MIN_INSTANCES", "MAX_INSTANCES")


def test_the_web_deploy_does_not_override_the_shape_it_is_budgeted_for() -> None:
    """Every test above measures the CONSTANTS. If the `web` job passed CONCURRENCY_LIMIT=80,
    all of them would still pass and production would still be running the shape that wedged
    twice — the guard would be measuring a number the deploy no longer uses."""
    import yaml

    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["deploy"]
    body = yaml.dump(job)
    # BOTH SPELLINGS. `VAR=value cmd` inline in a `run:` and a YAML `env:` mapping
    # (`CONCURRENCY_LIMIT: 80`) reach deploy_backend.sh identically, and checking only the
    # first let the second through — restoring the shape that wedged production twice, with
    # this test green. A ratchet matching one syntactic form permits the others. Found by a
    # mutation audit, 2026-09-05.
    for override in SHAPE_OVERRIDES:
        assert f"{override}:" not in body, (
            f"the `web` deploy job sets {override} as an `env:` key, which reaches "
            "deploy_backend.sh exactly as the inline form does and bypasses the connection "
            "budget every other test in this module pins."
        )
        assert f"{override}=" not in body, (
            f"the `web` deploy job sets {override}, bypassing the connection budget that "
            f"every other test in this module is pinning. web's shape lives in "
            f"{SCRIPT.name}'s constants, where a change to it is reviewed against the "
            "2026-08-28/29 outages."
        )


def test_an_override_reaches_the_deploy_when_it_is_given(tmp_path: Path) -> None:
    """The counterpart, and the reason the test above is not vacuous: the mechanism has to
    actually work, or `web` is 'safe' only because the flags do nothing and proof-issuer is
    being silently reshaped after all."""
    argv = _deploy_argv(tmp_path, "prod", {
        "CONCURRENCY_LIMIT": "80", "MAX_INSTANCES": "20",
        "CPU_LIMIT": "2", "MEMORY_LIMIT": "4Gi",
        # The preserve-mode pool guard multiplies the LIVE pool by the maxScale this deploy
        # would apply, and the shim reports a live pool. 20 instances trips the default
        # budget, which is correct behaviour and not what this case is measuring.
        "POOL_FLEET_BUDGET": "0",
    })
    # ALL of them. Asserting only two left --cpu and --memory unplumbed-and-unnoticed; they
    # are harmless today only because proof-issuer's values coincide with web's constants,
    # which the workflow's own comment calls a coincidence. Found by a mutation audit.
    for flag, want in (("--concurrency", "80"), ("--max-instances", "20"), ("--cpu", "2"),
                       ("--memory", "4Gi"), ("--min-instances", "2")):
        assert _flag(argv, flag) == want, (
            f"the override for {flag} did not reach the deploy (got {_flag(argv, flag)!r}, "
            f"want {want!r}): {argv}"
        )
