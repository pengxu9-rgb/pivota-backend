"""The lint baseline is a ratchet, and the only sanctioned way to carry a known undefined name.

WHY. Adding a linter to a repository that never had one means either fixing everything at once or
recording what is already broken. This repo recorded four files (ruff.toml `[lint.per-file-ignores]`),
each holding a REAL undefined name whose fix needs a decision the linter PR could not make.

A baseline nobody counts is an exemption list, and an exemption list grows. This asserts the shape
of it, so adding a fifth file is a visible act rather than a quiet one — and so that removing the
last entry is a test that fails and tells you to delete itself.

The linter itself runs in CI (`.github/workflows/backend-test-sweep.yml`); this file is about the
baseline, not about whether the code lints.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RUFF = _ROOT / "ruff.toml"
_SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "__pycache__", "build", "dist", ".claude"}

# The four files carrying a pre-existing undefined name on 2026-09-10. ONLY EVER REMOVE FROM THIS.
# The two shopify entries went off this list and came back: an earlier version of this PR "fixed"
# them by reading `merchant_onboarding.mcp_*`, which review showed is the LEGACY FALLBACK source,
# not the `merchant_stores` row that order creation actually uses. A wrong fix to a live route is
# worse than a documented 500. See ruff.toml for the correct fix, which is its own PR.
_BASELINE = {
    "routes/agent_shop_gateway.py",
    "routes/shopify_manual.py",
    "routes/shopify_setup.py",
    "services/pdp_governance_service.py",
}


def _ignores() -> dict:
    return tomllib.loads(_RUFF.read_text())["lint"]["per-file-ignores"]


def test_the_baseline_has_not_grown():
    """A fifth file means somebody merged a new undefined name and silenced it instead."""
    current = set(_ignores())
    added = sorted(current - _BASELINE)
    assert not added, (
        "these files were added to the lint baseline: %s. A baseline is for what was ALREADY "
        "broken when the linter arrived — fix the name instead, or make adding it a deliberate, "
        "reviewed change to _BASELINE with a reason." % added
    )


def test_removing_a_file_from_the_baseline_updates_this_test():
    """The other direction, and the point of the ratchet. When a file is fixed it leaves
    ruff.toml, and this fails until _BASELINE above is updated too — so the count in the docstring
    cannot drift away from reality the way a comment would."""
    current = set(_ignores())
    fixed = sorted(_BASELINE - current)
    assert not fixed, (
        "%s no longer needs a lint exemption — remove it from _BASELINE in this file too. "
        "When the set is empty, delete ruff.toml's [lint.per-file-ignores] and this module."
        % fixed
    )


def test_the_baseline_only_silences_F821():
    """Scope creep in an exemption is how a style waiver becomes a correctness waiver."""
    for path, codes in _ignores().items():
        assert codes == ["F821"], (
            "%s silences %s; the baseline exists only for pre-existing undefined names" % (path, codes)
        )


def test_the_gate_cannot_be_switched_off_from_the_config():
    """THE BYPASSES THE FIRST VERSION MISSED. It asserted the shape of `per-file-ignores` and
    nothing else, so four different one-line edits to ruff.toml made the gate vacuous while every
    test here stayed green: adding the baselined paths to `exclude`, an `extend-exclude` over
    whole directories, excluding a brand-new file to hide a fresh NameError, and a top-level
    `[lint] ignore = ["F821"]`, which beats `select` outright.

    A ratchet that only guards the door it knows about is not a ratchet."""
    config = tomllib.loads(_RUFF.read_text())
    lint = config.get("lint", {})

    # Three more keys that switch the gate off from inside this very file, all of which the
    # first version of this test ignored: a glob-keyed extend-per-file-ignores, a `builtins` list
    # that declares the undefined name defined, and an `include` that narrows the file set to
    # nothing Python.
    for key in ("extend-per-file-ignores",):
        for path, codes in (lint.get(key) or {}).items():
            assert "F821" not in codes, "[lint].%s silences F821 for %s" % (key, path)
    assert not config.get("builtins") and not lint.get("builtins"), (
        "`builtins` declares names defined that are not; it makes F821 unable to fire"
    )
    # ⚠️ THIS WAS `assert not config.get("include")` AND `include = []` PASSED IT — the exact
    # value the message names as the attack. `not []` is True. Presence is the invariant, not
    # truthiness; `include = []` makes ruff report "No Python files found" and exit 0.
    assert "include" not in config, (
        "`include` narrows the file set; `[]` or a non-Python glob makes the gate scan nothing"
    )
    assert config.get("respect-gitignore") is False, (
        "respect-gitignore must stay off: otherwise adding a source path to .gitignore removes "
        "it from the gate, and .gitignore is not read as lint config by any reviewer"
    )

    # BOTH SPELLINGS OF EACH. Ruff 0.16 still honours the deprecated TOP-LEVEL linter settings,
    # emitting a warning and not an error — so `ignore = ["F821"]` on line 1 of ruff.toml makes
    # the gate vacuous while a check that reads only `lint.ignore` sees nothing. Measured: ruff
    # exits 0 with a live NameError in the tree, and every test in this file stayed green.
    for table, label in ((lint, "[lint]."), (config, "top-level ")):
        assert "F821" not in (table.get("ignore") or []), (
            "%signore overrides `select`; F821 there makes the entire gate vacuous" % label
        )
        assert not table.get("extend-select-ignore") and not table.get("extend-ignore"), (
            "%sextend-*-ignore of F821 has the same effect as `ignore`" % label
        )

    # Exclusions are how a file leaves the gate WITHOUT appearing in the baseline. Pin the list
    # so widening it is a reviewed change, not a one-word edit.
    allowed_exclude = {
        ".venv", "venv", "node_modules", ".git", "__pycache__", "build", "dist", ".claude",
    }
    for key in ("exclude", "extend-exclude"):
        extra = sorted(set(config.get(key, [])) - allowed_exclude)
        assert not extra, (
            "%s adds %s. Source directories must not be excluded — that removes them from the "
            "gate entirely, which the baseline test cannot see. Baseline the file instead."
            % (key, extra)
        )
    for key in ("exclude", "extend-exclude"):
        extra = sorted(set(lint.get(key, [])) - allowed_exclude)
        assert not extra, "[lint].%s adds %s; same bypass" % (key, extra)


def test_ruff_toml_is_the_ONLY_ruff_config_in_the_tree():
    """A SECOND config file beats this one, and this test never opens it.

    Measured bypasses, all silent before this test existed: a root `.ruff.toml` (ruff prefers it
    over `ruff.toml`, so a copy minus F821 disables the gate while every assertion here still
    reads the untouched `ruff.toml`); a nested `services/.ruff.toml` or `services/pyproject.toml`
    with its own `select`, which captures that whole subtree; and the existing
    `integrations/commerce-agents/pyproject.toml` growing a `[tool.ruff]` table.

    So the invariant is not "ruff.toml says the right thing" — it is "ruff.toml is the only thing
    saying anything"."""
    others = []
    for path in _ROOT.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name in {".ruff.toml"} or (path.name == "ruff.toml" and path != _RUFF):
            others.append(str(path.relative_to(_ROOT)))
        elif path.name == "pyproject.toml":
            if "[tool.ruff" in path.read_text(encoding="utf-8", errors="ignore"):
                others.append(str(path.relative_to(_ROOT)) + " ([tool.ruff])")
    assert not others, (
        "these files also configure ruff and take precedence over or narrow ruff.toml: %s. "
        "One config, or the ratchet is guarding a file the linter no longer reads." % sorted(others)
    )


