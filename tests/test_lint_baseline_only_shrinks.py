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
    assert not config.get("include"), (
        "`include` narrows the file set; `[]` or a non-Python glob makes the gate scan nothing"
    )
    assert config.get("respect-gitignore") is False, (
        "respect-gitignore must stay off: otherwise adding a source path to .gitignore removes "
        "it from the gate, and .gitignore is not read as lint config by any reviewer"
    )

    assert "F821" not in lint.get("ignore", []), (
        "`ignore` overrides `select`; F821 there makes the entire gate vacuous"
    )
    assert not lint.get("extend-select-ignore") and not lint.get("extend-ignore"), (
        "an extend-*-ignore of F821 has the same effect as `ignore`"
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
    assert "F821" in select, "the rule the baseline exists for is no longer selected"
    # F404 is here because its ABSENCE let seven SyntaxErrors through the linter that was added to
    # stop them: a misplaced `from __future__` import parses fine, so only F404 objects.
    assert "F404" in select, (
        "F404 was unselected once and seven uncompilable files merged green; it stays"
    )
    assert "E9" in select
