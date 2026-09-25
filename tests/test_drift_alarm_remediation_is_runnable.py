"""The drift alarm's remediation text is read at the worst possible moment — and it rots.

WHY THIS EXISTS. `prod-deploy-drift.yml` fires when production is behind main and prints
the commands to fix it. Nothing asserted those commands still worked, so twice in one week
they did not: the worker line said `WORKERS=true PAUSED=0 setup_scheduler.sh prod <sha>`
(pre-#1903 phrasing; WORKERS is inert outside CONFIG=apply, PAUSED no longer means that),
then #2088 corrected it to a raw `gcloud run services update`, and #2087 deleted the whole
step and rewrote the footer — leaving #2088 and #1915 both open against text that no
longer existed. Meanwhile the rewritten web line read `deploy_backend.sh prod <sha>`, whose
default is CONFIG=apply: rewrite env + secrets from ported files that cannot be regenerated
since Railway was decommissioned 2026-08-22 (a stale copy is how five variables were wiped
once). An operator following the printed fix at 3am either gets exit 2 or a wiped service.

A comment cannot hold this: the failure is text drifting away from the tooling it describes
while everything stays green. So this module reads the footer as COMMANDS and checks each
one against the thing it invokes:

  * the script exists and is executable;
  * every KEY=VALUE it passes is a variable the script actually reads — an unread pin
    protects nothing, and the proof-issuer shape pins are the reason that job is safe to
    run at all (deploy_backend.sh's constants are WEB's budget);
  * the environment argument is one the script's own `case` accepts;
  * nothing it invokes is on the list of forms known to exit non-zero;
  * and, the strongest one: for each service the manual line pins EXACTLY what
    deploy-prod.yml passes when it ships the same service. The pipeline is the path that
    is exercised on every merge; the footer is the path nobody runs until it matters. If
    they can differ, the footer is the one that is wrong.

Prose in the footer may NAME a dead command to say "not this one"; only invocations count.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
DRIFT = WORKFLOWS / "prod-deploy-drift.yml"
DEPLOY = WORKFLOWS / "deploy-prod.yml"

_ASSIGN = re.compile(r"^[A-Z_][A-Z0-9_]*=")
_SERVICE_LABELS = {"web", "worker", "proof-issuer"}


# ── extraction ─────────────────────────────────────────────────────────────────────────


def _run_bodies(workflow: Path) -> List[str]:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    out: List[str] = []
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                out.append(step["run"])
    return out


def _drift_script() -> str:
    bodies = _run_bodies(DRIFT)
    assert len(bodies) == 1, f"expected one `run:` step in the drift job, found {len(bodies)}"
    return bodies[0]


def _remediation_lines() -> List[str]:
    """The heredoc the alarm prints AFTER `::error::drifted:` — the operator's fix."""
    lines = _drift_script().splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if "::error::drifted:" in ln), None,
    )
    assert start is not None, "the drift step no longer announces `::error::drifted:`"
    open_at = next(
        (i for i in range(start, len(lines)) if re.search(r"cat\s+<<\s*'?EOF'?", lines[i])),
        None,
    )
    assert open_at is not None, "the drifted branch prints no remediation heredoc"
    body: List[str] = []
    for ln in lines[open_at + 1:]:
        if ln.strip() == "EOF":
            break
        body.append(ln.rstrip())
    else:  # pragma: no cover — a heredoc that never closes is a broken workflow
        raise AssertionError("remediation heredoc never closes")
    assert len([ln for ln in body if ln.strip()]) >= 5, (
        "the remediation heredoc is implausibly short — extraction broke, and every "
        "assertion below would be measuring nothing"
    )
    return body


def _join_continuations(lines: List[str]) -> List[str]:
    joined: List[str] = []
    buf: List[str] = []
    for ln in lines:
        s = ln.strip()
        if s.endswith("\\"):
            buf.append(s[:-1].strip())
            continue
        buf.append(s)
        joined.append(" ".join(p for p in buf if p))
        buf = []
    if buf:
        joined.append(" ".join(p for p in buf if p))
    return joined


def _is_invocation(line: str) -> bool:
    """A line that RUNS something, as opposed to prose about it."""
    return "infra/gcp/" in line or line.startswith("gcloud ")


