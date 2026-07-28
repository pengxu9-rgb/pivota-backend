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
    Measured across all three repos when this landed: 180 total interpolations,
    of which 113 were free-form and 67 were constrained (65 boolean + 2 choice) —
    flagging the constrained ones would have tripled the noise for zero security
    value.
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

# LAUNDERED REFERENCES — the same hazard, one level of indirection away, and
# therefore invisible to the `inputs.` matcher above.
#
# The `env.` case is the one that matters, because it is the mistake THIS GATE'S
# OWN ADVICE invites: told to "pass it through env:", someone adds the `env:`
# entry and then writes `${{ env.VAR }}` in the body instead of `"$VAR"`. That is
# byte-for-byte as exploitable as the original bug — the expression engine still
# substitutes the value as text before the shell runs. `$VAR` is the fix;
# `${{ env.VAR }}` is the bug wearing the fix's clothes.
#
# Found by adversarial review, not by imagination: five bypasses were planted
# against the first version of this gate and only `github.event.inputs.*` was
# caught. None of these shapes exist in the repo today; they are blocked so the
# gate cannot be satisfied by moving the problem rather than solving it.
_LAUNDERED = (
    (re.compile(r"\benv\."), "`${{ env.X }}` in a run body is still textual "
                             'substitution — reference the variable as "$X"'),
    (re.compile(r"\bsteps\.[A-Za-z0-9_-]+\.outputs\."),
     "a step output can carry an unvalidated input — route it through `env:` "
     'and reference it as "$X"'),
    (re.compile(r"\btoJSON\s*\(\s*(?:inputs|github\.event)\b"),
     "toJSON() of an input/event context dumps every free-form value into the "
     "shell"),
    (re.compile(r"\binputs\s*\["), "index syntax reaches the same free-form "
                                   "inputs as `inputs.x`"),
)

# Contexts an outsider can influence WITHOUT repo write access. None appear in a
# run: body in any of the three repos today, and none ever should: unlike the
# dispatch inputs above, these need no privilege at all to set, so this arm
# guards genuine unauthenticated RCE rather than privilege escalation.
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
    """Declared type of every input a `run:` body could reference.

    Reads BOTH `workflow_dispatch` and `workflow_call`. `workflow_call` has no
    instance in these repos today, but its inputs land in the same `inputs.`
    namespace — so omitting it would classify a perfectly safe `type: boolean`
    caller input as UNDECLARED and fail the gate on it. A gate that cries wolf
    gets deleted, which is the failure mode that matters most here.

    Missing `type:` defaults to `string`, matching Actions' own default: an input
    with no declared type IS free-form, so treating it as such is correct rather
    than merely conservative.
    """
    # PyYAML parses the bare key `on:` as the boolean True.
    trigger = doc.get(True) if True in doc else doc.get("on")
    if not isinstance(trigger, dict):
        return {}
    types = {}
    for key in ("workflow_dispatch", "workflow_call"):
        section = trigger.get(key)
        if not isinstance(section, dict):
            continue
        declared = section.get("inputs")
        if not isinstance(declared, dict):
            continue
        for name, spec in declared.items():
            declared_type = (spec or {}).get("type", "string")
            # If the same name is declared on both triggers, the free-form
            # declaration wins — the gate must reason about the weakest guarantee.
            if types.get(name) in ("string", "UNDECLARED"):
                continue
            types[name] = declared_type
    return types


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
            where = f"{workflow.name} :: job={job_name} :: step={step_name}"

            for name in _INPUT_REF.findall(expression):
                declared = types.get(name, "UNDECLARED")
                if declared in ("string", "UNDECLARED"):
                    offenders.append(
                        f"{where} :: ${{{{ {expression} }}}} "
                        f"(inputs.{name} is {declared})"
                    )

            for pattern, why in _LAUNDERED:
                if pattern.search(expression):
                    offenders.append(f"{where} :: ${{{{ {expression} }}}} — {why}")

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


# ---- the gate's own logic, tested directly ------------------------------------
# The parametrised tests above only ever see workflows that PASS, so on a clean
# tree they cannot distinguish "correct" from "inert". These exercise the
# classifier on both answers.


def _doc(trigger_yaml):
    return yaml.safe_load(trigger_yaml)


def test_declared_types_reads_workflow_dispatch():
    doc = _doc(
        """
on:
  workflow_dispatch:
    inputs:
      free: {type: string}
      flag: {type: boolean}
      untyped: {description: no type key}
"""
    )
    types = _declared_input_types(doc)
    assert types["free"] == "string"
    assert types["flag"] == "boolean"
    # Actions defaults a type-less input to string, i.e. free-form. Treating it
    # as such is correctness, not caution.
    assert types["untyped"] == "string"


def test_declared_types_also_reads_workflow_call():
    """Omitting workflow_call would classify a safe boolean caller input as
    UNDECLARED and fail the gate on it — a false positive gets a gate deleted."""
    doc = _doc(
        """
on:
  workflow_call:
    inputs:
      flag: {type: boolean}
"""
    )
    assert _declared_input_types(doc)["flag"] == "boolean"


def test_free_form_declaration_wins_when_a_name_is_declared_twice():
    """Reason about the weakest guarantee, not the first one seen."""
    doc = _doc(
        """
on:
  workflow_dispatch:
    inputs:
      dual: {type: string}
  workflow_call:
    inputs:
      dual: {type: boolean}
"""
    )
    assert _declared_input_types(doc)["dual"] == "string"


def test_on_key_parsed_as_boolean_true_is_handled():
    """PyYAML turns the bare key `on:` into True. If this regressed, every input
    would read as UNDECLARED and the whole gate would fail closed on valid
    workflows — noisy, and it would get switched off."""
    doc = _doc("on:\n  workflow_dispatch:\n    inputs:\n      flag: {type: boolean}\n")
    assert True in doc  # the quirk itself, pinned
    assert _declared_input_types(doc)["flag"] == "boolean"


def test_laundered_patterns_match_what_they_claim():
    hits = lambda expr: [why for pat, why in _LAUNDERED if pat.search(expr)]
    assert hits("env.LEAK")
    assert hits("steps.build.outputs.tag")
    assert hits("toJSON(inputs)")
    assert hits("inputs['limit']")
    # and must NOT fire on the correct shapes
    assert not hits("github.run_id")
    assert not hits("inputs.limit")  # handled by the type-aware check instead
    assert not hits("needs.build.result")
