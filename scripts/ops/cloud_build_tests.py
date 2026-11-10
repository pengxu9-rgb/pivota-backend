"""Helpers for infra/gcp/cloudbuild.tests.yaml: the backend's SQLite sweep and Postgres dialect gate
on Cloud Build.

THE RUNNER EXECUTES CI'S OWN STEPS. Every test command, preflight, verdict and floor below is the
`run:` body of a step in .github/workflows/backend-test-sweep.yml or postgres-dialect-gate.yml,
read from the uploaded checkout at run time, with that workflow's (and job's, and step's) literal
env. Nothing is a hand copy, so when CI's ignores, floors, shard count or ride-alongs change, this
runner changes with them. When CI changes SHAPE (a step is renamed, a step's env starts needing a
value this runner does not supply, a body starts interpolating `${{ }}`), `step` fails loudly
instead of guessing.

    python scripts/ops/cloud_build_tests.py step OUT --workflow W --job J --step S [--provide K ...]
        Writes job J's step S (its `id`, or a unique prefix of its `name`) of .github/workflows/W.yml
        to OUT as a bash script: the shell options GitHub would use, `export`s of the workflow's,
        job's and step's literal env, then the body. Every key named by --provide is left to the
        caller's environment and overrides a literal; an env value that is a `${{ }}` expression
        MUST be provided.

    python scripts/ops/cloud_build_tests.py wf-env W KEY
        Prints workflow W's top-level env value KEY.

    python scripts/ops/cloud_build_tests.py summary --ci-dir DIR --sqlite-shards N --pg-shards M
        Reads every shard's exit code and JUnit, then runs CI's own verdict steps over the results:
        the sweep's merge + executed-test floor, and the dialect gate's `verify` (which re-plans
        the shards from the workflow's own DIALECT_GATE_SHARDS and requires the recovery report).
        Prints one block and exits non-zero unless every one of them passed.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SUMMARY_BEGIN = "=== CLOUD BUILD TEST SUMMARY ==="
SUMMARY_END = "=== END SUMMARY ==="
SWEEP_WF, GATE_WF = "backend-test-sweep", "postgres-dialect-gate"


class ShapeError(SystemExit):
    """CI's workflow no longer has the shape this runner was written against."""


def _workflow(name: str) -> dict:
    import yaml

    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())


def _literal_env(env: Optional[dict], where: str, provided: set) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in (env or {}).items():
        if key in provided:
            continue
        if isinstance(value, (bool, float)):
            # PyYAML reads YAML 1.1 (`yes`/`on` are booleans, 1.10 is 1.1); GitHub reads the text.
            raise ShapeError(f"{where} env {key} parses as {value!r} here but GitHub passes the "
                             "literal text; quote it in the workflow")
        text = "" if value is None else str(value)
        if "${{" in text:
            raise ShapeError(f"{where} env {key} is a GitHub expression ({text!r}); this runner "
                             f"must supply it (--provide {key}) or it cannot run the step as CI does")
        out[key] = text
    return out


_PIP_INSTALL = "python -m pip install --upgrade pip\npip install -r requirements.txt -r requirements-dev.txt\n"

#: EVERY `run:` step of the four CI jobs this runner stands in for. RUNS: executed here, by the
#: selector the build config passes to `step` (a step `id`, or a prefix of its `name`), with the
#: `if:` this runner's sequencing was written against. NOT_RUN: deliberately not executed, with
#: why, and the body it had when that was decided (None = any body). A run step in neither, a
#: step that vanished, a changed `if:` or body, or a step key outside _STEP_KEYS (a
#: `working-directory:`, say) is a ShapeError: CI changed, and a GREEN here would no longer mean
#: what CI's means until this table (and the build config) is updated to match.
RUNS: Dict[tuple, Dict[str, Optional[str]]] = {
    (SWEEP_WF, "sweep-shard"): {"sweep": None},
    (SWEEP_WF, "sweep"): {"Merge the shards": "!cancelled()", "Assert the sweep actually ran": "!cancelled()"},
    (GATE_WF, "shard"): {
        "Preflight": None, "discover": None, "run_gate": None,
        "Assert the gate": "${{ !cancelled() && steps.run_gate.conclusion != 'skipped' }}",
        "Recovery report": "${{ matrix.shard == fromJSON(env.RIDE_ALONG_SHARD) }}",
    },
    (GATE_WF, "postgres-dialect-gate"): {"Prove the shards are one complete run": None},
}
NOT_RUN: Dict[tuple, Dict[str, tuple]] = {
    (SWEEP_WF, "sweep-shard"): {"Install dependencies": ("the build's deps step installs exactly this", _PIP_INSTALL)},
    (SWEEP_WF, "sweep"): {"Run Shopify Web Pixel mapper contract": ("a node test; not covered here", None)},
    (GATE_WF, "shard"): {"Install deps": ("the build's deps step installs exactly this", _PIP_INSTALL)},
    (GATE_WF, "postgres-dialect-gate"): {},
}
_STEP_KEYS = {"name", "id", "run", "env", "shell", "if"}
_JOB_KEYS = {"name", "runs-on", "needs", "if", "strategy", "services", "env", "timeout-minutes", "steps"}


