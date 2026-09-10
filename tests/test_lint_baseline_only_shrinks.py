"""The lint baseline is a ratchet: four files may still carry an undefined name, and no more.

WHY. Adding a linter to a repository that never had one means either fixing everything at once or
recording what is already broken. This repo recorded two files (ruff.toml `[lint.per-file-ignores]`),
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

# The files carrying a pre-existing undefined name. ONLY EVER REMOVE FROM THIS.
# Started at four; shopify_manual.py and shopify_setup.py came off once review pointed out that
# both were GUARANTEED 500s on registered routes — "guessing the keys would swap one silent bug
# for another" is not a reason to keep a route that cannot answer at all, and the key names were
# one read of db/merchant_onboarding.py away.
_BASELINE = {
    "routes/agent_shop_gateway.py",
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