def test_no_baselined_path_is_also_excluded():
    """The specific trick: leave the entry in `per-file-ignores` so this file is satisfied, and
    ALSO exclude the path so ruff never reads it. The baseline then looks maintained while the
    file is outside the gate."""
    config = tomllib.loads(_RUFF.read_text())
    excluded = set(config.get("exclude", [])) | set(config.get("extend-exclude", []))
    for path in _ignores():
        assert path not in excluded, (
            "%s is baselined AND excluded — pick one; excluding it hides future findings too" % path
        )


def test_the_selected_rules_are_still_the_correctness_ones():
    """A later edit that drops F821 from `select` would make the whole gate vacuous while every
    test here still passed — the baseline would be silencing a rule that no longer runs."""
    select = tomllib.loads(_RUFF.read_text())["lint"]["select"]
    # THE WHOLE LIST. Asserting only F821/F404/E9 let the other five be deleted silently: ruff
    # still exits 1 on an undefined name, so the canary does not move and nothing else noticed.
    # Correctness rules leave by review, not by omission.
    assert set(select) == {"F821", "F404", "F822", "F823", "F702", "F704", "F706", "E9"}, (
        "the selected rule set changed to %s. Adding is fine — say so here. REMOVING one is the "
        "gate getting quietly narrower, which is what this file exists to prevent." % sorted(select)
    )
    assert "F821" in select, "the rule the baseline exists for is no longer selected"
    # F404 is here because its ABSENCE let seven SyntaxErrors through the linter that was added to
    # stop them: a misplaced `from __future__` import parses fine, so only F404 objects.
    assert "F404" in select, (
        "F404 was unselected once and seven uncompilable files merged green; it stays"
    )
    assert "E9" in select