def _matches(step: dict, selector: str) -> bool:
    return step.get("id") == selector or str(step.get("name") or "").startswith(selector)


def check_job(wf: dict, workflow: str, job: str) -> None:
    """Raise ShapeError unless job's run steps are exactly the ones this runner knows."""
    where = f"{workflow}.yml job {job}"
    if "defaults" in wf:
        raise ShapeError(f"{workflow}.yml sets `defaults:`, which this runner does not apply")
    try:
        job_def = wf["jobs"][job]
        runs, not_run = RUNS[(workflow, job)], NOT_RUN[(workflow, job)]
    except KeyError:
        raise ShapeError(f"{where}: not a job this runner knows")
    unknown = sorted(set(job_def) - _JOB_KEYS)
    if unknown:
        raise ShapeError(f"{where} has key(s) this runner does not apply: {', '.join(unknown)}")
    seen: Dict[str, int] = {sel: 0 for sel in [*runs, *not_run]}
    for s in job_def.get("steps") or []:
        if "run" not in s:
            continue
        label = f"{where} step {s.get('name') or s.get('id')!r}"
        hits = [sel for sel in seen if _matches(s, sel)]
        if len(hits) != 1:
            raise ShapeError(f"{label}: CI runs it and this runner does not know it (matches "
                             f"{hits or 'nothing'}). Run it in infra/gcp/cloudbuild.tests.yaml, or "
                             "list it in NOT_RUN with the reason")
        sel = hits[0]
        seen[sel] += 1
        if sel in not_run:
            body = not_run[sel][1]
            if body is not None and str(s["run"]) != body:
                raise ShapeError(f"{label}: its body changed since this runner decided not to run "
                                 f"it ({not_run[sel][0]})")
            continue
        extra = sorted(set(s) - _STEP_KEYS)
        if extra:
            raise ShapeError(f"{label} has key(s) this runner does not apply: {', '.join(extra)}")
        if s.get("shell") not in (None, "bash"):
            raise ShapeError(f"{label}: shell {s.get('shell')!r} is not bash")
        if s.get("if") != runs[sel]:
            raise ShapeError(f"{label}: `if:` is {s.get('if')!r}, this runner sequences it as {runs[sel]!r}")
    wrong = sorted(sel for sel, n in seen.items() if n != 1)
    if wrong:
        raise ShapeError(f"{where}: expected exactly one run step for each of {wrong}")


def step_script(workflow: str, job: str, step: str, provide: Sequence[str]) -> str:
    wf = _workflow(workflow)
    check_job(wf, workflow, job)
    if step not in RUNS[(workflow, job)]:
        raise ShapeError(f"{workflow}.yml job {job}: {step!r} is not a step this runner runs")
    job_def = wf["jobs"][job]
    hits = [s for s in job_def.get("steps") or []
            if "run" in s and (s.get("id") == step or str(s.get("name") or "").startswith(step))]
    if len(hits) != 1:
        raise ShapeError(f"{workflow}.yml job {job}: {len(hits)} run step(s) match {step!r}, need exactly 1")
    s = hits[0]
    body = str(s["run"])
    if "${{" in body:
        raise ShapeError(f"{workflow}.yml {job}/{step}: the run body interpolates ${{{{ }}}}; "
                         "it cannot be executed outside GitHub")
    provided = set(provide)
    env: Dict[str, str] = {}
    for scope, where in ((wf.get("env"), f"{workflow}.yml"), (job_def.get("env"), f"job {job}"),
                         (s.get("env"), f"step {step!r}")):
        env.update(_literal_env(scope, where, provided))
    # GitHub's own defaults: `shell: bash` is `bash --noprofile --norc -eo pipefail {0}`; no
    # `shell:` on Linux is `bash -e {0}`.
    opts = "set -eo pipefail" if s.get("shell") == "bash" else "set -e"
    lines = ["#!/usr/bin/env bash", f"# {workflow}.yml / {job} / {s.get('name') or s.get('id')}", opts]
    # A provided value that is unset at run time would silently become "" in the body.
    lines += [f': "${{{k}:?cloud_build_tests: {k} must be set to run this step}}"' for k in sorted(provided)]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in env.items()]
    return "\n".join(lines) + "\n" + body + ("" if body.endswith("\n") else "\n")