class Invocation:
    def __init__(self, raw: str):
        self.raw = raw
        tokens = shlex.split(raw)
        self.label = None
        if tokens and tokens[0] in _SERVICE_LABELS:
            self.label = tokens.pop(0)
        self.env: Dict[str, str] = {}
        while tokens and _ASSIGN.match(tokens[0]):
            k, _, v = tokens.pop(0).partition("=")
            self.env[k] = v
        assert tokens, f"no command after the assignments in: {raw}"
        self.command = tokens[0]
        self.args = tokens[1:]

    @property
    def service(self) -> str:
        if self.label:
            return self.label
        if "SERVICE" in self.env:
            return self.env["SERVICE"]
        if self.command.endswith("deploy_worker.sh"):
            return "worker"
        if self.command.endswith("deploy_backend.sh"):
            return "web"
        return self.command

    @property
    def repo_script(self) -> Path | None:
        return REPO / self.command if self.command.startswith("infra/gcp/") else None


def _remediation_invocations() -> List[Invocation]:
    inv = [Invocation(l) for l in _join_continuations(_remediation_lines()) if _is_invocation(l)]
    assert len(inv) >= 3, (
        f"expected at least the three service roll lines, found {len(inv)}: "
        f"{[i.raw for i in inv]}"
    )
    return inv


def _pipeline_invocations() -> Dict[str, Invocation]:
    """What deploy-prod.yml actually runs per service, keyed by service."""
    found: Dict[str, Invocation] = {}
    for body in _run_bodies(DEPLOY):
        for line in _join_continuations(body.splitlines()):
            if not re.search(r"infra/gcp/deploy_(backend|worker)\.sh\s+prod\b", line):
                continue
            # A pipeline line is `VAR=... \ script prod "$SHA"` — the join above put the
            # assignments and the script on one line.
            inv = Invocation(line)
            assert inv.service not in found, f"deploy-prod.yml ships {inv.service} twice"
            found[inv.service] = inv
    assert set(found) == _SERVICE_LABELS, (
        f"deploy-prod.yml ships {sorted(found)}; expected {sorted(_SERVICE_LABELS)} — "
        f"update _SERVICE_LABELS and the footer together if a service was added or removed"
    )
    return found


# ── the commands exist and take the arguments they are given ──────────────────────────


def test_every_script_the_remediation_invokes_exists_and_is_executable():
    for inv in _remediation_invocations():
        script = inv.repo_script
        if script is None:
            # `gcloud builds submit --config <file>`: the config must exist too.
            if inv.command == "gcloud" and "--config" in inv.args:
                cfg = REPO / inv.args[inv.args.index("--config") + 1]
                assert cfg.is_file(), f"remediation points at a missing build config: {cfg}"
            continue
        assert script.is_file(), f"remediation invokes a script that does not exist: {inv.raw}"
        assert os.access(script, os.X_OK), f"{inv.command} is not executable as printed"


def test_the_environment_argument_is_one_each_script_accepts():
    """`prod` is an argument the script's own `case "$ENV"` must have an arm for. A
    renamed environment (`production`) prints fine and exits 2."""
    for inv in _remediation_invocations():
        script = inv.repo_script
        if script is None:
            continue
        assert inv.args, f"{inv.command} is invoked with no environment argument"
        env_arg = inv.args[0]
        text = script.read_text(encoding="utf-8")
        assert re.search(rf"^\s*{re.escape(env_arg)}\)", text, re.M), (
            f"{inv.command} has no case arm for '{env_arg}' — the remediation would exit "
            f"at argument parsing"
        )


def test_every_variable_the_remediation_pins_is_one_the_script_reads():
    """An unread KEY=VALUE is a pin that pins nothing. The proof-issuer line exists BECAUSE
    of these: without CPU/MEMORY/CONCURRENCY/MIN/MAX the script applies WEB's constants and
    cuts proof-issuer from concurrency 80 / maxScale 20 to 20 / 10 as a side effect."""
    for inv in _remediation_invocations():
        script = inv.repo_script
        if script is None or not inv.env:
            continue
        text = script.read_text(encoding="utf-8")
        for key in inv.env:
            assert re.search(rf"\$\{{{key}[:\-}}]|\$\{{{key}\b|\${key}\b", text), (
                f"{inv.command} never reads ${key}, yet the remediation pins {key}="
                f"{inv.env[key]!r} on it. Either the script lost the knob or the footer "
                f"named the wrong one; both are the drift this test exists to catch."
            )


# ── nothing it prints is a form known to fail ─────────────────────────────────────────


