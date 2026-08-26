"""A GitHub-hosted runner cannot reach the production database. At all.

Cloud SQL `pivota-pg` has NO public IP — `ipv4Enabled=false`, private 10.25.0.2
only. Both DSN secrets (`DATABASE_URL`, `DATABASE_URL_NOVERIFY`) point at that
RFC1918 address. A GitHub-hosted runner is on the public internet with no route
into the VPC, so a scheduled workflow carrying `secrets.DATABASE_URL` is not
merely misconfigured: there is no value that secret could hold that would make
it work.

This was survivable while Railway was up, because the repo secret held Railway's
PUBLIC proxy URL and the workflows quietly kept reading the old platform after
the GCP cutover. Railway was decommissioned 2026-08-25 and every such lane began
failing in `database.connect()` with `ConnectionResetError: [Errno 104]`.

The fix for each is a Cloud Run Job, which runs inside the VPC — see
`infra/gcp/setup_scheduler.sh`. This test is the RATCHET that stops the
population from growing back: a new scheduled DB workflow is a lane that will
fail its first run, and the author should learn that from CI rather than from a
cron-failure email six hours later.

`workflow_dispatch`-only workflows are NOT covered. They are equally unable to
reach the database, but they fail in front of the human who pressed the button,
which is a different (and self-correcting) problem from an unattended cron.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
SWEEP_WORKFLOW = WORKFLOWS / "backend-test-sweep.yml"

# Scheduled lanes that still hold a DSN and are therefore ALREADY BROKEN. This
# list may only ever SHRINK. Each entry is a migration owed, not an exemption
# granted — do not add to it to make a new workflow pass.
#
#   agent-pdp-orphan-reaper.yml         daily 04:37 UTC. Confirmed failing since
#                                       2026-08-26 05:07 (last green 08-25).
#   derive-offer-market-currency.yml    weekly Mon 09:11 UTC. Last ran 08-24,
#                                       while Railway was still up, so it is
#                                       latent rather than red — it fails on its
#                                       next firing.
KNOWN_UNMIGRATED = {
    "agent-pdp-orphan-reaper.yml",
    "derive-offer-market-currency.yml",
}

# Both the dotted and the bracket-index forms, because `${{ secrets['DATABASE_URL'] }}`
# is valid Actions syntax and a plain `secrets.DATABASE_URL` substring match walks
# straight past it. This cannot see a DSN held under a differently-named secret —
# a ratchet, not a proof — but it should at least cover the two spellings of the
# names we actually use.
DSN_SECRET_NAMES = ("DATABASE_URL", "DATABASE_URL_NOVERIFY")
DSN_PATTERN = re.compile(
    r"secrets\s*(?:\.\s*(?:%s)\b|\[\s*['\"](?:%s)['\"]\s*\])"
    % ("|".join(DSN_SECRET_NAMES), "|".join(DSN_SECRET_NAMES))
)


def _workflow_files() -> list[Path]:
    # GitHub honours BOTH extensions. Globbing only *.yml let a scheduled DSN
    # workflow named *.yaml through every assertion in this file — verified by
    # mutant. The sibling guard tests/test_workflow_no_run_body_interpolation.py
    # already globs both; match it.
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def _scheduled_db_workflows() -> set[str]:
    found = set()
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        if not DSN_PATTERN.search(text):
            continue
        workflow = yaml.safe_load(text) or {}
        # `on` parses to the YAML 1.1 boolean True. Accept either key rather
        # than "fixing" the file, which would change the workflow itself.
        triggers = workflow.get("on", workflow.get(True, {})) or {}
        if isinstance(triggers, dict) and triggers.get("schedule"):
            found.add(path.name)
    return found


def test_this_gate_runs_when_a_workflow_changes():
    """A ratchet that is not triggered by its own change class enforces nothing.

    Adding a scheduled DB workflow is, by nature, a diff that touches only
    `.github/`. backend-test-sweep.yml derives its `paths` from which packages
    the swept tests IMPORT, and this test imports nothing — it parses YAML off
    disk. Without `.github/workflows/**` in `paths`, such a PR matches no gate,
    `CI Entrypoint` reports green having run nothing, and the broken lane ships.

    This is the same trap `test_deploy_mounts_every_dropped_dsn.py` documents
    for `infra/**`; it is restated here because the two guards can be edited
    independently and the next person to trim `paths` should meet both.
    """
    workflow = yaml.safe_load(SWEEP_WORKFLOW.read_text(encoding="utf-8")) or {}
    triggers = workflow.get("on", workflow.get(True, {})) or {}
    paths = (triggers.get("pull_request") or {}).get("paths") or []
    assert paths, f"{SWEEP_WORKFLOW} has no pull_request.paths — this would be vacuous"
    subject = str(WORKFLOWS.relative_to(REPO))
    assert any(p.startswith(subject) and p.rstrip("*/") == subject for p in paths), (
        f"{SWEEP_WORKFLOW.name} does not run for changes under '{subject}/', but "
        f"{Path(__file__).name} reads every file in it. A PR adding a scheduled "
        f"workflow with a production DSN would match no gate and go green. Add "
        f"'{subject}/**' to pull_request.paths."
    )


def test_the_scan_finds_something_to_scan():
    """Guard the guard: a glob typo would make every assertion below vacuous."""
    workflows = _workflow_files()
    assert len(workflows) > 5, f"only {len(workflows)} workflows found under {WORKFLOWS}"
    assert any(
        DSN_PATTERN.search(p.read_text(encoding="utf-8")) for p in workflows
    ), "no workflow mentions a DSN secret at all — the matcher is wrong, not the repo"
    # The bracket form must actually match, or the widening above is decorative.
    assert DSN_PATTERN.search("${{ secrets['DATABASE_URL'] }}")
    assert DSN_PATTERN.search('${{ secrets.DATABASE_URL_NOVERIFY }}')
    assert not DSN_PATTERN.search("${{ secrets.DATABASE_URL_SOMETHING_ELSE }}")


def test_no_new_scheduled_workflow_carries_a_database_dsn():
    offenders = _scheduled_db_workflows() - KNOWN_UNMIGRATED
    assert not offenders, (
        "these scheduled workflows carry a production DSN, and a GitHub-hosted "
        f"runner cannot route to Cloud SQL's private IP: {sorted(offenders)}. "
        "The first scheduled run will fail in database.connect(). Migrate the "
        "lane to a Cloud Run Job in infra/gcp/setup_scheduler.sh instead."
    )


@pytest.mark.parametrize("name", sorted(KNOWN_UNMIGRATED))
def test_known_unmigrated_lane_still_exists(name: str):
    """The allowlist must shrink by MIGRATION, not by rot.

    Without this, deleting or renaming one of these files would leave a stale
    name in KNOWN_UNMIGRATED that silently pre-approves a future workflow that
    happens to reuse it.
    """
    assert (WORKFLOWS / name).exists(), (
        f"{name} is in KNOWN_UNMIGRATED but no longer exists. If it was migrated "
        "or deleted, remove it from that set."
    )
    assert name in _scheduled_db_workflows(), (
        f"{name} no longer matches (scheduled + holds a DSN). If it was migrated, "
        "remove it from KNOWN_UNMIGRATED so the ratchet tightens."
    )