# --- the two MECHANISM-level guards, added after a third review pass ------------------------
#
# Everything above enumerates ruff keys BY NAME, and three passes of review have now found keys
# the enumeration did not know about: `extend-per-file-ignores`, `builtins`, `include`,
# `respect-gitignore`, the deprecated top-level `ignore`, and `extend`. Enumeration loses that
# game by construction — ruff has more settings than this file can track, and each new one is a
# silent bypass until someone thinks of it.
#
# So the two tests below do not name keys at all. They are the reason a fourth spelling does not
# need to be guessed:
#
#   * the CANARY asks ruff itself whether an undefined name is still an error. It catches any
#     setting that changes the VERDICT, whatever it is called — `extend` inheriting a `builtins`
#     list, a top-level `ignore`, a rule set swapped wholesale.
#   * the ALLOWLIST catches any setting that changes WHICH FILES are read. The canary is
#     structurally blind to those (`--stdin-filename` bypasses file discovery, so `include = []`
#     does not move it), and they are the more dangerous half: a file outside the gate has no
#     findings to suppress.
#
# Neither subsumes the other, and both were measured against real bypasses before being written.


def _ruff_argv() -> list:
    import shutil
    import sys

    exe = shutil.which("ruff")
    return [exe] if exe else [sys.executable, "-m", "ruff"]