@pytest.mark.parametrize(
    "dead, why",
    [
        ("setup_scheduler.sh", "provisions scheduler triggers and Jobs; it does not roll a "
                               "service, and `WORKERS=true PAUSED=0 setup_scheduler.sh prod` "
                               "was the pre-#1903 phrasing an operator got exit 1 from"),
        ("WORKERS=", "inert outside CONFIG=apply, which is unusable since the Railway "
                     "decommission"),
        ("PAUSED=", "applies only to triggers a run CREATES; existing ones keep their state"),
        ("SERVICE=worker", "deploy_backend.sh refuses SERVICE=worker without an ENV_PREFIX and "
                           "no worker env file exists — deploy_worker.sh is the worker's only "
                           "definition"),
        ("CONFIG=apply", "rewrites env + secrets from env.prod.yaml / secrets.prod.list, "
                         "generated from `railway variables`; Railway was decommissioned "
                         "2026-08-22 and a stale copy wiped five variables once"),
    ],
)
def test_no_invocation_in_the_remediation_is_a_form_known_to_fail(dead, why):
    offenders = [inv.raw for inv in _remediation_invocations() if dead in inv.raw]
    assert not offenders, (
        f"the remediation tells an operator to run {dead!r}, which does not work ({why}):\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_every_deploy_backend_invocation_says_config_preserve_explicitly():
    """The default is CONFIG=apply (see above). deploy_worker.sh defaults to preserve, but
    the footer states it there too so the two lines read the same and an operator who
    copies one to the other does not silently change modes."""
    for inv in _remediation_invocations():
        if inv.command.endswith(("deploy_backend.sh", "deploy_worker.sh")):
            assert inv.env.get("CONFIG") == "preserve", (
                f"{inv.raw!r} does not say CONFIG=preserve; deploy_backend.sh would default "
                f"to CONFIG=apply and rewrite the service's environment from a file that "
                f"cannot be regenerated"
            )


# ── the manual path is the pipeline path ──────────────────────────────────────────────


def _pinned(inv: Invocation) -> Dict[str, str]:
    """KEY=VALUE pairs whose value is a literal. The pipeline passes a few runtime values
    (`PROMOTE="$PROMOTE_FLAG"`) that a hand-typed line cannot carry; those are excluded
    from the comparison rather than asked of the operator."""
    return {k: v for k, v in inv.env.items() if "$" not in v}


@pytest.mark.parametrize("service", sorted(_SERVICE_LABELS))
def test_the_manual_line_for_each_service_pins_exactly_what_the_pipeline_passes(service):
    """The pipeline invocation is exercised on every merge to main; the footer is exercised
    only when the pipeline is the thing that broke. If they disagree, the one nobody runs
    is the one that is wrong — and the disagreement is a REAL production difference: a
    concurrency pin missing from the manual line ships a different service shape."""
    pipeline = _pipeline_invocations()[service]
    manual = [inv for inv in _remediation_invocations() if inv.service == service]
    assert len(manual) == 1, (
        f"expected exactly one remediation line for {service}, found "
        f"{[m.raw for m in manual]}"
    )
    manual_inv = manual[0]
    assert manual_inv.command == pipeline.command, (
        f"{service}: the footer rolls with {manual_inv.command}, the pipeline with "
        f"{pipeline.command}"
    )
    assert _pinned(manual_inv) == _pinned(pipeline), (
        f"{service}: the manual line and deploy-prod.yml pin different things.\n"
        f"  footer   : {_pinned(manual_inv)}\n"
        f"  pipeline : {_pinned(pipeline)}\n"
        f"Change both or neither."
    )
    assert manual_inv.args[0] == pipeline.args[0] == "prod"


def test_the_proof_issuer_shape_pins_are_present_and_numeric():
    """Belt to the parity test's braces: the parity test would pass if BOTH copies dropped
    the shape pins. These are the pins whose absence re-shapes production, so they are
    named here on their own — moving them is a deliberate act, not a copy-paste loss."""
    manual = next(i for i in _remediation_invocations() if i.service == "proof-issuer")
    for key in ("CPU_LIMIT", "MEMORY_LIMIT", "CONCURRENCY_LIMIT", "MIN_INSTANCES", "MAX_INSTANCES"):
        assert key in manual.env, f"proof-issuer remediation lost {key}"
    assert manual.env["CONFIG"] == "preserve"
    assert manual.env["CONCURRENCY_LIMIT"].isdigit() and int(manual.env["CONCURRENCY_LIMIT"]) > 0
    assert manual.env["MAX_INSTANCES"].isdigit() and int(manual.env["MAX_INSTANCES"]) >= int(
        manual.env["MIN_INSTANCES"]
    )
    assert manual.env.get("RUN_COMMAND") and manual.env.get("RUN_ARGS"), (
        "proof-issuer is the same image as web with a DIFFERENT ASGI app; without "
        "RUN_COMMAND/RUN_ARGS the roll boots web's app under proof-issuer's name"
    )
