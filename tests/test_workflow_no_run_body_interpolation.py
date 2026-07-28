"""No free-form `${{ }}` interpolated into a GitHub Actions `run:` body.

WHY THIS IS A TEST AND NOT A CODE REVIEW HABIT. `${{ }}` inside a `run:` is
TEXTUAL SUBSTITUTION performed before the shell ever parses the script, so a
free-form input value becomes executable shell. Demonstrated on this repo, not
theorised: `derive-offer-market-currency.yml` with `only_domains` set to

    "; echo PWNED; :; "

rendered as `only_raw=""; echo PWNED; :; ""` and executed — in a job whose `env:`
carries the production `DATABASE_URL`.

The rule was ALREADY written down, verbatim, in
`.github/workflows/postgres-dialect-gate.yml` ("`${{ }}` in a `run:` body is
textual substitution before the shell ever sees it… Demonstrated, not
theorised"). It was written once and then not applied to the next workflow
anybody added, which is exactly the failure mode a comment cannot fix and a test
can. This is the second instance of the shape in two days.

SCOPE — deliberately narrow, so this gate does not cry wolf:

  * Only `inputs.*` / `github.event.inputs.*` declared `type: string` (or not
    declared at all) are flagged. GitHub constrains `type: boolean` and
    `type: choice` to values it generates, so those cannot carry a payload.
    Measured across all three repos when this landed: 178 total interpolations,
    of which 113 were free-form and 67 were constrained — flagging the
    constrained ones would have tripled the noise for zero security value.
  * Only `run:` bodies. `if:`, `with:` and `env:` are EXPRESSION contexts,
    evaluated rather than pasted into a shell, and are safe.

THE FIX IS ALWAYS THE SAME: pass the value through the step's `env:` and
reference it as `"${VAR}"`. Never quoting tricks — quoting inside a string that
has already been substituted is too late.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}", re.S)
_INPUT_REF = re.compile(r"\b(?:inputs|github\.event\.inputs)\.([A-Za-z0-9_-]+)")

# Contexts an outsider can influence WITHOUT repo write access. None are used in
# a run: body in this repo today, and none should ever be: unlike the dispatch
# inputs above, these need no privilege at all to set.
_UNTRUSTED = (
    "github.event.issue.",
    "github.event.pull_request.",
    "github.event.comment",
    "github.event.review",
    "github.event.discussion",
    "github.head_ref",
    "github.event.head_commit",
    "github.event.client_payload",
)


def _workflows():
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def _declared_input_types(doc):
    # PyYAML parses the bare key `on:` as the boolean True.
    trigger = doc.get(True) if True in doc else doc.get("on")
    if not isinstance(trigger, dict):
        return {}
    dispatch = trigger.get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return {}
    declared = dispatch.get("inputs")
    if not isinstance(declared, dict):
        return {}
    return {
        name: (spec or {}).get("type", "string") for name, spec in declared.items()
    }


def _run_steps(doc):
    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if isinstance(run, str):
                yield job_name, step.get("name") or f"step#{index}", run


def test_there_are_workflows_to_check():
    """A gate that silently checks nothing is worse than no gate. If the glob
    ever stops matching, fail loudly rather than pass vacuously."""
    assert _workflows(), f"no workflows found under {WORKFLOW_DIR}"


@pytest.mark.parametrize(
    "workflow", _workflows(), ids=lambda p: p.name
)
def test_no_free_form_input_interpolated_into_a_run_body(workflow):
    doc = yaml.safe_load(workflow.read_text())
    if not isinstance(doc, dict):
        pytest.skip(f"{workflow.name} is not a mapping")

    types = _declared_input_types(doc)
    offenders = []
    for job_name, step_name, run in _run_steps(doc):
        for match in _EXPR.finditer(run):
            expression = match.group(1)
            for name in _INPUT_REF.findall(expression):
                declared = types.get(name, "UNDECLARED")
                if declared in ("string", "UNDECLARED"):
                    offenders.append(
                        f"{workflow.name} :: job={job_name} :: step={step_name} :: "
                        f"${{{{ {expression} }}}} (inputs.{name} is {declared})"
                    )

    assert not offenders, (
        "free-form workflow input interpolated into a `run:` body — this is "
        "textual substitution before the shell parses, so the value executes.\n"
        "Pass it through the step's `env:` and reference it as \"${VAR}\" "
        "instead; quoting the interpolation does not help.\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "workflow", _workflows(), ids=lambda p: p.name
)
def test_no_untrusted_context_interpolated_into_a_run_body(workflow):
    """The higher-severity half: contexts settable with NO repo access at all.

    Zero of these exist today (measured across all three repos). This gate is
    here so that stays true — a `pull_request_target` or `issue_comment` workflow
    reading `github.event.*.title` into a `run:` body is remote code execution in
    a job that can hold secrets, not merely a privilege-escalation nuisance.
    """
    doc = yaml.safe_load(workflow.read_text())
    if not isinstance(doc, dict):
        pytest.skip(f"{workflow.name} is not a mapping")

    offenders = []
    for job_name, step_name, run in _run_steps(doc):
        for match in _EXPR.finditer(run):
            expression = match.group(1).lower()
            for pattern in _UNTRUSTED:
                if pattern in expression:
                    offenders.append(
                        f"{workflow.name} :: job={job_name} :: step={step_name} :: "
                        f"${{{{ {match.group(1)} }}}}"
                    )

    assert not offenders, (
        "attacker-controllable context interpolated into a `run:` body — this "
        "needs no repo access to exploit.\n  " + "\n  ".join(offenders)
    )