def test_ruff_ITSELF_still_reports_an_undefined_name():
    """THE BEHAVIOURAL CHECK. Every other test here reads ruff.toml and reasons about it; this
    one asks the linter.

    `extend = "some-file.toml"` inherits every key ruff.toml does not set — including a
    `builtins` list that declares the undefined name defined — from a file that can have ANY
    name, so `test_ruff_toml_is_the_ONLY_ruff_config_in_the_tree` (which globs for three known
    filenames) cannot see it. Measured: ruff exits 0 with a live NameError in the tree and all
    eleven tests in this file pass. This one fails.

    Uses `--stdin-filename` so nothing is written to the working tree: the checkout is shared,
    and a stray canary file has been swept into an unrelated commit here before.
    """
    import subprocess

    canary = "def f():\n    return totally_undefined_name(1)\n"
    proc = subprocess.run(
        _ruff_argv() + ["check", "--ignore-noqa", "--stdin-filename", "services/zz_lint_canary.py", "-"],
        input=canary,
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    )
    assert proc.returncode == 1 and "F821" in proc.stdout, (
        "ruff did NOT report an undefined name under this repository's config. The gate is "
        "vacuous however green it looks. ruff said (exit %s):\n%s\n%s"
        % (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    )


def test_ruff_toml_has_no_key_this_ratchet_has_not_CONSIDERED():
    """THE ALLOWLIST, and the answer to whack-a-mole.

    A denylist of dangerous keys is only as good as the last review; this inverts it. Any key not
    listed here fails the test until someone decides what it does to the gate and adds it — which
    is the reviewed act the ratchet exists to force.

    It is what catches the FILE-SET bypasses the canary above cannot see, `include` and
    `extend-exclude` among them, and it caught `extend` retroactively."""
    config = tomllib.loads(_RUFF.read_text())
    allowed_top = {"line-length", "respect-gitignore", "exclude", "lint"}
    allowed_lint = {"select", "per-file-ignores"}

    unknown = sorted(set(config) - allowed_top)
    assert not unknown, (
        "ruff.toml has top-level key(s) %s that no test in this file evaluates. Ruff has more "
        "settings than a denylist can track — `extend` alone inherits EVERY unset key from a "
        "file of any name, which defeats the only-one-config test. Decide what the key does to "
        "the gate, assert it, and add it to `allowed_top` here." % unknown
    )
    unknown = sorted(set(config.get("lint", {})) - allowed_lint)
    assert not unknown, (
        "ruff.toml [lint] has key(s) %s that no test in this file evaluates; same reasoning as "
        "above. Add an assertion, then add the key to `allowed_lint`." % unknown
    )


def test_the_baseline_hides_exactly_the_nine_names_it_documents():
    """A whole-file `per-file-ignores` entry is blind to the NEXT undefined name in that file.

    Two of the four baselined files are large and busy — `routes/agent_shop_gateway.py` is 16,040
    lines and `services/pdp_governance_service.py` 5,936 — so the baseline currently exempts
    ~22,000 lines from F821 forever, not just the nine names it documents. Measured: appending a
    brand-new function with an undefined name to `agent_shop_gateway.py` left ruff green and
    every test here passing.

    So the baseline is a COUNT, not a blanket. Lifting `[lint.per-file-ignores]` must yield
    exactly these nine findings; a tenth is a new defect wearing an old exemption. When one is
    fixed the number here goes down, which is the same ratchet direction as the file list."""
    import json
    import subprocess

    # THE NAMES, not a tally. A count is blind to an offsetting edit: review fixed
    # `_subject_from_product_key` (repointing it at the real `_subject_from_external_seed`) and
    # appended a DIFFERENT undefined name in the same file, and the count test passed — one
    # defect retired, one introduced, net zero, silently. What the baseline documents is these
    # specific names.
    expected = {
        "routes/agent_shop_gateway.py": ["http_request"],                    # :14461
        "routes/shopify_manual.py": ["store_info"] * 3,                      # :51-53
        "routes/shopify_setup.py": ["store_info"] * 4,                       # :133-136
        "services/pdp_governance_service.py": ["_subject_from_product_key"],  # :2747
    }
    proc = subprocess.run(
        _ruff_argv() + [
            "check", "--ignore-noqa", "--select", "F821", "--output-format", "json",
            # Neutralise ONLY the baseline; everything else about the config stands.
            "--config", "lint.per-file-ignores = {}",
            *expected,
        ],
        capture_output=True, text=True, cwd=str(_ROOT),
    )
    assert proc.returncode in (0, 1), "ruff failed to run: %s" % proc.stderr[-2000:]
    found = {}
    for item in json.loads(proc.stdout or "[]"):
        rel = str(pathlib.Path(item["filename"]).resolve().relative_to(_ROOT))
        # "Undefined name `store_info`" — the identifier is what the baseline documents.
        name = re.search(r"`([^`]+)`", item.get("message", ""))
        found.setdefault(rel, []).append(name.group(1) if name else item.get("message", "?"))
    found = {k: sorted(v) for k, v in found.items()}
    assert found == {k: sorted(v) for k, v in expected.items()}, (
        "the F821s hidden by the baseline are no longer the nine names documented in ruff.toml.\n"
        "  expected: %s\n  found:    %s\n"
        "A NEW name is a fresh defect that the whole-file exemption silenced. A name that is GONE "
        "is progress — remove it here too, and from ruff.toml." % (
            sorted(expected.items()), sorted(found.items()))
    )


def test_the_ruff_version_is_pinned():
    """The gate's meaning is a function of the ruff version. 0.16 still honours the deprecated
    top-level `ignore` with a warning; a later one may not, and a later one may change what
    `select` covers. `ruff>=` is a one-character edit that makes the gate mean something
    different on a day nobody touched it."""
    import re

    req = (_ROOT / "requirements-dev.txt").read_text()
    lines = [ln.strip() for ln in req.splitlines() if re.match(r"^\s*ruff\b", ln)]
    assert len(lines) == 1, "expected exactly one ruff requirement, got %s" % lines
    assert re.match(r"^ruff==\d+\.\d+\.\d+$", lines[0]), (
        "ruff must be pinned with `==` (got %r): the linter's version decides what the gate "
        "catches, and requirements-dev.txt is what CI installs before running it" % lines[0]
    )


# --- the gate has to actually RUN ------------------------------------------------------------
#
# Everything above this line polices ruff.toml. None of it noticed that the CI step invoking ruff
# could simply be deleted: measured, removing the whole `Lint for names that cannot resolve` step
# left every test in this file and in test_every_python_file_compiles.py green. So did dropping
# the single word `--ignore-noqa`, which silently restores the `# noqa: F821` bypass a previous
# review pass found and which is proven load-bearing (with a `# noqa: F821` appended to a source
# file: with the flag ruff finds 1 error, without it "All checks passed").
#
# A ratchet on the config of a step that no longer exists is decoration.

_SWEEP = _ROOT / ".github/workflows/backend-test-sweep.yml"


def _sweep_job() -> dict:
    import yaml

    return yaml.safe_load(_SWEEP.read_text())["jobs"]["sweep"]


def _lint_step() -> dict:
    lint = [s for s in _sweep_job()["steps"] if "ruff check" in str(s.get("run", ""))]
    assert len(lint) == 1, (
        "expected exactly one `ruff check` step in the sweep job, found %d. The linter gate is "
        "this step; deleting it leaves every assertion in this file passing over nothing." % len(lint)
    )
    return lint[0]


def test_THE_COMMAND_CI_RUNS_still_fails_on_an_undefined_name():
    """THE ONE THAT MATTERS, and the gap the previous canary left wide open.

    `test_ruff_ITSELF_still_reports_an_undefined_name` builds its OWN argv, so it is structurally
    incapable of noticing what the workflow tells ruff to do. And the string assertions below
    ("contains --ignore-noqa", "ends with .") constrain almost nothing. Five different edits to
    that one `run:` line disabled the gate with all seventeen tests green:

        --config 'lint.ignore=["F821"]'   inline config beats ruff.toml, and is not ruff.toml
        --exit-zero                       report everything, fail on nothing
        ... tests/.                       still "ends with a dot"; lints one directory
        ruff ... || echo deferred         plus a trailing line, so the step's exit code is echo's
        # ruff check --ignore-noqa ...    the asserted flag, in a comment, on the line above

    So this test stops reasoning about the command and RUNS it — the workflow's own `run:` body,
    verbatim, against a planted canary. One probe catches all five.

    The canary carries `# noqa: F821`, so dropping `--ignore-noqa` fails here too (verified: with
    the flag ruff finds it, without the flag it does not).

    It has to be a real file: `--stdin-filename` would not notice `tests/.`, which is the whole
    point. The write is to the repository root, which no `per-file-ignores` entry covers, and it
    is removed in a `finally` — this checkout is SHARED with other sessions and a stray file here
    has been swept into an unrelated commit before."""
    import subprocess

    run = _lint_step()["run"]
    canary = _ROOT / "zz_lint_canary_probe.py"
    assert not canary.exists(), "%s already exists; refusing to clobber it" % canary

    # CI installs ruff with pip so it is on PATH; a local venv puts it beside the interpreter.
    # Without this the command exits 127 (`ruff: command not found`) and the CONTROL below fails
    # loudly -- which is how this was caught -- rather than the probe passing for the wrong reason.
    import os
    import sys

    env = dict(os.environ)
    env["PATH"] = str(pathlib.Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")

    def _run() -> int:
        return subprocess.run(
            ["bash", "-c", run], cwd=str(_ROOT), capture_output=True, text=True, env=env
        ).returncode

    try:
        # CONTROL FIRST. If the command exits non-zero on a clean tree, the assertion below would
        # pass for the wrong reason and this test would be measuring nothing.
        clean = _run()
        assert clean == 0, (
            "the CI lint command already fails on a clean tree (exit %s), so this test cannot "
            "tell a working gate from a broken one. Command:\n%s" % (clean, run)
        )
        canary.write_text("def zz_canary():\n    return totally_undefined_name(1)  # noqa: F821\n")
        assert _run() != 0, (
            "THE GATE IS OFF. The exact command CI runs exited 0 with an undefined name in the "
            "tree. It is not enough that ruff.toml is correct or that the step exists — this is "
            "the command, and it does not fail. Command:\n%s" % run
        )
    finally:
        canary.unlink(missing_ok=True)


def test_ci_still_runs_ruff_over_the_whole_tree():
    """Pins the invocation, not just the config it reads."""
    lint = [_lint_step()]
    assert len(lint) == 1, (
        "expected exactly one `ruff check` step in the sweep job, found %d. The linter gate is "
        "this step; deleting it leaves every assertion in this file passing over nothing." % len(lint)
    )
    run = lint[0]["run"]
    assert "--ignore-noqa" in run, (
        "--ignore-noqa is missing from `%s`. Without it a `# noqa: F821` on the line, or a "
        "file-level `# ruff: noqa`, silences the gate — and no test here can see a comment in a "
        "source file. The ruff.toml baseline is the one sanctioned way to carry a known name."
        % run.strip()
    )
    assert run.strip().endswith("."), (
        "the lint step must check the WHOLE tree (`ruff check ... .`), not a path list that can "
        "quietly stop covering a directory; got `%s`" % run.strip()
    )
    assert "continue-on-error" not in lint[0], "a lint step that cannot fail is not a gate"


def test_the_sweep_job_has_no_condition_that_can_skip_it():
    """`CI Entrypoint` TOLERATES a skipped sweep by design — a job-level `if:` that evaluates
    false makes the required check go green having run neither the linter, nor this ratchet, nor
    the compile test. That is the seventh way to switch off a gate whose whole claim is that it
    cannot be switched off, and it needs no edit to ruff.toml at all."""
    job = _sweep_job()
    # THE ALLOWLIST AGAIN, because `if:` was only the spelling I thought of. Measured against the
    # entrypoint's own matcher, each of these drops `sweep` from the set of gates it enforces,
    # with no `if:` anywhere and every test here green:
    #   strategy: {matrix: …}        the check-run name gains a matrix suffix -> not derivable
    #   strategy: {matrix: {s: []}}  WORST: zero job instances, and NO warning is emitted
    #   name: … ${{ … }}             dynamic name -> not derivable
    #   continue-on-error: true      at JOB level; the step-level check below does not see it
    allowed = {"runs-on", "timeout-minutes", "steps"}
    unknown = sorted(set(job) - allowed)
    assert not unknown, (
        "the sweep job grew key(s) %s. This job carries the linter, this ratchet and the compile "
        "test, and `CI Entrypoint` cannot enforce a job whose check-run name it cannot derive — "
        "a matrix or a dynamic `name:` silently removes it from the required set, and an EMPTY "
        "matrix runs it zero times without even a warning. Decide what the key does to "
        "enforcement, then add it here." % unknown
    )
    assert "if" not in job, (
        "the sweep job grew `if: %r`. A false condition skips the linter, this ratchet and the "
        "compile test, and the required check tolerates a skip — so the gate disappears with a "
        "green tick. If a condition is genuinely needed, make CI Entrypoint enforce this job."
        % job.get("if")
    )
    lint = [s for s in job["steps"] if "ruff check" in str(s.get("run", ""))]
    assert "if" not in lint[0], (
        "the lint STEP grew `if: %r`; same bypass one level down" % lint[0].get("if")
    )


def test_the_sweep_does_not_skip_the_gate_test_files():
    """One line in the sweep's own pytest denylist deletes this ratchet.

    `--ignore=tests/test_lint_baseline_only_shrinks.py` and the whole file stops running: the
    baseline can then grow, ruff.toml can gain any key, and the lint step can be defanged, with
    nothing to object. The FLOOR assertion cannot see it — it allows ~482 tests of slack against
    ~16,182 executed, and this file is 15.

    Same class as the bypass the lint-step pin closed, one file over: a gate that polices a
    command nobody runs.
    """
    step = [s for s in _sweep_job()["steps"] if s.get("id") == "sweep"]
    assert len(step) == 1, "the sweep's pytest step (id: sweep) is gone or duplicated"
    run = step[0]["run"]
    for name in ("test_lint_baseline_only_shrinks.py", "test_every_python_file_compiles.py"):
        for flag in ("--ignore", "--ignore-glob", "--deselect"):
            bad = [ln.strip() for ln in run.splitlines() if flag in ln and name in ln]
            assert not bad, (
                "the sweep excludes the lint gate's own tests via %s: %s. These two files are "
                "what stops the linter being switched off; excluding them is switching it off "
                "one level up." % (flag, bad)
            )
    assert "tests" in run.split(), (
        "the sweep no longer passes `tests` to pytest, so the gate's own tests may not run at all"
    )