def wf_env(workflow: str, key: str) -> str:
    env = _workflow(workflow).get("env") or {}
    if key not in env:
        raise ShapeError(f"{workflow}.yml has no top-level env {key}")
    return str(env[key])


# ── summary ───────────────────────────────────────────────────────────────────


def _junit(path: Path) -> Optional[Dict[str, object]]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = root.findall("testsuite") or ([root] if root.tag == "testsuite" else [])
    counts = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
    failed = []
    for case in root.iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is not None:
            message = " ".join(str(bad.get("message") or "").split())[:240]
            failed.append(f"{case.get('classname')}::{case.get('name')}  -- {message}")
    return {**counts, "failed_ids": failed}


def _rc(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _clip(text: str, lines: int = 60, width: int = 400) -> str:
    """The last `lines` lines, each cut to `width`: one huge line (verify's file lists) must not
    push the reasons out of the block."""
    kept = text.strip().splitlines()[-lines:]
    return "\n".join(line if len(line) <= width else line[:width] + " ..." for line in kept)


def _scratch_checkout() -> Path:
    """A private copy of the uploaded source: CI's verdict steps write into their cwd and read
    fixed relative paths (`shards`, `sweep.xml`), so each gets its own tree."""
    dest = Path(tempfile.mkdtemp(prefix="verdict-"))
    top_level_skip = {".ci-venv", "ci", ".git"}
    shutil.copytree(ROOT, dest, dirs_exist_ok=True, symlinks=True,
                    ignore=lambda d, names: [n for n in names if Path(d) == ROOT and n in top_level_skip])
    return dest


def _run_ci_step(cwd: Path, workflow: str, job: str, step: str, env: Dict[str, str]) -> subprocess.CompletedProcess:
    script = cwd / f".ci-step-{job}-{len(list(cwd.glob('.ci-step-*')))}.sh"
    try:
        script.write_text(step_script(workflow, job, step, list(env)))
    except ShapeError as exc:  # reported in the block, not as a traceback that hides it
        return subprocess.CompletedProcess([], 98, "", f"CI changed shape: {exc}")
    return subprocess.run(["bash", str(script)], cwd=cwd, env={**os.environ, **env},
                          capture_output=True, text=True)


def summary(ci_dir: Path, sqlite_shards: int, pg_shards: int) -> int:
    ci_dir = ci_dir.resolve()
    red: List[str] = []
    rows: List[str] = []
    failed_ids: List[str] = []
    pg_dir = ci_dir / "pg"
    for kind, n, xml_of in (
        ("sqlite", sqlite_shards, lambda k: ci_dir / "shards" / f"sweep-shard-{k}.xml"),
        ("postgres", pg_shards, lambda k: pg_dir / f"pytest-postgres-shard-{k}.xml"),
    ):
        for k in range(1, n + 1):
            rc = _rc(ci_dir / f"{kind}-{k}.rc")
            j = _junit(xml_of(k))
            if j is None:
                red.append(f"{kind} shard {k}: no readable JUnit XML (rc={rc})")
                rows.append(f"{kind:<8} {k:>2}  rc={rc}  NO RESULT")
                continue
            executed = int(j["tests"]) - int(j["skipped"])
            rows.append(f"{kind:<8} {k:>2}  rc={rc}  tests={j['tests']} executed={executed} "
                        f"failures={j['failures']} errors={j['errors']} skipped={j['skipped']}")
            failed_ids += list(j["failed_ids"])
            if rc != 0:
                red.append(f"{kind} shard {k} exited {rc}")
    recovery = _junit(pg_dir / f"pytest-recovery-postgres-shard-{wf_env(GATE_WF, 'RIDE_ALONG_SHARD')}.xml")
    if recovery is not None:
        rows.append(f"recovery     tests={recovery['tests']} failures={recovery['failures']} "
                    f"errors={recovery['errors']} skipped={recovery['skipped']}")
        failed_ids += list(recovery["failed_ids"])

    gate_shards = int(wf_env(GATE_WF, "DIALECT_GATE_SHARDS"))
    if pg_shards != gate_shards:
        red.append(f"this build ran {pg_shards} Postgres shard(s) but {GATE_WF}.yml plans "
                   f"DIALECT_GATE_SHARDS={gate_shards}: add or remove postgres-K steps in "
                   "infra/gcp/cloudbuild.tests.yaml")

    for workflow, job in RUNS:
        try:
            check_job(_workflow(workflow), workflow, job)
        except ShapeError as exc:
            red.append(f"CI changed shape: {exc}")

    verdicts: List[str] = []
    sqlite_ok = all(_rc(ci_dir / f"sqlite-{k}.rc") == 0 for k in range(1, sqlite_shards + 1))
    pg_ok = all(_rc(ci_dir / f"postgres-{k}.rc") == 0 for k in range(1, pg_shards + 1))

    # The sweep: CI's merge step, then CI's executed-test floor over the merged sweep.xml.
    sweep_tree = _scratch_checkout()
    shutil.copytree(ci_dir / "shards", sweep_tree / "shards")
    for label, job, step, env in (
        ("sweep: merge (backend-test-sweep.yml)", "sweep", "Merge the shards",
         {"SHARD_RESULT": "success" if sqlite_ok else "failure", "SWEEP_SHARDS": str(sqlite_shards)}),
        ("sweep: executed-test floor (backend-test-sweep.yml)", "sweep", "Assert the sweep actually ran",
         {"SWEEP_EVENT": "pull_request"}),
    ):
        done = _run_ci_step(sweep_tree, SWEEP_WF, job, step, env)
        verdicts.append(f"--- {label}: rc={done.returncode}\n{_clip(done.stdout + done.stderr)}")
        if done.returncode != 0:
            red.append(f"{label} failed")

    # The dialect gate: CI's final job, `verify`, over this build's manifests and JUnit.
    gate_tree = _scratch_checkout()
    shutil.copytree(pg_dir, gate_tree / "shards") if pg_dir.is_dir() else (gate_tree / "shards").mkdir()
    label = "postgres: verify (postgres-dialect-gate.yml)"
    done = _run_ci_step(gate_tree, GATE_WF, "postgres-dialect-gate", "Prove the shards are one complete run",
                        {"SHARD_RESULT": "success" if pg_ok else "failure"})
    verdicts.append(f"--- {label}: rc={done.returncode}\n{_clip(done.stdout + done.stderr)}")
    if done.returncode != 0:
        red.append(f"{label} failed")

    notices = sorted(p.read_text().strip() for p in ci_dir.glob("notice-*.txt"))

    print(SUMMARY_BEGIN)
    print("\n".join(rows))
    print("\n".join(verdicts))
    if failed_ids:
        print("--- failing tests")
        print("\n".join(sorted(set(failed_ids))[:200]))
    if notices:
        print("--- differences from CI in this run")
        print("\n".join(sorted(set(notices))))
    print("--- verdict:", "GREEN" if not red else "RED")
    for line in red:
        print("  *", line)
    print(SUMMARY_END)
    return 0 if not red else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_step = sub.add_parser("step")
    p_step.add_argument("out", type=Path)
    p_step.add_argument("--workflow", required=True)
    p_step.add_argument("--job", required=True)
    p_step.add_argument("--step", required=True)
    p_step.add_argument("--provide", nargs="*", default=[])
    p_env = sub.add_parser("wf-env")
    p_env.add_argument("workflow")
    p_env.add_argument("key")
    p_sum = sub.add_parser("summary")
    p_sum.add_argument("--ci-dir", type=Path, required=True)
    p_sum.add_argument("--sqlite-shards", type=int, required=True)
    p_sum.add_argument("--pg-shards", type=int, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "step":
        args.out.write_text(step_script(args.workflow, args.job, args.step, args.provide))
        return 0
    if args.cmd == "wf-env":
        print(wf_env(args.workflow, args.key))
        return 0
    return summary(args.ci_dir, args.sqlite_shards, args.pg_shards)


if __name__ == "__main__":
    raise SystemExit(main())
